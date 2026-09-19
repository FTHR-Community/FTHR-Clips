#include "audio_timing.h"

#include <cassert>
#include <cstdint>

int main() {
    constexpr int64_t second = 1'000'000'000LL;

    // A 10 ms block delivered at t=1.050 s from a stream reporting 40 ms
    // source-to-client latency belongs at t=1.000 s, not at read completion.
    const int64_t start = fthr::AudioChunkStartNs(
        1'050'000'000LL, 40'000, 480, 48'000);
    assert(start == second);

    // Negative/unknown latency must not move samples into the future.
    assert(fthr::AudioChunkStartNs(2'000'000'000LL, -1, 480, 48'000)
           == 1'990'000'000LL);

    return 0;
}
