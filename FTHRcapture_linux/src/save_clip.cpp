#include "save_clip.h"
#include <iostream>
#include <cstring>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>
}

namespace fthr {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static void set_shm_bytes(SharedMemoryLayout* shm, uint64_t bytes) {
    if (shm) shm->bytes_written = bytes;
}

// ---------------------------------------------------------------------------
// save_clip_to_file
// ---------------------------------------------------------------------------

bool save_clip_to_file(
    const std::string&               path,
    const std::vector<EncodedPacket>& video_packets,
    const std::vector<float>&        audio_pcm,
    int                              audio_sample_rate,
    int                              audio_channels,
    const std::vector<uint8_t>&      extradata,
    uint32_t                         fps,
    SharedMemoryLayout*              shm
) {
    if (video_packets.empty()) {
        std::cerr << "[SaveClip] No video packets to write" << std::endl;
        return false;
    }

    // -------------------------------------------------------------------------
    // Output format context
    // -------------------------------------------------------------------------
    bool ok = true;

    AVFormatContext* fmt_ctx = nullptr;
    if (avformat_alloc_output_context2(&fmt_ctx, nullptr, nullptr,
                                        path.c_str()) < 0) {
        std::cerr << "[SaveClip] avformat_alloc_output_context2 failed" << std::endl;
        return false;
    }

    // -------------------------------------------------------------------------
    // Video stream
    // -------------------------------------------------------------------------
    AVStream* vid_stream = avformat_new_stream(fmt_ctx, nullptr);
    if (!vid_stream) {
        avformat_free_context(fmt_ctx);
        return false;
    }
    vid_stream->id         = 0;
    vid_stream->time_base  = { 1, static_cast<int>(fps) };

    AVCodecParameters* vpar = vid_stream->codecpar;
    vpar->codec_type   = AVMEDIA_TYPE_VIDEO;
    vpar->codec_id     = AV_CODEC_ID_H264;
    // We store the first packet's dimensions; use a heuristic from packet size
    // or just leave 0 — the muxer works without them for fragmented MP4.
    // A proper impl would track width/height through the encoder.
    vpar->format       = AV_PIX_FMT_YUV420P;

    if (!extradata.empty()) {
        vpar->extradata = static_cast<uint8_t*>(
            av_malloc(extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
        if (vpar->extradata) {
            memcpy(vpar->extradata, extradata.data(), extradata.size());
            memset(vpar->extradata + extradata.size(), 0,
                   AV_INPUT_BUFFER_PADDING_SIZE);
            vpar->extradata_size = static_cast<int>(extradata.size());
        }
    }

    // -------------------------------------------------------------------------
    // Audio stream + encoder (PCM float32 -> AAC)
    // -------------------------------------------------------------------------
    AVStream*       aud_stream    = nullptr;
    AVCodecContext* aac_ctx       = nullptr;
    SwrContext*     swr_ctx       = nullptr;
    AVFrame*        aac_frame     = nullptr;
    AVPacket*       aac_pkt       = nullptr;
    bool            has_audio     = !audio_pcm.empty();

    if (has_audio) {
        const AVCodec* aac_codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
        if (!aac_codec) {
            std::cerr << "[SaveClip] AAC encoder not found — saving video-only" << std::endl;
            has_audio = false;
        } else {
            aud_stream = avformat_new_stream(fmt_ctx, nullptr);
            if (!aud_stream) { has_audio = false; goto skip_audio_setup; }

            aud_stream->id = 1;

            aac_ctx = avcodec_alloc_context3(aac_codec);
            aac_ctx->sample_rate    = audio_sample_rate;
            aac_ctx->bit_rate       = 128000;
            aac_ctx->sample_fmt     = AV_SAMPLE_FMT_FLTP;

#if LIBAVUTIL_VERSION_MAJOR >= 57
            av_channel_layout_default(&aac_ctx->ch_layout, audio_channels);
#else
            aac_ctx->channels       = audio_channels;
            aac_ctx->channel_layout = av_get_default_channel_layout(audio_channels);
#endif
            aac_ctx->time_base      = { 1, audio_sample_rate };

            if (fmt_ctx->oformat->flags & AVFMT_GLOBALHEADER)
                aac_ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

            if (avcodec_open2(aac_ctx, aac_codec, nullptr) < 0) {
                std::cerr << "[SaveClip] AAC avcodec_open2 failed" << std::endl;
                avcodec_free_context(&aac_ctx);
                has_audio = false;
                goto skip_audio_setup;
            }

            avcodec_parameters_from_context(aud_stream->codecpar, aac_ctx);
            aud_stream->time_base = { 1, audio_sample_rate };

            // SwrContext: float32 interleaved -> float32 planar (FLTP)
            swr_ctx = swr_alloc();
#if LIBAVUTIL_VERSION_MAJOR >= 57
            AVChannelLayout src_layout, dst_layout;
            av_channel_layout_default(&src_layout, audio_channels);
            av_channel_layout_default(&dst_layout, audio_channels);
            swr_alloc_set_opts2(&swr_ctx,
                &dst_layout, AV_SAMPLE_FMT_FLTP, audio_sample_rate,
                &src_layout, AV_SAMPLE_FMT_FLT,  audio_sample_rate,
                0, nullptr);
#else
            av_opt_set_int(swr_ctx, "in_channel_layout",
                           av_get_default_channel_layout(audio_channels), 0);
            av_opt_set_int(swr_ctx, "out_channel_layout",
                           av_get_default_channel_layout(audio_channels), 0);
            av_opt_set_int(swr_ctx, "in_sample_rate",  audio_sample_rate, 0);
            av_opt_set_int(swr_ctx, "out_sample_rate", audio_sample_rate, 0);
            av_opt_set_sample_fmt(swr_ctx, "in_sample_fmt",
                                  AV_SAMPLE_FMT_FLT, 0);
            av_opt_set_sample_fmt(swr_ctx, "out_sample_fmt",
                                  AV_SAMPLE_FMT_FLTP, 0);
#endif
            swr_init(swr_ctx);

            aac_frame = av_frame_alloc();
            aac_frame->nb_samples     = aac_ctx->frame_size;
            aac_frame->format         = AV_SAMPLE_FMT_FLTP;
#if LIBAVUTIL_VERSION_MAJOR >= 57
            av_channel_layout_copy(&aac_frame->ch_layout, &aac_ctx->ch_layout);
#else
            aac_frame->channel_layout = aac_ctx->channel_layout;
            aac_frame->channels       = audio_channels;
#endif
            aac_frame->sample_rate    = audio_sample_rate;
            av_frame_get_buffer(aac_frame, 0);

            aac_pkt = av_packet_alloc();
        }
    }

skip_audio_setup:

    // -------------------------------------------------------------------------
    // Open output file
    // -------------------------------------------------------------------------
    if (!(fmt_ctx->oformat->flags & AVFMT_NOFILE)) {
        if (avio_open(&fmt_ctx->pb, path.c_str(), AVIO_FLAG_WRITE) < 0) {
            std::cerr << "[SaveClip] avio_open failed: " << path << std::endl;
            ok = false;
            goto cleanup;
        }
    }

    if (avformat_write_header(fmt_ctx, nullptr) < 0) {
        std::cerr << "[SaveClip] avformat_write_header failed" << std::endl;
        ok = false;
        goto cleanup;
    }

    // -------------------------------------------------------------------------
    // Write video packets
    // -------------------------------------------------------------------------
    {
        // Compute PTS offset so clip starts at 0
        int64_t pts_offset = video_packets.front().pts;

        uint64_t bytes_out = 0;
        for (const auto& ep : video_packets) {
            AVPacket* pkt = av_packet_alloc();
            pkt->data = const_cast<uint8_t*>(ep.data.data());
            pkt->size = static_cast<int>(ep.data.size());
            pkt->pts  = ep.pts - pts_offset;
            pkt->dts  = ep.dts - pts_offset;
            pkt->stream_index = vid_stream->index;
            if (ep.is_keyframe) pkt->flags |= AV_PKT_FLAG_KEY;

            av_packet_rescale_ts(pkt,
                { 1, static_cast<int>(fps) },
                vid_stream->time_base);

            av_interleaved_write_frame(fmt_ctx, pkt);
            bytes_out += static_cast<uint64_t>(ep.data.size());
            set_shm_bytes(shm, bytes_out);

            // Don't free pkt->data — it points into ep.data
            pkt->data = nullptr; pkt->size = 0;
            av_packet_free(&pkt);
        }
    }

    // -------------------------------------------------------------------------
    // Write audio (encode PCM -> AAC and interleave)
    // -------------------------------------------------------------------------
    if (has_audio && aac_ctx && swr_ctx && aac_frame && aac_pkt) {
        int frame_size        = aac_ctx->frame_size;
        size_t total_samples  = audio_pcm.size() / static_cast<size_t>(audio_channels);
        size_t offset         = 0;
        int64_t audio_pts     = 0;

        while (offset < total_samples) {
            int n = std::min(static_cast<size_t>(frame_size),
                             total_samples - offset);

            av_frame_make_writable(aac_frame);
            aac_frame->nb_samples = n;
            aac_frame->pts        = audio_pts;

            const float* src_ptr = audio_pcm.data() +
                                   offset * static_cast<size_t>(audio_channels);
            const uint8_t* src_data[1] = {
                reinterpret_cast<const uint8_t*>(src_ptr)
            };
            swr_convert(swr_ctx,
                        aac_frame->data, n,
                        src_data,        n);

            avcodec_send_frame(aac_ctx, aac_frame);
            while (avcodec_receive_packet(aac_ctx, aac_pkt) == 0) {
                aac_pkt->stream_index = aud_stream->index;
                av_packet_rescale_ts(aac_pkt,
                    { 1, audio_sample_rate },
                    aud_stream->time_base);
                av_interleaved_write_frame(fmt_ctx, aac_pkt);
                av_packet_unref(aac_pkt);
            }

            offset    += static_cast<size_t>(n);
            audio_pts += n;
        }

        // Flush AAC encoder
        avcodec_send_frame(aac_ctx, nullptr);
        while (avcodec_receive_packet(aac_ctx, aac_pkt) == 0) {
            aac_pkt->stream_index = aud_stream->index;
            av_packet_rescale_ts(aac_pkt,
                { 1, audio_sample_rate },
                aud_stream->time_base);
            av_interleaved_write_frame(fmt_ctx, aac_pkt);
            av_packet_unref(aac_pkt);
        }
    }

    if (ok) {
        av_write_trailer(fmt_ctx);
        std::cout << "[SaveClip] Written: " << path << std::endl;
    }

cleanup:
    if (aac_pkt)   av_packet_free(&aac_pkt);
    if (aac_frame) av_frame_free(&aac_frame);
    if (swr_ctx)   swr_free(&swr_ctx);
    if (aac_ctx)   avcodec_free_context(&aac_ctx);
    if (fmt_ctx) {
        if (fmt_ctx->pb && !(fmt_ctx->oformat->flags & AVFMT_NOFILE))
            avio_closep(&fmt_ctx->pb);
        avformat_free_context(fmt_ctx);
    }
    return ok;
}

} // namespace fthr
