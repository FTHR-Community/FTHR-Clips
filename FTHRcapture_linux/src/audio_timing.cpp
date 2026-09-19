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

} // namespace fthr
