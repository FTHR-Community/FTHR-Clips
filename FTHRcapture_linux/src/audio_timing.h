#pragma once

#include <cstddef>
#include <cstdint>

namespace fthr {

int64_t AudioChunkStartNs(int64_t delivery_end_ns,
                          int64_t latency_us,
                          std::size_t frames,
                          uint32_t sample_rate);

} // namespace fthr
