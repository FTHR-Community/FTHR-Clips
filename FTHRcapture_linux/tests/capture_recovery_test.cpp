#include "recovery_policy.h"

#include <cassert>
#include <chrono>
#include <cstdint>

namespace {

class FakeBackend {
public:
    explicit FakeBackend(uint64_t frames_before_failure)
        : remaining_(frames_before_failure) {}

    bool CaptureFrame() {
        if (remaining_ == 0) return false;
        --remaining_;
        return true;
    }

private:
    uint64_t remaining_;
};

uint64_t RunGeneration(FakeBackend& backend) {
    uint64_t frames = 0;
    while (backend.CaptureFrame()) ++frames;
    return frames;
}

class LifecycleBackend {
public:
    explicit LifecycleBackend(uint64_t frames) : backend_(frames) {}

    bool Initialize() {
        ++initialize_calls;
        initialized_ = true;
        return true;
    }

    uint64_t Run() {
        assert(initialized_ && shutdown_calls == 0);
        return RunGeneration(backend_);
    }

    void Shutdown() {
        assert(initialized_);
        ++shutdown_calls;
    }

    uint32_t initialize_calls = 0;
    uint32_t shutdown_calls = 0;

private:
    FakeBackend backend_;
    bool initialized_ = false;
};

} // namespace

int main() {
    fthr::CaptureRecoveryPolicy policy(300);
    FakeBackend first(10);
    auto decision = policy.OnGenerationFailed(RunGeneration(first));
    assert(decision.retry && !decision.exhausted);
    assert(decision.attempt == 1 && decision.backoff.count() == 250);

    // A healthy generation makes a later failure a new incident.
    FakeBackend healthy(300);
    decision = policy.OnGenerationFailed(RunGeneration(healthy));
    assert(decision.retry && decision.attempt == 1);

    for (uint32_t expected = 2; expected <= 3; ++expected) {
        FakeBackend short_lived(1);
        decision = policy.OnGenerationFailed(RunGeneration(short_lived));
        assert(decision.retry && decision.attempt == expected);
    }
    FakeBackend terminal(0);
    decision = policy.OnGenerationFailed(RunGeneration(terminal));
    assert(!decision.retry && decision.exhausted);

    bool running = true;
    uint32_t sleep_calls = 0;
    const bool completed = fthr::WaitForRecoveryBackoff(
        std::chrono::milliseconds(1500),
        [&running] { return running; },
        [&running, &sleep_calls](std::chrono::milliseconds slice) {
            assert(slice.count() == 50);
            ++sleep_calls;
            running = false;
        });
    assert(!completed);
    assert(sleep_calls == 1);

    // A timed-out generation is torn down before a replacement starts. Real
    // frame progress in the replacement resets the incident retry budget.
    fthr::CaptureRecoveryPolicy reconnect_policy(300);
    LifecycleBackend stale(0);
    assert(stale.Initialize());
    const auto stalled_frames = stale.Run();
    stale.Shutdown();
    assert(stalled_frames == 0 && stale.shutdown_calls == 1);
    decision = reconnect_policy.OnGenerationFailed(stalled_frames);
    assert(decision.retry && decision.attempt == 1);

    LifecycleBackend replacement(300);
    assert(replacement.Initialize());
    const auto replacement_frames = replacement.Run();
    assert(replacement_frames == 300);
    replacement.Shutdown();
    decision = reconnect_policy.OnGenerationFailed(replacement_frames);
    assert(decision.retry && decision.attempt == 1);

    // Shutdown during a cancellable backoff cannot begin another reconnect.
    running = true;
    uint32_t reconnect_starts = 0;
    const bool start_reconnect = fthr::WaitForRecoveryBackoff(
        std::chrono::milliseconds(750),
        [&running] { return running; },
        [&running](std::chrono::milliseconds) { running = false; });
    if (start_reconnect) ++reconnect_starts;
    assert(!start_reconnect);
    assert(reconnect_starts == 0);
    

    // Encoder failures give up after max(fps/2, 5) consecutive misses and
    // reset on any success.
    {
        fthr::EncodeFailurePolicy encode_policy(60);
        for (uint32_t i = 0; i < 29; ++i)
            assert(!encode_policy.OnEncodeResult(false));
        assert(encode_policy.ConsecutiveFailures() == 29);
        assert(encode_policy.OnEncodeResult(false));          // 30th = fps/2
        assert(!encode_policy.OnEncodeResult(true));
        assert(encode_policy.ConsecutiveFailures() == 0);

        fthr::EncodeFailurePolicy low_fps(1);
        for (uint32_t i = 0; i < 4; ++i)
            assert(!low_fps.OnEncodeResult(false));
        assert(low_fps.OnEncodeResult(false));                // minimum of 5
    }

    // After a stall the throttle re-anchors instead of encoding a burst of
    // back-to-back frames to "catch up" with wall time.
    {
        constexpr int64_t frame_ns = 16'666'667;
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000) == 1'000'000'000);
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000 + frame_ns) == 1'000'000'000);
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000 + 3 * frame_ns)
               == 1'000'000'000 + 3 * frame_ns);
    }
    return 0;
}
