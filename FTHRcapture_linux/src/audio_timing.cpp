#include "audio_timing.h"

#include <algorithm>

namespace fthr {

int64_t AudioChunkStartNs(int64_t delivery_end_ns,
                          int64_t latency_us,
                          std::size_t frames,
                          uint32_t sample_rate) {
    if (sample_rate == 0) return delivery_end_ns;
    const int64_t latency_ns = std::max<int64_t>(0, latency_us) * 1'000LL;
    const int64_t block_ns = static_cast<int64_t>(
        (static_cast<uint64_t>(frames) * 1'000'000'000ULL)
        / sample_rate);
    return delivery_end_ns - latency_ns - block_ns;
}

std::vector<float> AssembleAudioWindow(const std::vector<TimedAudioBlock>& blocks,
                                       uint32_t channels,
                                       uint32_t sample_rate,
                                       int64_t end_ns,
                                       uint32_t duration_ms) {
    if (channels == 0 || sample_rate == 0 || duration_ms == 0) return {};
    const int64_t total_frames =
        static_cast<int64_t>(duration_ms) * sample_rate / 1000;
    const int64_t start_ns = end_ns - static_cast<int64_t>(duration_ms) * 1'000'000LL;
    const int64_t gap_frames = kAudioGapFillThresholdNs * sample_rate / 1'000'000'000LL;
    auto ns_to_frames = [sample_rate](int64_t ns) -> int64_t {
        return ns * static_cast<int64_t>(sample_rate) / 1'000'000'000LL;
    };

    std::vector<float> out;
    out.reserve(static_cast<std::size_t>(total_frames) * channels);
    int64_t cursor = 0;   // frames written so far
    bool any = false;
    for (const TimedAudioBlock& block : blocks) {
        if (!block.samples || block.frames == 0) continue;
        const int64_t frames = static_cast<int64_t>(block.frames);
        const int64_t block_end_ns = block.start_ns + frames * 1'000'000'000LL / sample_rate;
        if (block_end_ns <= start_ns) continue;
        if (block.start_ns >= end_ns || cursor >= total_frames) break;

        int64_t skip = 0;
        int64_t ideal = 0;
        if (block.start_ns < start_ns)
            skip = std::min(frames, ns_to_frames(start_ns - block.start_ns));
        else
            ideal = std::min(total_frames, ns_to_frames(block.start_ns - start_ns));

        if (ideal - cursor > gap_frames) {
            out.resize(static_cast<std::size_t>(ideal) * channels, 0.0f);
            cursor = ideal;
        }
        const int64_t take = std::min(frames - skip, total_frames - cursor);
        if (take <= 0) continue;
        const float* first = block.samples + skip * channels;
        out.insert(out.end(), first, first + take * channels);
        cursor += take;
        any = true;
    }
    if (!any) return {};
    out.resize(static_cast<std::size_t>(total_frames) * channels, 0.0f);
    return out;
}

} // namespace fthr
