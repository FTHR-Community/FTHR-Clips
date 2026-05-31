// video_encoder.h
// FTHR Capture Engine - x264 encoder and disk writer
//
// Responsibilities:
//   - Accept EncoderConfig describing source and target dimensions,
//     framerate, bitrate, and x264 preset/tune
//   - Configure libx264 via FFmpeg's AVCodec API
//   - Scale frames from native BGRA (src_width x src_height) to
//     target YUV420P (enc_width x enc_height) via libswscale
//   - Encode frames and push encoded packets to a packet queue
//   - Drain the packet queue on a dedicated DiskWriter thread so
//     disk I/O latency never stalls the encode loop
//
// Threading model:
//   EncodeFrame()       - called from CaptureEngine (SaveClip or EncodeThread)
//   DiskWriterThread()  - private thread owned by VideoEncoder
//                         started by Initialize(), joined by Finalize()
//
// What does NOT live here:
//   - Frame grabbing / ring buffer  ->  capture_engine.h
//   - IPC / shared memory           ->  shared_memory.h
//   - D3D11 / DXGI                  ->  capture_engine.h

#pragma once
#ifndef FTHR_VIDEO_ENCODER_H
#define FTHR_VIDEO_ENCODER_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <cstdint>
#include <vector>
#include <queue>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <memory>


// Full definition required - EncodedPacket destructor calls pool_ref->Release()
// and VideoEncoder holds unique_ptr<PacketBufferPool>. Forward declaration insufficient.
#include "packet_buffer_pool.h"

// Forward-declare FFmpeg structs only - keep FFmpeg headers out of this header.
struct AVFormatContext;
struct AVStream;
struct AVCodecContext;
struct AVFrame;
struct AVPacket;
struct SwsContext;


namespace fthr {



    // ---------------------------------------------------------------------------
    // EncoderConfig
    //
    // All fields are set by CaptureEngine before calling VideoEncoder::Initialize().
    // No field should have a default that could silently mask a misconfigured caller.
    //
    // Dimension contract:
    //   src_width / src_height  - native DXGI capture resolution (always > 0)
    //   enc_width / enc_height  - output encode resolution
    //                             if 0, enc dimensions are set equal to src (no scale)
    //
    // Preset / tune contract:
    //   preset  - x264 preset string. Recommended: "superfast"
    //             Valid values: ultrafast, superfast, veryfast, faster, fast,
    //                           medium, slow, slower, veryslow, placebo
    //   tune    - x264 tune string. Pass nullptr or "" for gaming clips.
    //             "film" is also acceptable. Never use "zerolatency" for SaveClip
    //             (it disables B-frames and lookahead, reducing quality).
    // ---------------------------------------------------------------------------
    struct EncoderConfig {
        uint32_t    src_width = 0;            // Native capture width  (must be > 0)
        uint32_t    src_height = 0;            // Native capture height (must be > 0)
        uint32_t    enc_width = 0;            // Target encode width   (0 = same as src)
        uint32_t    enc_height = 0;            // Target encode height  (0 = same as src)
        uint32_t    fps = 60;           // Frames per second
        uint32_t    bitrate_kbps = 16000;       // Target bitrate in kbps
        const char* preset = "superfast";  // x264 preset
        const char* tune = nullptr;      // x264 tune (nullptr = none)

        // 0 = stretch to enc dims (legacy default; aspect may distort).
        // 1 = preserve source aspect inside enc dims, pad with black bars.
        // Only consulted when enc dims differ from src dims AND aspects mismatch.
        uint32_t    scaling_mode = 0;
    };


    // ---------------------------------------------------------------------------
    // EncodedPacket
    //
    // PHASE 1 UPDATE: Now uses packet buffer pool instead of std::vector
    //
    // A self-contained encoded video packet ready to write to disk.
    // Copied out of AVPacket immediately after avcodec_receive_packet()
    // so the AVPacket can be unref'd and the encode loop can continue
    // without waiting for disk I/O.
    //
    // Phase 1: Uses pre-allocated buffers from PacketBufferPool to eliminate
    // malloc/free in the encode hot path (60 allocations/sec at 60fps).
    // ---------------------------------------------------------------------------
    struct EncodedPacket {
        // Phase 1: Packet buffer pool fields
        std::vector<uint8_t>* buffer = nullptr;  // Pointer to pooled buffer (owned)
        size_t                size = 0;        // Actual packet size in buffer
        int64_t               pts = 0;
        int64_t               dts = 0;
        int64_t               duration = 0;
        PacketBufferPool* pool_ref = nullptr;  // Non-owning ref for returning buffer

        // Phase 1: RAII - return buffer to pool on destruction
        ~EncodedPacket();

        // Move-only (no copy - we own a pooled buffer)
        EncodedPacket() = default;
        EncodedPacket(const EncodedPacket&) = delete;
        EncodedPacket& operator=(const EncodedPacket&) = delete;
        EncodedPacket(EncodedPacket&& other) noexcept;
        EncodedPacket& operator=(EncodedPacket&& other) noexcept;
    };


    // ---------------------------------------------------------------------------
    // VideoEncoder
    // ---------------------------------------------------------------------------
    class VideoEncoder {
    public:
        VideoEncoder();
        ~VideoEncoder();

        // Initialise the encoder and start the DiskWriter thread.
        // Must be called before any EncodeFrame() call.
        // Not thread-safe - call from one thread only (CaptureEngine).
        bool Initialize(const wchar_t* output_path, const EncoderConfig& config);

        // Flush the encoder, drain the disk queue, write the container trailer,
        // join the DiskWriter thread, and release all FFmpeg resources.
        // Safe to call even if Initialize() was never called.
        void Finalize();

        // Scale and encode one BGRA frame from the frame pool.
        // bgra_data must point to (src_width * src_height * 4) bytes.
        // Thread-safe for a single producer (CaptureEngine's encode path).
        bool EncodeFrame(const uint8_t* bgra_data);


    private:
        // -----------------------------------------------------------------------
        // Disk writer thread
        // -----------------------------------------------------------------------
        void DiskWriterThread();

        // Push an encoded packet to the disk queue.
        // Called from EncodeFrame() after avcodec_receive_packet().
        void PushPacket(AVPacket* pkt);

        // -----------------------------------------------------------------------
        // FFmpeg state
        // -----------------------------------------------------------------------
        AVFormatContext* format_ctx_;
        AVStream* video_stream_;
        AVCodecContext* codec_ctx_;
        AVFrame* frame_;         // YUV420P frame reused every EncodeFrame call
        AVPacket* packet_;        // Temporary packet, copied into EncodedPacket
        SwsContext* sws_ctx_;

        // -----------------------------------------------------------------------
        // Dimension state - set once by Initialize(), read-only after
        // -----------------------------------------------------------------------
        uint32_t src_width_;    // Native BGRA input width
        uint32_t src_height_;   // Native BGRA input height
        uint32_t enc_width_;    // YUV420P encode width  (may differ from src)
        uint32_t enc_height_;   // YUV420P encode height (may differ from src)

        // 0 = stretch (sws_scale stretches to enc dims).
        // 1 = fit  — sws scales source into an inscribed rect that
        //            preserves source aspect, with black padding around it.
        uint32_t scaling_mode_;
        uint32_t fit_dst_x_;    // Inscribed rect inside the YUV420P output frame
        uint32_t fit_dst_y_;
        uint32_t fit_dst_w_;
        uint32_t fit_dst_h_;

        // -----------------------------------------------------------------------
        // Encode state
        // -----------------------------------------------------------------------
        int64_t pts_;
        bool    initialized_;

        // -----------------------------------------------------------------------
        // Disk writer thread state
        //
        // packet_queue_   - FIFO of packets waiting to be written to disk
        // packet_mutex_   - guards packet_queue_
        // packet_cv_      - signals DiskWriterThread when queue is non-empty
        // disk_thread_    - the actual disk writer thread
        // disk_running_   - set false by Finalize() to drain and exit the thread
        // -----------------------------------------------------------------------
        std::queue<EncodedPacket> packet_queue_;
        std::mutex                packet_mutex_;
        std::condition_variable   packet_cv_;
        std::thread               disk_thread_;
        std::atomic<bool>         disk_running_{ false };

        // -----------------------------------------------------------------------
        // Phase 1: Packet buffer pool (eliminates malloc/free in encode path)
        // -----------------------------------------------------------------------
        std::unique_ptr<PacketBufferPool> packet_pool_;
    };


} // namespace fthr


#endif // FTHR_VIDEO_ENCODER_H