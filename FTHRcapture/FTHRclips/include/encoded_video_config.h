// Codec-neutral description of one compressed replay video stream.

#pragma once
#ifndef FTHR_ENCODED_VIDEO_CONFIG_H
#define FTHR_ENCODED_VIDEO_CONFIG_H

#include <cstdint>
#include <vector>

namespace fthr {

enum class VideoCodec : uint32_t {
    H264,
    HEVC,
    AV1,
};

enum class EncodedPacketFormat : uint32_t {
    // Four-byte big-endian NAL length prefixes; used by AVC/H.264 samples in
    // MP4. Decoder configuration lives in codec_extradata.
    LengthPrefixedNalUnits,
    // HEVC Annex B access units. The pinned FFmpeg MP4 muxer converts them to
    // length-prefixed samples and builds hvcC from Annex B VPS/SPS/PPS.
    AnnexBNalUnits,
    // AV1 low-overhead OBU samples with av1C decoder configuration.
    LowOverheadObu,
};

struct VideoRational {
    int32_t numerator = 0;
    int32_t denominator = 1;

    friend bool operator==(const VideoRational& left, const VideoRational& right) {
        return left.numerator == right.numerator
            && left.denominator == right.denominator;
    }
};

struct EncodedVideoConfig {
    VideoCodec codec = VideoCodec::H264;
    uint32_t width = 0;
    uint32_t height = 0;
    VideoRational frame_rate{60, 1};
    VideoRational time_base{1, 60};
    uint32_t bitrate_kbps = 0;
    uint32_t max_keyframe_interval_frames = 0;
    uint32_t max_b_frames = 0;
    EncodedPacketFormat packet_format =
        EncodedPacketFormat::LengthPrefixedNalUnits;
    std::vector<uint8_t> codec_extradata;
    bool hdr10 = false;

    friend bool operator==(
        const EncodedVideoConfig& left,
        const EncodedVideoConfig& right) {
        return left.codec == right.codec
            && left.width == right.width
            && left.height == right.height
            && left.frame_rate == right.frame_rate
            && left.time_base == right.time_base
            && left.bitrate_kbps == right.bitrate_kbps
            && left.max_keyframe_interval_frames
                == right.max_keyframe_interval_frames
            && left.max_b_frames == right.max_b_frames
            && left.packet_format == right.packet_format
            && left.codec_extradata == right.codec_extradata
            && left.hdr10 == right.hdr10;
    }
};

const char* VideoCodecName(VideoCodec codec) noexcept;
bool IsValidEncodedVideoConfig(const EncodedVideoConfig& config) noexcept;
bool IsMp4PacketFormatCompatible(const EncodedVideoConfig& config) noexcept;
int64_t DurationInVideoTicks(
    const EncodedVideoConfig& config, uint32_t seconds) noexcept;

constexpr bool IsProductionReplayCodecEnabled(VideoCodec codec) noexcept {
    return codec == VideoCodec::H264
        || codec == VideoCodec::HEVC
        || codec == VideoCodec::AV1;
}

} // namespace fthr

#endif // FTHR_ENCODED_VIDEO_CONFIG_H
