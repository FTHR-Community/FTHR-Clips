// Float PCM packetization, with partial-frame accumulation and legacy AAC support.

#ifdef _MSC_VER
#if __has_include("pch.h")
#include "pch.h"
#elif __has_include("stdafx.h")
#include "stdafx.h"
#endif
#endif

#include "audio_encoder.h"
#include <iostream>
#include <cassert>
#include <cstring>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libavutil/samplefmt.h>
}


namespace fthr {


    // Constructor / Destructor

    AudioEncoder::AudioEncoder()
        : codec_ctx_(nullptr)
        , frame_(nullptr)
        , packet_(nullptr)
        , sample_rate_(0)
        , channels_(0)
        , bitrate_kbps_(128)
        , initialized_(false)
        , pts_samples_(0)
        , accum_frames_(0)
    {
    }

    AudioEncoder::~AudioEncoder() {
        Finalize();
    }



    bool AudioEncoder::Initialize(uint32_t       sample_rate,
        uint32_t       channels,
        uint32_t       bitrate_kbps,
        PacketCallback callback,
        int64_t        initial_pts_samples, bool lossless_float)
    {
        if (initialized_) {
            Finalize();
        }

        if (!callback || sample_rate == 0 || channels == 0 || channels > 8) {
            std::cerr << "[AudioEncoder] PacketCallback must not be null" << std::endl;
            return false;
        }

        packet_callback_ = std::move(callback);
        frames_submitted_.store(0, std::memory_order_relaxed);
        packets_emitted_.store(0, std::memory_order_relaxed);
        encode_errors_.store(0, std::memory_order_relaxed);
        flush_errors_.store(0, std::memory_order_relaxed);
        extradata_.clear();
        sample_rate_ = sample_rate;
        channels_ = channels;
        bitrate_kbps_ = bitrate_kbps;
        pts_samples_ = initial_pts_samples;
        accum_frames_ = 0;
        lossless_float_ = lossless_float;
        if (lossless_float_) {
            initialized_ = true;
            return true;
        }

        const AVCodec* codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
        if (!codec) {
            std::cerr << "[AudioEncoder] AAC encoder not found - check FFmpeg build" << std::endl;
            return false;
        }

        codec_ctx_ = avcodec_alloc_context3(codec);
        if (!codec_ctx_) {
            std::cerr << "[AudioEncoder] avcodec_alloc_context3 failed" << std::endl;
            return false;
        }

        codec_ctx_->sample_rate = static_cast<int>(sample_rate_);
        codec_ctx_->bit_rate = static_cast<int64_t>(bitrate_kbps_) * 1000;
        codec_ctx_->sample_fmt = AV_SAMPLE_FMT_FLTP;  // AAC requires planar float

        // AV_CODEC_FLAG_GLOBAL_HEADER tells FFmpeg to write the MPEG-4
        // AudioSpecificConfig (ASC) into codec_ctx_->extradata instead of
        // prepending an ADTS header to each packet. The muxer needs the ASC
        // in stream->codecpar->extradata for the MP4 container.
        codec_ctx_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

        av_channel_layout_default(&codec_ctx_->ch_layout, static_cast<int>(channels_));

        int ret = avcodec_open2(codec_ctx_, codec, nullptr);
        if (ret < 0) {
            std::cerr << "[AudioEncoder] avcodec_open2 failed: " << ret << std::endl;
            avcodec_free_context(&codec_ctx_);
            codec_ctx_ = nullptr;
            return false;
        }

        // avcodec_open2 supplies ASC when AV_CODEC_FLAG_GLOBAL_HEADER is set.
        if (codec_ctx_->extradata && codec_ctx_->extradata_size > 0) {
            extradata_.assign(codec_ctx_->extradata,
                codec_ctx_->extradata + codec_ctx_->extradata_size);
            std::cout << "[AudioEncoder] ASC extradata: "
                << extradata_.size() << " bytes" << std::endl;
        }
        else {
            std::cerr << "[AudioEncoder] WARNING: no ASC extradata - "
                << "audio stream may not play in all players" << std::endl;
        }

        frame_ = av_frame_alloc();
        if (!frame_) {
            std::cerr << "[AudioEncoder] av_frame_alloc failed" << std::endl;
            avcodec_free_context(&codec_ctx_);
            codec_ctx_ = nullptr;
            return false;
        }

        // codec_ctx_->frame_size is 1024 for AAC
        frame_->nb_samples = codec_ctx_->frame_size;
        frame_->format = codec_ctx_->sample_fmt;
        av_channel_layout_copy(&frame_->ch_layout, &codec_ctx_->ch_layout);

        ret = av_frame_get_buffer(frame_, 0);
        if (ret < 0) {
            std::cerr << "[AudioEncoder] av_frame_get_buffer failed: " << ret << std::endl;
            av_frame_free(&frame_);
            avcodec_free_context(&codec_ctx_);
            frame_ = nullptr;
            codec_ctx_ = nullptr;
            return false;
        }

        packet_ = av_packet_alloc();
        if (!packet_) {
            std::cerr << "[AudioEncoder] av_packet_alloc failed" << std::endl;
            av_frame_free(&frame_);
            avcodec_free_context(&codec_ctx_);
            frame_ = nullptr;
            codec_ctx_ = nullptr;
            return false;
        }

        // Reserve one full AAC frame of interleaved samples for every channel.
        accum_buf_.resize(static_cast<size_t>(channels_) * 1024, 0.0f);
        accum_frames_ = 0;

        initialized_ = true;
        std::cout << "[AudioEncoder] Ready: "
            << sample_rate_ << " Hz, "
            << channels_ << " ch, "
            << bitrate_kbps_ << " kbps AAC, "
            << "frame_size=" << codec_ctx_->frame_size
            << std::endl;
        return true;
    }


    // Accumulate interleaved float32 PCM and encode each full AAC frame.
    // One input chunk can produce zero or multiple packet callbacks.

    bool AudioEncoder::EncodeSamples(const float* pcm_data, uint32_t num_samples)
    {
        if (!initialized_ || !pcm_data || num_samples == 0
                || num_samples % channels_ != 0) return false;

        bool success = true;

        const uint32_t frames_in = num_samples / channels_;
        if (lossless_float_) {
            // Raw capture has no codec frame boundary or hidden final tail.
            if (num_samples > UINT32_MAX / sizeof(float)) return false;
            packet_callback_(reinterpret_cast<const uint8_t*>(pcm_data),
                num_samples * sizeof(float), pts_samples_);
            pts_samples_ += frames_in;
            frames_submitted_.fetch_add(1, std::memory_order_relaxed);
            packets_emitted_.fetch_add(1, std::memory_order_relaxed);
            return true;
        }
        const uint32_t frame_size = static_cast<uint32_t>(codec_ctx_->frame_size);

        uint32_t consumed = 0;
        while (consumed < frames_in) {
            const uint32_t space = frame_size - accum_frames_;
            const uint32_t available = frames_in - consumed;
            const uint32_t copy_frames = (available < space) ? available : space;

            // Append to accumulation buffer (interleaved)
            const float* src = pcm_data + consumed * channels_;
            float* dst = accum_buf_.data() + accum_frames_ * channels_;
            std::memcpy(dst, src, copy_frames * channels_ * sizeof(float));

            accum_frames_ += copy_frames;
            consumed += copy_frames;

            if (accum_frames_ == frame_size) {
                if (!EncodeFrame()) success = false;
                accum_frames_ = 0;
            }
        }
        return success;
    }


    // EncodeFrame (private)
    //
    // De-interleave accum_buf_ into planar AVFrame, send to codec,
    // drain any output packets and fire the callback.

    bool AudioEncoder::EncodeFrame()
    {
        if (!initialized_) return false;

        const uint32_t frame_size = static_cast<uint32_t>(codec_ctx_->frame_size);

        if (av_frame_make_writable(frame_) < 0) {
            encode_errors_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }

        // De-interleave: WASAPI stores L0,R0,L1,R1,...
        // FFmpeg AAC expects separate planes: [all L] [all R]
        for (uint32_t channel = 0; channel < channels_; ++channel) {
            float* destination = reinterpret_cast<float*>(frame_->extended_data[channel]);
            const float* source = accum_buf_.data();
            for (uint32_t sample = 0; sample < frame_size; ++sample) {
                destination[sample] = source[sample * channels_ + channel];
            }
        }

        frame_->pts = pts_samples_;
        pts_samples_ += static_cast<int64_t>(frame_size);

        int ret = avcodec_send_frame(codec_ctx_, frame_);
        if (ret < 0) {
            encode_errors_.fetch_add(1, std::memory_order_relaxed);
            std::cerr << "[AudioEncoder] avcodec_send_frame failed: " << ret << std::endl;
            return false;
        }
        frames_submitted_.fetch_add(1, std::memory_order_relaxed);

        bool success = true;

        // Drain encoded packets (usually one per send for AAC)
        while (ret >= 0) {
            ret = avcodec_receive_packet(codec_ctx_, packet_);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
            if (ret < 0) {
                encode_errors_.fetch_add(1, std::memory_order_relaxed);
                success = false;
                std::cerr << "[AudioEncoder] avcodec_receive_packet failed: " << ret << std::endl;
                break;
            }

            if (packet_callback_ && packet_->size > 0) {
                packet_callback_(packet_->data,
                    static_cast<uint32_t>(packet_->size),
                    packet_->pts);
                packets_emitted_.fetch_add(1, std::memory_order_relaxed);
            }
            av_packet_unref(packet_);
        }
        return success;
    }


    // Finalize
    //
    // Flushes any partial frame (zero-padded), drains the codec,
    // and frees all FFmpeg resources.

    bool AudioEncoder::Finalize()
    {
        if (lossless_float_ && initialized_) {
            accum_buf_.clear(); accum_frames_ = 0; initialized_ = false;
            return true;
        }
        if (!initialized_) return true;

        bool success = true;

        // Zero-pad and encode any partial frame remaining in the accumulator
        if (accum_frames_ > 0 && codec_ctx_) {
            const uint32_t frame_size = static_cast<uint32_t>(codec_ctx_->frame_size);
            const uint32_t pad_frames = frame_size - accum_frames_;
            float* dst = accum_buf_.data() + accum_frames_ * channels_;
            std::memset(dst, 0, static_cast<size_t>(pad_frames) * channels_ * sizeof(float));
            accum_frames_ = frame_size;
            if (!EncodeFrame()) success = false;
        }

        // Send NULL frame to flush codec's internal delay
        if (codec_ctx_) {
            const int send_result = avcodec_send_frame(codec_ctx_, nullptr);
            if (send_result < 0 && send_result != AVERROR_EOF) {
                flush_errors_.fetch_add(1, std::memory_order_relaxed);
                success = false;
            }
            int receive_result = send_result < 0 ? send_result : 0;
            while (receive_result >= 0) {
                receive_result = avcodec_receive_packet(codec_ctx_, packet_);
                if (receive_result == AVERROR(EAGAIN) || receive_result == AVERROR_EOF)
                    break;
                if (receive_result < 0) {
                    flush_errors_.fetch_add(1, std::memory_order_relaxed);
                    success = false;
                    break;
                }
                if (packet_callback_ && packet_->size > 0) {
                    packet_callback_(packet_->data,
                        static_cast<uint32_t>(packet_->size),
                        packet_->pts);
                    packets_emitted_.fetch_add(1, std::memory_order_relaxed);
                }
                av_packet_unref(packet_);
            }
        }

        if (packet_) { av_packet_free(&packet_);          packet_ = nullptr; }
        if (frame_) { av_frame_free(&frame_);             frame_ = nullptr; }
        if (codec_ctx_) { avcodec_free_context(&codec_ctx_); codec_ctx_ = nullptr; }

        accum_buf_.clear();
        accum_frames_ = 0;
        pts_samples_ = 0;
        initialized_ = false;

        std::cout << "[AudioEncoder] Finalized." << std::endl;
        return success;
    }



    std::vector<uint8_t> AudioEncoder::GetExtradata() const {
        return extradata_;
    }


} // namespace fthr
