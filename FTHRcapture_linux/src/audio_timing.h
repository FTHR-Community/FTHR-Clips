#pragma once

#include <cstddef>
#include <cstdint>

namespace fthr {

// Convert the delivery time of an audio block to its source-time start.
// latency_us is the measured source-to-client latency; unknown/negative
// latency is treated as zero. The returned value is CLOCK_MONOTONIC ns.
int64_t AudioChunkStartNs(int64_t delivery_end_ns,
                          int64_t latency_us,
                          std::size_t frames,
                          uint32_t sample_rate);

} // namespace fthr
