#include "hdr_frame_converter.h"

#include <d3dcompiler.h>
#include <cstring>

namespace fthr {
namespace {

using Microsoft::WRL::ComPtr;

constexpr char kShader[] = R"(
Texture2D<float4> source_image : register(t0);
SamplerState linear_sampler : register(s0);
cbuffer Geometry : register(b0) {
    float4 source_rect;
    float4 dest_rect;
    float2 texture_size;
    float sdr_white;
    uint mode;
};

float4 Vertex(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}

float3 ReadSource(float2 position) {
    float2 relative = (position - dest_rect.xy) / dest_rect.zw;
    if (any(relative < 0) || any(relative >= 1)) return 0;
    float2 sample_position = source_rect.xy + clamp(
        relative * source_rect.zw, 0.5, source_rect.zw - 0.5);
    return source_image.SampleLevel(linear_sampler,
                                    sample_position / texture_size, 0).rgb;
}

float3 ToSdr(float3 rgb) {
    rgb = max(rgb / sdr_white, 0);
    float luminance = dot(rgb, float3(0.2126, 0.7152, 0.0722));
    // Keep normal desktop tones linear up to the shoulder. Compress the
    // rest monotonically into the remaining display range.
    const float knee = 0.75;
    float excess = max(luminance - knee, 0);
    float mapped = min(luminance, knee) + (1 - knee) * excess / (excess + 1 - knee);
    rgb *= mapped / max(luminance, 1e-6);
    // Compress out-of-gamut components toward the mapped neutral point.
    float peak = max(rgb.r, max(rgb.g, rgb.b));
    rgb = lerp(mapped.xxx, rgb, min(1, (1 - mapped) / max(peak - mapped, 1e-6)));
    // BT.709 OETF, matching the SDR encoder and container metadata.
    return lerp(4.5 * rgb, 1.099 * pow(max(rgb, 0), 0.45) - 0.099,
                step(0.018, rgb));
}

float3 ToPqYuv(float3 sc_rgb) {
    // Rec.709/sRGB primaries to BT.2020 in linear light, then absolute PQ.
    float3 rgb = float3(dot(sc_rgb, float3(0.627404, 0.329282, 0.043314)),
                        dot(sc_rgb, float3(0.069097, 0.919540, 0.011362)),
                        dot(sc_rgb, float3(0.016391, 0.088013, 0.895595)));
    float3 p = pow(saturate(rgb * (80.0 / 10000.0)), 2610.0 / 16384.0);
    rgb = pow((3424.0 / 4096.0 + (2413.0 / 128.0) * p)
              / (1 + (2392.0 / 128.0) * p), 2523.0 / 32.0);
    float y = dot(rgb, float3(0.2627, 0.6780, 0.0593));
    return float3(y, (rgb.b - y) / 1.8814, (rgb.r - y) / 1.4746);
}

float4 Pixel(float4 position : SV_Position) : SV_Target {
    if (mode == 3) {
        // P010 4:2:0 chroma is centered vertically and co-sited horizontally.
        float2 top_left = floor(position.xy) * 2 + 0.5;
        float2 uv = (ToPqYuv(ReadSource(top_left)).yz
                   + ToPqYuv(ReadSource(top_left + float2(0, 1))).yz) * 0.5;
        return float4(round(512 + 896 * uv) * (64.0 / 65535.0), 0, 1);
    }
    float3 rgb = ReadSource(position.xy);
    if (mode == 2) {
        float y = ToPqYuv(rgb).x;
        return float4(round(64 + 876 * y) * (64.0 / 65535.0), 0, 0, 1);
    }
    return float4(mode == 1 ? ToSdr(rgb) : rgb, 1);
}
)";

struct ShaderGeometry {
    float source[4];
    float destination[4];
    float source_size[2];
    float sdr_white;
    uint32_t mode;
};

} // namespace

HRESULT HdrFrameConverter::Initialize(ID3D11Device* device, uint32_t width,
                                      uint32_t height, Output output) {
    if (!device || !width || !height
        || (output == Output::Pq && ((width | height) & 1))) return E_INVALIDARG;
    device_ = device;
    mode_ = output;
    width_ = width;
    height_ = height;
    ComPtr<ID3DBlob> vertex, pixel;
    HRESULT hr = D3DCompile(kShader, sizeof(kShader) - 1, "HDR conversion",
        nullptr, nullptr, "Vertex", "vs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3,
        0, &vertex, nullptr);
    if (FAILED(hr)) return hr;
    hr = D3DCompile(kShader, sizeof(kShader) - 1, "HDR conversion",
        nullptr, nullptr, "Pixel", "ps_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3,
        0, &pixel, nullptr);
    if (FAILED(hr)) return hr;
    hr = device->CreateVertexShader(vertex->GetBufferPointer(),
        vertex->GetBufferSize(), nullptr, &vertex_);
    if (FAILED(hr)) return hr;
    hr = device->CreatePixelShader(pixel->GetBufferPointer(),
        pixel->GetBufferSize(), nullptr, &pixel_);
    if (FAILED(hr)) return hr;
    D3D11_BUFFER_DESC cb{};
    cb.ByteWidth = sizeof(ShaderGeometry);
    cb.Usage = D3D11_USAGE_DEFAULT;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    hr = device->CreateBuffer(&cb, nullptr, &constants_);
    if (FAILED(hr)) return hr;
    D3D11_SAMPLER_DESC sampler{};
    sampler.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxAnisotropy = 1;
    sampler.ComparisonFunc = D3D11_COMPARISON_NEVER;
    hr = device->CreateSamplerState(&sampler, &sampler_);
    if (FAILED(hr)) return hr;

    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    if (output == Output::ScRgb) desc.BindFlags |= D3D11_BIND_SHADER_RESOURCE;
    desc.Format = output == Output::Pq ? DXGI_FORMAT_P010
        : output == Output::Sdr ? DXGI_FORMAT_B8G8R8A8_UNORM
                                : DXGI_FORMAT_R16G16B16A16_FLOAT;
    hr = device->CreateTexture2D(&desc, nullptr, &output_);
    if (FAILED(hr)) return hr;
    if (output != Output::Pq)
        return device->CreateRenderTargetView(output_.Get(), nullptr, &luma_);

    ComPtr<ID3D11Device3> device3;
    hr = device_.As(&device3);
    if (FAILED(hr)) return hr;
    D3D11_RENDER_TARGET_VIEW_DESC1 rtv{};
    rtv.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
    rtv.Format = DXGI_FORMAT_R16_UNORM;
    ComPtr<ID3D11RenderTargetView1> plane;
    hr = device3->CreateRenderTargetView1(output_.Get(), &rtv, &plane);
    if (FAILED(hr)) return hr;
    luma_ = plane;
    rtv.Format = DXGI_FORMAT_R16G16_UNORM;
    rtv.Texture2D.PlaneSlice = 1;
    hr = device3->CreateRenderTargetView1(output_.Get(), &rtv, &plane);
    if (SUCCEEDED(hr)) chroma_ = plane;
    return hr;
}

HRESULT HdrFrameConverter::Convert(
    ID3D11DeviceContext* context, ID3D11Texture2D* source,
    const CaptureScaleRect& source_rect, const CaptureScaleRect& destination,
    float sdr_white) {
    if (!source || !context || !output_ || !source_rect.width || !source_rect.height
        || !destination.width || !destination.height || !(sdr_white > 0)
        || destination.left + destination.width > width_
        || destination.top + destination.height > height_) return E_INVALIDARG;
    D3D11_TEXTURE2D_DESC desc{};
    source->GetDesc(&desc);
    if (desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT
        || desc.SampleDesc.Count != 1 || desc.ArraySize != 1 || desc.MipLevels != 1
        || source_rect.left + source_rect.width > desc.Width
        || source_rect.top + source_rect.height > desc.Height) return E_INVALIDARG;

    ComPtr<ID3D11ShaderResourceView> view;
    if (desc.BindFlags & D3D11_BIND_SHADER_RESOURCE) {
        const HRESULT hr = device_->CreateShaderResourceView(source, nullptr, &view);
        if (FAILED(hr)) return hr;
    } else {
        if (source_width_ != desc.Width || source_height_ != desc.Height) {
            desc.Usage = D3D11_USAGE_DEFAULT;
            desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            desc.CPUAccessFlags = 0;
            desc.MiscFlags = 0;
            HRESULT hr = device_->CreateTexture2D(&desc, nullptr, &source_copy_);
            if (FAILED(hr)) return hr;
            hr = device_->CreateShaderResourceView(source_copy_.Get(), nullptr, &source_view_);
            if (FAILED(hr)) return hr;
            source_width_ = desc.Width;
            source_height_ = desc.Height;
        }
        context->CopyResource(source_copy_.Get(), source);
        view = source_view_;
    }

    ShaderGeometry geometry{
        {float(source_rect.left), float(source_rect.top),
         float(source_rect.width), float(source_rect.height)},
        {float(destination.left), float(destination.top),
         float(destination.width), float(destination.height)},
        {float(desc.Width), float(desc.Height)}, sdr_white,
        static_cast<uint32_t>(mode_)};
    context->IASetInputLayout(nullptr);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context->VSSetShader(vertex_.Get(), nullptr, 0);
    context->PSSetShader(pixel_.Get(), nullptr, 0);
    ID3D11ShaderResourceView* inputs[] = {view.Get()};
    ID3D11SamplerState* samplers[] = {sampler_.Get()};
    ID3D11Buffer* buffers[] = {constants_.Get()};
    context->PSSetShaderResources(0, 1, inputs);
    context->PSSetSamplers(0, 1, samplers);
    context->PSSetConstantBuffers(0, 1, buffers);
    const auto draw = [&](ID3D11RenderTargetView* target, uint32_t width, uint32_t height) {
        D3D11_VIEWPORT viewport{0, 0, float(width), float(height), 0, 1};
        context->RSSetViewports(1, &viewport);
        context->OMSetRenderTargets(1, &target, nullptr);
        context->UpdateSubresource(constants_.Get(), 0, nullptr, &geometry, 0, 0);
        context->Draw(3, 0);
    };
    draw(luma_.Get(), width_, height_);
    if (chroma_) {
        geometry.mode = 3;
        draw(chroma_.Get(), width_ / 2, height_ / 2);
    }
    inputs[0] = nullptr;
    context->PSSetShaderResources(0, 1, inputs);
    context->OMSetRenderTargets(0, nullptr, nullptr);
    return device_->GetDeviceRemovedReason();
}

} // namespace fthr
