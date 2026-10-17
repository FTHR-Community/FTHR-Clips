#include "capture_scale_geometry.h"

#include <cstdlib>
#include <iostream>

namespace {
int checks = 0;
void Check(bool value, const char* message) {
    ++checks;
    if (!value) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

void StretchAndFit() {
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(2560, 1440, 1920, 1080, false, geometry),
        "1440p to 1080p stretch resolves");
    Check(geometry.destination.left == 0 && geometry.destination.top == 0
        && geometry.destination.width == 1920 && geometry.destination.height == 1080,
        "stretch fills the target");

    Check(fthr::BuildCaptureScaleGeometry(2560, 1440, 1920, 1080, true, geometry),
        "16:9 fit resolves");
    Check(geometry.destination.width == 1920 && geometry.destination.height == 1080,
        "same aspect fit has no bars");

    Check(fthr::BuildCaptureScaleGeometry(1920, 1080, 1080, 1080, true, geometry),
        "landscape to square fit resolves");
    Check(geometry.destination.left == 0 && geometry.destination.top == 236
        && geometry.destination.width == 1080 && geometry.destination.height == 606,
        "square fit is centered with even letterbox bars");
}

void ExactTargetGeometriesAndQuadrants() {
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(1920, 1080, 1280, 720, true, geometry),
        "1080p to 720p fit resolves exactly");
    Check(geometry.destination.left == 0 && geometry.destination.top == 0
        && geometry.destination.width == 1280
        && geometry.destination.height == 720,
        "1080p to 720p has no crop or bars");
    Check(fthr::BuildCaptureScaleGeometry(2560, 1440, 1920, 1080, true, geometry),
        "1440p to 1080p fit resolves exactly");
    Check(geometry.destination.left == 0 && geometry.destination.top == 0
        && geometry.destination.width == 1920
        && geometry.destination.height == 1080,
        "1440p to 1080p has no crop or bars");

    Check(fthr::BuildCaptureScaleGeometry(1920, 1080, 1280, 1280, true, geometry),
        "quadrant source fit resolves");
    Check(geometry.destination.left == 0 && geometry.destination.top == 280
        && geometry.destination.width == 1280
        && geometry.destination.height == 720,
        "quadrant source is centered without top-left crop");
    const auto map_x = [&](uint32_t source_x) {
        return geometry.destination.left
            + static_cast<uint32_t>(
                (static_cast<uint64_t>(source_x)
                    * geometry.destination.width) / geometry.source_width);
    };
    const auto map_y = [&](uint32_t source_y) {
        return geometry.destination.top
            + static_cast<uint32_t>(
                (static_cast<uint64_t>(source_y)
                    * geometry.destination.height) / geometry.source_height);
    };
    Check(map_x(0) >= geometry.destination.left
            && map_x(geometry.source_width - 1)
                < geometry.destination.left + geometry.destination.width
            && map_y(0) >= geometry.destination.top
            && map_y(geometry.source_height - 1)
                < geometry.destination.top + geometry.destination.height,
        "all four source corners map inside the active quadrant area");
    Check(0 < geometry.destination.top
            && geometry.destination.top + geometry.destination.height
                < geometry.target_height,
        "quadrant mapping leaves black bars only outside the active area");
}

void UltrawideAndOddInputs() {
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(3440, 1440, 1920, 1080, true, geometry),
        "ultrawide fit resolves");
    Check(geometry.destination.left == 0 && geometry.destination.top == 138
        && geometry.destination.width == 1920 && geometry.destination.height == 802,
        "ultrawide fit has centered even bars");

    Check(fthr::BuildCaptureScaleGeometry(1919, 1079, 1279, 719, false, geometry),
        "odd dimensions normalize");
    Check(geometry.source_width == 1919 && geometry.source_height == 1079
        && geometry.target_width == 1278 && geometry.target_height == 718
        && geometry.valid(), "normalized geometry stays even");
    Check(!fthr::BuildCaptureScaleGeometry(1, 1080, 1920, 1080, false, geometry),
        "invalid source is rejected");
}

void HighResolutionMatrix() {
    struct Resolution {
        uint32_t width;
        uint32_t height;
    };
    const Resolution resolutions[] = {
        {1920, 1080}, {2560, 1440}, {3840, 2160},
    };
    for (const auto source : resolutions) {
        for (const auto target : resolutions) {
            for (const bool fit : {false, true}) {
                fthr::CaptureScaleGeometry geometry;
                Check(fthr::BuildCaptureScaleGeometry(
                          source.width, source.height,
                          target.width, target.height, fit, geometry),
                    "1080p/1440p/4K scale matrix resolves");
                Check(geometry.valid()
                        && geometry.destination.left % 2 == 0
                        && geometry.destination.top % 2 == 0,
                    "high-resolution scale matrix stays even and bounded");
                if (source.width * target.height
                    == target.width * source.height) {
                    Check(geometry.destination.width == target.width
                            && geometry.destination.height == target.height,
                        "same-aspect high-resolution fit has no bars");
                }
            }
        }
    }
}
}

int RunCaptureScaleGeometryTests() {
    StretchAndFit();
    ExactTargetGeometriesAndQuadrants();
    UltrawideAndOddInputs();
    HighResolutionMatrix();
    std::cout << "Capture scale geometry checks: " << checks << std::endl;
    return checks;
}
