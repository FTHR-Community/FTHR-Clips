#include "bgra_frame_scaler.h"
#include "windows_native_error.h"

#include <d3d11.h>
#include <d3dcompiler.h>
#include <wrl/client.h>

#include <algorithm>
#include <cmath>
#include <utility>
#include <vector>

namespace fthr {
namespace {

using Microsoft::WRL::ComPtr;
constexpr double kMaximumShrinkRatio = 16.0;

constexpr char kShader[] = R"(
Texture2D<float4> image : register(t0);
StructuredBuffer<uint2> spans : register(t1);
StructuredBuffer<float2> taps : register(t2);
SamplerState linear_clamp : register(s0);

cbuffer AxisSettings : register(b0) {
    float2 inverse_size;
    float2 viewport_origin;
    uint vertical;
};

float4 Vertex(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}

float4 Pixel(float4 position : SV_Position) : SV_Target {
    float2 local = position.xy - viewport_origin;
    uint2 span = spans[uint(vertical ? local.y : local.x)];
    float2 uv = local * inverse_size;
    float3 rgb = 0;
    for (uint i = 0; i < span.y; ++i) {
        float2 tap = taps[span.x + i];
        if (vertical) uv.y = tap.x;
        else uv.x = tap.x;
        rgb += image.SampleLevel(linear_clamp, uv, 0).rgb * tap.y;
    }
    return float4(rgb, 1);
}

float4 PixelExactHalf(float4 position : SV_Position) : SV_Target {
    float2 center = (position.xy - viewport_origin) * (2 * inverse_size);
    float2 offset = .75 * inverse_size;
    // Each bilinear pair yields [1, 3, 3, 1] / 8 along one axis.
    // The four Cartesian pairs are the same separable triangle in one pass.
    float3 rgb = image.SampleLevel(linear_clamp, center - offset, 0).rgb
        + image.SampleLevel(linear_clamp, center + float2(offset.x, -offset.y), 0).rgb
        + image.SampleLevel(linear_clamp, center + float2(-offset.x, offset.y), 0).rgb
        + image.SampleLevel(linear_clamp, center + offset, 0).rgb;
    return float4(rgb * .25, 1);
}
)";

struct Span { uint32_t first, count; };
struct Tap { float coordinate, weight; };
struct AxisSettings {
    float inverse_width, inverse_height;
    float origin_x, origin_y;
    uint32_t vertical;
    uint32_t padding[3]{};
};
struct Axis {
    ComPtr<ID3D11ShaderResourceView> spans;
    ComPtr<ID3D11ShaderResourceView> taps;
    ComPtr<ID3D11Buffer> constants;
};

HRESULT CreateSettings(ID3D11Device* device, const AxisSettings& settings,
                       ID3D11Buffer** output) {
    D3D11_BUFFER_DESC desc{};
    desc.ByteWidth = sizeof(AxisSettings);
    desc.Usage = D3D11_USAGE_IMMUTABLE;
    desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SUBRESOURCE_DATA initial{&settings};
    return device->CreateBuffer(&desc, &initial, output);
}

template<class Element>
HRESULT CreateStructuredView(ID3D11Device* device, const std::vector<Element>& elements,
                             ID3D11ShaderResourceView** output) {
    D3D11_BUFFER_DESC desc{};
    desc.ByteWidth = static_cast<UINT>(elements.size() * sizeof(Element));
    desc.Usage = D3D11_USAGE_IMMUTABLE;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    desc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    desc.StructureByteStride = sizeof(Element);
    D3D11_SUBRESOURCE_DATA initial{elements.data()};
    ComPtr<ID3D11Buffer> buffer;
    const HRESULT result = device->CreateBuffer(&desc, &initial, &buffer);
    if (FAILED(result)) return result;
    D3D11_SHADER_RESOURCE_VIEW_DESC view{};
    view.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    view.Buffer.NumElements = static_cast<UINT>(elements.size());
    return device->CreateShaderResourceView(buffer.Get(), &view, output);
}

HRESULT CreateAxis(ID3D11Device* device, uint32_t source, uint32_t target,
                   const AxisSettings& settings, Axis& axis) {
    const double scale = static_cast<double>(source) / target;
    const double radius = std::max(1.0, scale);
    std::vector<Span> spans;
    std::vector<Tap> taps;
    spans.reserve(target);
    taps.reserve(target * static_cast<size_t>(std::ceil(radius)));
    struct WeightedPixel { int index; double weight; };
    std::vector<WeightedPixel> pixels;
    pixels.reserve(static_cast<size_t>(2 * std::ceil(radius)));

    for (uint32_t i = 0; i < target; ++i) {
        const double center = (i + 0.5) * scale - 0.5;
        const int first = static_cast<int>(std::ceil(center - radius));
        const int last = static_cast<int>(std::floor(center + radius));
        double total = 0;
        pixels.clear();
        for (int sample = first; sample <= last; ++sample) {
            const double weight = 1.0 - std::abs(sample - center) / radius;
            if (weight <= 0) continue;
            const int index = std::clamp(sample, 0, static_cast<int>(source) - 1);
            total += weight;
            if (!pixels.empty() && pixels.back().index == index) pixels.back().weight += weight;
            else pixels.push_back({index, weight});
        }

        const auto start = static_cast<uint32_t>(taps.size());
        // Two neighboring positive taps can share one hardware bilinear read.
        // Merge clamped edge samples first so the remaining pairs are adjacent.
        for (size_t j = 0; j < pixels.size(); j += 2) {
            const auto& left = pixels[j];
            double weight = left.weight;
            double position = left.index + 0.5;
            if (j + 1 < pixels.size()) {
                const auto& right = pixels[j + 1];
                weight += right.weight;
                position += right.weight / weight;
            }
            taps.push_back({static_cast<float>(position / source), static_cast<float>(weight / total)});
        }
        spans.push_back({start, static_cast<uint32_t>(taps.size()) - start});
    }

    HRESULT result = CreateStructuredView(device, spans, &axis.spans);
    if (FAILED(result)) return result;
    result = CreateStructuredView(device, taps, &axis.taps);
    if (FAILED(result)) return result;
    return CreateSettings(device, settings, &axis.constants);
}

D3D11_TEXTURE2D_DESC TextureDesc(uint32_t width, uint32_t height,
                                DXGI_FORMAT format, UINT bindings) {
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = format;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = bindings;
    return desc;
}

bool SameDevice(ID3D11Device* expected, ID3D11Device* actual) {
    if (expected == actual) return true;
    if (!expected || !actual) return false;
    ComPtr<IUnknown> expected_identity, actual_identity;
    return SUCCEEDED(expected->QueryInterface(IID_PPV_ARGS(&expected_identity)))
        && SUCCEEDED(actual->QueryInterface(IID_PPV_ARGS(&actual_identity)))
        && expected_identity.Get() == actual_identity.Get();
}

bool Check(HRESULT result, const char* operation, std::string& error) {
    if (SUCCEEDED(result)) return true;
    error = diagnostics::FormatHResultFailure(operation, result);
    return false;
}

} // namespace

struct BgraFrameScaler::Resources {
    CaptureScaleGeometry geometry;
    bool exact_half = false;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D11VertexShader> vertex;
    ComPtr<ID3D11PixelShader> pixel;
    ComPtr<ID3D11SamplerState> sampler;
    ComPtr<ID3D11Texture2D> source_copy, intermediate, output;
    ComPtr<ID3D11ShaderResourceView> source_copy_view, intermediate_view;
    ComPtr<ID3D11RenderTargetView> intermediate_target, output_target;
    Axis horizontal, vertical;
};

BgraFrameScaler::BgraFrameScaler() = default;
BgraFrameScaler::~BgraFrameScaler() = default;

bool BgraFrameScaler::Initialize(ID3D11Device* device, ID3D11DeviceContext* context,
                                 const CaptureScaleGeometry& geometry, std::string& error) {
    resources_.reset();
    error.clear();
    const auto& dest = geometry.destination;
    constexpr uint32_t limit = D3D11_REQ_TEXTURE2D_U_OR_V_DIMENSION;
    if (!device || !context || context->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE
        || device->GetFeatureLevel() < D3D_FEATURE_LEVEL_11_0 || !geometry.valid()
        || geometry.source_width > limit || geometry.source_height > limit
        || geometry.target_width > limit || geometry.target_height > limit
        || dest.left > geometry.target_width || dest.top > geometry.target_height
        || dest.width > geometry.target_width - dest.left
        || dest.height > geometry.target_height - dest.top
        || geometry.source_width > dest.width * kMaximumShrinkRatio
        || geometry.source_height > dest.height * kMaximumShrinkRatio) {
        error = "BGRA scaler requires a supported D3D11 immediate context and bounded scale geometry";
        return false;
    }
    ComPtr<ID3D11Device> context_device;
    context->GetDevice(&context_device);
    if (!SameDevice(device, context_device.Get())) {
        error = "BGRA scaler context belongs to a different D3D11 device";
        return false;
    }

    auto next = std::make_unique<Resources>();
    next->geometry = geometry;
    next->exact_half = dest.width * 2 == geometry.source_width
        && dest.height * 2 == geometry.source_height;
    next->device = device;
    next->context = context;
    ComPtr<ID3DBlob> vertex, pixel;
    if (!Check(D3DCompile(kShader, sizeof(kShader) - 1, "BGRA scale", nullptr, nullptr,
               "Vertex", "vs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &vertex, nullptr),
               "D3DCompile(BGRA scale vertex)", error)
        || !Check(D3DCompile(kShader, sizeof(kShader) - 1, "BGRA scale", nullptr, nullptr,
                  next->exact_half ? "PixelExactHalf" : "Pixel", "ps_5_0",
                  D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &pixel, nullptr),
                  "D3DCompile(BGRA scale pixel)", error)
        || !Check(device->CreateVertexShader(vertex->GetBufferPointer(), vertex->GetBufferSize(),
                  nullptr, &next->vertex), "CreateVertexShader(BGRA scale)", error)
        || !Check(device->CreatePixelShader(pixel->GetBufferPointer(), pixel->GetBufferSize(),
                  nullptr, &next->pixel), "CreatePixelShader(BGRA scale)", error)) return false;

    D3D11_SAMPLER_DESC sampler{};
    sampler.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxAnisotropy = 1;
    sampler.ComparisonFunc = D3D11_COMPARISON_NEVER;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    const auto input = TextureDesc(geometry.source_width, geometry.source_height,
        DXGI_FORMAT_B8G8R8A8_UNORM, D3D11_BIND_SHADER_RESOURCE);
    const auto target = TextureDesc(geometry.target_width, geometry.target_height,
        DXGI_FORMAT_B8G8R8A8_UNORM, D3D11_BIND_RENDER_TARGET);
    if (!Check(device->CreateSamplerState(&sampler, &next->sampler), "CreateSamplerState(BGRA scale)", error)
        || !Check(device->CreateTexture2D(&input, nullptr, &next->source_copy), "CreateTexture2D(BGRA scale source)", error)
        || !Check(device->CreateShaderResourceView(next->source_copy.Get(), nullptr, &next->source_copy_view),
                  "CreateShaderResourceView(BGRA scale source)", error)
        || !Check(device->CreateTexture2D(&target, nullptr, &next->output), "CreateTexture2D(BGRA scale output)", error)
        || !Check(device->CreateRenderTargetView(next->output.Get(), nullptr, &next->output_target),
                  "CreateRenderTargetView(BGRA scale output)", error)) return false;

    const float inverse_width = 1.0f / geometry.source_width;
    const float inverse_height = 1.0f / geometry.source_height;
    const float left = static_cast<float>(dest.left), top = static_cast<float>(dest.top);
    if (next->exact_half) {
        const AxisSettings settings{inverse_width, inverse_height, left, top, 0};
        if (!Check(CreateSettings(device, settings, &next->horizontal.constants),
                   "CreateSettings(BGRA 2x downscale)", error)) return false;
    } else {
        const auto intermediate = TextureDesc(dest.width, geometry.source_height,
            DXGI_FORMAT_R16G16B16A16_FLOAT, D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET);
        const AxisSettings horizontal{inverse_width, inverse_height, 0, 0, 0};
        const AxisSettings vertical{1.0f / dest.width, inverse_height, left, top, 1};
        if (!Check(device->CreateTexture2D(&intermediate, nullptr, &next->intermediate),
                   "CreateTexture2D(BGRA scale intermediate)", error)
            || !Check(device->CreateShaderResourceView(next->intermediate.Get(), nullptr, &next->intermediate_view),
                      "CreateShaderResourceView(BGRA scale intermediate)", error)
            || !Check(device->CreateRenderTargetView(next->intermediate.Get(), nullptr, &next->intermediate_target),
                      "CreateRenderTargetView(BGRA scale intermediate)", error)
            || !Check(CreateAxis(device, geometry.source_width, dest.width, horizontal, next->horizontal),
                      "CreateAxis(BGRA horizontal scale)", error)
            || !Check(CreateAxis(device, geometry.source_height, dest.height, vertical, next->vertical),
                      "CreateAxis(BGRA vertical scale)", error)) return false;
    }

    resources_ = std::move(next);
    return true;
}

bool BgraFrameScaler::Scale(ID3D11Texture2D* source, ID3D11Texture2D*& output,
                            std::string& error) {
    output = nullptr;
    error.clear();
    if (!resources_ || !source) {
        error = "BGRA scaler requires a valid source and initialized resources";
        return false;
    }
    auto& r = *resources_;
    D3D11_TEXTURE2D_DESC desc{};
    source->GetDesc(&desc);
    ComPtr<ID3D11Device> source_device;
    source->GetDevice(&source_device);
    if (desc.Width != r.geometry.source_width || desc.Height != r.geometry.source_height
        || desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM || desc.SampleDesc.Count != 1
        || desc.MipLevels != 1 || desc.ArraySize != 1 || !SameDevice(r.device.Get(), source_device.Get())) {
        error = "BGRA scaler source does not match the capture device, geometry or pixel format";
        return false;
    }

    auto* context = r.context.Get();
    ComPtr<ID3D11ShaderResourceView> source_view;
    if (desc.BindFlags & D3D11_BIND_SHADER_RESOURCE) {
        if (!Check(r.device->CreateShaderResourceView(source, nullptr, &source_view),
                   "CreateShaderResourceView(BGRA scale input frame)", error)) return false;
    } else {
        context->CopyResource(r.source_copy.Get(), source);
        source_view = r.source_copy_view;
    }

    context->IASetInputLayout(nullptr);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context->RSSetState(nullptr);
    context->OMSetBlendState(nullptr, nullptr, UINT_MAX);
    context->VSSetShader(r.vertex.Get(), nullptr, 0);
    context->PSSetShader(r.pixel.Get(), nullptr, 0);
    auto* sampler = r.sampler.Get();
    context->PSSetSamplers(0, 1, &sampler);

    const auto draw = [&](ID3D11ShaderResourceView* image, ID3D11RenderTargetView* target,
                          const Axis& axis, const CaptureScaleRect& rect) {
        D3D11_VIEWPORT viewport{static_cast<float>(rect.left), static_cast<float>(rect.top),
            static_cast<float>(rect.width), static_cast<float>(rect.height), 0, 1};
        context->RSSetViewports(1, &viewport);
        context->OMSetRenderTargets(1, &target, nullptr);
        ID3D11ShaderResourceView* inputs[] = {image, axis.spans.Get(), axis.taps.Get()};
        context->PSSetShaderResources(0, 3, inputs);
        auto* constants = axis.constants.Get();
        context->PSSetConstantBuffers(0, 1, &constants);
        context->Draw(3, 0);
        ID3D11ShaderResourceView* unbound[3]{};
        context->PSSetShaderResources(0, 3, unbound);
        context->OMSetRenderTargets(0, nullptr, nullptr);
    };
    const auto& dest = r.geometry.destination;
    if (!r.exact_half) {
        draw(source_view.Get(), r.intermediate_target.Get(), r.horizontal,
             {0, 0, dest.width, r.geometry.source_height});
    }
    if (r.geometry.RequiresLetterboxing()) {
        constexpr float black[] = {0, 0, 0, 1};
        context->ClearRenderTargetView(r.output_target.Get(), black);
    }
    draw(r.exact_half ? source_view.Get() : r.intermediate_view.Get(),
         r.output_target.Get(), r.exact_half ? r.horizontal : r.vertical, dest);

    ID3D11Buffer* constants = nullptr;
    sampler = nullptr;
    context->PSSetConstantBuffers(0, 1, &constants);
    context->PSSetSamplers(0, 1, &sampler);
    context->PSSetShader(nullptr, nullptr, 0);
    context->VSSetShader(nullptr, nullptr, 0);
    if (!Check(r.device->GetDeviceRemovedReason(), "GetDeviceRemovedReason(BGRA scale)", error)) return false;
    output = r.output.Get();
    return true;
}

} // namespace fthr
