#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>

namespace fthr {

struct RecoveryDecision {
    bool retry = false;
    bool exhausted = false;
    uint32_t attempt = 0;
    std::chrono::milliseconds backoff{0};
};

// Pure policy kept separate from CaptureEngine so retry limits can be tested
// with a fake backend without a compositor or FFmpeg encoder.
class CaptureRecoveryPolicy {
public:
    explicit CaptureRecoveryPolicy(uint64_t healthy_reset_frames,
                                   uint32_t max_attempts = 3)
        : healthy_reset_frames_(std::max<uint64_t>(1, healthy_reset_frames))
        , max_attempts_(std::max<uint32_t>(1, max_attempts)) {}

    RecoveryDecision OnGenerationFailed(uint64_t produced_frames) {
        if (produced_frames >= healthy_reset_frames_)
            attempts_ = 0;
        ++attempts_;
        if (attempts_ > max_attempts_)
            return {false, true, attempts_, std::chrono::milliseconds(0)};
        const size_t index = std::min<size_t>(attempts_ - 1, kBackoffs.size() - 1);
        return {true, false, attempts_, kBackoffs[index]};
    }

    uint32_t Attempts() const noexcept { return attempts_; }

private:
    inline static constexpr std::array<std::chrono::milliseconds, 3> kBackoffs{
        std::chrono::milliseconds(250),
        std::chrono::milliseconds(750),
        std::chrono::milliseconds(1500),
    };
    uint64_t healthy_reset_frames_;
    uint32_t max_attempts_;
    uint32_t attempts_ = 0;
};

// A broken encoder (for example a VA-API upload that the driver refuses)
// otherwise leaves capture reporting healthy frames while the ring stays
// empty. Half a second of consecutive failures, but never fewer than five
// frames, ends the generation so the encoder is reopened.
class EncodeFailurePolicy {
public:
    explicit EncodeFailurePolicy(uint32_t fps)
        : threshold_(std::max<uint32_t>(5, fps / 2)) {}

    // Returns true when the generation must give up on this encoder.
    bool OnEncodeResult(bool ok) {
        if (ok) { consecutive_ = 0; return false; }
        ++consecutive_;
        return consecutive_ >= threshold_;
    }

    uint32_t ConsecutiveFailures() const noexcept { return consecutive_; }

private:
    uint32_t threshold_;
    uint32_t consecutive_ = 0;
};

// The capture loop encodes at most one frame per 1/fps of capture time. After
// a stall the next deadline would lie far in the past and every arriving
// frame would be encoded back to back until wall time was caught up, which
// compresses the gap in media time. Re-anchor on the first late frame instead.
inline int64_t NextEncodeDeadline(int64_t next_encode_ns, int64_t frame_ns,
                                  int64_t frame_timestamp_ns) {
    return frame_timestamp_ns > next_encode_ns + 2 * frame_ns
        ? frame_timestamp_ns : next_encode_ns;
}

// Production backoff primitive with injected clock/sleeper behavior.  The
// 50 ms slices bound shutdown latency and make cancellation deterministic in
// the native fake-backend harness without sleeping the test process.
template <typename IsRunning, typename Sleeper>
bool WaitForRecoveryBackoff(std::chrono::milliseconds remaining,
                            IsRunning&& is_running,
                            Sleeper&& sleep) {
    while (is_running() && remaining.count() > 0) {
        const auto slice = std::min(remaining, std::chrono::milliseconds(50));
        sleep(slice);
        remaining -= slice;
    }
    return is_running();
}

} // namespace fthr
