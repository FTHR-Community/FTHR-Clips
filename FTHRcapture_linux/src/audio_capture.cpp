#include "audio_capture.h"
#include <pulse/simple.h>
#include <pulse/error.h>
#include <iostream>
#include <cstring>
#include <time.h>

namespace fthr {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Start / Stop
// ---------------------------------------------------------------------------

bool AudioCapture::Start(const std::string& device_name) {
    if (running_.load())
        return true;
    running_.store(true);
    thread_ = std::thread(&AudioCapture::CaptureLoop, this, device_name);
    return true;
}

void AudioCapture::Stop() {
    running_.store(false);
    if (thread_.joinable())
        thread_.join();
}

// ---------------------------------------------------------------------------
// CaptureLoop — runs on background thread
// ---------------------------------------------------------------------------

void AudioCapture::CaptureLoop(std::string device_name) {
    // PulseAudio sample spec: float32, 48kHz, stereo
    pa_sample_spec ss;
    ss.format   = PA_SAMPLE_FLOAT32LE;
    ss.rate     = static_cast<uint32_t>(kSampleRate);
    ss.channels = static_cast<uint8_t>(kChannels);

    // Determine source: use default sink monitor for loopback unless overridden
    const char* source = nullptr;
    std::string monitor;
    if (!device_name.empty() && device_name != "auto") {
        source = device_name.c_str();
    }
    // If source is nullptr, PulseAudio will use the default source.
    // For true loopback, the user should configure their default source
    // to be a monitor (e.g. alsa_output.*.monitor in PipeWire-pulse).

    int pa_err = 0;
    pa_simple* pa = pa_simple_new(
        nullptr,          // default server
        "FTHRclips",      // application name
        PA_STREAM_RECORD,
        source,           // source device (nullptr = default)
        "loopback",       // stream description
        &ss,
        nullptr,          // default channel map
        nullptr,          // default buffering attributes
        &pa_err
    );

    if (!pa) {
        std::cerr << "[Audio] pa_simple_new failed: "
                  << pa_strerror(pa_err) << std::endl;
        running_.store(false);
        return;
    }

    std::cout << "[Audio] PulseAudio capture started ("
              << kSampleRate << "Hz stereo float32)" << std::endl;

    const int kSamplesPerChunk = kFramesPerChunk * kChannels;
    std::vector<float> buf(static_cast<size_t>(kSamplesPerChunk));

    while (running_.load()) {
        int64_t chunk_start_ns = mono_ns();

        if (pa_simple_read(pa, buf.data(),
                           buf.size() * sizeof(float), &pa_err) < 0) {
            std::cerr << "[Audio] pa_simple_read error: "
                      << pa_strerror(pa_err) << std::endl;
            break;
        }

        TimedChunk chunk;
        chunk.samples   = buf;
        chunk.start_ns  = chunk_start_ns;

        {
            std::lock_guard<std::mutex> lk(mutex_);
            chunks_.push_back(std::move(chunk));

            // Prune chunks to stay within kMaxSeconds
            size_t total = 0;
            for (auto& c : chunks_) total += c.samples.size();
            while (total > kMaxRingSamples && !chunks_.empty()) {
                total -= chunks_.front().samples.size();
                chunks_.pop_front();
            }
        }
    }

    pa_simple_free(pa);
    std::cout << "[Audio] PulseAudio capture stopped" << std::endl;
}

// ---------------------------------------------------------------------------
// ExtractSegment
// ---------------------------------------------------------------------------

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
