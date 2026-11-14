#include "encoded_video_config.h"
#include "encoded_video_config_ffmpeg.h"

extern "C" {
#include <libavformat/avformat.h>
#include <libavutil/dict.h>
}

#include <string>

namespace fthr {

const char* VideoCodecName(VideoCodec codec) noexcept {
    switch (codec) {
    case VideoCodec::H264: return "H.264";
    case VideoCodec::HEVC: return "HEVC";
    case VideoCodec::AV1: return "AV1";
    }
    return "Unknown";
}

bool IsMp4PacketFormatCompatible(const EncodedVideoConfig& config) noexcept {
    switch (config.codec) {
    case VideoCodec::H264:
        return config.packet_format
            == EncodedPacketFormat::LengthPrefixedNalUnits;
    case VideoCodec::HEVC:
        return config.packet_format == EncodedPacketFormat::AnnexBNalUnits;
    case VideoCodec::AV1:
        return config.packet_format == EncodedPacketFormat::LowOverheadObu;
    }
    return false;
}

bool IsValidEncodedVideoConfig(const EncodedVideoConfig& config) noexcept {
    return config.width > 0
        && config.height > 0
        && config.frame_rate.numerator > 0
        && config.frame_rate.denominator > 0
        && config.time_base.numerator > 0
        && config.time_base.denominator > 0
        && IsMp4PacketFormatCompatible(config);
}

int64_t DurationInVideoTicks(
    const EncodedVideoConfig& config, uint32_t seconds) noexcept {
    if (config.time_base.numerator <= 0
        || config.time_base.denominator <= 0) {
        return 0;
    }
    const int64_t numerator = static_cast<int64_t>(seconds)
        * static_cast<int64_t>(config.time_base.denominator);
    return numerator / static_cast<int64_t>(config.time_base.numerator);
}

AVCodecID ToAvCodecId(VideoCodec codec) noexcept {
    switch (codec) {
    case VideoCodec::H264: return AV_CODEC_ID_H264;
    case VideoCodec::HEVC: return AV_CODEC_ID_HEVC;
    case VideoCodec::AV1: return AV_CODEC_ID_AV1;
    }
    return AV_CODEC_ID_NONE;
}

void ApplyVideoColorMetadata(AVCodecParameters* parameters, bool hdr10) noexcept {
    if (!parameters) return;
    parameters->color_range = AVCOL_RANGE_MPEG;
    parameters->color_primaries = hdr10 ? AVCOL_PRI_BT2020 : AVCOL_PRI_BT709;
    parameters->color_trc = hdr10 ? AVCOL_TRC_SMPTE2084 : AVCOL_TRC_BT709;
    parameters->color_space = hdr10 ? AVCOL_SPC_BT2020_NCL : AVCOL_SPC_BT709;
    parameters->chroma_location = AVCHROMA_LOC_LEFT;
}

void ApplyConfiguredVideoMetadata(
    AVFormatContext* format_context,
    AVStream* stream,
    const EncodedVideoConfig& config) noexcept {
    if (!stream || !stream->codecpar) return;
    ApplyVideoColorMetadata(stream->codecpar, config.hdr10);

    const AVRational frame_rate = {
        config.frame_rate.numerator,
        config.frame_rate.denominator};
    stream->avg_frame_rate = frame_rate;
    stream->r_frame_rate = frame_rate;

    if (config.bitrate_kbps > 0) {
        stream->codecpar->bit_rate =
            static_cast<int64_t>(config.bitrate_kbps) * 1000;
    }

    // MP4 does not reliably retain arbitrary stream tags. Store the values as
    // global mdta entries instead; the muxer writes these when
    // "use_metadata_tags" is enabled, and they survive later audio/remux or
    // overlay passes.
    if (!format_context) return;
    const std::string rate = std::to_string(config.frame_rate.numerator)
        + "/" + std::to_string(config.frame_rate.denominator);
    av_dict_set(
        &format_context->metadata, "fthr_frame_rate", rate.c_str(), 0);
    av_dict_set_int(
        &format_context->metadata,
        "fthr_video_bitrate_bps",
        static_cast<int64_t>(config.bitrate_kbps) * 1000,
        0);
}

} // namespace fthr
