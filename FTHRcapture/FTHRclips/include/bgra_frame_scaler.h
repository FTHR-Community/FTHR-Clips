#pragma once

#include "capture_scale_geometry.h"

#include <memory>
#include <string>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;

namespace fthr {

// Separable full-range SDR scaling on the capture adapter's immediate context.
// The source must already match the cropped input geometry.
class BgraFrameScaler {
public:
    BgraFrameScaler();
    ~BgraFrameScaler();

    BgraFrameScaler(const BgraFrameScaler&) = delete;
    BgraFrameScaler& operator=(const BgraFrameScaler&) = delete;

    bool Initialize(ID3D11Device* device, ID3D11DeviceContext* context,
                    const CaptureScaleGeometry& geometry, std::string& error);

    // Output is a borrowed target-sized BGRA texture. Its contents are
    // replaced by the next Scale call on the capture thread.
    bool Scale(ID3D11Texture2D* source, ID3D11Texture2D*& output, std::string& error);

private:
    struct Resources;
    std::unique_ptr<Resources> resources_;
};

} // namespace fthr
