// Compact, deterministic codec-specific policy for the native NVENC backend.

#pragma once
#ifndef FTHR_NVENC_CODEC_CONFIG_H
#define FTHR_NVENC_CODEC_CONFIG_H

#include "encoded_video_config.h"
#include "nvEncodeAPI.h"

#include <cstddef>
#include <cstdint>
#include <vector>

namespace fthr {

struct NvencCodecSelection {
    VideoCodec codec;
    GUID encode_guid;
    GUID profile_guid;
    EncodedPacketFormat packet_format;
    const char* active_codec_name;
};

const NvencCodecSelection* GetNvencCodecSelection(
    VideoCodec codec) noexcept;

bool IsNvencCodecSupported(
    VideoCodec codec,
    const GUID* supported_guids,
    size_t supported_guid_count) noexcept;

bool IsNvencInputFormatSupported(
    NV_ENC_BUFFER_FORMAT required_format,
    const NV_ENC_BUFFER_FORMAT* supported_formats,
    size_t supported_format_count) noexcept;

// Applies only codec/GOP policy. Preset and common rate-control policy remain
// owned by HardwareEncoder so the established H.264 behavior stays unchanged.
bool ConfigureNvencCodec(
    VideoCodec codec,
    uint32_t fps,
    NV_ENC_CONFIG& config, bool hdr10 = false) noexcept;

EncodedVideoConfig BuildNvencVideoConfig(
    VideoCodec codec,
    uint32_t width,
    uint32_t height,
    uint32_t fps,
    uint32_t bitrate_kbps,
    const std::vector<uint8_t>& codec_extradata, bool hdr10 = false);

} // namespace fthr

#endif // FTHR_NVENC_CODEC_CONFIG_H
