#include "ring_buffer.h"

#include "replay_interval.h"

namespace fthr {

void EncodedRingBuffer::Push(EncodedPacket packet) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        total_bytes_ += packet.data.size();
        packets_.push_back(std::move(packet));
        while (packets_.size() > 1) {
            const int64_t span_ms =
                (packets_.back().wall_time_ns - packets_.front().wall_time_ns)
                / 1'000'000;
            const bool duration_exceeded =
                span_ms > static_cast<int64_t>(max_duration_ms_);
            const bool memory_exceeded =
                (max_bytes_ > 0 && total_bytes_ > max_bytes_);
            if (!duration_exceeded && !memory_exceeded) break;

            total_bytes_ -= packets_.front().data.size();
            packets_.pop_front();
        }
        latest_wall_time_ns_.store(
            packets_.back().wall_time_ns, std::memory_order_release);
    }
    publication_cv_.notify_all();
}

EncodedRingSnapshot EncodedRingBuffer::TakeSnapshot(
    uint32_t duration_ms, int64_t target_end_ns) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (packets_.empty() || duration_ms == 0 || fps_ == 0) return {};
    if (target_end_ns <= 0) target_end_ns = packets_.back().wall_time_ns;

    std::vector<replay_interval::Sample> samples;
    samples.reserve(packets_.size());
    for (const auto& packet : packets_)
        samples.push_back({packet.wall_time_ns, packet.is_keyframe});
    const auto selection = replay_interval::Select(
        samples,
        target_end_ns,
        static_cast<int64_t>(duration_ms) * 1'000'000LL);
    if (!selection.valid) return {};

    EncodedRingSnapshot snapshot;
    snapshot.packets.assign(
        packets_.begin() + static_cast<ptrdiff_t>(selection.decode_start),
        packets_.begin() + static_cast<ptrdiff_t>(selection.end + 1));
    snapshot.full_history = selection.full_history;
    snapshot.presentation_start_pts = replay_interval::PresentationStartPts(
        snapshot.packets.back().pts,
        static_cast<int64_t>(duration_ms) * fps_ / 1000,
        snapshot.full_history,
        snapshot.packets.front().pts);
    snapshot.presentation_end_ns = target_end_ns;
    snapshot.presentation_start_ns = snapshot.full_history
        ? target_end_ns - static_cast<int64_t>(duration_ms) * 1'000'000LL
        : snapshot.packets.front().wall_time_ns;
    snapshot.media_end_ns = snapshot.packets.back().wall_time_ns;
    return snapshot;
}

bool EncodedRingBuffer::WaitUntilPublished(
    int64_t target_end_ns, std::chrono::milliseconds timeout) const {
    if (target_end_ns <= 0) return true;
    std::unique_lock<std::mutex> lock(mutex_);
    return publication_cv_.wait_for(lock, timeout, [this, target_end_ns] {
        return latest_wall_time_ns_.load(std::memory_order_acquire)
            >= target_end_ns;
    });
}

size_t EncodedRingBuffer::PacketCount() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return packets_.size();
}

void EncodedRingBuffer::Clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    packets_.clear();
    total_bytes_ = 0;
    latest_wall_time_ns_.store(0, std::memory_order_release);
}

size_t EncodedRingBuffer::TotalBytes() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return total_bytes_;
}

size_t EncodedRingBuffer::MaxBytes() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return max_bytes_;
}

void EncodedRingBuffer::SetMaxBytes(size_t max_bytes) {
    std::lock_guard<std::mutex> lock(mutex_);
    max_bytes_ = max_bytes;
    while (packets_.size() > 1 && max_bytes_ > 0 && total_bytes_ > max_bytes_) {
        total_bytes_ -= packets_.front().data.size();
        packets_.pop_front();
    }
}

} // namespace fthr
