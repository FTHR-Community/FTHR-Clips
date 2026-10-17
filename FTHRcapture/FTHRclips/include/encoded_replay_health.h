#pragma once

// Production packet selection-to-health input preparation shared with the
// deterministic replay-policy tests. The health predicate itself remains in
// replay_health.h and is intentionally unchanged here.

#include <cmath>
#include <cstdint>
#include <vector>

#include "encoded_ring_buffer.h"
#include "replay_health.h"

namespace fthr::encoded_replay_health {

inline replay_health::Input MakeInput(
    const EncodedRingSnapshot& snapshot,
    uint32_t requested_duration_seconds,
    double fallback_fps,
    int64_t wall_qpc_frequency,
    int64_t target_end_wall_qpc) {
    replay_health::Input input;
    input.requested_duration_seconds = requested_duration_seconds;
    input.expected_fps = snapshot.video_config.frame_rate.denominator > 0
        ? static_cast<double>(snapshot.video_config.frame_rate.numerator)
            / static_cast<double>(snapshot.video_config.frame_rate.denominator)
        : fallback_fps;
    input.wall_qpc_frequency = wall_qpc_frequency;
    input.target_end_wall_qpc = target_end_wall_qpc;
    input.pts_per_second = snapshot.video_config.time_base.numerator > 0
        ? static_cast<double>(snapshot.video_config.time_base.denominator)
            / static_cast<double>(snapshot.video_config.time_base.numerator)
        : static_cast<double>(std::llround(input.expected_fps));
    input.full_history = snapshot.full_history;
    input.packets.reserve(snapshot.packets.size());
    for (const auto& packet : snapshot.packets) {
        // Decoder keyframe pre-roll is intentionally part of the mux
        // snapshot, but it is not presentation coverage. Keep this exact
        // production selection rule shared with tests.
        if (packet.pts < snapshot.presentation_start_pts) {
            continue;
        }
        input.packets.push_back({
            packet.wall_qpc, packet.pts, packet.generation});
    }
    return input;
}

} // namespace fthr::encoded_replay_health
