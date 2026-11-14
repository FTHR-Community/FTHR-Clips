// Single-producer packetizer for lossless float PCM or legacy AAC encoding.
// Packet PTS uses the input sample-rate timebase. Raw mode retains a partial
// final packet exactly; AAC mode supplies muxer ASC through GetExtradata().
// Finalize on the producer thread or after it has stopped.

#pragma once
#ifndef FTHR_AUDIO_ENCODER_H
#define FTHR_AUDIO_ENCODER_H

#include <cstdint>
#include <atomic>
#include <vector>
#include <functional>

// Forward-declare FFmpeg structs - keep FFmpeg headers out of this header.
struct AVCodecContext;
struct AVFrame;
struct AVPacket;


namespace fthr {


    class AudioEncoder {
    public:
        struct Stats {
            uint64_t frames_submitted = 0;
            uint64_t packets_emitted = 0;
            uint64_t encode_errors = 0;
            uint64_t flush_errors = 0;
        };
        // Receives raw AAC bytes without ADTS headers and PTS in audio samples.
        // Runs on the capture thread; the callback must not block.
        using PacketCallback = std::function<void(const uint8_t* data,
                                                   uint32_t       size,
                                                   int64_t        pts)>;

        AudioEncoder();
        ~AudioEncoder();

        // Initialize AAC with the upstream PCM rate, 1..8 channels, and target kbps.
        // The packet callback is required. Returns false if initialization fails.
        bool Initialize(uint32_t       sample_rate,
                        uint32_t       channels,
                        uint32_t       bitrate_kbps,
                        PacketCallback callback,
                        int64_t        initial_pts_samples = 0,
                        bool           lossless_float = false);

        // Feed interleaved float32 PCM; num_samples counts frames * channels.
        // Raw mode emits immediately. AAC mode buffers partial codec frames.
        bool EncodeSamples(const float* pcm_data, uint32_t num_samples);

        // Flush any remaining buffered samples and free all FFmpeg resources.
        // Must be called before destruction.
        bool Finalize();

        // Return the MPEG-4 AudioSpecificConfig (ASC) extracted from
        // codec_ctx->extradata after Initialize(). Pass this to the MP4
        // muxer via stream->codecpar->extradata.
        // Returns empty vector if Initialize() has not been called or failed.
        std::vector<uint8_t> GetExtradata() const;

        uint32_t GetSampleRate() const { return sample_rate_; }
        uint32_t GetChannels()   const { return channels_; }
        bool     IsInitialized() const { return initialized_; }
        Stats stats() const {
            return {frames_submitted_.load(std::memory_order_relaxed),
                    packets_emitted_.load(std::memory_order_relaxed),
                    encode_errors_.load(std::memory_order_relaxed),
                    flush_errors_.load(std::memory_order_relaxed)};
        }


    private:
        // Encode one full AVFrame (1024 samples) and fire the callback
        // for every packet avcodec_receive_packet returns.
        bool EncodeFrame();

        // FFmpeg objects
        AVCodecContext* codec_ctx_;
        AVFrame*        frame_;
        AVPacket*       packet_;

        // Encoder configuration
        uint32_t sample_rate_;
        uint32_t channels_;
        uint32_t bitrate_kbps_;
        bool     initialized_;
        bool     lossless_float_ = false;

        // Monotonically increasing PTS in samples
        int64_t pts_samples_;

        // PCM accumulation buffer.
        // Holds interleaved float32 samples waiting for a full 1024-frame.
        // Size: channels * 1024 floats maximum.
        std::vector<float> accum_buf_;
        uint32_t           accum_frames_;  // frames (not samples) currently buffered

        // Encoded AAC packet callback
        PacketCallback packet_callback_;

        // MPEG-4 AudioSpecificConfig for MP4 muxer
        std::vector<uint8_t> extradata_;
        std::atomic<uint64_t> frames_submitted_{ 0 };
        std::atomic<uint64_t> packets_emitted_{ 0 };
        std::atomic<uint64_t> encode_errors_{ 0 };
        std::atomic<uint64_t> flush_errors_{ 0 };
    };


} // namespace fthr

#endif // FTHR_AUDIO_ENCODER_H
