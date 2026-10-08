#pragma once
// wl_shm frame glue shared by the wlr-screencopy and ext-image-copy-capture
// backends. The encoder and the content sampler expect top-down BGRA/BGR0
// rows; compositors may hand out other 32-bit layouts (RGB order, 10-bit
// depth) and y-inverted buffers, which are normalised here.

#include "capture_backend.h"
#include "x11_frame_converter.h"

#include <cstdint>
#include <vector>

namespace fthr {

// AV_PIX_FMT_* for a 32-bit wl_shm format, or -1 when unsupported.
int WlShmFormatToAvPixFmt(uint32_t wl_shm_format);

// The most suitable of the offered wl_shm formats: 8-bit BGR order first
// (no conversion), then other supported 32-bit layouts. Returns false when
// none of the offered formats is supported.
bool PickWlShmFormat(const std::vector<uint32_t>& offered, uint32_t& chosen);

class ShmFrameNormalizer {
public:
    // Fills `out` from a mapped wl_shm buffer. BGR0/BGRA top-down frames are
    // passed through without a copy; anything else is converted into an
    // owned BGR0 buffer that stays valid until the next call.
    bool Normalize(const uint8_t* data, uint32_t stride,
                   uint32_t width, uint32_t height, uint32_t wl_shm_format,
                   bool y_invert, int64_t timestamp_ns, RawFrame& out);
    void Reset() { converter_.Reset(); }

private:
    X11FrameConverter converter_;
};

} // namespace fthr
