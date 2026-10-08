#include "shm_frame.h"

#include <wayland-client-protocol.h>

extern "C" {
#include <libavutil/frame.h>
#include <libavutil/pixfmt.h>
}

namespace fthr {

int WlShmFormatToAvPixFmt(uint32_t wl_shm_format) {
    // wl_shm formats name the bits of a little-endian 32-bit word; FFmpeg's
    // packed RGB formats name the bytes in memory order.
    switch (wl_shm_format) {
    case WL_SHM_FORMAT_ARGB8888:    return AV_PIX_FMT_BGRA;
    case WL_SHM_FORMAT_XRGB8888:    return AV_PIX_FMT_BGR0;
    case WL_SHM_FORMAT_ABGR8888:    return AV_PIX_FMT_RGBA;
    case WL_SHM_FORMAT_XBGR8888:    return AV_PIX_FMT_RGB0;
    case WL_SHM_FORMAT_RGBA8888:    return AV_PIX_FMT_ABGR;
    case WL_SHM_FORMAT_RGBX8888:    return AV_PIX_FMT_0BGR;
    case WL_SHM_FORMAT_BGRA8888:    return AV_PIX_FMT_ARGB;
    case WL_SHM_FORMAT_BGRX8888:    return AV_PIX_FMT_0RGB;
    case WL_SHM_FORMAT_XRGB2101010:
    case WL_SHM_FORMAT_ARGB2101010: return AV_PIX_FMT_X2RGB10LE;
    case WL_SHM_FORMAT_XBGR2101010:
    case WL_SHM_FORMAT_ABGR2101010: return AV_PIX_FMT_X2BGR10LE;
    default:                        return -1;
    }
}

bool PickWlShmFormat(const std::vector<uint32_t>& offered, uint32_t& chosen) {
    static const uint32_t kPreference[] = {
        WL_SHM_FORMAT_XRGB8888,    WL_SHM_FORMAT_ARGB8888,
        WL_SHM_FORMAT_XBGR8888,    WL_SHM_FORMAT_ABGR8888,
        WL_SHM_FORMAT_RGBX8888,    WL_SHM_FORMAT_RGBA8888,
        WL_SHM_FORMAT_BGRX8888,    WL_SHM_FORMAT_BGRA8888,
        WL_SHM_FORMAT_XRGB2101010, WL_SHM_FORMAT_ARGB2101010,
        WL_SHM_FORMAT_XBGR2101010, WL_SHM_FORMAT_ABGR2101010,
    };
    for (uint32_t format : kPreference) {
        for (uint32_t candidate : offered) {
            if (candidate == format) {
                chosen = format;
                return true;
            }
        }
    }
    return false;
}

bool ShmFrameNormalizer::Normalize(const uint8_t* data, uint32_t stride,
                                   uint32_t width, uint32_t height,
                                   uint32_t wl_shm_format, bool y_invert,
                                   int64_t timestamp_ns, RawFrame& out) {
    const int av_fmt = WlShmFormatToAvPixFmt(wl_shm_format);
    if (!data || width == 0 || height == 0 || av_fmt < 0 ||
            stride < width * 4)
        return false;

    out.width = width;
    out.height = height;
    out.timestamp_ns = timestamp_ns;
    if (!y_invert && (av_fmt == AV_PIX_FMT_BGR0 || av_fmt == AV_PIX_FMT_BGRA)) {
        out.data = data;
        out.stride = stride;
        out.av_pix_fmt = av_fmt;
        return true;
    }

    // swscale reads the source bottom-up when given the last row and a
    // negative linesize, which undoes a y-inverted buffer during conversion.
    AVFrame source{};
    source.format = av_fmt;
    source.width = static_cast<int>(width);
    source.height = static_cast<int>(height);
    source.data[0] = const_cast<uint8_t*>(data);
    source.linesize[0] = static_cast<int>(stride);
    if (y_invert) {
        source.data[0] += static_cast<size_t>(height - 1) * stride;
        source.linesize[0] = -source.linesize[0];
    }
    if (!converter_.Convert(source)) return false;

    out.data = converter_.Data();
    out.stride = converter_.Stride();
    out.av_pix_fmt = AV_PIX_FMT_BGR0;
    return true;
}

} // namespace fthr
