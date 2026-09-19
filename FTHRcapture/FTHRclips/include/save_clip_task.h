// Queued clip-save data owned by the save worker.
// Compressed saves carry video packets, codec configuration, and per-source
// AAC snapshots for muxing. Raw frame indices and PCM fields support the
// legacy path only.

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
#include "audio_packet_ring.h"    // EncodedAudioSnapshot
#include "audio_ring_buffer.h"    // AudioPCMSnapshot
#include "audio_source_model.h"   // AudioSourceMetadata


namespace fthr {


    struct SharedMemoryLayout;

    // A source exists here only after its provider has emitted real packets for
    // this capture generation.  `presentation_start_pts_samples` is expressed
    // in the source's sample-rate timebase and is used to normalize packet PTS
    // against the video presentation boundary.
    struct EncodedAudioTrack {
        AudioSourceMetadata  source;
        EncodedAudioSnapshot snapshot;
        int64_t              presentation_start_pts_samples = 0;

        bool valid() const {
            return source.identity.id.IsValid()
                && snapshot.valid()
                && snapshot.source_id == source.identity.id;
        }
    };


    struct SaveClipTask {
        std::wstring output_path;
        uint32_t     duration_seconds = 0;

        // Disk-spooled replay merge path
        bool                use_spooler = false;

        // VIDEO: Encoded path (NVENC) - use_encoded_path = true
        bool                use_encoded_path = false;
        EncodedRingSnapshot encoded_snapshot;
        uint32_t            enc_width = 0;
        uint32_t            enc_height = 0;

        // Legacy raw-frame path: use_encoded_path = false.
        size_t   start_frame_idx = 0;
        size_t   frame_count = 0;
        uint32_t src_width = 0;
        uint32_t src_height = 0;

        // Legacy PCM fallback: has_audio selects the owned float32 snapshot.
        // MuxEncodedClip encodes it using audio_bitrate_kbps; production saves use
        // encoded_audio_tracks.
        bool             has_audio = false;
        AudioPCMSnapshot audio_snapshot;
        uint32_t         audio_bitrate_kbps = 128;

        // Persistent AAC replay path. The packet snapshot is selected against
        // the same QPC presentation interval as video; PTS are source timeline
        // samples and are normalized by MuxEncodedClip without re-encoding.
        bool                 has_encoded_audio = false;
        EncodedAudioSnapshot encoded_audio_snapshot;
        int64_t              audio_presentation_start_pts_samples = 0;

        // AUDIT-050 production replay contract.  Muxing may contain one
        // Default Mix, native Microphone, and up to eight actual application stems; never category
        // guesses or silent placeholders.  The legacy single-track fields
        // above are converted to one Default Mix only while older callers
        // remain in the tree.
        std::vector<EncodedAudioTrack> encoded_audio_tracks;
        // Native Windows saves are assembled from source packets first. The
        // UI's combined-mode finalizer collapses those streams after commit.
        bool separate_audio_enabled = false;

        // Shared fields
        uint32_t fps = 60;
        uint32_t bitrate_kbps = 16000;

        // 0 = stretch, 1 = fit/letterbox for legacy raw encoding.
        // Encoded snapshots already have their final geometry.
        uint32_t scaling_mode = 0;

        // Legacy video clock mapping: QPC = epoch + PTS * qpc_freq / fps.
        // WASAPI timestamps use 100 ns units: divide by 10,000,000 for seconds, or
        // multiply raw QPC by 10,000,000 / qpc_freq to compare in the same domain.
        int64_t  video_qpc_epoch = 0;   // encode_start_qpc_ from HardwareEncoder
        int64_t  video_qpc_freq = 0;   // qpc_freq_ (counts/second)

        SharedMemoryLayout* shared_memory = nullptr;
        uint32_t            task_id = 0;
    };


    class SaveClipQueue {
    public:
        static constexpr size_t kMaxPendingTasks = 4;

        SaveClipQueue();
        ~SaveClipQueue();

        bool   Push(SaveClipTask&& task);
        bool   Pop(SaveClipTask& out_task);
        void   Shutdown();
        bool   IsShutdown()    const;
        size_t GetQueueDepth() const;
        size_t GetCapacity()   const { return kMaxPendingTasks; }

    private:
        std::queue<SaveClipTask> queue_;
        mutable std::mutex       mutex_;
        std::condition_variable  cv_;
        bool                     shutdown_ = false;
    };


} // namespace fthr

#endif // FTHR_SAVE_CLIP_TASK_H
