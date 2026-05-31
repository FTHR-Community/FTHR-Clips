// audio_encoder.h
// FTHR Capture Engine - AAC Audio Encoder
//
// Accepts raw PCM float32 interleaved stereo from WASAPI and encodes it
// into AAC packets via FFmpeg's built-in AAC encoder. Each encoded packet
// is delivered to a caller-supplied callback, which pushes it into the
// AudioRingBuffer.
//
// Why float32 / 48kHz / stereo:
//   WASAPI shared-mode loopback delivers audio in the device's mix format,
//   which is almost always float32 48kHz stereo on modern Windows systems.
//   Accepting this format natively avoids a resampling step.
//
// PCM accumulation:
//   AAC encodes in fixed-size frames of exactly 1024 samples. WASAPI delivers
//   variable-sized chunks (typically 480 or 960 samples at 48kHz / 10ms or
//   20ms). The encoder maintains an internal accumulation buffer and only
//   calls avcodec_send_frame when a full 1024-sample frame is ready.
//   Remaining samples carry over to the next EncodeSamples() call.
//
// Extradata:
//   FFmpeg writes the MPEG-4 AudioSpecificConfig (ASC) into codec_ctx->extradata
//   after avcodec_open2 when AV_CODEC_FLAG_GLOBAL_HEADER is set.
//   GetExtradata() returns a copy for the MP4 muxer to attach to the audio
//   stream's codecpar->extradata.
//
// PTS:
//   Tracks a monotonically increasing sample counter (pts_samples_).
//   Each encoded packet carries its PTS in samples (48000Hz timebase).
//   This matches AudioRingBuffer and makes muxer alignment straightforward.
//
// Threading:
//   EncodeSamples() is called from AudioCapture's WASAPI callback thread.
//   Not thread-safe for concurrent callers - single producer assumed.
//   Finalize() must be called from the same thread as EncodeSamples(), or
//   after that thread has exited.

#pragma once
#ifndef FTHR_AUDIO_ENCODER_H
#define FTHR_AUDIO_ENCODER_H

#include <cstdint>
#include <vector>
#include <functional>

// Forward-declare FFmpeg structs - keep FFmpeg headers out of this header.
struct AVCodecContext;
struct AVFrame;
struct AVPacket;


namespace fthr {


    // ---------------------------------------------------------------------------
    // AudioEncoder
    // ---------------------------------------------------------------------------
    class AudioEncoder {
    public:
        // Callback fired once per encoded AAC packet.
        // data:     raw AAC frame bytes (no ADTS header)
        // size:     byte count
        // pts:      presentation timestamp in audio samples (48000Hz timebase)
        //
        // Callback runs on the WASAPI callback thread. Must be fast.
        // Typically just calls AudioRingBuffer::Push().
        using PacketCallback = std::function<void(const uint8_t* data,
                                                   uint32_t       size,
                                                   int64_t        pts)>;

        AudioEncoder();
        ~AudioEncoder();

        // Initialize the FFmpeg AAC encoder.
        // sample_rate:   input sample rate in Hz (must match WASAPI device format)
        // channels:      number of channels (1=mono, 2=stereo)
        // bitrate_kbps:  target AAC bitrate (128 is a good default)
        // callback:      receives every encoded packet - must not be null
        //
        // Returns true on success.
        bool Initialize(uint32_t       sample_rate,
                        uint32_t       channels,
                        uint32_t       bitrate_kbps,
                        PacketCallback callback);

        // Feed raw interleaved float32 PCM samples into the encoder.
        // pcm_data:    pointer to interleaved samples (L,R,L,R,...) as float32
        // num_samples: total sample count (frames * channels)
        //              e.g. 960 frames stereo -> num_samples = 1920
        //
        // Internally accumulates samples until a full 1024-sample AAC frame
        // is ready, then encodes and fires the callback.
        // May fire the callback zero or multiple times per call.
        void EncodeSamples(const float* pcm_data, uint32_t num_samples);

        // Flush any remaining buffered samples and free all FFmpeg resources.
        // Must be called before destruction.
        void Finalize();

        // Return the MPEG-4 AudioSpecificConfig (ASC) extracted from
        // codec_ctx->extradata after Initialize(). Pass this to the MP4
        // muxer via stream->codecpar->extradata.
        // Returns empty vector if Initialize() has not been called or failed.
        std::vector<uint8_t> GetExtradata() const;

        uint32_t GetSampleRate() const { return sample_rate_; }
        uint32_t GetChannels()   const { return channels_; }
        bool     IsInitialized() const { return initialized_; }


    private:
        // Encode one full AVFrame (1024 samples) and fire the callback
        // for every packet avcodec_receive_packet returns.
        void EncodeFrame();

        // FFmpeg objects
        AVCodecContext* codec_ctx_;
        AVFrame*        frame_;
        AVPacket*       packet_;

        // Encoder configuration
        uint32_t sample_rate_;
        uint32_t channels_;
        uint32_t bitrate_kbps_;
        bool     initialized_;

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
    };


} // namespace fthr

#endif // FTHR_AUDIO_ENCODER_H
