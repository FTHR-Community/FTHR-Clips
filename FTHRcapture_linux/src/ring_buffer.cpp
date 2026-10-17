#include "ring_buffer.h"

#include "replay_interval.h"

#include <iostream>

namespace fthr {

void EncodedRingBuffer::PopFrontLocked() {
    bytes_ -= packets_.front().data.size();
    if (packets_.front().is_keyframe) --keyframes_;
    packets_.pop_front();
}

void EncodedRingBuffer::Push(EncodedPacket packet) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        bytes_ += packet.data.size();
        if (packet.is_keyframe) ++keyframes_;
        packets_.push_back(std::move(packet));
        while (packets_.size() > 1) {
            const int64_t span_ms =
                (packets_.back().wall_time_ns - packets_.front().wall_time_ns)
                / 1'000'000;
            if (span_ms <= static_cast<int64_t>(max_duration_ms_)) break;
            PopFrontLocked();
        }
        // Byte budget: drop the oldest GOP while a newer keyframe remains, so
        // the front stays decodable and the current GOP is never touched.
        // Leading packets without a keyframe (left by the time trim above)
        // go with the first GOP.
        bool trimmed = false;
        while (max_bytes_ != 0 && bytes_ > max_bytes_ &&
               keyframes_ > (packets_.front().is_keyframe ? 1u : 0u) &&
               packets_.size() > 1) {
            PopFrontLocked();
            while (!packets_.empty() && !packets_.front().is_keyframe)
                PopFrontLocked();
            trimmed = true;
        }
        if (trimmed) {
            ++budget_trims_;
            if (!budget_logged_) {
                budget_logged_ = true;
                const int64_t span_ms =
                    (packets_.back().wall_time_ns - packets_.front().wall_time_ns)
                    / 1'000'000;
                std::cerr << "[Ring] Byte budget of " << (max_bytes_ >> 20)
                          << " MiB reached; dropping the oldest video, "
                          << span_ms / 1000 << " s of replay history kept"
                          << std::endl;
            }
        }
        latest_wall_time_ns_.store(
            packets_.back().wall_time_ns, std::memory_order_release);
    }
    publication_cv_.notify_all();
}

EncodedRingSnapshot EncodedRingBuffer::TakeSnapshot(
    uint32_t duration_ms, int64_t target_end_ns, uint32_t generation) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (packets_.empty() || duration_ms == 0 || fps_ == 0) return {};

    std::vector<const EncodedPacket*> eligible;
    eligible.reserve(packets_.size());
    for (const auto& packet : packets_)
        if (generation == 0 || packet.generation == generation)
            eligible.push_back(&packet);
    if (eligible.empty()) return {};
    if (target_end_ns <= 0) target_end_ns = eligible.back()->wall_time_ns;

    std::vector<replay_interval::Sample> samples;
    samples.reserve(eligible.size());
    for (const auto* packet : eligible)
        samples.push_back({packet->wall_time_ns, packet->is_keyframe});
    const auto selection = replay_interval::Select(
        samples,
        target_end_ns,
        static_cast<int64_t>(duration_ms) * 1'000'000LL);
    if (!selection.valid) return {};

    EncodedRingSnapshot snapshot;
    snapshot.packets.reserve(selection.end + 1 - selection.decode_start);
    for (size_t i = selection.decode_start; i <= selection.end; ++i)
        snapshot.packets.push_back(*eligible[i]);
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

size_t EncodedRingBuffer::ByteCount() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return bytes_;
}

int64_t EncodedRingBuffer::SpanNs() const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (packets_.empty()) return 0;
    return packets_.back().wall_time_ns - packets_.front().wall_time_ns;
}

uint64_t EncodedRingBuffer::BudgetTrimCount() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return budget_trims_;
}

void EncodedRingBuffer::Clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    packets_.clear();
    bytes_ = 0;
    keyframes_ = 0;
    budget_trims_ = 0;
    budget_logged_ = false;
    latest_wall_time_ns_.store(0, std::memory_order_release);
}

} // namespace fthr
