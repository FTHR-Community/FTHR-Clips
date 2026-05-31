// audio_ring_buffer.h
// FTHR Capture Engine - Raw PCM audio ring buffer
//
// Stores raw interleaved float32 PCM samples from WASAPI loopback.
// No encoding during gameplay - zero CPU overhead on the hot path.
// AAC encoding happens once at clip-save time on SaveClipThread.
//
// This is the same approach Shadowplay uses: capture PCM into a ring,
// encode only when the user actually saves a clip.
//
// Memory cost:
//   48000 Hz * 2 ch * 4 bytes * 32s buffer = ~12 MB (negligible)
//
// Threading model:
//   Push()         - called from AudioCaptureThread (~every 10ms, WASAPI callback)
//                    Atomics + mutex for the write, same pattern as EncodedRingBuffer.
//   TakeSnapshot() - called from SaveClipThread (rare, at clip save time)
//                    Locks, copies the requested window, returns PCM snapshot.
//
// Safety margin:
//   TakeSnapshot excludes the newest 0.5s of samples to avoid racing
//   with concurrent Push() calls from the audio thread.

#pragma once
#ifndef FTHR_AUDIO_RING_BUFFER_H
#define FTHR_AUDIO_RING_BUFFER_H

#include <cstdint>
#include <vector>
#include <atomic>
#include <mutex>


namespace fthr {


    // ---------------------------------------------------------------------------
    // AudioPCMSnapshot
    //
    // Returned by TakeSnapshot(). Caller owns all data.
    // samples: interleaved float32 [L0,R0,L1,R1,...] covering ~duration_s.
    // actual_newest_wall_s: the ring's absolute newest timestamp in seconds
    //   (before safety margin). Used by MuxEncodedClip Step D to align with
    //   the video ring's newest_video_pts without any safety-margin bias.
    // ---------------------------------------------------------------------------
    struct AudioPCMSnapshot {
        std::vector<float> samples;               // Interleaved float32 stereo
        uint32_t           sample_rate = 48000;
        uint32_t           channels = 2;

        // QPC-based wall-clock time (seconds) of the FIRST and LAST samples in
        // this snapshot. Derived from WASAPI's pu64QPCPosition (100ns units).
        // Same clock domain as HardwareEncoder's QPC PTS, so alignment is exact.
        double             qpc_start_s = 0.0;
        double             qpc_end_s = 0.0;

        bool               valid = false;
    };


    // ---------------------------------------------------------------------------
    // AudioRingBuffer
    // ---------------------------------------------------------------------------
    class AudioRingBuffer {
    public:
        // capacity_frames: total stereo frames pre-allocated.
        //   Recommended: sample_rate * (buffer_seconds + 4) for headroom.
        //   Default safety: 0.5s (24000 samples at 48kHz) excluded from newest end.
        AudioRingBuffer(uint32_t capacity_frames,
            uint32_t sample_rate = 48000,
            uint32_t channels = 2,
            uint32_t safety_frames = 24000);

        AudioRingBuffer(const AudioRingBuffer&) = delete;
        AudioRingBuffer& operator=(const AudioRingBuffer&) = delete;


        // -----------------------------------------------------------------------
        // Push
        //
        // Write interleaved float32 PCM frames into the ring.
        // Called ~every 10ms from AudioCaptureThread. Must be fast.
        // qpc_100ns: WASAPI pu64QPCPosition for the first frame in this packet
        //            (in 100-nanosecond units, as returned by GetBuffer).
        //            Pass 0 if not available (alignment will be approximate).
        // volume: linear gain applied before storing (1.0 = unity).
        // -----------------------------------------------------------------------
        void Push(const float* interleaved_data,
            uint32_t      frame_count,
            uint64_t      qpc_100ns,
            float         volume = 1.0f);


        // -----------------------------------------------------------------------
        // TakeSnapshot
        //
        // Copy the last duration_s seconds of PCM into an AudioPCMSnapshot.
        // Applies safety_frames margin from the newest end.
        //
        // Called from SaveClipThread - blocking is acceptable.
        // -----------------------------------------------------------------------
        AudioPCMSnapshot TakeSnapshot(double duration_s) const;


        // -----------------------------------------------------------------------
        // Stats
        // -----------------------------------------------------------------------
        uint32_t GetSampleRate()     const { return sample_rate_; }
        uint32_t GetChannels()       const { return channels_; }
        uint64_t GetFramesPushed()   const { return head_.load(std::memory_order_relaxed); }
        size_t   GetCapacityFrames() const { return capacity_frames_; }


    private:
        uint32_t capacity_frames_;
        uint32_t sample_rate_;
        uint32_t channels_;
        uint32_t safety_frames_;

        // Flat circular storage: capacity_frames_ * channels_ floats.
        std::vector<float> storage_;

        // Monotonically increasing frame counter.
        // Write slot index = head_ % capacity_frames_.
        std::atomic<uint64_t> head_{ 0 };

        // Number of valid frames written, capped at capacity_frames_.
        std::atomic<uint64_t> frames_written_{ 0 };

        // Per-frame QPC timestamp ring (100-nanosecond units from WASAPI).
        // Indexed the same way as storage_: qpc_ring_[head_ % capacity_frames_].
        // Only one entry written per Push() call (one WASAPI packet = one QPC value).
        // Frames within a packet are assigned by linear interpolation at read time.
        // This lets TakeSnapshot return the true wall-clock start/end of the snapshot.
        std::vector<uint64_t> qpc_ring_;

        mutable std::mutex ring_mutex_;
    };


} // namespace fthr

#endif // FTHR_AUDIO_RING_BUFFER_H