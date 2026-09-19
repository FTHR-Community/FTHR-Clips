#include "pipewire_frame.h"

#include <cstring>

#include <spa/param/video/raw.h>
extern "C" {
#include <libavutil/pixfmt.h>
}

namespace fthr {

const std::vector<uint32_t>& PreferredSpaVideoFormats() {
    static const std::vector<uint32_t> formats = {
        SPA_VIDEO_FORMAT_BGRx,
        SPA_VIDEO_FORMAT_BGRA,
        SPA_VIDEO_FORMAT_RGBx,
        SPA_VIDEO_FORMAT_RGBA,
    };
    return formats;
}

bool DescribeSpaVideoFormat(uint32_t spa_video_format, PixelLayout& out) {
    switch (spa_video_format) {
    case SPA_VIDEO_FORMAT_BGRx:
        out = {AV_PIX_FMT_BGR0, false};
        return true;
    case SPA_VIDEO_FORMAT_BGRA:
        out = {AV_PIX_FMT_BGRA, false};
        return true;
    case SPA_VIDEO_FORMAT_RGBx:
        // Swapped during the copy, so the encoder still receives BGR0.
        out = {AV_PIX_FMT_BGR0, true};
        return true;
    case SPA_VIDEO_FORMAT_RGBA:
        out = {AV_PIX_FMT_BGRA, true};
        return true;
    default:
        return false;
    }
}

const char* SpaVideoFormatName(uint32_t spa_video_format) noexcept {
    switch (spa_video_format) {
    case SPA_VIDEO_FORMAT_BGRx: return "BGRx";
    case SPA_VIDEO_FORMAT_BGRA: return "BGRA";
    case SPA_VIDEO_FORMAT_RGBx: return "RGBx";
    case SPA_VIDEO_FORMAT_RGBA: return "RGBA";
    default: return "unsupported";
    }
}

void CopyFrameRows(uint8_t* dst, size_t dst_stride,
                   const uint8_t* src, size_t src_stride,
                   uint32_t width, uint32_t height, bool swap_rb) {
    const size_t row_bytes = static_cast<size_t>(width) * 4;
    for (uint32_t y = 0; y < height; ++y) {
        const uint8_t* s = src + y * src_stride;
        uint8_t* d = dst + y * dst_stride;
        if (!swap_rb) {
            std::memcpy(d, s, row_bytes);
            continue;
        }
        for (size_t x = 0; x < row_bytes; x += 4) {
            d[x + 0] = s[x + 2];
            d[x + 1] = s[x + 1];
            d[x + 2] = s[x + 0];
            d[x + 3] = s[x + 3];
        }
    }
}

} // namespace fthr
