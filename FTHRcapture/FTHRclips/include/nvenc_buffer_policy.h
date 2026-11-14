#pragma once

#include <cstdint>

namespace fthr {

// NVIDIA's Windows asynchronous API requires at least 4 + B-frame count
// input/output pairs. Lookahead needs additional input slots before it can
// produce output. Current replay configuration disables both features, so four
// pairs suffice; a replay's duration must never size this raw-frame pool.
constexpr uint32_t NvencAsyncBufferCount(
    uint32_t frame_interval_p, bool lookahead_enabled,
    uint32_t lookahead_depth) noexcept {
    const uint32_t b_frames = frame_interval_p > 1 ? frame_interval_p - 1 : 0;
    return 4 + b_frames + (lookahead_enabled ? lookahead_depth : 0);
}

} // namespace fthr
