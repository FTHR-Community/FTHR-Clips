#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace fthr {

// Convert the delivery time of an audio block to its source-time start.
// latency_us is the measured source-to-client latency; unknown/negative
// latency is treated as zero. The returned value is CLOCK_MONOTONIC ns.
int64_t AudioChunkStartNs(int64_t delivery_end_ns,
                          int64_t latency_us,
                          std::size_t frames,
                          uint32_t sample_rate);

// One captured block as seen by AssembleAudioWindow: interleaved samples
// and the CLOCK_MONOTONIC time of its first frame.
struct TimedAudioBlock {
    const float* samples = nullptr;
    std::size_t  frames = 0;
    int64_t      start_ns = 0;
};

// Gaps shorter than this are timestamp jitter or device clock drift and are
// joined seamlessly; longer ones (a reconnect, dropped buffers, audio that
// started after the video) are filled with silence to keep A/V sync.
inline constexpr int64_t kAudioGapFillThresholdNs = 50'000'000LL;

// Interleaved PCM for the window [end_ns - duration_ms, end_ns] from blocks
// in capture order. Blocks are laid back to back; a block starting more than
// kAudioGapFillThresholdNs after the audio written so far is preceded by
// silence, so later audio stays at its capture time instead of sliding
// earlier. The result is exactly the window length, or empty when no block
// overlaps the window.
std::vector<float> AssembleAudioWindow(const std::vector<TimedAudioBlock>& blocks,
                                       uint32_t channels,
                                       uint32_t sample_rate,
                                       int64_t end_ns,
                                       uint32_t duration_ms);

} // namespace fthr
