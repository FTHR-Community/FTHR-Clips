#pragma once
#include <vector>
#include <deque>
#include <mutex>
#include <cstdint>
#include <cstddef>

namespace fthr {

struct EncodedPacket {
    std::vector<uint8_t> data;
    int64_t  pts;            // presentation timestamp (time_base units)
    int64_t  dts;
    bool     is_keyframe;
    int64_t  wall_time_ns;  // CLOCK_MONOTONIC nanoseconds when captured
};

// Lock-based encoded ring buffer. Packets are pushed from the encode thread
// and snapshots taken from the save-clip thread.
class EncodedRingBuffer {
public:
    explicit EncodedRingBuffer(size_t max_duration_ms)
        : max_duration_ms_(max_duration_ms) {}

    void Push(EncodedPacket pkt);

    // Returns all packets that fall within the last duration_ms milliseconds.
    // Starts from the nearest keyframe before the window.
    std::vector<EncodedPacket> TakeSnapshot(uint32_t duration_ms) const;

    size_t PacketCount() const;

private:
    mutable std::mutex        mutex_;
    std::deque<EncodedPacket> packets_;
    size_t                    max_duration_ms_;
};

} // namespace fthr
