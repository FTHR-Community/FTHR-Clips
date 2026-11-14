// Legacy raw-frame encoder and asynchronous packet writer.
// Scale BGRA to the output format, encode with FFmpeg, and hand packets to
// DiskWriterThread. The save worker calls EncodeFrame; Finalize drains
// and joins the writer.

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

// AudioEncoder used to pre-encode PCM -> AAC when audio data is provided.
#include "audio_encoder.h"

// Forward-declare FFmpeg structs only - keep FFmpeg headers out of this header.
struct AVFormatContext;
struct AVStream;
struct AVCodecContext;
struct AVFrame;
struct AVPacket;
struct SwsContext;


namespace fthr {



    // Set source dimensions before Initialize; zero output dimensions use the
    // source size. Preset/tune apply only to encoders supporting those options.
    struct EncoderConfig {
        uint32_t    src_width = 0;            // Native capture width  (must be > 0)
        uint32_t    src_height = 0;            // Native capture height (must be > 0)
        uint32_t    enc_width = 0;            // Target encode width   (0 = same as src)
        uint32_t    enc_height = 0;            // Target encode height  (0 = same as src)
        uint32_t    fps = 60;           // Frames per second
        uint32_t    bitrate_kbps = 16000;       // Target bitrate in kbps
        const char* preset = "superfast";  // x264 preset
        const char* tune = nullptr;      // x264 tune (nullptr = none)
        uint32_t    hardware_preset = 4;  // NVENC P1-P7 quality/performance level

        // 0 = stretch to enc dims (legacy default; aspect may distort).
        // 1 = preserve source aspect inside enc dims, pad with black bars.
        // Only consulted when enc dims differ from src dims AND aspects mismatch.
        uint32_t    scaling_mode = 0;
        bool        hdr10 = false;       // P010 input with BT.2100 PQ signaling
    };


    // Owned encoded packet copied from AVPacket into a pooled buffer.
    // The encoder can release AVPacket while DiskWriterThread holds this data.
    struct EncodedPacket {
        std::vector<uint8_t>* buffer = nullptr;  // Pointer to pooled buffer (owned)
        size_t                size = 0;        // Actual packet size in buffer
        int64_t               pts = 0;
        int64_t               dts = 0;
        int64_t               duration = 0;
        PacketBufferPool* pool_ref = nullptr;  // Non-owning ref for returning buffer

        // Return the owned buffer to its pool on destruction.
        ~EncodedPacket();

        // Move-only (no copy - we own a pooled buffer)
        EncodedPacket() = default;
        EncodedPacket(const EncodedPacket&) = delete;
        EncodedPacket& operator=(const EncodedPacket&) = delete;
        EncodedPacket(EncodedPacket&& other) noexcept;
        EncodedPacket& operator=(EncodedPacket&& other) noexcept;
    };


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
        bool Finalize();

        // Scale and encode one BGRA frame from the frame pool.
        // bgra_data must point to (src_width * src_height * 4) bytes.
        // Thread-safe for a single producer (CaptureEngine's encode path).
        bool EncodeFrame(const uint8_t* bgra_data);

        // Optional: provide audio PCM that will be encoded to AAC and muxed
        // into the output file alongside video. Must be called BEFORE Initialize().
        // pcm: interleaved float32 stereo samples (L,R,L,R,...)
        void SetAudioData(std::vector<float> pcm,
                          uint32_t           sample_rate,
                          uint32_t           channels,
                          uint32_t           bitrate_kbps = 128);


    private:
        // Disk writer thread
        void DiskWriterThread();

        // Close/free resources left by either a completed encode or a partial
        // Initialize() failure. Does not write a trailer.
        void ReleaseResources();

        // Push an encoded packet to the disk queue.
        // Called from EncodeFrame() after avcodec_receive_packet().
        void PushPacket(AVPacket* pkt);

        // FFmpeg state
        AVFormatContext* format_ctx_;
        AVStream* video_stream_;
        AVCodecContext* codec_ctx_;
        AVFrame* frame_;         // YUV420P frame reused every EncodeFrame call
        AVPacket* packet_;        // Temporary packet, copied into EncodedPacket
        SwsContext* sws_ctx_;

        // Dimension state - set once by Initialize(), read-only after
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

        // Encode state
        int64_t pts_;
        bool    initialized_;

        // packet_mutex_ protects the FIFO; packet_cv_ wakes the disk writer.
        // Finalize clears disk_running_ so the writer drains the queue and exits.
        std::queue<EncodedPacket> packet_queue_;
        std::mutex                packet_mutex_;
        std::condition_variable   packet_cv_;
        std::thread               disk_thread_;
        std::atomic<bool>         disk_running_{ false };
        std::atomic<int>          disk_write_error_{ 0 };

        // Preallocated packet buffers shared with the writer.
        std::unique_ptr<PacketBufferPool> packet_pool_;

        // Optional audio: pre-encoded AAC packets written in Finalize()
        bool                              has_audio_           = false;
        std::vector<float>                audio_pcm_;
        uint32_t                          audio_sample_rate_   = 48000;
        uint32_t                          audio_channels_      = 2;
        uint32_t                          audio_bitrate_kbps_  = 128;
        AVStream*                         audio_stream_        = nullptr;
        std::vector<std::vector<uint8_t>> aac_packets_;
        std::vector<int64_t>              aac_pts_list_;
    };


} // namespace fthr


#endif // FTHR_VIDEO_ENCODER_H
