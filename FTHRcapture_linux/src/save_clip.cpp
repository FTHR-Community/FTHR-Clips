#include "save_clip.h"
#include "transactional_save.h"
#include <iostream>
#include <cstring>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/dict.h>
#include <libavutil/mathematics.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>
}

namespace fthr {

// Helpers

static void set_shm_bytes(SharedMemoryLayout* shm, uint64_t bytes) {
    if (shm) shm->bytes_written = bytes;
}

static void apply_configured_video_metadata(
    AVFormatContext* format_context,
    AVStream* stream, uint32_t fps, uint32_t bitrate_kbps) {
    if (!stream || !stream->codecpar) return;

    stream->avg_frame_rate = AVRational{static_cast<int>(fps), 1};
    stream->r_frame_rate = stream->avg_frame_rate;
    if (bitrate_kbps > 0) {
        stream->codecpar->bit_rate =
            static_cast<int64_t>(bitrate_kbps) * 1000;
    }

    if (!format_context) return;
    const std::string rate = std::to_string(fps) + "/1";
    av_dict_set(&format_context->metadata, "fthr_frame_rate", rate.c_str(), 0);
    av_dict_set_int(
        &format_context->metadata,
        "fthr_video_bitrate_bps",
        static_cast<int64_t>(bitrate_kbps) * 1000,
        0);
}

// save_clip_to_file

static bool write_clip_to_temporary_file(
    const std::string&               path,
    const std::vector<EncodedPacket>& video_packets,
    int64_t                          presentation_start_pts,
    const std::vector<float>&        audio_pcm,
    int                              audio_sample_rate,
    int                              audio_channels,
    const std::vector<uint8_t>&      extradata,
    uint32_t                         fps,
    uint32_t                         width,
    uint32_t                         height,
    AVCodecID                        video_codec_id,
    SharedMemoryLayout*              shm,
    std::string&                     error_message,
    bool                             separate_audio,
    uint32_t                         bitrate_kbps
) {
    if (video_packets.empty()) {
        std::cerr << "[SaveClip] No video packets to write" << std::endl;
        error_message = "Nothing to save: the replay buffer contains no video packets";
        return false;
    }

    bool ok = true;
    AVFormatContext* fmt_ctx = nullptr;
    AVStream* vid_stream = nullptr;
    AVStream* aud_stream = nullptr;
    AVCodecContext* aac_ctx = nullptr;
    SwrContext* swr_ctx = nullptr;
    AVFrame* aac_frame = nullptr;
    AVPacket* aac_pkt = nullptr;
    bool has_audio = !audio_pcm.empty();

    // Single owner-release path for every FFmpeg allocation in this function.
    // Keep it idempotent so a future early-return cannot turn cleanup into a
    // double free while error handling is being extended.
    const auto cleanup = [&]() {
        if (aac_pkt) av_packet_free(&aac_pkt);
        if (aac_frame) av_frame_free(&aac_frame);
        if (swr_ctx) swr_free(&swr_ctx);
        if (aac_ctx) avcodec_free_context(&aac_ctx);
        if (fmt_ctx) {
            if (fmt_ctx->pb && !(fmt_ctx->oformat->flags & AVFMT_NOFILE)) {
                const int close_ret = avio_closep(&fmt_ctx->pb);
                if (close_ret < 0) {
                    if (ok) {
                        char errbuf[128];
                        av_strerror(close_ret, errbuf, sizeof(errbuf));
                        error_message = "Failed to close the temporary clip: "
                            + std::string(errbuf);
                        ok = false;
                    } else {
                        std::cerr << "[SaveClip] Secondary close failure: "
                                  << close_ret << std::endl;
                    }
                }
            }
            avformat_free_context(fmt_ctx);
            fmt_ctx = nullptr;
        }
    };

    if (avformat_alloc_output_context2(&fmt_ctx, nullptr, "mp4", path.c_str()) < 0) {
        std::cerr << "[SaveClip] avformat_alloc_output_context2 failed" << std::endl;
        error_message = "Failed to create the MP4 output container for the temporary clip";
        cleanup();
        return false;
    }
    fmt_ctx->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;
    av_dict_set(&fmt_ctx->metadata, "comment",
                separate_audio ? "fthr-audio-mode=separated"
                                : "fthr-audio-mode=combined", 0);

    vid_stream = avformat_new_stream(fmt_ctx, nullptr);
    if (!vid_stream) {
        error_message = "Failed to create the video stream in the temporary clip";
        cleanup();
        return false;
    }
    vid_stream->id = 0;
    vid_stream->time_base = { 1, static_cast<int>(fps) };

    AVCodecParameters* vpar = vid_stream->codecpar;
    vpar->codec_type = AVMEDIA_TYPE_VIDEO;
    vpar->codec_id = video_codec_id;
    vpar->width = static_cast<int>(width);
    vpar->height = static_cast<int>(height);
    vpar->format = AV_PIX_FMT_YUV420P;
    vpar->color_range = AVCOL_RANGE_MPEG;
    vpar->color_primaries = AVCOL_PRI_BT709;
    vpar->color_trc = AVCOL_TRC_BT709;
    vpar->color_space = AVCOL_SPC_BT709;
    vpar->chroma_location = AVCHROMA_LOC_LEFT;
    apply_configured_video_metadata(fmt_ctx, vid_stream, fps, bitrate_kbps);

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

    if (has_audio) {
        const AVCodec* aac_codec = avcodec_find_encoder(AV_CODEC_ID_AAC);

        // The audio stream is added to the container only after every piece
        // of the AAC pipeline exists: libavformat cannot remove a stream, and
        // an empty one would make avformat_write_header fail the whole save.
        const auto abandon_audio = [&](const char* failed_call) {
            std::cerr << "[SaveClip] " << failed_call
                      << " failed; saving video only" << std::endl;
            if (aac_pkt) av_packet_free(&aac_pkt);
            if (aac_frame) av_frame_free(&aac_frame);
            if (swr_ctx) swr_free(&swr_ctx);
            if (aac_ctx) avcodec_free_context(&aac_ctx);
            has_audio = false;
        };

        if (!aac_codec) {
            std::cerr << "[SaveClip] AAC encoder not found — saving video-only" << std::endl;
            has_audio = false;
        } else if (!(aac_ctx = avcodec_alloc_context3(aac_codec))) {
            abandon_audio("avcodec_alloc_context3");
        } else {
            aac_ctx->sample_rate = audio_sample_rate;
            aac_ctx->bit_rate = 128000;
            aac_ctx->sample_fmt = AV_SAMPLE_FMT_FLTP;

#if LIBAVUTIL_VERSION_MAJOR >= 57
            av_channel_layout_default(&aac_ctx->ch_layout, audio_channels);
#else
            aac_ctx->channels = audio_channels;
            aac_ctx->channel_layout = av_get_default_channel_layout(audio_channels);
#endif
            aac_ctx->time_base = { 1, audio_sample_rate };

            if (fmt_ctx->oformat->flags & AVFMT_GLOBALHEADER)
                aac_ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

            if (avcodec_open2(aac_ctx, aac_codec, nullptr) < 0) {
                abandon_audio("avcodec_open2");
            } else {
                swr_ctx = swr_alloc();
#if LIBAVUTIL_VERSION_MAJOR >= 57
                AVChannelLayout src_layout, dst_layout;
                av_channel_layout_default(&src_layout, audio_channels);
                av_channel_layout_default(&dst_layout, audio_channels);
                swr_alloc_set_opts2(&swr_ctx,
                    &dst_layout, AV_SAMPLE_FMT_FLTP, audio_sample_rate,
                    &src_layout, AV_SAMPLE_FMT_FLT, audio_sample_rate,
                    0, nullptr);
#else
                av_opt_set_int(swr_ctx, "in_channel_layout",
                               av_get_default_channel_layout(audio_channels), 0);
                av_opt_set_int(swr_ctx, "out_channel_layout",
                               av_get_default_channel_layout(audio_channels), 0);
                av_opt_set_int(swr_ctx, "in_sample_rate", audio_sample_rate, 0);
                av_opt_set_int(swr_ctx, "out_sample_rate", audio_sample_rate, 0);
                av_opt_set_sample_fmt(swr_ctx, "in_sample_fmt",
                                      AV_SAMPLE_FMT_FLT, 0);
                av_opt_set_sample_fmt(swr_ctx, "out_sample_fmt",
                                      AV_SAMPLE_FMT_FLTP, 0);
#endif
                if (!swr_ctx || swr_init(swr_ctx) < 0) {
                    abandon_audio("swr_init");
                } else if (!(aac_frame = av_frame_alloc())) {
                    abandon_audio("av_frame_alloc");
                } else {
                    aac_frame->nb_samples = aac_ctx->frame_size;
                    aac_frame->format = AV_SAMPLE_FMT_FLTP;
#if LIBAVUTIL_VERSION_MAJOR >= 57
                    av_channel_layout_copy(&aac_frame->ch_layout, &aac_ctx->ch_layout);
#else
                    aac_frame->channel_layout = aac_ctx->channel_layout;
                    aac_frame->channels = audio_channels;
#endif
                    aac_frame->sample_rate = audio_sample_rate;
                    if (av_frame_get_buffer(aac_frame, 0) < 0) {
                        abandon_audio("av_frame_get_buffer");
                    } else if (!(aac_pkt = av_packet_alloc())) {
                        abandon_audio("av_packet_alloc");
                    } else {
                        // Parameters are built off-stream first so a failure
                        // here cannot leave an empty stream in the container.
                        AVCodecParameters* aac_par = avcodec_parameters_alloc();
                        if (!aac_par ||
                                avcodec_parameters_from_context(aac_par, aac_ctx) < 0) {
                            abandon_audio("avcodec_parameters_from_context");
                        } else if (!(aud_stream = avformat_new_stream(fmt_ctx, nullptr))) {
                            abandon_audio("avformat_new_stream");
                        } else if (avcodec_parameters_copy(aud_stream->codecpar, aac_par) < 0) {
                            // The stream already exists; the video-only
                            // fallback cannot remove it, so fail the save.
                            abandon_audio("avcodec_parameters_copy");
                            error_message = "Failed to set up the audio stream";
                            ok = false;
                            avcodec_parameters_free(&aac_par);
                            cleanup();
                            return false;
                        } else {
                            aud_stream->id = 1;
                            aud_stream->time_base = { 1, audio_sample_rate };
                        }
                        avcodec_parameters_free(&aac_par);
                    }
                }
            }
        }
    }

    if (!(fmt_ctx->oformat->flags & AVFMT_NOFILE)) {
        if (avio_open(&fmt_ctx->pb, path.c_str(), AVIO_FLAG_WRITE) < 0) {
            std::cerr << "[SaveClip] avio_open failed: " << path << std::endl;
            error_message = "Could not open the temporary clip for writing; check free disk "
                "space and folder permissions: " + path;
            ok = false;
            cleanup();
            return false;
        }
    }

    AVDictionary* output_options = nullptr;
    av_dict_set(&output_options, "movflags", "use_metadata_tags", 0);
    const int header_error = avformat_write_header(fmt_ctx, &output_options);
    av_dict_free(&output_options);
    if (header_error < 0) {
        std::cerr << "[SaveClip] avformat_write_header failed" << std::endl;
        error_message = "Failed to write the MP4 header for the temporary clip: " + path;
        ok = false;
        cleanup();
        return false;
    }

    uint64_t bytes_out = 0;
    for (const auto& ep : video_packets) {
        if (ep.data.empty()) continue;
        AVPacket* pkt = av_packet_alloc();
        pkt->data = const_cast<uint8_t*>(ep.data.data());
        pkt->size = static_cast<int>(ep.data.size());
        pkt->pts = ep.pts - presentation_start_pts;
        pkt->dts = ep.dts - presentation_start_pts;
        pkt->duration = 1;
        pkt->stream_index = vid_stream->index;
        if (ep.is_keyframe) pkt->flags |= AV_PKT_FLAG_KEY;

        av_packet_rescale_ts(pkt,
            { 1, static_cast<int>(fps) },
            vid_stream->time_base);

        const int ret = av_interleaved_write_frame(fmt_ctx, pkt);
        pkt->data = nullptr; pkt->size = 0;
        av_packet_free(&pkt);

        if (ret < 0) {
            char errbuf[128];
            av_strerror(ret, errbuf, sizeof(errbuf));
            std::cerr << "[SaveClip] av_interleaved_write_frame failed: "
                      << errbuf << std::endl;
            error_message = "Failed while writing video packets to the temporary clip: "
                + std::string(errbuf);
            ok = false;
            cleanup();
            return false;
        }
        bytes_out += static_cast<uint64_t>(ep.data.size());
        set_shm_bytes(shm, bytes_out);
    }

    if (has_audio && aac_ctx && swr_ctx && aac_frame && aac_pkt) {
        const int frame_size = aac_ctx->frame_size;
        const size_t total_samples = audio_pcm.size() / static_cast<size_t>(audio_channels);
        size_t offset = 0;
        int64_t audio_pts = 0;

        while (offset < total_samples) {
            const int n = std::min(static_cast<size_t>(frame_size),
                                   total_samples - offset);

            av_frame_make_writable(aac_frame);
            aac_frame->nb_samples = n;
            aac_frame->pts = audio_pts;

            const float* src_ptr = audio_pcm.data() +
                                   offset * static_cast<size_t>(audio_channels);
            const uint8_t* src_data[1] = {
                reinterpret_cast<const uint8_t*>(src_ptr)
            };
            swr_convert(swr_ctx,
                        aac_frame->data, n,
                        src_data, n);

            if (avcodec_send_frame(aac_ctx, aac_frame) < 0) {
                std::cerr << "[SaveClip] avcodec_send_frame (audio) failed\n";
                error_message = "Failed while encoding audio for the temporary clip";
                ok = false;
                cleanup();
                return false;
            }
            while (true) {
                const int receive_ret = avcodec_receive_packet(aac_ctx, aac_pkt);
                if (receive_ret == AVERROR(EAGAIN) || receive_ret == AVERROR_EOF) break;
                if (receive_ret < 0) {
                    error_message = "Failed while receiving encoded audio for the temporary clip";
                    ok = false;
                    cleanup();
                    return false;
                }
                aac_pkt->stream_index = aud_stream->index;
                av_packet_rescale_ts(aac_pkt,
                    { 1, audio_sample_rate },
                    aud_stream->time_base);
                const int wret = av_interleaved_write_frame(fmt_ctx, aac_pkt);
                av_packet_unref(aac_pkt);
                if (wret < 0) {
                    char errbuf[128];
                    av_strerror(wret, errbuf, sizeof(errbuf));
                    std::cerr << "[SaveClip] audio write failed: " << errbuf << "\n";
                    error_message = "Failed while writing audio packets to the temporary clip: "
                        + std::string(errbuf);
                    ok = false;
                    cleanup();
                    return false;
                }
            }

            offset += static_cast<size_t>(n);
            audio_pts += n;
        }

        int flush_ret = avcodec_send_frame(aac_ctx, nullptr);
        if (flush_ret < 0 && flush_ret != AVERROR_EOF) {
            error_message = "Failed while finalizing audio for the temporary clip";
            ok = false;
            cleanup();
            return false;
        }
        while (true) {
            flush_ret = avcodec_receive_packet(aac_ctx, aac_pkt);
            if (flush_ret == AVERROR(EAGAIN) || flush_ret == AVERROR_EOF) break;
            if (flush_ret < 0) {
                error_message = "Failed while draining audio for the temporary clip";
                ok = false;
                cleanup();
                return false;
            }
            aac_pkt->stream_index = aud_stream->index;
            av_packet_rescale_ts(aac_pkt,
                { 1, audio_sample_rate },
                aud_stream->time_base);
            flush_ret = av_interleaved_write_frame(fmt_ctx, aac_pkt);
            if (flush_ret < 0) {
                av_packet_unref(aac_pkt);
                error_message = "Failed while writing finalized audio to the temporary clip";
                ok = false;
                cleanup();
                return false;
            }
            av_packet_unref(aac_pkt);
        }
    }

    if (ok) {
        const int trailer_ret = av_write_trailer(fmt_ctx);
        if (trailer_ret < 0) {
            char errbuf[128];
            av_strerror(trailer_ret, errbuf, sizeof(errbuf));
            error_message = "Failed to finalize the temporary MP4 container: "
                + std::string(errbuf);
            ok = false;
        }
    }

    cleanup();
    return ok;
}

bool save_clip_to_file(
    const std::string&               path,
    const std::vector<EncodedPacket>& video_packets,
    int64_t                          presentation_start_pts,
    const std::vector<float>&        audio_pcm,
    int                              audio_sample_rate,
    int                              audio_channels,
    const std::vector<uint8_t>&      extradata,
    uint32_t                         fps,
    uint32_t                         width,
    uint32_t                         height,
    AVCodecID                        video_codec_id,
    SharedMemoryLayout*              shm,
    std::string*                     error_message,
    bool                             separate_audio,
    uint32_t                         bitrate_kbps
) {
    const auto result = transactional_save::Run(
        path,
        [&](const std::filesystem::path& temporary_path, std::string& writer_error) {
            return write_clip_to_temporary_file(
                temporary_path.string(),
                video_packets,
                presentation_start_pts,
                audio_pcm,
                audio_sample_rate,
                audio_channels,
                extradata,
                fps,
                width,
                height,
                video_codec_id,
                shm,
                writer_error,
                separate_audio,
                bitrate_kbps);
        });

    if (result.cleanup_error) {
        std::cerr << "[SaveClip] Secondary cleanup failure for "
                  << result.temporary_path << ": "
                  << result.cleanup_error.message() << std::endl;
    }
    if (!result.success) {
        if (error_message)
            *error_message = transactional_save::DescribeFailure(result);
        return false;
    }

    std::cout << "[SaveClip] Committed: " << path << std::endl;
    return true;
}

} // namespace fthr
