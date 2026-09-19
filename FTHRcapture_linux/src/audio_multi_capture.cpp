#include "audio_multi_capture.h"
#include "audio_timing.h"
#include <iostream>
#include <cstring>
#include <time.h>
#include <cctype>

namespace fthr {

// Helpers

static int64_t mono_ns_multi() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1'000'000'000LL + ts.tv_nsec;
}

static constexpr size_t kMaxRingSamples =
    (size_t)AudioMultiCapture::kSampleRate *
    (size_t)AudioMultiCapture::kChannels   *
    (size_t)AudioMultiCapture::kMaxSeconds;

static constexpr size_t kChunkFrames =
    (size_t)AudioMultiCapture::kSampleRate / 100; // 10ms chunks

static std::string make_sink_name(const std::string& name) {
    std::string s = "fthr_";
    for (char c : name)
        s += std::isalnum((unsigned char)c)
             ? (char)std::tolower((unsigned char)c) : '_';
    return s;
}

// Static callbacks

void AudioMultiCapture::context_state_cb(pa_context*, void* userdata) {
    pa_threaded_mainloop_signal((pa_threaded_mainloop*)userdata, 0);
}

void AudioMultiCapture::stream_read_cb(pa_stream* s, size_t /*nbytes*/,
                                        void* userdata) {
    auto* state = (CategoryState*)userdata;
    const void* data  = nullptr;
    size_t      nbytes = 0;

    if (pa_stream_peek(s, &data, &nbytes) < 0 || !data || nbytes == 0) {
        pa_stream_drop(s);
        return;
    }

    const float* samples = (const float*)data;
    size_t       nfloats = nbytes / sizeof(float);

    pa_usec_t latency_us = 0;
    pa_usec_t stream_latency = 0;
    int negative = 0;
    if (pa_stream_get_latency(s, &stream_latency, &negative) >= 0 && !negative)
        latency_us = stream_latency;
    const int64_t delivery_end_ns = mono_ns_multi();

    TimedChunk chunk;
    chunk.samples.assign(samples, samples + nfloats);
    chunk.start_ns = AudioChunkStartNs(
        delivery_end_ns, static_cast<int64_t>(latency_us),
        nfloats / AudioMultiCapture::kChannels,
        AudioMultiCapture::kSampleRate);
    pa_stream_drop(s);

    {
        std::lock_guard<std::mutex> lk(state->mutex);
        state->ring_total += chunk.samples.size();
        state->chunks.push_back(std::move(chunk));
        while (state->ring_total > kMaxRingSamples && !state->chunks.empty()) {
            state->ring_total -= state->chunks.front().samples.size();
            state->chunks.pop_front();
        }
    }
}

struct SinkInputCtx {
    AudioMultiCapture* self;
};

void AudioMultiCapture::sink_input_info_cb(pa_context* ctx,
                                            const pa_sink_input_info* i,
                                            int eol, void* userdata) {
    auto* c = (SinkInputCtx*)userdata;
    if (!eol && i) {
        const char* binary = pa_proplist_gets(i->proplist,
                                              PA_PROP_APPLICATION_PROCESS_BINARY);
        if (!binary) binary = "";
        std::string app = binary;

        std::string matched_cat;
        uint32_t    target_sink = UINT32_MAX;

        // Match against category patterns (first match wins)
        for (auto& cat_ptr : c->self->categories_) {
            auto& cat = *cat_ptr;
            for (auto& pat : cat.config.patterns) {
                if (!pat.empty() && app.find(pat) != std::string::npos) {
                    matched_cat = cat.config.name;
                    target_sink = cat.sink_idx;
                    break;
                }
            }
            if (!matched_cat.empty()) break;
        }

        // Fallback to "Sonstige" (first category with empty patterns + valid sink)
        if (matched_cat.empty()) {
            for (auto& cat_ptr : c->self->categories_) {
                auto& cat = *cat_ptr;
                if (cat.config.patterns.empty() && cat.sink_idx != UINT32_MAX) {
                    matched_cat = cat.config.name;
                    target_sink = cat.sink_idx;
                    break;
                }
            }
        }

        if (target_sink != UINT32_MAX && target_sink != i->sink) {
            pa_operation* op = pa_context_move_sink_input_by_index(
                ctx, i->index, target_sink, nullptr, nullptr);
            if (op) pa_operation_unref(op);
        }

        if (!app.empty() && !matched_cat.empty()) {
            std::lock_guard<std::mutex> lk(c->self->mappings_mutex_);
            c->self->current_mappings_[app] = matched_cat;
        }
    }
    if (eol) delete c;
}

void AudioMultiCapture::subscribe_cb(pa_context* ctx,
                                      pa_subscription_event_type_t t,
                                      uint32_t idx, void* userdata) {
    auto* self     = (AudioMultiCapture*)userdata;
    auto  facility = t & PA_SUBSCRIPTION_EVENT_FACILITY_MASK;
    auto  type     = t & PA_SUBSCRIPTION_EVENT_TYPE_MASK;

    if (facility == PA_SUBSCRIPTION_EVENT_SINK_INPUT &&
        (type == PA_SUBSCRIPTION_EVENT_NEW ||
         type == PA_SUBSCRIPTION_EVENT_CHANGE)) {
        auto* c = new SinkInputCtx{self};
        pa_operation* op = pa_context_get_sink_input_info(
            ctx, idx, sink_input_info_cb, c);
        if (op) {
            pa_operation_unref(op);
        } else {
            // If the operation can't be queued, the callback will never fire
            // and delete c — clean up here to avoid a leak.
            delete c;
        }
    }
}


bool AudioMultiCapture::Start(const std::vector<AudioCategoryConfig>& configs) {
    if (running_.load()) return true;

    mainloop_ = pa_threaded_mainloop_new();
    if (!mainloop_) {
        std::cerr << "[MultiAudio] pa_threaded_mainloop_new failed\n";
        return false;
    }

    pa_mainloop_api* api = pa_threaded_mainloop_get_api(mainloop_);
    context_ = pa_context_new(api, "FTHRclips_multi");
    if (!context_) {
        pa_threaded_mainloop_free(mainloop_); mainloop_ = nullptr;
        return false;
    }

    pa_context_set_state_callback(context_, context_state_cb, mainloop_);

    pa_threaded_mainloop_lock(mainloop_);
    pa_threaded_mainloop_start(mainloop_);

    if (pa_context_connect(context_, nullptr, PA_CONTEXT_NOFLAGS, nullptr) < 0) {
        pa_threaded_mainloop_unlock(mainloop_);
        Stop(); return false;
    }

    // Wait for context ready
    for (;;) {
        auto st = pa_context_get_state(context_);
        if (st == PA_CONTEXT_READY)     break;
        if (st == PA_CONTEXT_FAILED || st == PA_CONTEXT_TERMINATED) {
            std::cerr << "[MultiAudio] PA context failed\n";
            pa_threaded_mainloop_unlock(mainloop_);
            Stop(); return false;
        }
        pa_threaded_mainloop_wait(mainloop_);
    }

    categories_.clear();
    for (const auto& cfg : configs) {
        auto cs = std::make_unique<CategoryState>();
        cs->config = cfg;
        if (cs->config.sink_name.empty())
            cs->config.sink_name = make_sink_name(cfg.name);
        categories_.push_back(std::move(cs));
    }

    // Load module-null-sink per category and open record stream
    for (auto& cat_ptr : categories_) {
        auto& cat = *cat_ptr;
        std::string args = "sink_name=" + cat.config.sink_name;

        struct LoadCtx { pa_threaded_mainloop* ml; uint32_t idx; };
        LoadCtx lctx{mainloop_, UINT32_MAX};

        pa_operation* op = pa_context_load_module(context_,
            "module-null-sink", args.c_str(),
            [](pa_context*, uint32_t idx, void* ud) {
                auto* l = (LoadCtx*)ud;
                l->idx = idx;
                pa_threaded_mainloop_signal(l->ml, 0);
            }, &lctx);

        if (op) {
            while (pa_operation_get_state(op) == PA_OPERATION_RUNNING)
                pa_threaded_mainloop_wait(mainloop_);
            pa_operation_unref(op);
        }
        cat.module_idx = lctx.idx;

        if (cat.module_idx == UINT32_MAX) {
            std::cerr << "[MultiAudio] Failed to load null-sink for "
                      << cat.config.name << "\n";
            continue;
        }

        // Get sink index by name
        struct SinkCtx { pa_threaded_mainloop* ml; uint32_t idx; };
        SinkCtx sctx{mainloop_, UINT32_MAX};
        op = pa_context_get_sink_info_by_name(context_,
            cat.config.sink_name.c_str(),
            [](pa_context*, const pa_sink_info* si, int eol, void* ud) {
                auto* s = (SinkCtx*)ud;
                if (!eol && si) s->idx = si->index;
                if (eol) pa_threaded_mainloop_signal(s->ml, 0);
            }, &sctx);

        if (op) {
            while (pa_operation_get_state(op) == PA_OPERATION_RUNNING)
                pa_threaded_mainloop_wait(mainloop_);
            pa_operation_unref(op);
        }
        cat.sink_idx = sctx.idx;

        // Open record stream on category's monitor source
        pa_sample_spec ss;
        ss.format   = PA_SAMPLE_FLOAT32LE;
        ss.rate     = (uint32_t)kSampleRate;
        ss.channels = (uint8_t)kChannels;

        cat.stream = pa_stream_new(context_, cat.config.name.c_str(), &ss, nullptr);
        if (!cat.stream) {
            std::cerr << "[MultiAudio] pa_stream_new failed for "
                      << cat.config.name << "\n";
            continue;
        }

        pa_stream_set_read_callback(cat.stream, stream_read_cb, cat_ptr.get());

        pa_buffer_attr ba{};
        ba.maxlength = (uint32_t)-1;
        ba.fragsize  = (uint32_t)(kChunkFrames * kChannels * sizeof(float));
        ba.prebuf = ba.minreq = ba.tlength = (uint32_t)-1;

        std::string mon = cat.config.sink_name + ".monitor";
        if (pa_stream_connect_record(cat.stream, mon.c_str(), &ba,
                                     PA_STREAM_ADJUST_LATENCY) < 0) {
            std::cerr << "[MultiAudio] connect_record failed for " << mon << "\n";
            pa_stream_unref(cat.stream);
            cat.stream = nullptr;
        }
    }

    // Subscribe to sink input events for app routing
    pa_context_set_subscribe_callback(context_, subscribe_cb, this);
    struct SubCtx { pa_threaded_mainloop* ml; };
    SubCtx sub_ctx{mainloop_};
    pa_operation* sub_op = pa_context_subscribe(context_,
        PA_SUBSCRIPTION_MASK_SINK_INPUT,
        [](pa_context*, int, void* ud) {
            pa_threaded_mainloop_signal(((SubCtx*)ud)->ml, 0);
        }, &sub_ctx);

    if (sub_op) {
        while (pa_operation_get_state(sub_op) == PA_OPERATION_RUNNING)
            pa_threaded_mainloop_wait(mainloop_);
        pa_operation_unref(sub_op);
    }

    pa_threaded_mainloop_unlock(mainloop_);
    running_.store(true);
    std::cout << "[MultiAudio] Started with " << categories_.size()
              << " categories\n";
    return true;
}


void AudioMultiCapture::Stop() {
    if (!mainloop_) return;
    running_.store(false);

    pa_threaded_mainloop_lock(mainloop_);

    for (auto& cat_ptr : categories_) {
        auto& cat = *cat_ptr;
        if (cat.stream) {
            pa_stream_disconnect(cat.stream);
            pa_stream_unref(cat.stream);
            cat.stream = nullptr;
        }
        if (cat.module_idx != UINT32_MAX && context_) {
            // Use a signalling callback so we can wait for the unload to
            // complete before disconnecting the context. Without waiting, the
            // mainloop stops before the async unload fires, leaving orphaned
            // null-sink modules in PulseAudio across restarts.
            pa_operation* op = pa_context_unload_module(
                context_, cat.module_idx,
                [](pa_context*, int, void* ud) {
                    pa_threaded_mainloop_signal(
                        static_cast<pa_threaded_mainloop*>(ud), 0);
                }, mainloop_);
            if (op) {
                while (pa_operation_get_state(op) == PA_OPERATION_RUNNING)
                    pa_threaded_mainloop_wait(mainloop_);
                pa_operation_unref(op);
            }
        }
    }
    categories_.clear();

    if (context_) {
        pa_context_disconnect(context_);
        pa_context_unref(context_);
        context_ = nullptr;
    }

    pa_threaded_mainloop_unlock(mainloop_);
    pa_threaded_mainloop_stop(mainloop_);
    pa_threaded_mainloop_free(mainloop_);
    mainloop_ = nullptr;
}

// ExtractSegment — identical logic to AudioCapture::ExtractSegment

std::vector<float> AudioMultiCapture::ExtractSegment(
        const std::string& category_name,
        int64_t end_time_ns,
        uint32_t duration_ms) const {

    const CategoryState* state = nullptr;
    for (const auto& cat_ptr : categories_)
        if (cat_ptr->config.name == category_name) { state = cat_ptr.get(); break; }
    if (!state) return {};

    std::lock_guard<std::mutex> lk(state->mutex);
    if (state->chunks.empty()) return {};

    if (end_time_ns == 0) {
        const auto& last = state->chunks.back();
        int64_t ns_per_sample = 1'000'000'000LL / kSampleRate;
        end_time_ns = last.start_ns +
            (int64_t)(last.samples.size() / kChannels) * ns_per_sample;
    }

    int64_t start_ns = end_time_ns - (int64_t)duration_ms * 1'000'000LL;
    std::vector<float> result;
    int64_t ns_per_frame = 1'000'000'000LL / kSampleRate;

    for (const auto& chunk : state->chunks) {
        int64_t n_frames  = (int64_t)(chunk.samples.size() / kChannels);
        int64_t chunk_end = chunk.start_ns + n_frames * ns_per_frame;
        if (chunk_end  < start_ns)    continue;
        if (chunk.start_ns > end_time_ns) break;

        int64_t skip = 0;
        if (chunk.start_ns < start_ns)
            skip = (start_ns - chunk.start_ns) / ns_per_frame;
        int64_t take = n_frames - skip;
        int64_t over = (chunk_end - end_time_ns) / ns_per_frame;
        if (over > 0) take -= over;
        if (take <= 0) continue;

        size_t ss = (size_t)(skip * kChannels);
        size_t ts = (size_t)(take * kChannels);
        if (ss + ts > chunk.samples.size()) ts = chunk.samples.size() - ss;
        result.insert(result.end(),
                      chunk.samples.begin() + (ptrdiff_t)ss,
                      chunk.samples.begin() + (ptrdiff_t)(ss + ts));
    }
    return result;
}


std::map<std::string, std::string> AudioMultiCapture::GetCurrentMappings() const {
    std::lock_guard<std::mutex> lk(mappings_mutex_);
    return current_mappings_;
}

} // namespace fthr
