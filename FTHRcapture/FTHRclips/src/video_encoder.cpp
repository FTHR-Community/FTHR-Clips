// video_encoder.cpp
// FTHR Capture Engine - x264 encoder implementation
//
// Phase 1 Optimization: Packet buffer pool
//   - Eliminates malloc/free in PushPacket() (60fps = 60 allocs/sec)
//   - Pre-allocates 128 buffers of 512KB each (~64MB total)
//   - Buffers returned to pool automatically via EncodedPacket destructor

#ifdef _MSC_VER
#if __has_include("pch.h")
#include "pch.h"
#elif __has_include("stdafx.h")
#include "stdafx.h"
#endif
#endif

#include "video_encoder.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libswscale/swscale.h>
#include <libavutil/opt.h>
#include <libavutil/imgutils.h>
}

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <iostream>
#include <cstring>

namespace fthr {

    // ---------------------------------------------------------------------------
    // EncodedPacket move operations
    // ---------------------------------------------------------------------------

    EncodedPacket::~EncodedPacket() {
        // Return buffer to pool if we own one
        if (pool_ref && buffer) {
            pool_ref->Release(buffer);
        }
    }

    EncodedPacket::EncodedPacket(EncodedPacket&& other) noexcept
        : buffer(other.buffer)
        , size(other.size)
        , pts(other.pts)
        , dts(other.dts)
        , duration(other.duration)
        , pool_ref(other.pool_ref)
    {
        other.buffer = nullptr;  // Transfer ownership
        other.pool_ref = nullptr;
    }

    EncodedPacket& EncodedPacket::operator=(EncodedPacket&& other) noexcept {
        if (this != &other) {
            // Return current buffer before taking ownership of new one
            if (pool_ref && buffer) {
                pool_ref->Release(buffer);
            }

            buffer = other.buffer;
            size = other.size;
            pts = other.pts;
            dts = other.dts;
            duration = other.duration;
            pool_ref = other.pool_ref;

            other.buffer = nullptr;
            other.pool_ref = nullptr;
        }
        return *this;
    }

    // ---------------------------------------------------------------------------
    // VideoEncoder construction
    // ---------------------------------------------------------------------------

    VideoEncoder::VideoEncoder()
        : format_ctx_(nullptr)
        , video_stream_(nullptr)
        , codec_ctx_(nullptr)
        , frame_(nullptr)
        , packet_(nullptr)
        , sws_ctx_(nullptr)
        , src_width_(0)
        , src_height_(0)
        , enc_width_(0)
        , enc_height_(0)
        , scaling_mode_(0)
        , fit_dst_x_(0)
        , fit_dst_y_(0)
        , fit_dst_w_(0)
        , fit_dst_h_(0)
        , pts_(0)
        , initialized_(false)
        , disk_running_(false)
    {
    }

    VideoEncoder::~VideoEncoder() {
        Finalize();
    }

    // ---------------------------------------------------------------------------
    // Initialize
    // ---------------------------------------------------------------------------

    bool VideoEncoder::Initialize(const wchar_t* output_path, const EncoderConfig& config) {
        if (initialized_) {
            std::cerr << "[VideoEncoder] Already initialized - call Finalize() first" << std::endl;
            Finalize();
        }

        pts_ = 0;

        // Resolve dimensions
        src_width_ = config.src_width;
        src_height_ = config.src_height;
        enc_width_ = (config.enc_width > 0) ? config.enc_width : config.src_width;
        enc_height_ = (config.enc_height > 0) ? config.enc_height : config.src_height;

        if (src_width_ == 0 || src_height_ == 0) {
            std::cerr << "[VideoEncoder] Invalid src dimensions: "
                << src_width_ << "x" << src_height_ << std::endl;
            return false;
        }

        // Compute fit-mode inscribed rect (preserve aspect, pad with black bars).
        // sws_scale doesn't pad on its own; we render into the inscribed rect of
        // a pre-blackened YUV420P frame each EncodeFrame call.
        scaling_mode_ = config.scaling_mode;
        if (scaling_mode_ == 1
            && enc_width_ > 0 && enc_height_ > 0
            && src_width_ > 0 && src_height_ > 0)
        {
            const double src_ar  = static_cast<double>(src_width_)  / static_cast<double>(src_height_);
            const double enc_ar  = static_cast<double>(enc_width_)  / static_cast<double>(enc_height_);
            uint32_t dst_w, dst_h;
            if (src_ar > enc_ar) {
                // source is wider — letterbox: full width, shorter height
                dst_w = enc_width_;
                dst_h = static_cast<uint32_t>(static_cast<double>(enc_width_) / src_ar);
            } else {
                // source is taller — pillarbox: full height, narrower width
                dst_h = enc_height_;
                dst_w = static_cast<uint32_t>(static_cast<double>(enc_height_) * src_ar);
            }
            // Force even dims (YUV420P chroma sub-sampling requires it)
            dst_w &= ~1u;
            dst_h &= ~1u;
            if (dst_w == 0) dst_w = 2;
            if (dst_h == 0) dst_h = 2;
            fit_dst_w_ = dst_w;
            fit_dst_h_ = dst_h;
            fit_dst_x_ = ((enc_width_  - dst_w) / 2u) & ~1u;
            fit_dst_y_ = ((enc_height_ - dst_h) / 2u) & ~1u;
            std::cout << "  Scaling      : Fit (letterbox) "
                      << fit_dst_w_ << "x" << fit_dst_h_
                      << " @ (" << fit_dst_x_ << "," << fit_dst_y_ << ")" << std::endl;
        } else {
            scaling_mode_ = 0;
            fit_dst_x_ = 0;
            fit_dst_y_ = 0;
            fit_dst_w_ = enc_width_;
            fit_dst_h_ = enc_height_;
        }

        // Convert output path to UTF-8
        char output_path_utf8[512] = { 0 };
        int conv_result = WideCharToMultiByte(
            CP_UTF8, 0, output_path, -1,
            output_path_utf8, sizeof(output_path_utf8) - 1,
            nullptr, nullptr);

        if (conv_result == 0) {
            std::cerr << "[VideoEncoder] Failed to convert output path to UTF-8" << std::endl;
            return false;
        }

        std::cout << "[VideoEncoder] Initializing..." << std::endl;
        std::cout << "  Src resolution : " << src_width_ << "x" << src_height_ << std::endl;
        std::cout << "  Enc resolution : " << enc_width_ << "x" << enc_height_ << std::endl;
        std::cout << "  FPS            : " << config.fps << std::endl;
        std::cout << "  Bitrate        : " << config.bitrate_kbps << " kbps" << std::endl;
        std::cout << "  Preset         : " << (config.preset ? config.preset : "superfast") << std::endl;
        std::cout << "  Tune           : " << (config.tune ? config.tune : "(none)") << std::endl;

        // Initialize packet buffer pool (Phase 1 optimization)
        packet_pool_ = std::make_unique<PacketBufferPool>();

        // Allocate format context
        avformat_alloc_output_context2(&format_ctx_, nullptr, nullptr, output_path_utf8);
        if (!format_ctx_) {
            std::cerr << "[VideoEncoder] Failed to allocate format context" << std::endl;
            return false;
        }

        // Find libx264 encoder
        const AVCodec* codec = avcodec_find_encoder(AV_CODEC_ID_H264);
        if (!codec) {
            std::cerr << "[VideoEncoder] libx264 not found - check FFmpeg build" << std::endl;
            return false;
        }

        // Create video stream
        video_stream_ = avformat_new_stream(format_ctx_, nullptr);
        if (!video_stream_) {
            std::cerr << "[VideoEncoder] Failed to create video stream" << std::endl;
            return false;
        }
        video_stream_->id = format_ctx_->nb_streams - 1;

        // Allocate and configure codec context
        codec_ctx_ = avcodec_alloc_context3(codec);
        if (!codec_ctx_) {
            std::cerr << "[VideoEncoder] Failed to allocate codec context" << std::endl;
            return false;
        }

        codec_ctx_->codec_id = AV_CODEC_ID_H264;
        codec_ctx_->codec_type = AVMEDIA_TYPE_VIDEO;
        codec_ctx_->width = static_cast<int>(enc_width_);
        codec_ctx_->height = static_cast<int>(enc_height_);
        codec_ctx_->time_base = AVRational{ 1, static_cast<int>(config.fps) };
        codec_ctx_->framerate = AVRational{ static_cast<int>(config.fps), 1 };
        codec_ctx_->pix_fmt = AV_PIX_FMT_YUV420P;

        // CBR rate control
        const int64_t bitrate_bps = static_cast<int64_t>(config.bitrate_kbps) * 1000;
        codec_ctx_->bit_rate = bitrate_bps;
        codec_ctx_->rc_min_rate = bitrate_bps;
        codec_ctx_->rc_max_rate = bitrate_bps;
        codec_ctx_->rc_buffer_size = static_cast<int>(bitrate_bps * 2);

        // GOP structure
        codec_ctx_->gop_size = static_cast<int>(config.fps);
        codec_ctx_->max_b_frames = 2;
        codec_ctx_->thread_count = 0;  // Auto-detect

        if (format_ctx_->oformat->flags & AVFMT_GLOBALHEADER) {
            codec_ctx_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        }

        // Apply x264 options
        const char* x264_preset = (config.preset && config.preset[0] != '\0')
            ? config.preset : "superfast";
        av_opt_set(codec_ctx_->priv_data, "preset", x264_preset, 0);

        if (config.tune && config.tune[0] != '\0') {
            av_opt_set(codec_ctx_->priv_data, "tune", config.tune, 0);
        }

        av_opt_set(codec_ctx_->priv_data, "profile", "high", 0);
        av_opt_set(codec_ctx_->priv_data, "nal-hrd", "cbr", 0);

        // Open codec
        int ret = avcodec_open2(codec_ctx_, codec, nullptr);
        if (ret < 0) {
            std::cerr << "[VideoEncoder] avcodec_open2 failed: " << ret << std::endl;
            return false;
        }

        // Copy codec parameters to stream
        ret = avcodec_parameters_from_context(video_stream_->codecpar, codec_ctx_);
        if (ret < 0) {
            std::cerr << "[VideoEncoder] avcodec_parameters_from_context failed: " << ret << std::endl;
            return false;
        }

        // Set stream time base
        video_stream_->time_base = codec_ctx_->time_base;

        // Allocate frame
        frame_ = av_frame_alloc();
        if (!frame_) {
            std::cerr << "[VideoEncoder] Failed to allocate AVFrame" << std::endl;
            return false;
        }

        frame_->format = codec_ctx_->pix_fmt;
        frame_->width = codec_ctx_->width;
        frame_->height = codec_ctx_->height;

        ret = av_frame_get_buffer(frame_, 0);
        if (ret < 0) {
            std::cerr << "[VideoEncoder] av_frame_get_buffer failed: " << ret << std::endl;
            return false;
        }

        // Allocate packet
        packet_ = av_packet_alloc();
        if (!packet_) {
            std::cerr << "[VideoEncoder] Failed to allocate AVPacket" << std::endl;
            return false;
        }

        // Open output file
        if (!(format_ctx_->oformat->flags & AVFMT_NOFILE)) {
            ret = avio_open(&format_ctx_->pb, output_path_utf8, AVIO_FLAG_WRITE);
            if (ret < 0) {
                std::cerr << "[VideoEncoder] avio_open failed: " << ret << std::endl;
                return false;
            }
        }

        // Write stream header
        ret = avformat_write_header(format_ctx_, nullptr);
        if (ret < 0) {
            std::cerr << "[VideoEncoder] avformat_write_header failed: " << ret << std::endl;
            return false;
        }

        // Create swscale context
        int sws_filter = SWS_BILINEAR;  // Default

        // Use higher quality filter if downscaling significantly
        if (enc_width_ < src_width_ && enc_height_ < src_height_) {
            double scale_ratio = static_cast<double>(enc_width_) / static_cast<double>(src_width_);
            if (scale_ratio < 0.75) {
                sws_filter = SWS_LANCZOS;
            }
        }

        // In fit mode we render only into the inscribed rect; sws produces
        // exactly fit_dst_w_ x fit_dst_h_ pixels, the rest of the YUV420P frame
        // is black-filled per EncodeFrame.
        const int sws_dst_w = static_cast<int>(scaling_mode_ == 1 ? fit_dst_w_ : enc_width_);
        const int sws_dst_h = static_cast<int>(scaling_mode_ == 1 ? fit_dst_h_ : enc_height_);

        sws_ctx_ = sws_getContext(
            static_cast<int>(src_width_),
            static_cast<int>(src_height_),
            AV_PIX_FMT_BGRA,
            sws_dst_w,
            sws_dst_h,
            AV_PIX_FMT_YUV420P,
            sws_filter,
            nullptr, nullptr, nullptr);

        if (!sws_ctx_) {
            std::cerr << "[VideoEncoder] sws_getContext failed" << std::endl;
            return false;
        }

        // Start disk writer thread
        disk_running_.store(true);
        disk_thread_ = std::thread(&VideoEncoder::DiskWriterThread, this);

        initialized_ = true;
        std::cout << "[VideoEncoder] Initialized successfully." << std::endl;
        return true;
    }

    // ---------------------------------------------------------------------------
    // Finalize
    // ---------------------------------------------------------------------------

    void VideoEncoder::Finalize() {
        if (!initialized_) return;

        std::cout << "[VideoEncoder] Finalizing..." << std::endl;

        // Flush encoder
        if (codec_ctx_) {
            avcodec_send_frame(codec_ctx_, nullptr);

            int flush_count = 0;
            while (avcodec_receive_packet(codec_ctx_, packet_) == 0) {
                av_packet_rescale_ts(packet_, codec_ctx_->time_base, video_stream_->time_base);
                packet_->stream_index = video_stream_->index;
                PushPacket(packet_);
                av_packet_unref(packet_);
                flush_count++;
            }
            std::cout << "[VideoEncoder] Flushed " << flush_count << " packets" << std::endl;
        }

        // Signal disk thread and wait for it to drain
        {
            std::lock_guard<std::mutex> lock(packet_mutex_);
            disk_running_.store(false);
        }
        packet_cv_.notify_all();

        if (disk_thread_.joinable()) {
            disk_thread_.join();
        }

        // Write trailer and close file
        if (format_ctx_) {
            av_write_trailer(format_ctx_);
            if (!(format_ctx_->oformat->flags & AVFMT_NOFILE)) {
                avio_closep(&format_ctx_->pb);
            }
        }

        // Free FFmpeg resources
        if (sws_ctx_) {
            sws_freeContext(sws_ctx_);
            sws_ctx_ = nullptr;
        }
        if (frame_) {
            av_frame_free(&frame_);
            frame_ = nullptr;
        }
        if (packet_) {
            av_packet_free(&packet_);
            packet_ = nullptr;
        }
        if (codec_ctx_) {
            avcodec_free_context(&codec_ctx_);
            codec_ctx_ = nullptr;
        }
        if (format_ctx_) {
            avformat_free_context(format_ctx_);
            format_ctx_ = nullptr;
        }

        // Release packet pool
        packet_pool_.reset();

        initialized_ = false;
        std::cout << "[VideoEncoder] Finalized." << std::endl;
    }

    // ---------------------------------------------------------------------------
    // EncodeFrame
    // ---------------------------------------------------------------------------

    bool VideoEncoder::EncodeFrame(const uint8_t* bgra_data) {
        if (!initialized_) {
            std::cerr << "[VideoEncoder] EncodeFrame called before Initialize()" << std::endl;
            return false;
        }

        if (av_frame_make_writable(frame_) < 0) {
            std::cerr << "[VideoEncoder] Failed to make frame writable" << std::endl;
            return false;
        }

        // Scale BGRA -> YUV420P
        const uint8_t* src_slices[1] = { bgra_data };
        int            src_strides[1] = { static_cast<int>(src_width_) * 4 };

        if (scaling_mode_ == 1) {
            // FIT mode — paint the whole frame black, then scale the source
            // into the inscribed rect (offset by fit_dst_x_, fit_dst_y_).
            // Y plane: 16 = video-range black. U/V planes: 128 = neutral chroma.
            std::memset(frame_->data[0], 16,
                static_cast<size_t>(frame_->linesize[0]) * static_cast<size_t>(enc_height_));
            std::memset(frame_->data[1], 128,
                static_cast<size_t>(frame_->linesize[1]) * static_cast<size_t>(enc_height_ / 2));
            std::memset(frame_->data[2], 128,
                static_cast<size_t>(frame_->linesize[2]) * static_cast<size_t>(enc_height_ / 2));

            uint8_t* dst_planes[3] = {
                frame_->data[0] + (fit_dst_y_       * frame_->linesize[0]) + fit_dst_x_,
                frame_->data[1] + ((fit_dst_y_ / 2) * frame_->linesize[1]) + (fit_dst_x_ / 2),
                frame_->data[2] + ((fit_dst_y_ / 2) * frame_->linesize[2]) + (fit_dst_x_ / 2),
            };
            int dst_strides[3] = {
                frame_->linesize[0],
                frame_->linesize[1],
                frame_->linesize[2],
            };
            sws_scale(sws_ctx_,
                src_slices, src_strides, 0, static_cast<int>(src_height_),
                dst_planes, dst_strides);
        } else {
            sws_scale(sws_ctx_,
                src_slices, src_strides, 0, static_cast<int>(src_height_),
                frame_->data, frame_->linesize);
        }

        frame_->pts = pts_++;

        // Send to x264
        int ret = avcodec_send_frame(codec_ctx_, frame_);
        if (ret < 0) {
            std::cerr << "[VideoEncoder] avcodec_send_frame failed: " << ret << std::endl;
            return false;
        }

        // Drain encoded packets
        while (ret >= 0) {
            ret = avcodec_receive_packet(codec_ctx_, packet_);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) {
                break;
            }
            if (ret < 0) {
                std::cerr << "[VideoEncoder] avcodec_receive_packet failed: " << ret << std::endl;
                return false;
            }

            av_packet_rescale_ts(packet_, codec_ctx_->time_base, video_stream_->time_base);
            packet_->stream_index = video_stream_->index;

            PushPacket(packet_);
            av_packet_unref(packet_);
        }

        return true;
    }

    // ---------------------------------------------------------------------------
    // PushPacket - PHASE 1 OPTIMIZATION: Use pool instead of resize
    //
    // OLD CODE (60 allocations/sec at 60fps):
    //   ep.data.resize(pkt->size);
    //   std::memcpy(ep.data.data(), pkt->data, pkt->size);
    //
    // NEW CODE (zero allocations):
    //   Acquire pre-allocated buffer from pool
    //   Fill it with packet data
    //   Transfer ownership to EncodedPacket (via release())
    //   Buffer automatically returned to pool when EncodedPacket is destroyed
    // ---------------------------------------------------------------------------

    void VideoEncoder::PushPacket(AVPacket* pkt) {
        // Acquire a pre-allocated buffer from the pool
        auto pooled_buf = packet_pool_->Acquire();

        // Resize to actual packet size (won't reallocate if size < capacity)
        pooled_buf->resize(static_cast<size_t>(pkt->size));

        // Copy packet data
        std::memcpy(pooled_buf->data(), pkt->data, static_cast<size_t>(pkt->size));

        // Create EncodedPacket and transfer buffer ownership
        EncodedPacket ep;
        ep.buffer = pooled_buf.release();      // Transfer ownership from RAII wrapper
        ep.size = static_cast<size_t>(pkt->size);
        ep.pts = pkt->pts;
        ep.dts = pkt->dts;
        ep.duration = pkt->duration;
        ep.pool_ref = packet_pool_.get();        // Non-owning pointer for destructor

        // pooled_buf now nullptr - won't return buffer to pool on destruction
        // EncodedPacket destructor will return it instead

        {
            std::lock_guard<std::mutex> lock(packet_mutex_);
            packet_queue_.push(std::move(ep));
        }
        packet_cv_.notify_one();
    }

    // ---------------------------------------------------------------------------
    // DiskWriterThread
    // ---------------------------------------------------------------------------

    void VideoEncoder::DiskWriterThread() {
        while (true) {
            std::unique_lock<std::mutex> lock(packet_mutex_);

            packet_cv_.wait(lock, [this] {
                return !packet_queue_.empty() || !disk_running_.load();
                });

            while (!packet_queue_.empty()) {
                EncodedPacket ep = std::move(packet_queue_.front());
                packet_queue_.pop();
                lock.unlock();

                // Write to disk
                AVPacket* write_pkt = av_packet_alloc();
                if (write_pkt) {
                    if (av_new_packet(write_pkt, static_cast<int>(ep.size)) == 0) {
                        std::memcpy(write_pkt->data, ep.buffer->data(), ep.size);
                        write_pkt->pts = ep.pts;
                        write_pkt->dts = ep.dts;
                        // ep.duration is int64_t; AVPacket::duration may be int on older FFmpeg
                        write_pkt->duration = static_cast<decltype(write_pkt->duration)>(ep.duration);
                        write_pkt->stream_index = video_stream_->index;

                        int ret = av_interleaved_write_frame(format_ctx_, write_pkt);
                        if (ret < 0) {
                            std::cerr << "[DiskWriter] av_interleaved_write_frame failed: "
                                << ret << std::endl;
                        }
                    }
                    av_packet_free(&write_pkt);
                }

                // ep destroyed here - buffer automatically returned to pool

                lock.lock();
            }

            if (!disk_running_.load() && packet_queue_.empty()) {
                break;
            }
        }
    }

} // namespace fthr