// audio_ring_buffer.cpp
// FTHR Capture Engine - Raw PCM audio ring buffer implementation

#include "audio_ring_buffer.h"
#include <iostream>
#include <algorithm>
#include <cassert>
#include <cstring>


namespace fthr {


    // ---------------------------------------------------------------------------
    // Constructor
    // ---------------------------------------------------------------------------

    AudioRingBuffer::AudioRingBuffer(uint32_t capacity_frames,
                                     uint32_t sample_rate,
                                     uint32_t channels,
                                     uint32_t safety_frames)
        : capacity_frames_(capacity_frames)
        , sample_rate_(sample_rate)
        , channels_(channels)
        , safety_frames_(safety_frames)
    {
        assert(capacity_frames_ > 0);
        assert(channels_ > 0);

        storage_.resize(static_cast<size_t>(capacity_frames_) * channels_, 0.0f);
        qpc_ring_.resize(capacity_frames_, 0ULL);

        std::cout << "[AudioRingBuffer] Initialized: "
            << capacity_frames_ << " frames x "
            << channels_ << "ch @ "
            << sample_rate_ << "Hz ("
            << (storage_.size() * sizeof(float) / 1024 / 1024) << " MB)"
            << std::endl;
    }


    // ---------------------------------------------------------------------------
    // Push
    //
    // Write interleaved float32 PCM frames into the ring with a QPC timestamp.
    // qpc_100ns: the WASAPI pu64QPCPosition for the FIRST frame in this packet
    //            (100-nanosecond units). We stamp each frame slot so TakeSnapshot
    //            can read back the exact wall-clock time of any frame.
    //
    // Interpolation: qpc[frame f] = qpc_100ns + f * (10_000_000 / sample_rate)
    // At 48kHz: each frame = 208.3 100ns-ticks. Integer approx is fine here
    // because the residual error (~0.3 ticks/frame) over 30s is only ~0.4ms.
    // ---------------------------------------------------------------------------

    void AudioRingBuffer::Push(const float*  interleaved_data,
                               uint32_t      frame_count,
                               uint64_t      qpc_100ns,
                               float         volume)
    {
        if (!interleaved_data || frame_count == 0) return;

        std::lock_guard<std::mutex> lock(ring_mutex_);

        const uint64_t write_head = head_.load(std::memory_order_relaxed);

        // 100ns ticks per audio frame
        const uint64_t ticks_per_frame = (sample_rate_ > 0)
            ? (10000000ULL / static_cast<uint64_t>(sample_rate_))
            : 208ULL;

        for (uint32_t f = 0; f < frame_count; f++) {
            const size_t slot       = static_cast<size_t>((write_head + f) % capacity_frames_);
            const size_t src_offset = static_cast<size_t>(f) * channels_;
            const size_t dst_offset = slot * channels_;

            if (volume == 1.0f) {
                std::memcpy(storage_.data() + dst_offset,
                            interleaved_data + src_offset,
                            channels_ * sizeof(float));
            }
            else {
                for (uint32_t c = 0; c < channels_; c++) {
                    storage_[dst_offset + c] = interleaved_data[src_offset + c] * volume;
                }
            }

            qpc_ring_[slot] = (qpc_100ns > 0)
                ? (qpc_100ns + static_cast<uint64_t>(f) * ticks_per_frame)
                : 0ULL;
        }

        head_.fetch_add(frame_count, std::memory_order_release);

        uint64_t prev = frames_written_.load(std::memory_order_relaxed);
        uint64_t next = std::min(prev + frame_count,
                                 static_cast<uint64_t>(capacity_frames_));
        frames_written_.store(next, std::memory_order_relaxed);
    }


    // ---------------------------------------------------------------------------
    // TakeSnapshot
    // ---------------------------------------------------------------------------

    AudioPCMSnapshot AudioRingBuffer::TakeSnapshot(double duration_s) const {
        AudioPCMSnapshot snap;
        snap.sample_rate = sample_rate_;
        snap.channels    = channels_;

        std::lock_guard<std::mutex> lock(ring_mutex_);

        const uint64_t current_head   = head_.load(std::memory_order_acquire);
        const uint64_t frames_written = frames_written_.load(std::memory_order_relaxed);

        if (frames_written == 0) {
            std::cerr << "[AudioRingBuffer] TakeSnapshot: buffer is empty" << std::endl;
            return snap;
        }

        const uint64_t safe_frames = (frames_written > safety_frames_)
            ? (frames_written - safety_frames_)
            : frames_written;

        const uint64_t wanted_frames = static_cast<uint64_t>(
            duration_s * static_cast<double>(sample_rate_) + 0.5);

        const uint64_t frames_to_copy = std::min(wanted_frames, safe_frames);

        if (frames_to_copy == 0) {
            std::cerr << "[AudioRingBuffer] TakeSnapshot: no safe frames" << std::endl;
            return snap;
        }

        if (frames_to_copy < wanted_frames) {
            std::cerr << "[AudioRingBuffer] TakeSnapshot: only "
                << frames_to_copy << " frames available" << std::endl;
        }

        const uint64_t start_pos = (current_head - safety_frames_) - frames_to_copy;
        const uint64_t end_pos   =  current_head - safety_frames_ - 1;

        snap.samples.resize(static_cast<size_t>(frames_to_copy) * channels_);

        for (uint64_t f = 0; f < frames_to_copy; f++) {
            const size_t src_slot   = static_cast<size_t>((start_pos + f) % capacity_frames_);
            const size_t src_offset = src_slot * channels_;
            const size_t dst_offset = static_cast<size_t>(f) * channels_;
            std::memcpy(snap.samples.data() + dst_offset,
                        storage_.data() + src_offset,
                        channels_ * sizeof(float));
        }

        // Read QPC timestamps of first and last frames (100ns -> seconds)
        const size_t first_slot = static_cast<size_t>(start_pos % capacity_frames_);
        const size_t last_slot  = static_cast<size_t>(end_pos   % capacity_frames_);
        const uint64_t qpc_first = qpc_ring_[first_slot];
        const uint64_t qpc_last  = qpc_ring_[last_slot];

        snap.qpc_start_s = (qpc_first > 0)
            ? static_cast<double>(qpc_first) / 10000000.0
            : 0.0;
        snap.qpc_end_s = (qpc_last > 0)
            ? static_cast<double>(qpc_last) / 10000000.0
            : snap.qpc_start_s + static_cast<double>(frames_to_copy) / sample_rate_;

        snap.valid = true;

        std::cout << "[AudioRingBuffer] TakeSnapshot: "
            << frames_to_copy << " frames ("
            << (static_cast<double>(frames_to_copy) / sample_rate_) << "s), "
            << "QPC " << snap.qpc_start_s << "s - " << snap.qpc_end_s << "s"
            << std::endl;

        return snap;
    }


} // namespace fthr
