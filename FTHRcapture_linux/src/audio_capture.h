#pragma once
#include <string>
#include <vector>
#include <deque>
#include <mutex>
#include <thread>
#include <atomic>
#include <cstdint>
#include <pulse/simple.h>

namespace fthr {

// PulseAudio simple API audio loopback capture.
// Records float32 stereo 48kHz from a monitor source (system audio loopback).
// Keeps up to max_seconds of audio in a ring deque, thread-safe.
class AudioCapture {
public:
    static constexpr int kSampleRate  = 48000;
    static constexpr int kChannels    = 2;
    static constexpr int kMaxSeconds  = 1800;

    AudioCapture() = default;
    ~AudioCapture() { Stop(); }

    // device_name: PulseAudio source name.
    //   Pass "" or "auto" to use the default sink monitor (loopback).
    bool Start(const std::string& device_name = "");
    void Stop();

    bool IsRunning() const { return running_.load(); }

    // Returns interleaved float32 samples covering the last duration_ms
    // milliseconds ending at end_time_ns (CLOCK_MONOTONIC). If end_time_ns
    // is 0, uses the most recent captured sample as the endpoint.
    std::vector<float> ExtractSegment(int64_t end_time_ns,
                                      uint32_t duration_ms) const;

private:
    void CaptureLoop();

    struct TimedChunk {
        std::vector<float> samples;  // interleaved stereo float32
        int64_t            start_ns; // CLOCK_MONOTONIC when first sample was captured
    };

    mutable std::mutex        mutex_;
    std::deque<TimedChunk>    chunks_;
    size_t                    ring_total_{0};  // guarded by mutex_
    std::thread               thread_;
    std::atomic<bool>         running_{false};
    pa_simple*                stream_{nullptr};
};

} // namespace fthr
