// Deterministic source-to-encoder geometry shared by Windows hardware paths.
#pragma once

#include <cstdint>

namespace fthr {

struct CaptureScaleRect {
    uint32_t left = 0;
    uint32_t top = 0;
    uint32_t width = 0;
    uint32_t height = 0;
};

struct CaptureScaleGeometry {
    uint32_t source_width = 0;
    uint32_t source_height = 0;
    uint32_t target_width = 0;
    uint32_t target_height = 0;
    CaptureScaleRect destination;
    bool fit = false;

    bool RequiresLetterboxing() const noexcept {
        return fit && (destination.width != target_width
            || destination.height != target_height);
    }

    bool valid() const noexcept {
        return source_width >= 2 && source_height >= 2
            && target_width >= 2 && target_height >= 2
            && (target_width % 2) == 0 && (target_height % 2) == 0
            && destination.width >= 2 && destination.height >= 2
            && destination.left + destination.width <= target_width
            && destination.top + destination.height <= target_height;
    }
};

// Resolve a source frame and encoded target without floating point rounding.
// The target dimensions are normalized down to an even value because all
// supported hardware paths consume 4:2:0-compatible dimensions. FIT computes
// a centered, even destination rectangle; the unfilled target is black.
inline bool BuildCaptureScaleGeometry(
    uint32_t source_width,
    uint32_t source_height,
    uint32_t requested_target_width,
    uint32_t requested_target_height,
    bool fit,
    CaptureScaleGeometry& result) noexcept {
    result = {};
    if (requested_target_width == 0) requested_target_width = source_width;
    if (requested_target_height == 0) requested_target_height = source_height;
    requested_target_width &= ~1u;
    requested_target_height &= ~1u;
    if (source_width < 2 || source_height < 2
        || requested_target_width < 2 || requested_target_height < 2) {
        return false;
    }

    result.source_width = source_width;
    result.source_height = source_height;
    result.target_width = requested_target_width;
    result.target_height = requested_target_height;
    result.fit = fit;
    result.destination = {
        0, 0, requested_target_width, requested_target_height};
    if (!fit) return true;

    // Compare source/target aspect ratios using 64-bit products. This avoids
    // a floating-point edge where a one-pixel bar is placed asymmetrically.
    const uint64_t source_aspect = static_cast<uint64_t>(source_width)
        * requested_target_height;
    const uint64_t target_aspect = static_cast<uint64_t>(requested_target_width)
        * source_height;
    uint32_t fit_width = requested_target_width;
    uint32_t fit_height = requested_target_height;
    if (source_aspect > target_aspect) {
        fit_height = static_cast<uint32_t>(
            (static_cast<uint64_t>(requested_target_width) * source_height)
            / source_width) & ~1u;
    } else if (source_aspect < target_aspect) {
        fit_width = static_cast<uint32_t>(
            (static_cast<uint64_t>(requested_target_height) * source_width)
            / source_height) & ~1u;
    }
    if (fit_width < 2 || fit_height < 2) return false;
    const uint32_t left = ((requested_target_width - fit_width) / 2) & ~1u;
    const uint32_t top = ((requested_target_height - fit_height) / 2) & ~1u;
    result.destination = {left, top, fit_width, fit_height};
    return result.valid();
}

} // namespace fthr
