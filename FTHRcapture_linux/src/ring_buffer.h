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
    // Encoder generation that produced the packet. Packets from different
    // generations carry different extradata and cannot share an MP4 track.
    uint32_t generation = 0;
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
    // max_bytes bounds the packet payload held, 0 = time-bounded only. Over
    // budget, whole GOPs are dropped from the front so the oldest retained
    // packet is always a keyframe; the newest GOP is never dropped.
    EncodedRingBuffer(size_t max_duration_ms, uint32_t fps, size_t max_bytes = 0)
        : max_duration_ms_(max_duration_ms), fps_(fps), max_bytes_(max_bytes) {}

    void Push(EncodedPacket pkt);

    // Returns all packets that fall within the last duration_ms milliseconds.
    // Starts from the nearest keyframe before the window. A non-zero
    // generation restricts the selection to packets of that encoder
    // generation; 0 accepts every packet.
    EncodedRingSnapshot TakeSnapshot(
        uint32_t duration_ms, int64_t target_end_ns,
        uint32_t generation = 0) const;
    bool WaitUntilPublished(
        int64_t target_end_ns, std::chrono::milliseconds timeout) const;

    size_t PacketCount() const;
    size_t ByteCount() const;
    // Wall-clock span held, newest minus oldest packet.
    int64_t SpanNs() const;
    // Times the byte budget dropped history since construction or Clear().
    uint64_t BudgetTrimCount() const;
    void Clear();

private:
    mutable std::mutex        mutex_;
    std::deque<EncodedPacket> packets_;
    size_t                    max_duration_ms_;
    uint32_t                  fps_;
    size_t                    max_bytes_;
    size_t                    bytes_ = 0;           // guarded by mutex_
    size_t                    keyframes_ = 0;       // guarded by mutex_
    uint64_t                  budget_trims_ = 0;    // guarded by mutex_
    bool                      budget_logged_ = false;  // guarded by mutex_

    void PopFrontLocked();
    std::atomic<int64_t>      latest_wall_time_ns_{0};
    mutable std::condition_variable publication_cv_;
};

} // namespace fthr
