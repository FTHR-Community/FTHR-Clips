#include "ring_buffer.h"

namespace fthr {

void EncodedRingBuffer::Push(EncodedPacket pkt) {
    std::lock_guard<std::mutex> lk(mutex_);
    packets_.push_back(std::move(pkt));

    // Prune packets older than max_duration_ms_
    while (packets_.size() > 1) {
        int64_t newest   = packets_.back().wall_time_ns;
        int64_t oldest   = packets_.front().wall_time_ns;
        int64_t span_ms  = (newest - oldest) / 1'000'000;
        if (span_ms <= static_cast<int64_t>(max_duration_ms_))
            break;
        packets_.pop_front();
    }
}

std::vector<EncodedPacket> EncodedRingBuffer::TakeSnapshot(uint32_t duration_ms) const {
    std::lock_guard<std::mutex> lk(mutex_);
    if (packets_.empty())
        return {};

    int64_t newest = packets_.back().wall_time_ns;
    int64_t cutoff = newest - static_cast<int64_t>(duration_ms) * 1'000'000LL;

    // Find last keyframe at or before cutoff — snapshot starts there so
    // the MP4 decoder has a clean IDR at the beginning of the clip.
    size_t start_idx = 0;
    for (size_t i = 0; i < packets_.size(); ++i) {
        if (packets_[i].wall_time_ns <= cutoff && packets_[i].is_keyframe)
            start_idx = i;
    }

    // If start_idx doesn't point to a keyframe (e.g. no keyframe exists
    // before cutoff right after engine start), scan forward to the first
    // keyframe so the clip never begins on a non-IDR frame. If none exists,
    // start_idx reaches packets_.size() and the snapshot is empty — better
    // than returning corrupted/green frames.
    while (start_idx < packets_.size() && !packets_[start_idx].is_keyframe)
        ++start_idx;

    return std::vector<EncodedPacket>(
        packets_.begin() + static_cast<ptrdiff_t>(start_idx),
        packets_.end()
    );
}

size_t EncodedRingBuffer::PacketCount() const {
    std::lock_guard<std::mutex> lk(mutex_);
    return packets_.size();
}

} // namespace fthr
