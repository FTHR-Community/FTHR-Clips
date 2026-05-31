#include "encoder.h"
#include <iostream>
#include <cstring>
#include <time.h>

extern "C" {
#include <libavutil/imgutils.h>
#include <libavutil/error.h>
}

namespace fthr {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static int64_t clock_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
}

// ---------------------------------------------------------------------------
// TryOpen — attempt to open one codec by name
// ---------------------------------------------------------------------------

bool Encoder::TryOpen(const char* codec_name, const EncoderConfig& cfg) {
    const AVCodec* codec = avcodec_find_encoder_by_name(codec_name);
    if (!codec) {
        return false;
    }

    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    if (!ctx) {
        return false;
    }

    ctx->width     = static_cast<int>(cfg.enc_width);
    ctx->height    = static_cast<int>(cfg.enc_height);
    ctx->time_base = { 1, static_cast<int>(cfg.fps) };
    ctx->framerate = { static_cast<int>(cfg.fps), 1 };
    ctx->bit_rate  = static_cast<int64_t>(cfg.bitrate_kbps) * 1000LL;
    ctx->gop_size  = static_cast<int>(cfg.fps) * 2;  // keyframe every 2 seconds
    ctx->max_b_frames = 0;  // no B-frames for low-latency ring buffer

    // Choose pixel format. NVENC/AMF/QSV prefer NV12; libx264 uses YUV420P.
    bool is_hw = (strstr(codec_name, "nvenc") || strstr(codec_name, "amf") ||
                  strstr(codec_name, "qsv"));
    ctx->pix_fmt = is_hw ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;

    // Codec-specific options
    if (strstr(codec_name, "nvenc")) {
        av_opt_set(ctx->priv_data, "preset",   "p2",  0);
        av_opt_set(ctx->priv_data, "tune",     "ll",  0);
        av_opt_set(ctx->priv_data, "rc",       "vbr", 0);
        av_opt_set(ctx->priv_data, "cbr",      "0",   0);
    } else if (strstr(codec_name, "amf")) {
        av_opt_set(ctx->priv_data, "quality",  "speed", 0);
        av_opt_set(ctx->priv_data, "rc",       "vbr_latency", 0);
    } else if (strstr(codec_name, "qsv")) {
        av_opt_set(ctx->priv_data, "preset",   "veryfast", 0);
        av_opt_set(ctx->priv_data, "look_ahead", "0",      0);
    } else {
        // libx264
        av_opt_set(ctx->priv_data, "preset",   "veryfast",    0);
        av_opt_set(ctx->priv_data, "tune",     "zerolatency", 0);
    }

    // Request annexb/global header for MP4 muxing
    ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    if (avcodec_open2(ctx, codec, nullptr) < 0) {
        avcodec_free_context(&ctx);
        return false;
    }

    codec_ctx_ = ctx;
    cfg_       = cfg;
    return true;
}

// ---------------------------------------------------------------------------
// Open — try codecs in priority order
// ---------------------------------------------------------------------------

bool Encoder::Open(const EncoderConfig& cfg, std::string& codec_used_out) {
    static const char* kCodecPriority[] = {
        "h264_nvenc",
        "h264_amf",
        "h264_qsv",
        "libx264",
        nullptr,
    };

    for (int i = 0; kCodecPriority[i]; ++i) {
        std::cout << "[Encoder] Trying codec: " << kCodecPriority[i] << std::endl;
        if (TryOpen(kCodecPriority[i], cfg)) {
            codec_used_out = kCodecPriority[i];
            std::cout << "[Encoder] Using: " << kCodecPriority[i]
                      << "  " << cfg.enc_width << "x" << cfg.enc_height
                      << "  " << cfg.fps << "fps  "
                      << cfg.bitrate_kbps << "kbps" << std::endl;
            break;
        }
    }

    if (!codec_ctx_) {
        std::cerr << "[Encoder] All codecs failed — no encoder available" << std::endl;
        return false;
    }

    // Build SwsContext for BGRA -> pixel format conversion
    AVPixelFormat dst_fmt = codec_ctx_->pix_fmt;
    sws_ctx_ = sws_getContext(
        static_cast<int>(cfg.src_width),
        static_cast<int>(cfg.src_height),
        AV_PIX_FMT_BGRA,
        codec_ctx_->width,
        codec_ctx_->height,
        dst_fmt,
        SWS_BILINEAR, nullptr, nullptr, nullptr
    );
    if (!sws_ctx_) {
        std::cerr << "[Encoder] sws_getContext failed" << std::endl;
        Close();
        return false;
    }

    // Allocate reusable YUV frame
    yuv_frame_ = av_frame_alloc();
    if (!yuv_frame_) {
        Close();
        return false;
    }
    yuv_frame_->format = dst_fmt;
    yuv_frame_->width  = codec_ctx_->width;
    yuv_frame_->height = codec_ctx_->height;
    if (av_frame_get_buffer(yuv_frame_, 32) < 0) {
        std::cerr << "[Encoder] av_frame_get_buffer failed" << std::endl;
        Close();
        return false;
    }

    // Allocate packet
    pkt_ = av_packet_alloc();
    if (!pkt_) {
        Close();
        return false;
    }

    next_pts_ = 0;
    return true;
}

// ---------------------------------------------------------------------------
// Close
// ---------------------------------------------------------------------------

void Encoder::Close() {
    if (pkt_)       { av_packet_free(&pkt_);       }
    if (yuv_frame_) { av_frame_free(&yuv_frame_);  }
    if (sws_ctx_)   { sws_freeContext(sws_ctx_);   sws_ctx_   = nullptr; }
    if (codec_ctx_) { avcodec_free_context(&codec_ctx_); }
}

// ---------------------------------------------------------------------------
// EncodeFrame
// ---------------------------------------------------------------------------

bool Encoder::EncodeFrame(const uint8_t* bgra, uint32_t stride,
                           int64_t wall_time_ns, PushFn push_fn) {
    if (!codec_ctx_ || !sws_ctx_ || !yuv_frame_ || !pkt_)
        return false;

    if (av_frame_make_writable(yuv_frame_) < 0)
        return false;

    // Convert BGRA -> YUV
    const uint8_t* src_planes[4] = { bgra, nullptr, nullptr, nullptr };
    int src_strides[4] = { static_cast<int>(stride), 0, 0, 0 };
    sws_scale(sws_ctx_,
              src_planes, src_strides,
              0, static_cast<int>(cfg_.src_height),
              yuv_frame_->data, yuv_frame_->linesize);

    yuv_frame_->pts = next_pts_++;

    int ret = avcodec_send_frame(codec_ctx_, yuv_frame_);
    if (ret < 0) {
        char errbuf[128];
        av_strerror(ret, errbuf, sizeof(errbuf));
        std::cerr << "[Encoder] avcodec_send_frame: " << errbuf << std::endl;
        return false;
    }

    while (true) {
        ret = avcodec_receive_packet(codec_ctx_, pkt_);
        if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF)
            break;
        if (ret < 0) {
            char errbuf[128];
            av_strerror(ret, errbuf, sizeof(errbuf));
            std::cerr << "[Encoder] avcodec_receive_packet: " << errbuf << std::endl;
            return false;
        }

        EncodedPacket ep;
        ep.data.assign(pkt_->data, pkt_->data + pkt_->size);
        ep.pts          = pkt_->pts;
        ep.dts          = pkt_->dts;
        ep.is_keyframe  = (pkt_->flags & AV_PKT_FLAG_KEY) != 0;
        ep.wall_time_ns = wall_time_ns;

        av_packet_unref(pkt_);
        push_fn(std::move(ep));
    }

    return true;
}

// ---------------------------------------------------------------------------
// GetExtradata
// ---------------------------------------------------------------------------

std::vector<uint8_t> Encoder::GetExtradata() const {
    if (!codec_ctx_ || !codec_ctx_->extradata || codec_ctx_->extradata_size <= 0)
        return {};
    return std::vector<uint8_t>(
        codec_ctx_->extradata,
        codec_ctx_->extradata + codec_ctx_->extradata_size
    );
}

} // namespace fthr
