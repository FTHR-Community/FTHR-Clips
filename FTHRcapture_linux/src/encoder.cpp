#include "encoder.h"
#include <algorithm>
#include <iostream>
#include <cstring>
#include <iterator>
#include <cstdlib>


#include <dlfcn.h>

extern "C" {
#include <libavutil/imgutils.h>
#include <libavutil/error.h>
#include <libavutil/hwcontext.h>
}

namespace fthr {

static bool IsVaapiSymbolSupported() {
    void* h = dlopen("libva.so.2", RTLD_LAZY | RTLD_LOCAL);
    if (!h) return false;
    void* sym = dlsym(h, "vaMapBuffer2");
    if (!sym) {
        sym = dlsym(h, "vaMapBuffer");
    }
    dlclose(h);
    return sym != nullptr;
}

// ApplyPreset — maps P1–P7 to vendor-specific preset strings

bool ApplyPreset(AVCodecContext* ctx, const char* codec_name, int p) {
    if (p < 1) p = 1;
    if (p > 7) p = 7;

    if (strstr(codec_name, "nvenc")) {
        char ps[3] = {'p', static_cast<char>('0' + p), '\0'};
        av_opt_set(ctx->priv_data, "preset", ps,     0);
        av_opt_set(ctx->priv_data, "tune",   "ll",   0);
        av_opt_set(ctx->priv_data, "rc",     "vbr",  0);
        av_opt_set(ctx->priv_data, "cbr",    "0",    0);
        av_opt_set(ctx->priv_data, "forced-idr", "1", 0);
    } else if (strstr(codec_name, "amf")) {
        static const char* kAmf[] = {
            "speed","speed","balanced","balanced","balanced","quality","quality"};
        av_opt_set(ctx->priv_data, "quality", kAmf[p - 1],   0);
        av_opt_set(ctx->priv_data, "rc",      "vbr_latency", 0);
    } else if (strstr(codec_name, "qsv")) {
        static const char* kQsv[] = {
            "veryfast","fast","medium","medium","slow","slower","slowest"};
        av_opt_set(ctx->priv_data, "preset",     kQsv[p - 1], 0);
        av_opt_set(ctx->priv_data, "look_ahead", "0",         0);
    } else if (strstr(codec_name, "openh264")) {
        // Set explicit OpenH264 rate control so bit_rate is respected. Disable
        // frame skipping to keep video aligned with audio muxed after capture.
        av_opt_set(ctx->priv_data, "rc_mode",           "bitrate", 0);
        av_opt_set(ctx->priv_data, "allow_skip_frames", "0",       0);
        av_opt_set(ctx->priv_data, "profile",           "high",    0);
        ctx->max_b_frames = 0;   // OpenH264 does not produce B-frames
    } else if (strstr(codec_name, "kvazaar")) {
        // kvazaar exposes x265-style preset names through a single option
        // string; anything it does not recognise is ignored rather than fatal.
        static const char* kKvz[] = {
            "ultrafast","superfast","veryfast","fast","medium","slow","veryslow"};
        av_opt_set(ctx->priv_data, "preset", kKvz[p - 1], 0);
    } else if (strstr(codec_name, "libx26")) {
        // Retained for distro FFmpeg builds that still ship x264/x265. FTHR
        // never selects these itself (see BuildCodecList) — this branch only
        // matters if a downstream packager re-adds them deliberately.
        static const char* kX26x[] = {
            "ultrafast","superfast","veryfast","fast","medium","slow","veryslow"};
        av_opt_set(ctx->priv_data, "preset", kX26x[p - 1],  0);
        av_opt_set(ctx->priv_data, "tune",   "zerolatency", 0);
    } else if (strstr(codec_name, "svtav1")) {
        static const int kSvt[] = {12, 9, 7, 6, 4, 1, 0};
        char ps[4];
        snprintf(ps, sizeof(ps), "%d", kSvt[p - 1]);
        av_opt_set(ctx->priv_data, "preset", ps, 0);
    } else if (strstr(codec_name, "libaom")) {
        // libaom-av1 has no mapping for this preset scale.
    } else {
        return false;   // unrecognised codec_name
    }
    return true;
}

// TryOpen — attempt to open one codec by name

bool Encoder::TryOpen(const char* codec_name, const EncoderConfig& cfg) {
    const AVCodec* codec = avcodec_find_encoder_by_name(codec_name);
    if (!codec) return false;

    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    if (!ctx) return false;

    ctx->width        = static_cast<int>(cfg.enc_width);
    ctx->height       = static_cast<int>(cfg.enc_height);
    ctx->time_base    = { 1, static_cast<int>(cfg.fps) };
    ctx->framerate    = { static_cast<int>(cfg.fps), 1 };
    ctx->bit_rate     = static_cast<int64_t>(cfg.bitrate_kbps) * 1000LL;
    ctx->gop_size     = static_cast<int>(cfg.fps) * 2;
    ctx->max_b_frames = 0;
    // Keep Linux's output contract identical to the Windows encoders:
    // desktop BGRA is full-range, encoded SDR video is studio-range BT.709.
    ctx->color_range = AVCOL_RANGE_MPEG;
    ctx->color_primaries = AVCOL_PRI_BT709;
    ctx->color_trc = AVCOL_TRC_BT709;
    ctx->colorspace = AVCOL_SPC_BT709;
    ctx->chroma_sample_location = AVCHROMA_LOC_LEFT;

    const bool is_vaapi = strstr(codec_name, "_vaapi") != nullptr;
    const bool is_hw = (strstr(codec_name, "nvenc") || strstr(codec_name, "amf") ||
                        strstr(codec_name, "qsv") || is_vaapi);
    AVBufferRef* device = nullptr;
    AVBufferRef* frames = nullptr;
    if (is_vaapi) {
        if (!IsVaapiSymbolSupported()) {
            std::cerr << "[Encoder] VA-API runtime library missing required buffer mapping symbols — skipping VA-API" << std::endl;
            avcodec_free_context(&ctx);
            return false;
        }
        const char* device_path = std::getenv("FTHR_VAAPI_DEVICE");
        if (!device_path || !*device_path) device_path = "/dev/dri/renderD128";
        if (av_hwdevice_ctx_create(&device, AV_HWDEVICE_TYPE_VAAPI,
                                   device_path, nullptr, 0) < 0) {
            avcodec_free_context(&ctx);
            return false;
        }
        frames = av_hwframe_ctx_alloc(device);
        if (!frames) {
            av_buffer_unref(&device);
            avcodec_free_context(&ctx);
            return false;
        }
        auto* frames_ctx = reinterpret_cast<AVHWFramesContext*>(frames->data);
        frames_ctx->format = AV_PIX_FMT_VAAPI;
        frames_ctx->sw_format = AV_PIX_FMT_NV12;
        frames_ctx->width = ctx->width;
        frames_ctx->height = ctx->height;
        frames_ctx->initial_pool_size = 8;
        if (av_hwframe_ctx_init(frames) < 0) {
            av_buffer_unref(&frames);
            av_buffer_unref(&device);
            avcodec_free_context(&ctx);
            return false;
        }
        ctx->hw_device_ctx = av_buffer_ref(device);
        ctx->hw_frames_ctx = av_buffer_ref(frames);
        ctx->pix_fmt = AV_PIX_FMT_VAAPI;
    } else {
        ctx->pix_fmt = is_hw ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
    }

    ApplyPreset(ctx, codec_name, cfg.preset);
    ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    if (avcodec_open2(ctx, codec, nullptr) < 0) {
        av_buffer_unref(&frames);
        av_buffer_unref(&device);
        avcodec_free_context(&ctx);
        return false;
    }

    codec_ctx_ = ctx;
    hw_device_ctx_ = device;
    hw_frames_ctx_ = frames;
    cfg_ = cfg;
    return true;
}

// Build priority list for Open()

static void BuildCodecList(
    CodecPref codec_pref,
    EncoderPref encoder_pref,
    std::vector<const char*>& out) {
    auto codec_allowed = [&](CodecPref codec) {
        return codec_pref == CodecPref::Auto || codec_pref == codec;
    };
    auto backend_allowed = [&](EncoderPref backend) {
        return encoder_pref == EncoderPref::Auto || encoder_pref == backend;
    };
    auto add_backend = [&](EncoderPref backend) {
        if (!backend_allowed(backend)) return;
        if (backend == EncoderPref::Nvidia) {
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_nvenc");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_nvenc");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_nvenc");
        } else if (backend == EncoderPref::Amd) {
            // VA-API is the native Linux AMD path. AMF remains a fallback for
            // systems that provide AMD's separate AMF runtime.
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_vaapi");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_vaapi");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_vaapi");
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_amf");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_amf");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_amf");
        } else if (backend == EncoderPref::Intel) {
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_qsv");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_qsv");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_qsv");
        } else if (backend == EncoderPref::Software) {
            // LGPL-compatible software fallbacks only.
            if (codec_allowed(CodecPref::H264)) out.push_back("libopenh264");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("libkvazaar");
            if (codec_allowed(CodecPref::AV1)) {
                out.push_back("libsvtav1");
                out.push_back("libaom-av1");
                out.push_back("librav1e");
            }
        }
    };

    // Auto is backend-first so a working hardware H.264 encoder wins over a
    // much slower software AV1 encoder discovered earlier by codec family.
    add_backend(EncoderPref::Nvidia);
    add_backend(EncoderPref::Amd);
    add_backend(EncoderPref::Intel);
    add_backend(EncoderPref::Software);
}

// Open — try codecs in priority order

bool Encoder::Open(const EncoderConfig& cfg, std::string& codec_used_out) {
    std::vector<const char*> candidates;
    BuildCodecList(cfg.codec_pref, cfg.encoder_pref, candidates);

    for (const char* name : candidates) {
        std::cout << "[Encoder] Trying codec: " << name << std::endl;
        if (TryOpen(name, cfg)) {
            codec_used_out = name;
            std::cout << "[Encoder] Using: " << name
                      << "  " << cfg.enc_width << "x" << cfg.enc_height
                      << "  " << cfg.fps << "fps  "
                      << cfg.bitrate_kbps << "kbps"
                      << "  preset=P" << cfg.preset << std::endl;
            break;
        }
    }

    if (!codec_ctx_) {
        std::cerr << "[Encoder] All codecs failed — no encoder available" << std::endl;
        return false;
    }

    const bool is_vaapi = IsVaapi();
    AVPixelFormat dst_fmt = is_vaapi ? AV_PIX_FMT_NV12 : codec_ctx_->pix_fmt;
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
    const int* bt709 = sws_getCoefficients(SWS_CS_ITU709);
    if (!bt709 || sws_setColorspaceDetails(
            sws_ctx_, bt709, 1, bt709, 0, 0, 1 << 16, 1 << 16) < 0) {
        std::cerr << "[Encoder] Failed to configure explicit full-range BGRA -> "
                     "studio-range BT.709 conversion" << std::endl;
        Close();
        return false;
    }

    yuv_frame_ = av_frame_alloc();
    if (!yuv_frame_) { Close(); return false; }
    yuv_frame_->format = dst_fmt;
    yuv_frame_->width  = codec_ctx_->width;
    yuv_frame_->height = codec_ctx_->height;
    if (av_frame_get_buffer(yuv_frame_, 32) < 0) {
        std::cerr << "[Encoder] av_frame_get_buffer failed" << std::endl;
        Close();
        return false;
    }
    if (is_vaapi) {
        hw_frame_ = av_frame_alloc();
        if (!hw_frame_) { Close(); return false; }
    }

    pkt_ = av_packet_alloc();
    if (!pkt_) { Close(); return false; }

    have_pts_epoch_ = false;
    pts_epoch_ns_ = 0;
    last_input_pts_ = -1;
    last_forced_keyframe_pts_ = -1;
    pending_timings_.clear();
    return true;
}


void Encoder::Close() {
    if (pkt_)       { av_packet_free(&pkt_);       }
    if (hw_frame_)  { av_frame_free(&hw_frame_);  }
    if (yuv_frame_) { av_frame_free(&yuv_frame_);  }
    if (sws_ctx_)   { sws_freeContext(sws_ctx_);   sws_ctx_   = nullptr; }
    if (codec_ctx_) { avcodec_free_context(&codec_ctx_); }
    av_buffer_unref(&hw_frames_ctx_);
    av_buffer_unref(&hw_device_ctx_);
    have_pts_epoch_ = false;
    pts_epoch_ns_ = 0;
    last_input_pts_ = -1;
    last_forced_keyframe_pts_ = -1;
    pending_timings_.clear();
}


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

    // Derive presentation time from CLOCK_MONOTONIC capture time. A frame
    // counter makes a five-second replay shorter whenever capture drops or is
    // delayed, because the muxer still interprets every increment as 1/fps.
    int64_t frame_pts = 0;
    if (!have_pts_epoch_) {
        have_pts_epoch_ = true;
        pts_epoch_ns_ = wall_time_ns;
    } else {
        const int64_t elapsed_ns = std::max<int64_t>(0, wall_time_ns - pts_epoch_ns_);
        const int64_t whole_seconds = elapsed_ns / 1'000'000'000LL;
        const int64_t remainder_ns = elapsed_ns % 1'000'000'000LL;
        frame_pts = whole_seconds * static_cast<int64_t>(cfg_.fps)
            + (remainder_ns * static_cast<int64_t>(cfg_.fps) + 500'000'000LL)
                / 1'000'000'000LL;
    }
    frame_pts = std::max(frame_pts, last_input_pts_ + 1);
    last_input_pts_ = frame_pts;
    yuv_frame_->pts = frame_pts;
    const bool force_keyframe = last_forced_keyframe_pts_ < 0
        || frame_pts - last_forced_keyframe_pts_
            >= static_cast<int64_t>(cfg_.fps) * 2;
    yuv_frame_->pict_type = force_keyframe
        ? AV_PICTURE_TYPE_I
        : AV_PICTURE_TYPE_NONE;
    if (force_keyframe)
        yuv_frame_->flags |= AV_FRAME_FLAG_KEY;
    else
        yuv_frame_->flags &= ~AV_FRAME_FLAG_KEY;

    AVFrame* frame_to_encode = yuv_frame_;
    if (IsVaapi()) {
        av_frame_unref(hw_frame_);
        if (av_hwframe_get_buffer(codec_ctx_->hw_frames_ctx, hw_frame_, 0) < 0 ||
            av_hwframe_transfer_data(hw_frame_, yuv_frame_, 0) < 0 ||
            av_frame_copy_props(hw_frame_, yuv_frame_) < 0) {
            std::cerr << "[Encoder] VA-API frame upload failed" << std::endl;
            return false;
        }
        frame_to_encode = hw_frame_;
    }

    int ret = avcodec_send_frame(codec_ctx_, frame_to_encode);
    if (ret < 0) {
        char errbuf[128];
        av_strerror(ret, errbuf, sizeof(errbuf));
        std::cerr << "[Encoder] avcodec_send_frame: " << errbuf << std::endl;
        return false;
    }
    pending_timings_.push_back({frame_pts, wall_time_ns});
    if (force_keyframe) last_forced_keyframe_pts_ = frame_pts;

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
        // Encoders may delay output. Associate the packet with the input frame
        // carrying its PTS instead of the most recently submitted frame.
        ep.wall_time_ns = wall_time_ns;
        for (auto it = pending_timings_.begin(); it != pending_timings_.end(); ++it) {
            if (it->pts != pkt_->pts) continue;
            ep.wall_time_ns = it->wall_time_ns;
            pending_timings_.erase(pending_timings_.begin(), std::next(it));
            break;
        }

        av_packet_unref(pkt_);
        push_fn(std::move(ep));
    }

    return true;
}


std::vector<uint8_t> Encoder::GetExtradata() const {
    if (!codec_ctx_ || !codec_ctx_->extradata || codec_ctx_->extradata_size <= 0)
        return {};
    return std::vector<uint8_t>(
        codec_ctx_->extradata,
        codec_ctx_->extradata + codec_ctx_->extradata_size
    );
}

} // namespace fthr
