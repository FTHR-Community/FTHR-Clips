// encoded_ring_buffer.cpp
// FTHR Capture Engine - Encoded packet ring buffer implementation

#include "encoded_ring_buffer.h"
#include <iostream>
#include <algorithm>
#include <cassert>


namespace fthr {


    // ---------------------------------------------------------------------------
    // Construction
    //
    // Pre-allocates all slot objects. The vectors inside each slot start empty
    // and grow on first use, stabilizing at steady-state packet size after a
    // few seconds. No further heap allocations after warm-up.
    // ---------------------------------------------------------------------------

    EncodedRingBuffer::EncodedRingBuffer(size_t capacity, uint32_t fps, int64_t qpc_freq)
        : capacity_(capacity)
        , fps_(fps)
        , qpc_freq_(qpc_freq)
    {
        assert(capacity_ > 0);
        slots_.resize(capacity_);

        // Allocate slot state flags and explicitly initialise to EMPTY (0).
        // std::make_unique<T[]> value-initialises atomics, but the standard
        // does not guarantee that leaves them at 0, so we store explicitly.
        slot_states_ = std::make_unique<std::atomic<uint32_t>[]>(capacity_);
        for (size_t i = 0; i < capacity_; i++)
            slot_states_[i].store(static_cast<uint32_t>(SlotState::EMPTY),
                                  std::memory_order_relaxed);

        std::cout << "[EncodedRingBuffer] Initialized: "
            << capacity_ << " slots, "
            << fps_ << " fps safety margin"
            << std::endl;
    }


    // ---------------------------------------------------------------------------
    // Push
    //
    // Write one packet into the ring. Fast path - called every frame from
    // CaptureThread via HardwareEncoder callback. Fully lock-free.
    //
    // Protocol:
    //   1. Mark slot WRITING  (relaxed — no one reads this slot due to margin)
    //   2. Write data into slot
    //   3. Mark slot READY    (release — all writes visible before READY is seen)
    //   4. Advance head_      (release — new head visible after READY is stored)
    //
    // TakeSnapshot() only reads slots at least fps_ positions behind head_, so
    // it never observes a WRITING slot under normal operation. The state flag
    // check in TakeSnapshot is defense-in-depth only.
    // ---------------------------------------------------------------------------

    void EncodedRingBuffer::Push(const uint8_t* avcc_data, uint32_t size,
        int64_t pts, bool is_keyframe, int64_t wall_qpc) {

        const uint64_t write_pos = head_.load(std::memory_order_relaxed);
        const size_t   slot_idx  = static_cast<size_t>(write_pos % capacity_);

        // Mark slot as being written before touching its data.
        slot_states_[slot_idx].store(static_cast<uint32_t>(SlotState::WRITING),
                                     std::memory_order_relaxed);

        EncodedRingPacket& slot = slots_[slot_idx];

        // Resize vector to actual packet size.
        // After the first few keyframes, this is just a size assignment (no alloc).
        slot.data.resize(size);

        if (size > 0 && avcc_data) {
            std::memcpy(slot.data.data(), avcc_data, size);
        }

        slot.pts         = pts;
        slot.wall_qpc    = wall_qpc;
        slot.is_keyframe = is_keyframe;
        slot.valid       = true;

        // Release: all slot writes above must be visible before READY is observed.
        slot_states_[slot_idx].store(static_cast<uint32_t>(SlotState::READY),
                                     std::memory_order_release);

        // Advance head after READY so TakeSnapshot cannot see this slot until
        // its data is fully committed.
        head_.fetch_add(1, std::memory_order_release);

        // Update count, capped at capacity.
        size_t prev = count_.load(std::memory_order_relaxed);
        if (prev < capacity_) {
            count_.fetch_add(1, std::memory_order_relaxed);
        }
    }


    // ---------------------------------------------------------------------------
    // SetExtradata
    //
    // Called once after nvEncGetSequenceParams. Thread-safe.
    // ---------------------------------------------------------------------------

    void EncodedRingBuffer::SetExtradata(const uint8_t* data, size_t size) {
        std::lock_guard<std::mutex> lock(extradata_mutex_);
        extradata_.assign(data, data + size);
        std::cout << "[EncodedRingBuffer] Extradata set (" << size << " bytes)" << std::endl;
    }


    // ---------------------------------------------------------------------------
    // TakeSnapshot
    //
    // Copy up to frame_count packets into a Snapshot for the muxer.
    // No mutex held. Safe because:
    //   1. head_ is read with acquire, pairing with the release fetch_add in Push().
    //   2. The 1-second safety margin means Push() is always at least fps_ slots
    //      ahead of any slot we read — it cannot be WRITING a slot we are copying.
    //   3. Each slot's state is checked for READY (acquire) before the copy as
    //      defense-in-depth. A non-READY slot would indicate a logic error.
    // ---------------------------------------------------------------------------

    EncodedRingSnapshot EncodedRingBuffer::TakeSnapshot(size_t frame_count) const {
        EncodedRingSnapshot snap;

        // Copy extradata first (separate lock, fast)
        {
            std::lock_guard<std::mutex> lock(extradata_mutex_);
            snap.extradata = extradata_;
        }

        // Acquire: pairs with release fetch_add in Push(). Ensures all slot
        // writes for positions < current_head are visible on this thread.
        const uint64_t current_head  = head_.load(std::memory_order_acquire);
        const size_t   current_count = count_.load(std::memory_order_relaxed);

        if (current_count == 0) {
            std::cerr << "[EncodedRingBuffer] TakeSnapshot: buffer is empty" << std::endl;
            return snap;
        }

        // Apply safety margin: exclude newest fps_ packets
        const size_t safety     = static_cast<size_t>(fps_);
        const size_t safe_count = (current_count > safety)
            ? (current_count - safety)
            : current_count;

        const size_t frames_to_copy = std::min(frame_count, safe_count);

        if (frames_to_copy == 0) {
            std::cerr << "[EncodedRingBuffer] TakeSnapshot: no safe frames available" << std::endl;
            return snap;
        }

        if (frames_to_copy < frame_count) {
            std::cerr << "[EncodedRingBuffer] TakeSnapshot: only "
                << frames_to_copy << " frames available (requested "
                << frame_count << ")" << std::endl;
        }

        // Start position: take the NEWEST frames, not the oldest.
        // current_head points to the next write slot.
        // The newest safe frame is at: current_head - safety - 1
        // We want frames_to_copy frames ending there.
        const uint64_t safe_end  = current_head - safety;
        const uint64_t start_pos = safe_end - frames_to_copy;

        snap.packets.reserve(frames_to_copy);

        for (size_t i = 0; i < frames_to_copy; i++) {
            const uint64_t pos      = start_pos + static_cast<uint64_t>(i);
            const size_t   slot_idx = static_cast<size_t>(pos % capacity_);

            // Acquire: pairs with the READY release store in Push().
            // Ensures slot data is visible before we read it.
            const uint32_t raw_state =
                slot_states_[slot_idx].load(std::memory_order_acquire);

            if (raw_state != static_cast<uint32_t>(SlotState::READY)) {
                // Should never happen: safety margin keeps Push() out of this range.
                std::cerr << "[EncodedRingBuffer] TakeSnapshot: slot " << slot_idx
                    << " not READY (state=" << raw_state << "), skipping" << std::endl;
                continue;
            }

            const EncodedRingPacket& src = slots_[slot_idx];

            // Deep copy — caller owns this data independently of the ring buffer.
            EncodedRingPacket dst;
            dst.data        = src.data;   // vector copy
            dst.pts         = src.pts;
            dst.wall_qpc    = src.wall_qpc;
            dst.is_keyframe = src.is_keyframe;
            dst.valid       = true;
            snap.packets.push_back(std::move(dst));
        }

        // Compute QPC wall-clock range from first/last packet timestamps.
        // Convert raw QPC ticks to seconds using the same method as WASAPI:
        //   WASAPI: seconds = qpc_100ns / 10_000_000
        //   Video:  seconds = wall_qpc / qpc_freq  (same underlying QPC clock)
        if (qpc_freq_ > 0 && !snap.packets.empty()) {
            // Find first packet with valid QPC
            for (const auto& p : snap.packets) {
                if (p.wall_qpc > 0) {
                    snap.qpc_start_s = static_cast<double>(p.wall_qpc)
                        / static_cast<double>(qpc_freq_);
                    break;
                }
            }
            // Find last packet with valid QPC
            for (auto it = snap.packets.rbegin(); it != snap.packets.rend(); ++it) {
                if (it->wall_qpc > 0) {
                    snap.qpc_end_s = static_cast<double>(it->wall_qpc)
                        / static_cast<double>(qpc_freq_);
                    break;
                }
            }
        }

        std::cout << "[EncodedRingBuffer] TakeSnapshot: "
            << snap.packets.size() << " packets copied ("
            << safe_count << " safe, "
            << current_count << " total in ring), "
            << "QPC " << snap.qpc_start_s << "s - " << snap.qpc_end_s << "s"
            << std::endl;

        return snap;
    }


} // namespace fthr