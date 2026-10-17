#include "audio_capture.h"
#include "audio_timing.h"
#include "audio_devices.h"
#include <pulse/simple.h>
#include <pulse/error.h>
#include <pulse/pulseaudio.h>
#include <chrono>
#include <functional>
#include <iostream>
#include <cstring>
#include <thread>
#include <time.h>

namespace fthr {

// Helpers

static int64_t mono_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
}

// Chunk size: 10ms worth of frames
static constexpr int kFramesPerChunk = AudioCapture::kSampleRate / 100;

struct MonitorResolver {
    std::string default_sink;
    std::string monitor_source;
    bool server_done{false};
    bool sink_done{false};
};

static void server_info_cb(pa_context*, const pa_server_info* info, void* userdata) {
    auto* state = static_cast<MonitorResolver*>(userdata);
    if (info && info->default_sink_name)
        state->default_sink = info->default_sink_name;
    state->server_done = true;
}

static void sink_info_cb(pa_context*, const pa_sink_info* info, int eol,
                         void* userdata) {
    auto* state = static_cast<MonitorResolver*>(userdata);
    if (info && info->monitor_source_name)
        state->monitor_source = info->monitor_source_name;
    if (eol != 0) state->sink_done = true;
}

static bool iterate_until(pa_mainloop* loop, pa_context* context,
                          const std::function<bool()>& done,
                          std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (!done() && std::chrono::steady_clock::now() < deadline) {
        int retval = 0;
        if (pa_mainloop_iterate(loop, 0, &retval) < 0) return false;
        const auto state = pa_context_get_state(context);
        if (state == PA_CONTEXT_FAILED || state == PA_CONTEXT_TERMINATED)
            return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return done();
}

// Runs body(loop, context) on a freshly connected context and tears it down
// again. False when the server cannot be reached within the timeout.
static bool with_pa_context(
    const std::function<void(pa_mainloop*, pa_context*)>& body) {
    pa_mainloop* loop = pa_mainloop_new();
    if (!loop) return false;
    pa_context* context = pa_context_new(pa_mainloop_get_api(loop), "FTHRclips");
    if (!context) {
        pa_mainloop_free(loop);
        return false;
    }
    const bool ready =
        pa_context_connect(context, nullptr, PA_CONTEXT_NOAUTOSPAWN, nullptr) >= 0
        && iterate_until(loop, context, [context] {
            return pa_context_get_state(context) == PA_CONTEXT_READY;
        }, std::chrono::seconds(2));
    if (ready) body(loop, context);
    pa_context_disconnect(context);
    pa_context_unref(context);
    pa_mainloop_free(loop);
    return ready;
}

// Waits for op to finish (done() true) and releases it. False on timeout or
// a failed context.
static bool run_operation(pa_mainloop* loop, pa_context* context,
                          pa_operation* op, const std::function<bool()>& done) {
    if (!op) return false;
    const bool ok = iterate_until(loop, context, done, std::chrono::seconds(2));
    pa_operation_unref(op);
    return ok;
}

// Monitor source of `sink`, or of the default sink when `sink` is empty.
// Empty when the server is unreachable or the sink does not exist;
// `reached` (optional) tells the two apart.
static std::string resolve_sink_monitor(const std::string& sink,
                                        bool* reached = nullptr) {
    std::string result;
    const bool connected = with_pa_context([&](pa_mainloop* loop, pa_context* context) {
        MonitorResolver resolver;
        resolver.default_sink = sink;
        if (sink.empty()) {
            if (!run_operation(loop, context,
                    pa_context_get_server_info(context, server_info_cb, &resolver),
                    [&resolver] { return resolver.server_done; }))
                return;
        }
        if (resolver.default_sink.empty()) return;
        if (run_operation(loop, context,
                pa_context_get_sink_info_by_name(
                    context, resolver.default_sink.c_str(), sink_info_cb, &resolver),
                [&resolver] { return resolver.sink_done; }))
            result = resolver.monitor_source;
    });
    if (reached) *reached = connected;
    return result;
}

static std::string resolve_default_sink_monitor() {
    return resolve_sink_monitor({});
}

struct SinkLister {
    std::vector<AudioEndpoint> sinks;
    bool done{false};
    bool failed{false};
};

static void sink_list_cb(pa_context*, const pa_sink_info* info, int eol,
                         void* userdata) {
    auto* state = static_cast<SinkLister*>(userdata);
    if (eol != 0) {
        // eol < 0 is a server-side error: the list is incomplete.
        state->failed = eol < 0;
        state->done = true;
        return;
    }
    if (!info || !info->name || !info->name[0]) return;
    AudioEndpoint endpoint;
    endpoint.endpoint_id = info->name;
    endpoint.display_name = (info->description && info->description[0])
        ? info->description : info->name;
    state->sinks.push_back(std::move(endpoint));
}

bool EnumeratePlaybackSinks(std::vector<AudioEndpoint>* endpoints,
                            std::string* error) {
    bool listed = false;
    SinkLister lister;
    MonitorResolver server;
    const bool connected = with_pa_context([&](pa_mainloop* loop, pa_context* context) {
        // The default sink is informational: a server that does not report
        // one still lists its sinks.
        run_operation(loop, context,
            pa_context_get_server_info(context, server_info_cb, &server),
            [&server] { return server.server_done; });
        listed = run_operation(loop, context,
            pa_context_get_sink_info_list(context, sink_list_cb, &lister),
            [&lister] { return lister.done; }) && !lister.failed;
    });
    if (!connected) {
        if (error) *error = "PulseAudio server unreachable";
        return false;
    }
    if (!listed) {
        if (error) *error = "PulseAudio did not list its sinks";
        return false;
    }
    for (auto& sink : lister.sinks)
        sink.is_default = !server.default_sink.empty() &&
                          sink.endpoint_id == server.default_sink;
    if (endpoints) *endpoints = std::move(lister.sinks);
    return true;
}

// Start / Stop

// Reconnect pacing after the audio server drops the stream, and how often
// the default output device is re-checked.
static constexpr auto kReopenInterval = std::chrono::seconds(1);
static constexpr auto kDefaultOutputPoll = std::chrono::seconds(2);

pa_simple* AudioCapture::OpenStream(const std::string& source_name) const {
    pa_sample_spec ss;
    ss.format   = PA_SAMPLE_FLOAT32LE;
    ss.rate     = static_cast<uint32_t>(kSampleRate);
    ss.channels = static_cast<uint8_t>(kChannels);
    pa_buffer_attr buffer_attr{};
    buffer_attr.maxlength = static_cast<uint32_t>(kFramesPerChunk * kChannels * sizeof(float) * 20);
    buffer_attr.fragsize = static_cast<uint32_t>(kFramesPerChunk * kChannels * sizeof(float));
    buffer_attr.prebuf = buffer_attr.minreq = buffer_attr.tlength = static_cast<uint32_t>(-1);
    int pa_err = 0;
    pa_simple* stream = pa_simple_new(
        nullptr, "FTHRclips", PA_STREAM_RECORD, source_name.c_str(),
        "desktop-output-loopback", &ss, nullptr, &buffer_attr, &pa_err);
    if (!stream) {
        std::cerr << "[Audio] Could not open output monitor '" << source_name
                  << "': " << pa_strerror(pa_err) << std::endl;
    }
    return stream;
}

bool AudioCapture::Start(const std::string& device_name, uint32_t keep_seconds) {
    Stop();

    follow_default_ = device_name.empty() || device_name == "auto";
    keep_seconds_ = keep_seconds;
    ring_capacity_samples_ =
        AudioRingCapacitySamples(keep_seconds, kSampleRate, kChannels);
    std::string source_name =
        follow_default_ ? resolve_default_sink_monitor() : device_name;
    if (source_name.empty() && !follow_default_) {
        std::cerr << "[Audio] No audio device name given" << std::endl;
        return false;
    }

    // A monitor that cannot be opened now is not fatal: the capture thread
    // retries every second (the same path as a mid-session server loss), and
    // the watcher supplies the source once the server is back.
    if (!source_name.empty())
        stream_ = OpenStream(source_name);
    stream_open_.store(stream_ != nullptr);
    if (!stream_) {
        std::cerr << "[Audio] "
                  << (follow_default_ ? std::string("Default output monitor")
                                      : "Source '" + source_name + "'")
                  << " unavailable; will keep trying every second" << std::endl;
    }

    {
        std::lock_guard<std::mutex> lk(mutex_);
        chunks_.clear();
        ring_total_ = 0;
    }
    {
        std::lock_guard<std::mutex> lk(source_mutex_);
        // An explicit device keeps its name so the reopen loop retries it.
        source_name_ = (stream_ || !follow_default_) ? source_name : std::string();
        pending_source_.clear();
    }
    if (stream_) {
        std::cout << "[Audio] Capturing "
                  << (follow_default_ ? "default output monitor: "
                                      : "output monitor: ")
                  << source_name << std::endl;
    }
    running_.store(true);
    thread_ = std::thread(&AudioCapture::CaptureLoop, this);
    if (follow_default_)
        watcher_ = std::thread(&AudioCapture::WatchDefaultOutput, this);
    return true;
}

bool AudioCapture::StartForSink(const std::string& sink, uint32_t keep_seconds) {
    if (sink.empty()) return Start("", keep_seconds);
    // Ask the server for the sink's monitor rather than appending
    // ".monitor": that also tells a missing sink apart.
    bool reached = false;
    const std::string monitor = resolve_sink_monitor(sink, &reached);
    if (monitor.empty() && !reached) {
        // The server is not up yet (pipewire-pulse still starting): keep the
        // user's choice and let the capture loop retry its monitor rather
        // than settling on the default for the whole session.
        std::cerr << "[Audio] PulseAudio unreachable; will keep trying playback"
                  << " device '" << sink << "'" << std::endl;
        return Start(sink + ".monitor", keep_seconds);
    }
    if (monitor.empty()) {
        std::cerr << "[Audio] Playback device '" << sink << "' not found;"
                  << " capturing the default output monitor instead" << std::endl;
        return Start("", keep_seconds);
    }
    std::cout << "[Audio] Playback device '" << sink << "' -> " << monitor
              << std::endl;
    return Start(monitor, keep_seconds);
}

void AudioCapture::Stop() {
    {
        // Under the watcher's mutex so a watcher between its predicate check
        // and wait cannot miss the wake-up.
        std::lock_guard<std::mutex> lk(source_mutex_);
        running_.store(false);
    }
    watcher_cv_.notify_all();
    if (watcher_.joinable())
        watcher_.join();
    if (thread_.joinable())
        thread_.join();
    if (stream_) {
        pa_simple_free(stream_);
        stream_ = nullptr;
    }
    stream_open_.store(false);
}

void AudioCapture::WatchDefaultOutput() {
    std::unique_lock<std::mutex> lk(source_mutex_);
    while (running_.load()) {
        watcher_cv_.wait_for(lk, kDefaultOutputPoll,
                             [this] { return !running_.load(); });
        if (!running_.load()) break;
        lk.unlock();
        const std::string resolved = resolve_default_sink_monitor();
        lk.lock();
        if (!resolved.empty() && resolved != source_name_ &&
                resolved != pending_source_) {
            std::cout << "[Audio] Default output changed to " << resolved
                      << std::endl;
            pending_source_ = resolved;
        }
    }
}

// CaptureLoop — runs on background thread

void AudioCapture::CaptureLoop() {
    int pa_err = 0;
    // Announced when audio actually flows, which may be well after Start()
    // if PulseAudio was not running yet.
    bool announced = false;
    auto announce = [&announced] {
        if (announced) return;
        announced = true;
        std::cout << "[Audio] PulseAudio capture started ("
                  << kSampleRate << "Hz stereo float32)" << std::endl;
    };
    if (stream_) announce();

    const int kSamplesPerChunk = kFramesPerChunk * kChannels;
    std::vector<float> buf(static_cast<size_t>(kSamplesPerChunk));
    auto next_reopen = std::chrono::steady_clock::now();

    while (running_.load()) {
        std::string switch_to;
        {
            std::lock_guard<std::mutex> lk(source_mutex_);
            switch_to.swap(pending_source_);
        }
        if (!switch_to.empty()) {
            // Open the new monitor before dropping the old one so a failed
            // switch keeps recording from the previous device.
            if (pa_simple* next = OpenStream(switch_to)) {
                const bool was_closed = (stream_ == nullptr);
                if (stream_) pa_simple_free(stream_);
                stream_ = next;
                stream_open_.store(true);
                {
                    std::lock_guard<std::mutex> lk(source_mutex_);
                    source_name_ = switch_to;
                }
                // The watcher can resolve the monitor before the reopen
                // branch below does; log the open from either path.
                if (was_closed) {
                    announce();
                    std::cout << "[Audio] Reconnected to " << switch_to
                              << std::endl;
                }
            }
        }

        if (!stream_) {
            // The audio server went away. Retry at a fixed pace; captured
            // video keeps its timeline and the gap is saved as silence.
            if (std::chrono::steady_clock::now() < next_reopen) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            next_reopen = std::chrono::steady_clock::now() + kReopenInterval;
            std::string source;
            if (follow_default_) {
                source = resolve_default_sink_monitor();
            } else {
                std::lock_guard<std::mutex> lk(source_mutex_);
                source = source_name_;
            }
            if (source.empty()) continue;
            stream_ = OpenStream(source);
            if (!stream_) continue;
            stream_open_.store(true);
            announce();
            {
                std::lock_guard<std::mutex> lk(source_mutex_);
                source_name_ = source;
            }
            std::cout << "[Audio] Reconnected to " << source << std::endl;
        }

        if (pa_simple_read(stream_, buf.data(),
                           buf.size() * sizeof(float), &pa_err) < 0) {
            std::cerr << "[Audio] pa_simple_read error: "
                      << pa_strerror(pa_err) << "; reconnecting" << std::endl;
            pa_simple_free(stream_);
            stream_ = nullptr;
            stream_open_.store(false);
            next_reopen = std::chrono::steady_clock::now() + kReopenInterval;
            continue;
        }
        int64_t delivery_end_ns = mono_ns();
        pa_usec_t latency_us = pa_simple_get_latency(stream_, &pa_err);
        if (latency_us == static_cast<pa_usec_t>(-1))
            latency_us = 0;

        TimedChunk chunk;
        chunk.samples   = buf;
        chunk.start_ns  = AudioChunkStartNs(
            delivery_end_ns, static_cast<int64_t>(latency_us),
            buf.size() / kChannels, kSampleRate);

        {
            std::lock_guard<std::mutex> lk(mutex_);
            ring_total_ += chunk.samples.size();
            chunks_.push_back(std::move(chunk));

            // Prune oldest chunks to stay within the replay window.
            // Maintain ring_total_ incrementally instead of re-summing all
            // chunks every callback — at 10ms intervals with 90s of history
            // that's ~9000 iterations per callback without this optimization.
            while (ring_total_ > ring_capacity_samples_ && !chunks_.empty()) {
                ring_total_ -= chunks_.front().samples.size();
                chunks_.pop_front();
            }
        }
    }

    std::cout << "[Audio] PulseAudio capture stopped" << std::endl;
}


std::vector<float> AudioCapture::ExtractSegment(int64_t end_time_ns,
                                                  uint32_t duration_ms) const {
    std::lock_guard<std::mutex> lk(mutex_);
    if (chunks_.empty())
        return {};

    if (end_time_ns == 0) {
        // Use newest captured sample as endpoint
        const auto& last = chunks_.back();
        int64_t ns_per_sample = 1'000'000'000LL / kSampleRate;
        end_time_ns = last.start_ns +
            static_cast<int64_t>(last.samples.size() / kChannels) * ns_per_sample;
    }

    std::vector<TimedAudioBlock> blocks;
    blocks.reserve(chunks_.size());
    for (const auto& chunk : chunks_)
        blocks.push_back({chunk.samples.data(), chunk.samples.size() / kChannels,
                          chunk.start_ns});
    return AssembleAudioWindow(blocks, kChannels, kSampleRate,
                               end_time_ns, duration_ms);
}

} // namespace fthr
