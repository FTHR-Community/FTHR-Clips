#pragma once

#include "shared_memory.h"

namespace fthr {

inline bool ReplayWatchdogSuspended(uint32_t health) noexcept {
    return (health & (CAPTURE_HEALTH_RECOVERING | CAPTURE_HEALTH_PAUSED)) != 0;
}

// Compact replay time is safe only for video-only protected-window capture.
// Audio producers and process-loopback correlation remain on raw QPC so a
// mixed generation never combines compact and uncompact coordinate systems.
constexpr bool ShouldUseCompactReplayPauseClock(
    bool focus_gated, bool audio_enabled, bool separate_audio_enabled) noexcept {
    return focus_gated && !audio_enabled && !separate_audio_enabled;
}

struct CaptureFocusTransition {
    bool paused = false;
    bool resumed = false;
    bool discard_queued_frames = false;
};

class CaptureFocusPolicy {
public:
    CaptureFocusTransition Observe(bool paused) noexcept {
        const CaptureFocusTransition result{paused, was_paused_ && !paused,
                                            paused || was_paused_};
        was_paused_ = paused;
        return result;
    }

private:
    bool was_paused_ = false;
};

} // namespace fthr
