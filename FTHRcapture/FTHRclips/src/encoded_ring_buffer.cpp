// FTHR Capture Engine - encoded packet replay ring

#include "encoded_ring_buffer.h"

#include "replay_interval.h"

#include <cassert>
#include <algorithm>
#include <cstring>
#include <iostream>

namespace fthr {

EncodedRingBuffer::EncodedRingBuffer(
    size_t capacity, uint32_t fps, int64_t qpc_freq)
    : capacity_(capacity), fps_(fps), qpc_freq_(qpc_freq) {
    assert(capacity_ > 0);
    slots_.resize(capacity_);
    slot_states_ = std::make_unique<std::atomic<uint32_t>[]>(capacity_);
    slot_mutexes_ = std::make_unique<std::mutex[]>(capacity_);
    for (size_t i = 0; i < capacity_; ++i) {
        slot_states_[i].store(
            static_cast<uint32_t>(SlotState::EMPTY),
            std::memory_order_relaxed);
    }
    std::cout << "[EncodedRingBuffer] Initialized: "
              << capacity_ << " slots @ " << fps_ << " fps" << std::endl;
}

bool EncodedRingBuffer::Push(
    const uint8_t* encoded_data,
    uint32_t size,
    int64_t pts,
    bool is_keyframe,
    int64_t wall_qpc) {
    // Serialize publication against Clear(). Without this boundary a delayed
    // old-generation encoder callback could race the reset, acquire the new
    // generation token, and publish stale output into the fresh ring.
    std::lock_guard<std::mutex> publication_lock(publication_mutex_);
    const int64_t recovery_cutoff = recovery_cutoff_qpc_.load(
        std::memory_order_acquire);
    if (recovery_cutoff > 0 && wall_qpc <= recovery_cutoff) {
        return false;
    }
    const uint64_t write_pos = head_.load(std::memory_order_relaxed);
    const size_t slot_idx = static_cast<size_t>(write_pos % capacity_);
    std::lock_guard<std::mutex> slot_lock(slot_mutexes_[slot_idx]);
    const uint64_t generation = generation_.load(std::memory_order_acquire);

    slot_states_[slot_idx].store(
        static_cast<uint32_t>(SlotState::WRITING),
        std::memory_order_relaxed);
    auto& slot = slots_[slot_idx];
    slot.data.resize(size);
    if (size > 0 && encoded_data)
        std::memcpy(slot.data.data(), encoded_data, size);
    slot.pts = pts;
    slot.wall_qpc = wall_qpc;
    slot.generation = generation;
    slot.is_keyframe = is_keyframe;
    slot.valid = true;
    slot.absolute_position = write_pos;

    slot_states_[slot_idx].store(
        static_cast<uint32_t>(SlotState::READY),
        std::memory_order_release);
    head_.fetch_add(1, std::memory_order_release);
    const size_t previous = count_.load(std::memory_order_relaxed);
    if (previous < capacity_)
        count_.fetch_add(1, std::memory_order_relaxed);
    latest_wall_qpc_.store(wall_qpc, std::memory_order_release);
    publication_cv_.notify_all();
    return true;
}

bool EncodedRingBuffer::SetVideoConfig(const EncodedVideoConfig& config) {
    std::lock_guard<std::mutex> lock(video_config_mutex_);
    if (video_config_set_ && !(video_config_ == config)) {
        std::cerr << "[EncodedRingBuffer] Rejected stream config change within "
                     "one capture generation" << std::endl;
        return false;
    }
    video_config_ = config;
    video_config_set_ = true;
    video_config_cv_.notify_all();
    std::cout << "[EncodedRingBuffer] Video config set: "
              << VideoCodecName(config.codec) << ' '
              << config.width << 'x' << config.height << ", "
              << config.codec_extradata.size() << " config bytes" << std::endl;
    return true;
}

bool EncodedRingBuffer::HasVideoConfig() const {
    std::lock_guard<std::mutex> lock(video_config_mutex_);
    return video_config_set_;
}

bool EncodedRingBuffer::WaitForVideoConfig(
    EncodedVideoConfig& config, std::chrono::milliseconds timeout) const {
    std::unique_lock<std::mutex> lock(video_config_mutex_);
    if (!video_config_cv_.wait_for(lock, timeout, [this] {
            return video_config_set_ && IsValidEncodedVideoConfig(video_config_)
                && !video_config_.codec_extradata.empty();
        })) return false;
    config = video_config_;
    return true;
}

EncodedRingSnapshot EncodedRingBuffer::TakeSnapshotByTime(
    uint32_t duration_seconds, int64_t target_end_qpc) const {
    using replay_interval::Sample;
    if (duration_seconds == 0 || qpc_freq_ <= 0) return {};

    struct PacketRef {
        uint64_t position;
        Sample sample;
    };

    for (int attempt = 0; attempt < 3; ++attempt) {
        EncodedRingSnapshot snapshot;
        snapshot.selection_attempts = static_cast<uint32_t>(attempt + 1);
        snapshot.ring_capacity = capacity_;
        const uint64_t current_generation =
            generation_.load(std::memory_order_acquire);
        snapshot.capture_generation = current_generation;
        snapshot.generation_continuous = true;
        {
            std::lock_guard<std::mutex> lock(video_config_mutex_);
            snapshot.video_config = video_config_;
        }

        const uint64_t current_head = head_.load(std::memory_order_acquire);
        const size_t current_count = count_.load(std::memory_order_acquire);
        snapshot.ring_head = current_head;
        snapshot.ring_count = current_count;
        snapshot.snapshot_start_ring_head = current_head;
        snapshot.snapshot_start_ring_count = current_count;
        snapshot.snapshot_start_ring_generation = current_generation;
        snapshot.scanned_packet_count = current_count;
        const auto capture_after_state = [&snapshot, this]() {
            snapshot.snapshot_end_ring_head = head_.load(
                std::memory_order_acquire);
            snapshot.snapshot_end_ring_count = count_.load(
                std::memory_order_acquire);
            snapshot.snapshot_end_ring_generation = generation_.load(
                std::memory_order_acquire);
        };
        if (current_count == 0) {
            snapshot.selection_outcome = replay_save::SnapshotOutcome::EmptyRing;
            capture_after_state();
            return snapshot;
        }
        if (target_end_qpc <= 0) {
            target_end_qpc = latest_wall_qpc_.load(std::memory_order_acquire);
        }

        const uint64_t oldest = current_head - current_count;
        std::vector<PacketRef> refs;
        refs.reserve(current_count);
        for (uint64_t pos = oldest; pos < current_head; ++pos) {
            const size_t slot_idx = static_cast<size_t>(pos % capacity_);
            std::lock_guard<std::mutex> lock(slot_mutexes_[slot_idx]);
            const auto state = slot_states_[slot_idx].load(
                std::memory_order_acquire);
            const auto& packet = slots_[slot_idx];
            if (state != static_cast<uint32_t>(SlotState::READY)
                || !packet.valid
                || packet.absolute_position != pos
                || packet.generation != current_generation
                || packet.wall_qpc <= 0) {
                continue;
            }
            refs.push_back({pos, {packet.wall_qpc, packet.is_keyframe}});
        }

        snapshot.eligible_packet_count = refs.size();
        for (const auto& ref : refs) {
            if (ref.sample.keyframe) ++snapshot.keyframe_count;
            if (snapshot.oldest_wall_qpc == 0
                || ref.sample.timestamp < snapshot.oldest_wall_qpc) {
                snapshot.oldest_wall_qpc = ref.sample.timestamp;
            }
            snapshot.newest_wall_qpc = std::max(
                snapshot.newest_wall_qpc, ref.sample.timestamp);
        }
        if (refs.empty()) {
            snapshot.selection_outcome = replay_save::SnapshotOutcome::NoEligiblePackets;
            capture_after_state();
            return snapshot;
        }

        std::vector<Sample> samples;
        samples.reserve(refs.size());
        for (const auto& ref : refs) samples.push_back(ref.sample);
        const auto selection = replay_interval::Select(
            samples,
            target_end_qpc,
            static_cast<int64_t>(duration_seconds) * qpc_freq_);
        snapshot.requested_start_qpc = selection.requested_start;
        snapshot.target_end_qpc = selection.target_end;
        if (!selection.valid) {
            snapshot.selection_outcome = selection.outcome
                == replay_interval::Selection::Outcome::MissingStartKeyframe
                ? replay_save::SnapshotOutcome::MissingStartKeyframe
                : replay_save::SnapshotOutcome::NoEligiblePackets;
            capture_after_state();
            return snapshot;
        }
        snapshot.valid_starting_keyframe = selection.decode_start < samples.size()
            && samples[selection.decode_start].keyframe;

        snapshot.packets.reserve(
            selection.end - selection.decode_start + 1);
        bool stale = false;
        for (size_t i = selection.decode_start; i <= selection.end; ++i) {
            const uint64_t pos = refs[i].position;
            const size_t slot_idx = static_cast<size_t>(pos % capacity_);
            std::lock_guard<std::mutex> lock(slot_mutexes_[slot_idx]);
            const auto& source = slots_[slot_idx];
            if (!source.valid || source.absolute_position != pos
                || source.generation != current_generation) {
                stale = true;
                break;
            }
            snapshot.packets.push_back(source);
        }
        if (stale) {
            snapshot.internal_consistency = false;
            continue;
        }
        if (generation_.load(std::memory_order_acquire) != current_generation) {
            // Clear() may have invalidated the selected history while it was
            // being copied. Never return a snapshot that crosses that boundary.
            snapshot.internal_consistency = false;
            continue;
        }

        snapshot.full_history = selection.full_history;
        snapshot.selection_outcome = replay_save::SnapshotOutcome::Valid;
        capture_after_state();
        snapshot.interval_spanned_qpc = snapshot.packets.empty()
            ? 0
            : snapshot.packets.back().wall_qpc
                - snapshot.packets.front().wall_qpc;
        int64_t requested_duration_ticks = DurationInVideoTicks(
            snapshot.video_config, duration_seconds);
        if (requested_duration_ticks <= 0) {
            requested_duration_ticks = static_cast<int64_t>(fps_)
                * static_cast<int64_t>(duration_seconds);
        }
        snapshot.presentation_start_pts = replay_interval::PresentationStartPts(
            snapshot.packets.back().pts,
            requested_duration_ticks,
            snapshot.full_history,
            snapshot.packets.front().pts);
        snapshot.qpc_start_s = static_cast<double>(
            snapshot.packets.front().wall_qpc) / qpc_freq_;
        snapshot.qpc_end_s = static_cast<double>(
            snapshot.packets.back().wall_qpc) / qpc_freq_;
        snapshot.media_end_qpc_s = snapshot.qpc_end_s;
        snapshot.presentation_end_qpc_s =
            static_cast<double>(target_end_qpc) / qpc_freq_;
        snapshot.presentation_start_qpc_s = snapshot.full_history
            ? snapshot.presentation_end_qpc_s - duration_seconds
            : snapshot.qpc_start_s;

        std::cout << "[EncodedRingBuffer] TakeSnapshot: "
                  << snapshot.packets.size() << " packets, QPC "
                  << snapshot.qpc_start_s << "s - " << snapshot.qpc_end_s
                  << "s, "
                  << (snapshot.full_history ? "full history" : "partial history")
                  << std::endl;
        return snapshot;
    }

    std::cerr << "[EncodedRingBuffer] ring advanced during snapshot retries"
              << std::endl;
    EncodedRingSnapshot snapshot;
    snapshot.selection_outcome =
        replay_save::SnapshotOutcome::RetryExhaustedGenerationOrSlotRace;
    snapshot.selection_attempts = 3;
    snapshot.internal_consistency = false;
    snapshot.ring_capacity = capacity_;
    snapshot.ring_count = count_.load(std::memory_order_acquire);
    snapshot.ring_head = head_.load(std::memory_order_acquire);
    snapshot.capture_generation = generation_.load(std::memory_order_acquire);
    snapshot.snapshot_start_ring_count = snapshot.ring_count;
    snapshot.snapshot_start_ring_head = snapshot.ring_head;
    snapshot.snapshot_start_ring_generation = snapshot.capture_generation;
    snapshot.snapshot_end_ring_count = snapshot.ring_count;
    snapshot.snapshot_end_ring_head = snapshot.ring_head;
    snapshot.snapshot_end_ring_generation = snapshot.capture_generation;
    snapshot.scanned_packet_count = snapshot.ring_count;
    return snapshot;
}

bool EncodedRingBuffer::WaitUntilPublished(
    int64_t target_qpc, std::chrono::milliseconds timeout) const {
    if (target_qpc <= 0) return true;
    std::unique_lock<std::mutex> lock(publication_mutex_);
    return publication_cv_.wait_for(lock, timeout, [this, target_qpc] {
        return latest_wall_qpc_.load(std::memory_order_acquire) >= target_qpc;
    });
}

void EncodedRingBuffer::Clear(int64_t recovery_cutoff_qpc) {
    // Advance the token before invalidating slots. A concurrent save snapshot
    // will either observe the new generation or retry after it sees the token
    // change, so pre-recovery packets cannot be mixed into a post-recovery clip.
    std::lock_guard<std::mutex> publication_lock(publication_mutex_);
    if (recovery_cutoff_qpc > 0) {
        recovery_cutoff_qpc_.store(recovery_cutoff_qpc,
            std::memory_order_release);
    }
    generation_.fetch_add(1, std::memory_order_acq_rel);
    for (size_t i = 0; i < capacity_; ++i) {
        std::lock_guard<std::mutex> lock(slot_mutexes_[i]);
        slots_[i].valid = false;
        slots_[i].generation = generation_.load(std::memory_order_relaxed);
        slots_[i].absolute_position = std::numeric_limits<uint64_t>::max();
        slot_states_[i].store(
            static_cast<uint32_t>(SlotState::EMPTY),
            std::memory_order_release);
    }
    count_.store(0, std::memory_order_release);
    head_.store(0, std::memory_order_release);
    latest_wall_qpc_.store(0, std::memory_order_release);
}

} // namespace fthr
