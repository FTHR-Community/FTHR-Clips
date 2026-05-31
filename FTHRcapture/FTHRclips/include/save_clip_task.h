// save_clip_task.h
// FTHR Capture Engine - Async SaveClip task queue
//
// SaveClipTask supports two video paths and one audio path:
//
//   VIDEO - use_encoded_path = false (x264 fallback):
//     start_frame_idx / frame_count index into the raw FramePool.
//
//   VIDEO - use_encoded_path = true (NVENC path):
//     encoded_snapshot holds a deep copy of packets from EncodedRingBuffer.
//     MuxEncodedClip() wraps them in an MP4 with no re-encoding.
//
//   AUDIO (both video paths):
//     audio_snapshot holds raw PCM float32 from AudioRingBuffer.
//     MuxEncodedClip() encodes it to AAC once on SaveClipThread.
//     Zero encoding overhead during gameplay - encode only at save time.

#pragma once
#ifndef FTHR_SAVE_CLIP_TASK_H
#define FTHR_SAVE_CLIP_TASK_H

#include <string>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <cstdint>
#include <vector>

#include "encoded_ring_buffer.h"  // EncodedRingSnapshot
#include "audio_ring_buffer.h"    // AudioPCMSnapshot


namespace fthr {


    // Forward declarations
    struct SharedMemoryLayout;


    // ---------------------------------------------------------------------------
    // SaveClipTask
    // ---------------------------------------------------------------------------
    struct SaveClipTask {
        std::wstring output_path;
        uint32_t     duration_seconds = 0;

        // ------------------------------------------------------------------
        // VIDEO: Encoded path (NVENC) - use_encoded_path = true
        // ------------------------------------------------------------------
        bool                use_encoded_path = false;
        EncodedRingSnapshot encoded_snapshot;
        uint32_t            enc_width = 0;
        uint32_t            enc_height = 0;

        // ------------------------------------------------------------------
        // VIDEO: Raw frame path (x264 fallback) - use_encoded_path = false
        // ------------------------------------------------------------------
        size_t   start_frame_idx = 0;
        size_t   frame_count = 0;
        uint32_t src_width = 0;
        uint32_t src_height = 0;

        // ------------------------------------------------------------------
        // AUDIO: Raw PCM snapshot - present on both video paths.
        //
        // has_audio = false: audio capture not running, mux video-only.
        // has_audio = true:  audio_snapshot contains float32 PCM.
        //   MuxEncodedClip() encodes it to AAC on SaveClipThread.
        //   audio_bitrate_kbps: AAC target bitrate (default 128).
        // ------------------------------------------------------------------
        bool             has_audio = false;
        AudioPCMSnapshot audio_snapshot;
        uint32_t         audio_bitrate_kbps = 128;

        // ------------------------------------------------------------------
        // Shared fields
        // ------------------------------------------------------------------
        uint32_t fps = 60;
        uint32_t bitrate_kbps = 16000;

        // 0 = stretch, 1 = fit/letterbox. Forwarded to VideoEncoder for the
        // x264 fallback path; ignored on the NVENC mux-only path.
        uint32_t scaling_mode = 0;

        // ------------------------------------------------------------------
        // Video QPC epoch (NVENC path only).
        //
        // Video PTS P was captured at real wall-clock time:
        //   qpc_of_packet = video_qpc_epoch + (P * video_qpc_freq / fps)
        //
        // WASAPI stores audio timestamps in 100ns units (pu64QPCPosition).
        // To convert a raw QPC value to 100ns units: raw * 10_000_000 / qpc_freq.
        // To convert audio 100ns to seconds: value / 10_000_000.
        //
        // MuxEncodedClip uses these to compute audio_aligned_start_sample
        // without any estimation - pure clock arithmetic in the same domain.
        // ------------------------------------------------------------------
        int64_t  video_qpc_epoch = 0;   // encode_start_qpc_ from HardwareEncoder
        int64_t  video_qpc_freq = 0;   // qpc_freq_ (counts/second)

        SharedMemoryLayout* shared_memory = nullptr;
        uint32_t            task_id = 0;
    };


    // ---------------------------------------------------------------------------
    // SaveClipQueue
    // ---------------------------------------------------------------------------
    class SaveClipQueue {
    public:
        SaveClipQueue();
        ~SaveClipQueue();

        void   Push(SaveClipTask&& task);
        bool   Pop(SaveClipTask& out_task);
        void   Shutdown();
        bool   IsShutdown()    const;
        size_t GetQueueDepth() const;

    private:
        std::queue<SaveClipTask> queue_;
        mutable std::mutex       mutex_;
        std::condition_variable  cv_;
        bool                     shutdown_ = false;
    };


} // namespace fthr

#endif // FTHR_SAVE_CLIP_TASK_H