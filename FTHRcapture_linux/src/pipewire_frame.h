#pragma once
// Pixel-format glue between PipeWire video buffers and the encoder, which
// expects tightly packed BGRA/BGRx rows in CPU memory. Header-only SPA types,
// no library dependency, so backend_portal.cpp and its test share it.

#include <cstddef>
#include <cstdint>
#include <vector>

namespace fthr {

// SPA_VIDEO_FORMAT_* values the portal stream may negotiate, most preferred
// first. BGRx/BGRA need no conversion; RGBx/RGBA are accepted with a channel
// swap because some compositors only advertise those without a modifier.
const std::vector<uint32_t>& PreferredSpaVideoFormats();

struct PixelLayout {
    int av_pix_fmt = 0;    // AV_PIX_FMT_* the copied rows are in
    bool swap_rb = false;  // source stores R and B swapped relative to BGRA
};

// False when the format is not one of PreferredSpaVideoFormats().
bool DescribeSpaVideoFormat(uint32_t spa_video_format, PixelLayout& out);

const char* SpaVideoFormatName(uint32_t spa_video_format) noexcept;

// Copies `height` rows of `width` 32-bit pixels, dropping any source padding
// and, with swap_rb, exchanging the R and B channels. dst_stride and
// src_stride are in bytes and must cover width * 4.
void CopyFrameRows(uint8_t* dst, size_t dst_stride,
                   const uint8_t* src, size_t src_stride,
                   uint32_t width, uint32_t height, bool swap_rb);

} // namespace fthr
