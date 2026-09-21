#include "audio_capture.h"
#include "audio_timing.h"
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

// Maximum ring buffer length in samples (interleaved stereo float32)
static constexpr size_t kMaxRingSamples =
    static_cast<size_t>(AudioCapture::kSampleRate) *
    static_cast<size_t>(AudioCapture::kChannels)   *
    static_cast<size_t>(AudioCapture::kMaxSeconds);

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

static std::string resolve_default_sink_monitor() {
    pa_mainloop* loop = pa_mainloop_new();
    if (!loop) return {};
    pa_context* context = pa_context_new(pa_mainloop_get_api(loop), "FTHRclips");
    if (!context) {
        pa_mainloop_free(loop);
        return {};
    }

    std::string result;
    MonitorResolver resolver;
    if (pa_context_connect(context, nullptr, PA_CONTEXT_NOAUTOSPAWN, nullptr) >= 0
        && iterate_until(loop, context, [context] {
            return pa_context_get_state(context) == PA_CONTEXT_READY;
        }, std::chrono::seconds(2))) {
        pa_operation* server_op = pa_context_get_server_info(
            context, server_info_cb, &resolver);
        if (server_op && iterate_until(loop, context, [&resolver] {
                return resolver.server_done;
            }, std::chrono::seconds(2))) {
            pa_operation_unref(server_op);
            server_op = nullptr;
            if (!resolver.default_sink.empty()) {
                pa_operation* sink_op = pa_context_get_sink_info_by_name(
                    context, resolver.default_sink.c_str(), sink_info_cb, &resolver);
                if (sink_op && iterate_until(loop, context, [&resolver] {
                        return resolver.sink_done;
                    }, std::chrono::seconds(2))) {
                    result = resolver.monitor_source;
                }
                if (sink_op) pa_operation_unref(sink_op);
            }
        }
        if (server_op) pa_operation_unref(server_op);
    }

    pa_context_disconnect(context);
    pa_context_unref(context);
    pa_mainloop_free(loop);
    return result;
}

// Start / Stop

bool AudioCapture::Start(const std::string& device_name) {
    Stop();

    std::string source_name = device_name;
    if (source_name.empty() || source_name == "auto")
        source_name = resolve_default_sink_monitor();
    if (source_name.empty()) {
        std::cerr << "[Audio] Default output monitor could not be resolved"
                  << std::endl;
        return false;
    }

    pa_sample_spec ss;
    ss.format   = PA_SAMPLE_FLOAT32LE;
    ss.rate     = static_cast<uint32_t>(kSampleRate);
    ss.channels = static_cast<uint8_t>(kChannels);
    pa_buffer_attr buffer_attr{};
    buffer_attr.maxlength = static_cast<uint32_t>(kFramesPerChunk * kChannels * sizeof(float) * 20);
    buffer_attr.fragsize = static_cast<uint32_t>(kFramesPerChunk * kChannels * sizeof(float));
    buffer_attr.prebuf = buffer_attr.minreq = buffer_attr.tlength = static_cast<uint32_t>(-1);
    int pa_err = 0;
    stream_ = pa_simple_new(
        nullptr, "FTHRclips", PA_STREAM_RECORD, source_name.c_str(),
        "desktop-output-loopback", &ss, nullptr, &buffer_attr, &pa_err);
    if (!stream_) {
        std::cerr << "[Audio] Could not open output monitor '" << source_name
                  << "': " << pa_strerror(pa_err) << std::endl;
        return false;
    }

    {
        std::lock_guard<std::mutex> lk(mutex_);
        chunks_.clear();
        ring_total_ = 0;
    }
    std::cout << "[Audio] Capturing default output monitor: "
              << source_name << std::endl;
    running_.store(true);
    thread_ = std::thread(&AudioCapture::CaptureLoop, this);
    return true;
}

void AudioCapture::Stop() {
    running_.store(false);
    if (thread_.joinable())
        thread_.join();
    if (stream_) {
        pa_simple_free(stream_);
        stream_ = nullptr;
    }
}

// CaptureLoop — runs on background thread

void AudioCapture::CaptureLoop() {
    int pa_err = 0;
    std::cout << "[Audio] PulseAudio capture started ("
              << kSampleRate << "Hz stereo float32)" << std::endl;

    const int kSamplesPerChunk = kFramesPerChunk * kChannels;
    std::vector<float> buf(static_cast<size_t>(kSamplesPerChunk));

    while (running_.load()) {
        if (pa_simple_read(stream_, buf.data(),
                           buf.size() * sizeof(float), &pa_err) < 0) {
            std::cerr << "[Audio] pa_simple_read error: "
                      << pa_strerror(pa_err) << std::endl;
            break;
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

            // Prune oldest chunks to stay within kMaxSeconds.
            // Maintain ring_total_ incrementally instead of re-summing all
            // chunks every callback — at 10ms intervals with 90s of history
            // that's ~9000 iterations per callback without this optimization.
            while (ring_total_ > kMaxRingSamples && !chunks_.empty()) {
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

    int64_t start_time_ns = end_time_ns -
        static_cast<int64_t>(duration_ms) * 1'000'000LL;

    std::vector<float> result;
    int64_t ns_per_frame = 1'000'000'000LL / kSampleRate;

    for (const auto& chunk : chunks_) {
        int64_t frames_in_chunk = static_cast<int64_t>(chunk.samples.size() / kChannels);
        int64_t chunk_end_ns    = chunk.start_ns + frames_in_chunk * ns_per_frame;

        // Skip chunks entirely outside the window
        if (chunk_end_ns < start_time_ns) continue;
        if (chunk.start_ns > end_time_ns)  break;

        // Compute per-chunk sample slice
        int64_t skip_frames = 0;
        if (chunk.start_ns < start_time_ns)
            skip_frames = (start_time_ns - chunk.start_ns) / ns_per_frame;

        int64_t take_frames = frames_in_chunk - skip_frames;
        int64_t overshoot   = (chunk_end_ns - end_time_ns) / ns_per_frame;
        if (overshoot > 0) take_frames -= overshoot;
        if (take_frames <= 0) continue;

        size_t skip_samples = static_cast<size_t>(skip_frames * kChannels);
        size_t take_samples = static_cast<size_t>(take_frames * kChannels);
        if (skip_samples + take_samples > chunk.samples.size())
            take_samples = chunk.samples.size() - skip_samples;

        result.insert(result.end(),
                      chunk.samples.begin() + static_cast<ptrdiff_t>(skip_samples),
                      chunk.samples.begin() + static_cast<ptrdiff_t>(skip_samples + take_samples));
    }

    return result;
}

} // namespace fthr
