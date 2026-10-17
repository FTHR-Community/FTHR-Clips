// Compressed replay ring with codec configuration copied into each snapshot.
// Push has one producer; snapshots take per-slot locks while copying so a
// wrapped slot cannot be overwritten mid-copy. Select by capture timestamp
// and retain keyframe pre-roll. Packet format conversion belongs to the encoder.

#pragma once
#ifndef FTHR_ENCODED_RING_BUFFER_H
#define FTHR_ENCODED_RING_BUFFER_H

#include <cstdint>
#include <cstddef>
#include <memory>
#include <vector>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <limits>
#include <mutex>

#include "encoded_video_config.h"
#include "replay_save_diagnostics.h"


namespace fthr {

    constexpr uint32_t kEncodedReplayKeyframePrerollSeconds = 4;
    constexpr uint32_t kEncodedReplaySafetySeconds = 1;

    inline size_t CalculateEncodedReplaySlotCapacity(
        uint32_t buffer_seconds, uint32_t fps) {
        if (buffer_seconds == 0 || fps == 0) return 0;
        const uint64_t seconds = static_cast<uint64_t>(buffer_seconds)
            + kEncodedReplayKeyframePrerollSeconds
            + kEncodedReplaySafetySeconds;
        const uint64_t slots = seconds * static_cast<uint64_t>(fps);
        return slots > std::numeric_limits<size_t>::max()
            ? std::numeric_limits<size_t>::max()
            : static_cast<size_t>(slots);
    }


    // Slot publication states: unused, being written, or ready for a snapshot.
    enum class SlotState : uint32_t {
        EMPTY   = 0,
        WRITING = 1,
        READY   = 2,
    };


    // One compressed frame in video_config.packet_format.
    struct EncodedRingPacket {
        std::vector<uint8_t> data;         // Encoded sample in configured packet format
        int64_t              pts = 0;
        int64_t              wall_qpc = 0; // Raw QPC ticks at capture time (same clock as WASAPI)
        uint64_t             generation = 0; // Internal capture generation; not shared-memory ABI
        bool                 is_keyframe = false;
        bool                 valid = false; // False on uninitialized slots
        uint64_t             absolute_position =
            std::numeric_limits<uint64_t>::max();
    };


    // EncodedRingSnapshot
    //
    // Returned by TakeSnapshot(). Caller owns all data - safe to use while
    // CaptureThread continues encoding into the ring buffer.
    struct EncodedRingSnapshot {
        std::vector<EncodedRingPacket> packets;   // Ordered oldest -> newest
        EncodedVideoConfig video_config;
        uint64_t capture_generation = 0;
        bool generation_continuous = true;

        // Wall-clock QPC range of packets in this snapshot (seconds).
        // Derived from first/last packet wall_qpc, converted using qpc_freq.
        // Same clock domain as AudioPCMSnapshot::qpc_start_s / qpc_end_s.
        double qpc_start_s = 0.0;
        double qpc_end_s   = 0.0;
        int64_t presentation_start_pts = 0;
        // Actual video presentation interval corresponding to
        // presentation_start_pts through last_pts + one PTS tick. This is
        // deliberately independent from target_end_qpc: that requested
        // boundary remains the stale-end/health guard and is not a captured
        // frame when the newest packet is older than the request.
        double presentation_start_qpc_s = 0.0;
        double presentation_end_qpc_s = 0.0;
        double media_end_qpc_s = 0.0;
        bool full_history = false;

        // Bounded, save-time selection evidence.  These fields are internal
        // diagnostics only and never cross the shared-memory ABI.
        replay_save::SnapshotOutcome selection_outcome =
            replay_save::SnapshotOutcome::Unknown;
        uint32_t selection_attempts = 0;
        bool internal_consistency = true;
        size_t scanned_packet_count = 0;
        size_t eligible_packet_count = 0;
        size_t keyframe_count = 0;
        size_t ring_count = 0;
        size_t ring_capacity = 0;
        uint64_t ring_head = 0;
        uint64_t snapshot_start_ring_head = 0;
        size_t snapshot_start_ring_count = 0;
        uint64_t snapshot_start_ring_generation = 0;
        uint64_t snapshot_end_ring_head = 0;
        size_t snapshot_end_ring_count = 0;
        uint64_t snapshot_end_ring_generation = 0;
        int64_t oldest_wall_qpc = 0;
        int64_t newest_wall_qpc = 0;
        int64_t requested_start_qpc = 0;
        int64_t target_end_qpc = 0;
        int64_t interval_spanned_qpc = 0;
        bool valid_starting_keyframe = false;
    };


    class EncodedRingBuffer {
    public:
        // capacity:  number of packet slots to pre-allocate.
        // fps:       capture framerate / encoded packet timebase.
        // qpc_freq:  QueryPerformanceFrequency value, used to convert wall_qpc to seconds.
        // CalculateEncodedReplaySlotCapacity() retains the requested history,
        // one maximum four-second GOP of pre-roll, and one second of safety.
        explicit EncodedRingBuffer(size_t capacity, uint32_t fps, int64_t qpc_freq = 0);

        // Not copyable or movable - owns large pre-allocated storage.
        EncodedRingBuffer(const EncodedRingBuffer&) = delete;
        EncodedRingBuffer& operator=(const EncodedRingBuffer&) = delete;


        // Store a packet from CaptureThread in video_config.packet_format.
        // The encoder must finish any format conversion before publication.
        bool Push(const uint8_t* encoded_data, uint32_t size,
            int64_t pts, bool is_keyframe, int64_t wall_qpc = 0);


        // Store codec, geometry, timing, packet format and decoder config as
        // one atomic stream description for future snapshots.
        // The stream description is immutable after its first publication in
        // this generation, preventing codec/config and packet mismatches.
        bool SetVideoConfig(const EncodedVideoConfig& config);
        bool HasVideoConfig() const;
        bool WaitForVideoConfig(EncodedVideoConfig& config,
            std::chrono::milliseconds timeout) const;


        // Copy the requested wall-clock interval with decoder keyframe pre-roll.
        // Called from the save thread, where blocking is permitted.
        EncodedRingSnapshot TakeSnapshotByTime(
            uint32_t duration_seconds, int64_t target_end_qpc) const;

        bool WaitUntilPublished(
            int64_t target_qpc, std::chrono::milliseconds timeout) const;

        // Invalidate replay after recovery without reallocating slot storage.
        // Concurrent snapshots see an empty/non-ready ring. Reject delayed packets
        // at or before recovery_cutoff_qpc so old output cannot enter the new generation.
        void Clear(int64_t recovery_cutoff_qpc = 0);

        // Internal packet-generation token used to prove that a snapshot did
        // not cross a backend recovery boundary. This is deliberately not part
        // of the shared-memory v4 contract.
        uint64_t GetGeneration() const {
            return generation_.load(std::memory_order_acquire);
        }


        // Stats
        size_t   GetCount()    const { return count_.load(std::memory_order_relaxed); }
        size_t   GetCapacity() const { return capacity_; }
        uint64_t GetPushCount() const { return head_.load(std::memory_order_relaxed); }


    private:
        size_t              capacity_;
        uint32_t            fps_;
        int64_t             qpc_freq_;

        std::vector<EncodedRingPacket>  slots_;

        // Per-slot atomic state flags (SlotState enum stored as uint32_t).
        // Separate from slots_ because std::atomic is not copyable/movable,
        // which would prevent std::vector<EncodedRingPacket> from compiling.
        // Indexed identically to slots_: state for slot i is slot_states_[i].
        std::unique_ptr<std::atomic<uint32_t>[]> slot_states_;
        std::unique_ptr<std::mutex[]> slot_mutexes_;

        // Monotonically increasing push counter.
        // Slot index = head_ % capacity_.
        std::atomic<uint64_t>  head_{ 0 };

        // Number of valid slots, capped at capacity_.
        std::atomic<size_t>    count_{ 0 };
        std::atomic<int64_t>   latest_wall_qpc_{ 0 };
        std::atomic<uint64_t>  generation_{ 0 };
        std::atomic<int64_t>   recovery_cutoff_qpc_{ 0 };

        mutable std::mutex publication_mutex_;
        mutable std::condition_variable publication_cv_;

        EncodedVideoConfig video_config_;
        mutable std::mutex video_config_mutex_;
        mutable std::condition_variable video_config_cv_;
        bool video_config_set_ = false;
    };


} // namespace fthr

#endif // FTHR_ENCODED_RING_BUFFER_H
