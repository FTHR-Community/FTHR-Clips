#pragma once

#include "capture_scale_geometry.h"

#include <d3d11_3.h>
#include <wrl/client.h>

namespace fthr {

// WGC supplies linear scRGB, where 1.0 is 80 nits. This owns the conversion
// targets so borrowed frame-pool surfaces are never held by the encoder.
class HdrFrameConverter {
public:
    enum class Output { ScRgb, Sdr, Pq };

    HRESULT Initialize(ID3D11Device* device, uint32_t width, uint32_t height,
                       Output output);
    HRESULT Convert(ID3D11DeviceContext* context, ID3D11Texture2D* source,
                    const CaptureScaleRect& source_rect,
                    const CaptureScaleRect& destination, float sdr_white);
    ID3D11Texture2D* Texture() const { return output_.Get(); }

private:
    Output mode_ = Output::Sdr;
    Microsoft::WRL::ComPtr<ID3D11Device> device_;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> source_copy_;
    Microsoft::WRL::ComPtr<ID3D11ShaderResourceView> source_view_;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> output_;
    Microsoft::WRL::ComPtr<ID3D11RenderTargetView> luma_;
    Microsoft::WRL::ComPtr<ID3D11RenderTargetView> chroma_;
    Microsoft::WRL::ComPtr<ID3D11VertexShader> vertex_;
    Microsoft::WRL::ComPtr<ID3D11PixelShader> pixel_;
    Microsoft::WRL::ComPtr<ID3D11Buffer> constants_;
    Microsoft::WRL::ComPtr<ID3D11SamplerState> sampler_;
    uint32_t width_ = 0;
    uint32_t height_ = 0;
    uint32_t source_width_ = 0;
    uint32_t source_height_ = 0;
};

} // namespace fthr
