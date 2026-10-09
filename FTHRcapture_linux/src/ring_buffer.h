#pragma once
#include <vector>
#include <deque>
#include <mutex>
#include <cstdint>
#include <cstddef>
#include <atomic>
#include <chrono>
#include <condition_variable>

namespace fthr {

struct EncodedPacket {
    std::vector<uint8_t> data;
    int64_t  pts;            // presentation timestamp (time_base units)
    int64_t  dts;
    bool     is_keyframe;
    int64_t  wall_time_ns;  // CLOCK_MONOTONIC nanoseconds when captured
};

struct EncodedRingSnapshot {
    std::vector<EncodedPacket> packets;
    int64_t presentation_start_pts = 0;
    int64_t presentation_start_ns = 0;
    int64_t presentation_end_ns = 0;
    int64_t media_end_ns = 0;
    bool full_history = false;
};

// Lock-based encoded ring buffer. Packets are pushed from the encode thread
// and snapshots taken from the save-clip thread.
class EncodedRingBuffer {
public:
    explicit EncodedRingBuffer(
        size_t max_duration_ms, uint32_t fps, size_t max_bytes = 0)
        : max_duration_ms_(max_duration_ms), fps_(fps), max_bytes_(max_bytes) {}

    void Push(EncodedPacket pkt);

    // Returns all packets that fall within the last duration_ms milliseconds.
    // Starts from the nearest keyframe before the window.
    EncodedRingSnapshot TakeSnapshot(
        uint32_t duration_ms, int64_t target_end_ns) const;
    bool WaitUntilPublished(
        int64_t target_end_ns, std::chrono::milliseconds timeout) const;

    size_t PacketCount() const;
    size_t TotalBytes() const;
    size_t MaxBytes() const;
    void SetMaxBytes(size_t max_bytes);
    void Clear();

private:
    mutable std::mutex        mutex_;
    std::deque<EncodedPacket> packets_;
    size_t                    max_duration_ms_;
    uint32_t                  fps_;
    size_t                    max_bytes_{0};
    size_t                    total_bytes_{0};
    std::atomic<int64_t>      latest_wall_time_ns_{0};
    mutable std::condition_variable publication_cv_;
};

} // namespace fthr
