#include "nvenc_codec_config.h"

#include <cstring>

namespace fthr {
namespace {

bool GuidEquals(const GUID& left, const GUID& right) noexcept {
    return std::memcmp(&left, &right, sizeof(GUID)) == 0;
}

const NvencCodecSelection kH264{
    VideoCodec::H264,
    NV_ENC_CODEC_H264_GUID,
    NV_ENC_H264_PROFILE_MAIN_GUID,
    EncodedPacketFormat::LengthPrefixedNalUnits,
    "h264_nvenc"};

const NvencCodecSelection kHevc{
    VideoCodec::HEVC,
    NV_ENC_CODEC_HEVC_GUID,
    NV_ENC_HEVC_PROFILE_MAIN_GUID,
    EncodedPacketFormat::AnnexBNalUnits,
    "hevc_nvenc"};

const NvencCodecSelection kAv1{
    VideoCodec::AV1,
    NV_ENC_CODEC_AV1_GUID,
    NV_ENC_AV1_PROFILE_MAIN_GUID,
    EncodedPacketFormat::LowOverheadObu,
    "av1_nvenc"};

} // namespace

const NvencCodecSelection* GetNvencCodecSelection(
    VideoCodec codec) noexcept {
    switch (codec) {
    case VideoCodec::H264: return &kH264;
    case VideoCodec::HEVC: return &kHevc;
    case VideoCodec::AV1: return &kAv1;
    }
    return nullptr;
}

bool IsNvencCodecSupported(
    VideoCodec codec,
    const GUID* supported_guids,
    size_t supported_guid_count) noexcept {
    const auto* selection = GetNvencCodecSelection(codec);
    if (!selection || (!supported_guids && supported_guid_count != 0)) {
        return false;
    }
    for (size_t i = 0; i < supported_guid_count; ++i) {
        if (GuidEquals(selection->encode_guid, supported_guids[i])) return true;
    }
    return false;
}

bool IsNvencInputFormatSupported(
    NV_ENC_BUFFER_FORMAT required_format,
    const NV_ENC_BUFFER_FORMAT* supported_formats,
    size_t supported_format_count) noexcept {
    if (!supported_formats && supported_format_count != 0) return false;
    for (size_t i = 0; i < supported_format_count; ++i) {
        if (supported_formats[i] == required_format) return true;
    }
    return false;
}

bool ConfigureNvencCodec(
    VideoCodec codec,
    uint32_t fps,
    NV_ENC_CONFIG& config, bool hdr10) noexcept {
    const auto* selection = GetNvencCodecSelection(codec);
    if (!selection || fps == 0 || (hdr10 && codec == VideoCodec::H264)) return false;

    const uint32_t keyframe_interval = fps;
    // H.264/HEVC VUI timing is part of the encoded stream metadata. Publish
    // the configured fixed rate explicitly so media-property readers do not
    // have to infer it from irregular capture timestamps.
    const uint32_t vui_time_scale = fps * 2;
    config.profileGUID = hdr10 && codec == VideoCodec::HEVC
        ? NV_ENC_HEVC_PROFILE_MAIN10_GUID : selection->profile_guid;
    const auto depth = hdr10 ? NV_ENC_BIT_DEPTH_10 : NV_ENC_BIT_DEPTH_8;
    const auto primaries = hdr10 ? NV_ENC_VUI_COLOR_PRIMARIES_BT2020
                                 : NV_ENC_VUI_COLOR_PRIMARIES_BT709;
    const auto transfer = hdr10 ? NV_ENC_VUI_TRANSFER_CHARACTERISTIC_SMPTE2084
                                : NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709;
    const auto matrix = hdr10 ? NV_ENC_VUI_MATRIX_COEFFS_BT2020_NCL
                              : NV_ENC_VUI_MATRIX_COEFFS_BT709;
    config.frameIntervalP = 1;
    config.gopLength = keyframe_interval;

    switch (codec) {
    case VideoCodec::H264: {
        auto& h264 = config.encodeCodecConfig.h264Config;
        h264.idrPeriod = keyframe_interval;
        h264.chromaFormatIDC = 1;
        h264.level = NV_ENC_LEVEL_AUTOSELECT;
        h264.enableVFR = 0;
        h264.outputPictureTimingSEI = 0;
        h264.outputBufferingPeriodSEI = 0;
        h264.h264VUIParameters.timingInfoPresentFlag = 1;
        h264.h264VUIParameters.numUnitInTicks = 1;
        h264.h264VUIParameters.timeScale = vui_time_scale;
        h264.h264VUIParameters.videoSignalTypePresentFlag = 1;
        h264.h264VUIParameters.videoFormat =
            NV_ENC_VUI_VIDEO_FORMAT_UNSPECIFIED;
        h264.h264VUIParameters.videoFullRangeFlag = 0;
        h264.h264VUIParameters.colourDescriptionPresentFlag = 1;
        h264.h264VUIParameters.colourPrimaries =
            NV_ENC_VUI_COLOR_PRIMARIES_BT709;
        h264.h264VUIParameters.transferCharacteristics =
            NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709;
        h264.h264VUIParameters.colourMatrix =
            NV_ENC_VUI_MATRIX_COEFFS_BT709;
        h264.inputBitDepth = NV_ENC_BIT_DEPTH_8;
        h264.outputBitDepth = NV_ENC_BIT_DEPTH_8;
        return true;
    }
    case VideoCodec::HEVC: {
        auto& hevc = config.encodeCodecConfig.hevcConfig;
        hevc.idrPeriod = keyframe_interval;
        hevc.chromaFormatIDC = 1;
        hevc.level = NV_ENC_LEVEL_AUTOSELECT;
        hevc.outputPictureTimingSEI = 0;
        hevc.outputBufferingPeriodSEI = 0;
        hevc.hevcVUIParameters.timingInfoPresentFlag = 1;
        hevc.hevcVUIParameters.numUnitInTicks = 1;
        hevc.hevcVUIParameters.timeScale = vui_time_scale;
        hevc.hevcVUIParameters.videoSignalTypePresentFlag = 1;
        hevc.hevcVUIParameters.videoFormat =
            NV_ENC_VUI_VIDEO_FORMAT_UNSPECIFIED;
        hevc.hevcVUIParameters.videoFullRangeFlag = 0;
        hevc.hevcVUIParameters.colourDescriptionPresentFlag = 1;
        hevc.hevcVUIParameters.colourPrimaries = primaries;
        hevc.hevcVUIParameters.transferCharacteristics = transfer;
        hevc.hevcVUIParameters.colourMatrix = matrix;
        hevc.hevcVUIParameters.chromaSampleLocationFlag = hdr10 ? 1 : 0;
        hevc.hevcVUIParameters.chromaSampleLocationTop = 0;
        hevc.hevcVUIParameters.chromaSampleLocationBot = 0;
        hevc.disableSPSPPS = 1;
        hevc.repeatSPSPPS = 0;
        hevc.inputBitDepth = depth;
        hevc.outputBitDepth = depth;
        return true;
    }
    case VideoCodec::AV1: {
        auto& av1 = config.encodeCodecConfig.av1Config;
        av1.idrPeriod = keyframe_interval;
        av1.chromaFormatIDC = 1;
        av1.level = NV_ENC_LEVEL_AV1_AUTOSELECT;
        av1.outputAnnexBFormat = 0;
        av1.enableTimingInfo = 1;
        av1.enableDecoderModelInfo = 0;
        // ISO BMFF AV1 requires the sequence header on every keyframe. This
        // matches the pinned FFmpeg NVENC wrapper and lets movenc filter the
        // low-overhead OBUs into valid MP4 samples.
        av1.disableSeqHdr = 0;
        av1.repeatSeqHdr = 1;
        av1.inputBitDepth = depth;
        av1.outputBitDepth = depth;
        av1.colorPrimaries = primaries;
        av1.transferCharacteristics = transfer;
        av1.matrixCoefficients = matrix;
        av1.colorRange = 0;
        av1.chromaSamplePosition = 1;
        return true;
    }
    }
    return false;
}

EncodedVideoConfig BuildNvencVideoConfig(
    VideoCodec codec,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    uint32_t bitrate_kbps,
    const std::vector<uint8_t>& codec_extradata, bool hdr10) {
    EncodedVideoConfig config;
    const auto* selection = GetNvencCodecSelection(codec);
    config.codec = codec;
    config.width = width;
    config.height = height;
    config.frame_rate = {static_cast<int32_t>(fps), 1};
    config.time_base = {1, static_cast<int32_t>(fps)};
    config.bitrate_kbps = bitrate_kbps;
    config.max_keyframe_interval_frames = fps;
    config.max_b_frames = 0;
    if (selection) config.packet_format = selection->packet_format;
    config.codec_extradata = codec_extradata;
    config.hdr10 = hdr10;
    return config;
}

} // namespace fthr
