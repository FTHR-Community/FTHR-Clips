// FTHR Capture Engine - encoded packet replay ring

#include "encoded_ring_buffer.h"
#include "replay_storage.h"

#include "replay_interval.h"

#include <cassert>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <new>
#include <numeric>

namespace fthr {

namespace {

bool CheckedQpcDelta(
    int64_t qpc_frequency, int64_t time_base_numerator,
    int64_t time_base_denominator, int64_t pts_delta,
    int64_t& out) noexcept {
    if (qpc_frequency <= 0 || time_base_numerator <= 0
        || time_base_denominator <= 0 || pts_delta < 0) return false;
    uint64_t numerator = static_cast<uint64_t>(qpc_frequency);
    uint64_t factor = static_cast<uint64_t>(time_base_numerator);
    uint64_t ticks = static_cast<uint64_t>(pts_delta);
    uint64_t denominator = static_cast<uint64_t>(time_base_denominator);
    // Cancel denominator factors before multiplying so malformed stream
    // configurations cannot wrap the intermediate product.
    for (uint64_t* factor_to_reduce : {&numerator, &factor, &ticks}) {
        const uint64_t common = std::gcd(*factor_to_reduce, denominator);
        *factor_to_reduce /= common;
        denominator /= common;
    }
    if (factor != 0 && numerator > std::numeric_limits<uint64_t>::max() / factor)
        return false;
    numerator *= factor;
    if (ticks != 0 && numerator > std::numeric_limits<uint64_t>::max() / ticks)
        return false;
    numerator *= ticks;
    uint64_t quotient = numerator / denominator;
    const uint64_t remainder = numerator % denominator;
    const bool rounds_up = remainder > denominator / 2
        || (denominator % 2 == 0 && remainder == denominator / 2);
    if (rounds_up) {
        if (quotient == std::numeric_limits<uint64_t>::max()) return false;
        ++quotient;
    }
    if (quotient > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()))
        return false;
    out = static_cast<int64_t>(quotient);
    return true;
}

int64_t CheckedDurationInVideoTicks(
    const EncodedVideoConfig& config, uint32_t seconds) noexcept {
    if (config.time_base.numerator <= 0
        || config.time_base.denominator <= 0) return 0;
    const uint64_t denominator = static_cast<uint64_t>(
        config.time_base.denominator);
    const uint64_t seconds_u = static_cast<uint64_t>(seconds);
    if (seconds_u > static_cast<uint64_t>(
            std::numeric_limits<int64_t>::max()) / denominator) return 0;
    const uint64_t numerator = seconds_u * denominator;
    const uint64_t result = numerator / static_cast<uint64_t>(
        config.time_base.numerator);
    return result > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
        ? 0 : static_cast<int64_t>(result);
}

// Convert exactly one configured video PTS tick to the capture QPC domain.
// This is used only for the exclusive end boundary; requested target_end_qpc
// remains the independent stale-end/health guard.
int64_t VideoPtsTickQpc(int64_t qpc_frequency,
                        const EncodedVideoConfig& config) noexcept {
    int64_t ticks = 0;
    if (!CheckedQpcDelta(
            qpc_frequency, config.time_base.numerator,
            config.time_base.denominator, 1, ticks)) return 0;
    return ticks > 0 ? ticks : 1;
}

}  // namespace

size_t EncodedRingPacket::byte_size() const {
    return storage ? storage->size() : data.size();
}

bool EncodedRingPacket::ReadInto(uint8_t* output) const {
    if (storage) return storage->ReadInto(output, storage->size());
    if (!data.empty()) std::memcpy(output, data.data(), data.size());
    return true;
}

EncodedRingBuffer::EncodedRingBuffer(
    size_t capacity, uint32_t fps, int64_t qpc_freq,
    std::shared_ptr<ReplayMemoryBudget> memory_budget,
    std::shared_ptr<ReplayStorage> storage)
    : capacity_(capacity), fps_(fps), qpc_freq_(qpc_freq),
      memory_budget_(memory_budget ? std::move(memory_budget)
          : std::make_shared<ReplayMemoryBudget>(2ULL * 1024 * 1024 * 1024,
                                                 1024ULL * 1024 * 1024)),
      ring_memory_(memory_budget_, ReplayMemoryUse::Ring),
      storage_(std::move(storage)) {
    assert(capacity_ > 0);
    const uint64_t metadata = static_cast<uint64_t>(capacity_)
        * (sizeof(EncodedRingPacket) + sizeof(std::atomic<uint32_t>) + sizeof(std::mutex));
    if (!ring_memory_.Grow(metadata)) throw std::bad_alloc();
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
    int64_t wall_qpc,
    PublishedVideoPoint* published) {
    // Serialize publication against Clear(). Without this boundary a delayed
    // old-generation encoder callback could race the reset, acquire the new
    // generation token, and publish stale output into the fresh ring.
    std::lock_guard<std::mutex> publication_lock(publication_mutex_);
    if (memory_failed_.load()) return false;
    const int64_t recovery_cutoff = recovery_cutoff_qpc_.load(
        std::memory_order_acquire);
    if (recovery_cutoff > 0 && wall_qpc <= recovery_cutoff) {
        return false;
    }
    const uint64_t write_pos = head_.load(std::memory_order_relaxed);
    const size_t slot_idx = static_cast<size_t>(write_pos % capacity_);
    std::lock_guard<std::mutex> slot_lock(slot_mutexes_[slot_idx]);
    const uint64_t generation = generation_.load(std::memory_order_acquire);

    auto& slot = slots_[slot_idx];
    if (storage_) {
        auto payload = storage_->Push(encoded_data, size);
        if (!payload) return false;
        slot.storage = std::move(payload);
    } else {
        constexpr uint64_t kUnusedCapacityCacheDivisor = 8;
        // Keep a best-effort unused-capacity cache of at most 1/8 of the ring
        // quota across slots. Failed optional compaction retains existing storage.
        const uint64_t unused_capacity_allowance = memory_budget_->ring_limit()
            / capacity_ / kUnusedCapacityCacheDivisor;
        const size_t old_capacity = slot.data.capacity();
        const bool requires_growth = size > old_capacity;
        const bool reclaim_unused_capacity = old_capacity > size
            && old_capacity - size > unused_capacity_allowance;
        // Reserve before allocating. Charge both old and new storage during growth,
        // and keep the old published packet intact if admission/allocation fails.
        if (requires_growth || reclaim_unused_capacity) {
            const bool replaced = [&]() {
                if (!ring_memory_.Grow(size)) return false;
                try {
                    std::vector<uint8_t> replacement(size);
                    // The supported standard libraries allocate exactly size here.
                    // Account any extra capacity before publishing the new slot.
                    const size_t extra = replacement.capacity() - size;
                    if (extra && !ring_memory_.Grow(extra)) {
                        std::vector<uint8_t>().swap(replacement);
                        ring_memory_.Shrink(size);
                        return false;
                    }
                    slot.data.swap(replacement);
                    std::vector<uint8_t>().swap(replacement);
                    ring_memory_.Shrink(old_capacity);
                    return true;
                } catch (const std::bad_alloc&) {
                    ring_memory_.Shrink(size);
                    return false;
                }
            }();
            if (!replaced && requires_growth) {
                memory_failed_.store(true);
                return false;
            }
        }
        slot_states_[slot_idx].store(
            static_cast<uint32_t>(SlotState::WRITING),
            std::memory_order_relaxed);
        slot.data.resize(size);
        if (size > 0 && encoded_data)
            std::memcpy(slot.data.data(), encoded_data, size);
    }
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
    if (published) {
        published->pts = pts;
        published->generation = generation;
    }
    publication_cv_.notify_all();
    return true;
}

bool EncodedRingBuffer::MarkHighlight(
    const std::string& marker_id, int64_t out_point_qpc,
    CapturedHighlightMark* captured) {
    if (!IsCanonicalHighlightUuid(marker_id)) return false;
    std::lock_guard<std::mutex> publication_lock(publication_mutex_);
    const uint64_t current_generation = generation_.load(std::memory_order_acquire);
    const uint64_t current_head = head_.load(std::memory_order_acquire);
    const size_t current_count = count_.load(std::memory_order_acquire);
    if (current_head == 0 || current_count == 0) return false;
    const uint64_t oldest = current_head - current_count;
    // Marker quota covers the currently retained media window. Evicted ring
    // positions must not consume the live bound for the rest of the run.
    highlight_marks_.erase(std::remove_if(
        highlight_marks_.begin(), highlight_marks_.end(),
        [oldest, current_generation](const CapturedHighlightMark& mark) {
            return mark.generation != current_generation
                || mark.absolute_position < oldest;
        }), highlight_marks_.end());
    const int64_t requested_qpc = out_point_qpc > 0
        ? out_point_qpc
        : latest_wall_qpc_.load(std::memory_order_acquire);
    for (uint64_t pos = current_head; pos > oldest; --pos) {
        const uint64_t absolute_position = pos - 1;
        const size_t slot_idx = static_cast<size_t>(absolute_position % capacity_);
        std::lock_guard<std::mutex> slot_lock(slot_mutexes_[slot_idx]);
        const auto state = slot_states_[slot_idx].load(std::memory_order_acquire);
        const auto& packet = slots_[slot_idx];
        if (state != static_cast<uint32_t>(SlotState::READY)
            || !packet.valid || packet.absolute_position != absolute_position
            || packet.generation != current_generation || packet.wall_qpc <= 0
            || packet.wall_qpc > requested_qpc) continue;
        CapturedHighlightMark mark;
        mark.marker_id = marker_id;
        mark.pts = packet.pts;
        mark.wall_time = packet.wall_qpc;
        mark.absolute_position = absolute_position;
        mark.generation = current_generation;
        for (auto& existing : highlight_marks_) {
            if (existing.marker_id == marker_id) {
                existing = std::move(mark);
                if (captured) *captured = existing;
                return true;
            }
        }
        if (highlight_marks_.size() >= kMaxHighlightMarkers) {
            std::cerr << "[EncodedRingBuffer] Highlight marker limit reached"
                      << std::endl;
            return false;
        }
        highlight_marks_.push_back(std::move(mark));
        if (captured) *captured = highlight_marks_.back();
        return true;
    }
    return false;
}

bool EncodedRingBuffer::SetVideoConfig(const EncodedVideoConfig& config) {
    std::lock_guard<std::mutex> publication_lock(publication_mutex_);
    std::lock_guard<std::mutex> lock(video_config_mutex_);
    if (video_config_set_ && !(video_config_ == config)) {
        std::cerr << "[EncodedRingBuffer] Rejected stream config change within "
                     "one capture generation" << std::endl;
        return false;
    }
    if (video_config_set_) return true;
    if (!ring_memory_.Grow(config.codec_extradata.size())) {
        memory_failed_.store(true);
        return false;
    }
    try {
        video_config_ = config;
    } catch (const std::bad_alloc&) {
        ring_memory_.Shrink(config.codec_extradata.size());
        memory_failed_.store(true);
        return false;
    }
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
    try {
        return TakeSnapshotByTimeImpl(duration_seconds, target_end_qpc);
    } catch (const std::bad_alloc&) {
        EncodedRingSnapshot failed;
        failed.selection_outcome = replay_save::SnapshotOutcome::MemoryBudgetExceeded;
        return failed;
    }
}

EncodedRingSnapshot EncodedRingBuffer::TakeSnapshotByTimeImpl(
    uint32_t duration_seconds, int64_t target_end_qpc) const {
    using replay_interval::Sample;
    if (duration_seconds == 0 || qpc_freq_ <= 0
        || static_cast<uint64_t>(duration_seconds)
            > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
                / static_cast<uint64_t>(qpc_freq_)) return {};

    struct PacketRef {
        uint64_t position;
        Sample sample;
        size_t bytes;
    };

    for (int attempt = 0; attempt < 3; ++attempt) {
        EncodedRingSnapshot snapshot;
        snapshot.memory = ReplayMemoryReservation(memory_budget_, ReplayMemoryUse::Save);
        snapshot.selection_attempts = static_cast<uint32_t>(attempt + 1);
        snapshot.ring_capacity = capacity_;
        const uint64_t current_generation =
            generation_.load(std::memory_order_acquire);
        snapshot.capture_generation = current_generation;
        snapshot.generation_continuous = true;
        {
            std::lock_guard<std::mutex> lock(video_config_mutex_);
            if (!snapshot.memory.Grow(video_config_.codec_extradata.size()))
                throw std::bad_alloc();
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
        const int64_t requested_duration_qpc =
            static_cast<int64_t>(duration_seconds) * qpc_freq_;
        int64_t requested_start_qpc = 0;
        if (!CheckedSignedSubtract(
                target_end_qpc, requested_duration_qpc, requested_start_qpc)) {
            snapshot.selection_outcome = replay_save::SnapshotOutcome::NoEligiblePackets;
            capture_after_state();
            return snapshot;
        }

        const uint64_t oldest = current_head - current_count;
        // Bound selection scratch allocations as well as packet payload copies.
        ReplayMemoryReservation scratch(memory_budget_, ReplayMemoryUse::Save);
        if (!scratch.Grow(static_cast<uint64_t>(current_count)
                * (sizeof(PacketRef) + sizeof(Sample)))) throw std::bad_alloc();
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
            refs.push_back({pos, {packet.wall_qpc, packet.is_keyframe}, packet.data.size()});
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
            requested_duration_qpc);
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

        uint64_t snapshot_bytes = static_cast<uint64_t>(
            selection.end - selection.decode_start + 1) * sizeof(EncodedRingPacket);
        for (size_t i = selection.decode_start; i <= selection.end; ++i) {
            if (refs[i].bytes > std::numeric_limits<uint64_t>::max() - snapshot_bytes)
                throw std::bad_alloc();
            snapshot_bytes += refs[i].bytes;
        }
        // One admission for the complete copy. A rejected second/third save
        // never starts copying gigabytes before discovering the limit.
        if (!snapshot.memory.Grow(snapshot_bytes)) throw std::bad_alloc();
        snapshot.packets.reserve(selection.end - selection.decode_start + 1);
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
        if (snapshot.packets.empty()) {
            snapshot.interval_spanned_qpc = 0;
        } else if (!CheckedSignedSubtract(
                       snapshot.packets.back().wall_qpc,
                       snapshot.packets.front().wall_qpc,
                       snapshot.interval_spanned_qpc)) {
            snapshot.internal_consistency = false;
            continue;
        }
        int64_t requested_duration_ticks = CheckedDurationInVideoTicks(
            snapshot.video_config, duration_seconds);
        if (requested_duration_ticks <= 0) {
            if (fps_ == 0
                || static_cast<uint64_t>(duration_seconds)
                    > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
                        / static_cast<uint64_t>(fps_)) {
                snapshot.internal_consistency = false;
                continue;
            }
            requested_duration_ticks = static_cast<int64_t>(fps_)
                * static_cast<int64_t>(duration_seconds);
        }
        if (snapshot.full_history) {
            if (snapshot.packets.back().pts == std::numeric_limits<int64_t>::max()) {
                snapshot.internal_consistency = false;
                continue;
            }
            if (!CheckedSignedSubtract(
                    snapshot.packets.back().pts + 1,
                    requested_duration_ticks,
                    snapshot.presentation_start_pts)) {
                snapshot.internal_consistency = false;
                continue;
            }
        } else {
            snapshot.presentation_start_pts = snapshot.packets.front().pts;
        }
        snapshot.qpc_start_s = static_cast<double>(
            snapshot.packets.front().wall_qpc) / qpc_freq_;
        snapshot.qpc_end_s = static_cast<double>(
            snapshot.packets.back().wall_qpc) / qpc_freq_;
        snapshot.media_end_qpc_s = snapshot.qpc_end_s;

        // Map the audio interval to the same actual video PTS range that the
        // muxer rebases with presentation_start_pts.  target_end_qpc remains
        // the requested save boundary and is still used by replay-health's
        // stale-end predicate; it must never be treated as a captured frame.
        snapshot.presentation_start_qpc_s = snapshot.qpc_start_s;
        for (const auto& packet : snapshot.packets) {
            if (packet.pts >= snapshot.presentation_start_pts) {
                int64_t pts_delta = 0;
                int64_t qpc_delta = 0;
                int64_t mapped_start = 0;
                if (CheckedNonNegativeDelta(
                        packet.pts, snapshot.presentation_start_pts, pts_delta)
                    && CheckedQpcDelta(
                        qpc_freq_,
                        snapshot.video_config.time_base.numerator,
                        snapshot.video_config.time_base.denominator,
                        pts_delta, qpc_delta)
                    && CheckedSignedSubtract(
                        packet.wall_qpc, qpc_delta, mapped_start)) {
                    snapshot.presentation_start_qpc_s =
                        static_cast<double>(mapped_start) / qpc_freq_;
                }
                break;
            }
        }
        const int64_t one_tick_qpc = VideoPtsTickQpc(
            qpc_freq_, snapshot.video_config);
        const int64_t last_wall_qpc = snapshot.packets.back().wall_qpc;
        const bool end_tick_fits = one_tick_qpc > 0
            && last_wall_qpc <= std::numeric_limits<int64_t>::max()
                - one_tick_qpc;
        snapshot.presentation_end_qpc_s = end_tick_fits
            ? static_cast<double>(last_wall_qpc + one_tick_qpc) / qpc_freq_
            : snapshot.qpc_end_s;

        // Copy and map marks while the publication boundary is held.  A mark
        // is eligible only in the retained presentation interval; decoder
        // pre-roll before presentation_start_pts must never become a visible
        // marker position.
        {
            std::lock_guard<std::mutex> publication_lock(publication_mutex_);
            if (generation_.load(std::memory_order_acquire)
                    != current_generation) {
                snapshot.internal_consistency = false;
                continue;
            }
            const int64_t last_pts = snapshot.packets.back().pts;
            snapshot.capture_timeline = MakeCaptureTimeline(
                capture_generation_id_, snapshot.presentation_start_pts, last_pts,
                snapshot.video_config.time_base.numerator,
                snapshot.video_config.time_base.denominator);
            const uint64_t first_position = snapshot.packets.front().absolute_position;
            const uint64_t last_position = snapshot.packets.back().absolute_position;
            for (const auto& mark : highlight_marks_) {
                if (mark.generation != current_generation
                    || mark.pts < snapshot.presentation_start_pts
                    || mark.pts > last_pts
                    || mark.absolute_position < first_position
                    || mark.absolute_position > last_position) continue;
                int64_t pts_delta = 0;
                int64_t position_100ns = 0;
                if (!CheckedNonNegativeDelta(
                        mark.pts, snapshot.presentation_start_pts, pts_delta)) {
                    continue;
                }
                if (!CheckedPtsTo100ns(
                        pts_delta,
                        snapshot.video_config.time_base.numerator,
                        snapshot.video_config.time_base.denominator,
                        position_100ns)) continue;
                snapshot.highlights.push_back({mark.marker_id, position_100ns, {}});
            }
        }

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
    capture_generation_id_ = NewCaptureGenerationId();
    const uint64_t previous_generation =
        generation_.load(std::memory_order_relaxed);
    generation_.store(
        previous_generation == std::numeric_limits<uint64_t>::max()
            ? 1 : previous_generation + 1,
        std::memory_order_release);
    for (size_t i = 0; i < capacity_; ++i) {
        std::lock_guard<std::mutex> lock(slot_mutexes_[i]);
        slots_[i].valid = false;
        slots_[i].generation = generation_.load(std::memory_order_relaxed);
        slots_[i].absolute_position = std::numeric_limits<uint64_t>::max();
        slots_[i].storage.reset();
        const size_t released = slots_[i].data.capacity();
        std::vector<uint8_t>().swap(slots_[i].data);
        ring_memory_.Shrink(released);
        slot_states_[i].store(
            static_cast<uint32_t>(SlotState::EMPTY),
            std::memory_order_release);
    }
    count_.store(0, std::memory_order_release);
    head_.store(0, std::memory_order_release);
    latest_wall_qpc_.store(0, std::memory_order_release);
    highlight_marks_.clear();
    memory_failed_.store(false);
}

} // namespace fthr
