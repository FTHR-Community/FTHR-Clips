#ifndef NOMINMAX
#define NOMINMAX
#endif

#include "dxgi_cursor.h"

#include <d3dcompiler.h>

#include <algorithm>
#include <cstring>
#include <limits>
#include <sstream>

namespace fthr::dxgi_cursor {
namespace {

constexpr uint32_t kShapeTypeColor =
    static_cast<uint32_t>(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR);
constexpr uint32_t kShapeTypeMonochrome =
    static_cast<uint32_t>(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME);
constexpr uint32_t kShapeTypeMaskedColor =
    static_cast<uint32_t>(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR);

const char* CursorVertexShader() {
    return R"(
cbuffer CursorConstants : register(b0) {
    float2 output_size;
    float2 cursor_position;
    float2 cursor_size;
    uint cursor_mode;
    uint unused;
};
struct VSOut {
    float4 position : SV_POSITION;
    float2 local : TEXCOORD0;
};
VSOut main(uint id : SV_VertexID) {
    float2 local;
    if (id == 0) local = float2(0.0, 0.0);
    else if (id == 1) local = float2(cursor_size.x, 0.0);
    else if (id == 2) local = float2(0.0, cursor_size.y);
    else local = cursor_size;
    float2 pixel = cursor_position + local;
    float2 ndc = float2(
        pixel.x / output_size.x * 2.0 - 1.0,
        1.0 - pixel.y / output_size.y * 2.0);
    VSOut output;
    output.position = float4(ndc, 0.0, 1.0);
    output.local = local;
    return output;
}
)";
}

const char* CursorPixelShader() {
    return R"(
cbuffer CursorConstants : register(b0) {
    float2 output_size;
    float2 cursor_position;
    float2 cursor_size;
    uint cursor_mode;
    uint unused;
};
Texture2D<float4> background_texture : register(t0);
Texture2D<float4> cursor_texture : register(t1);
SamplerState point_sampler : register(s0);

float4 main(float4 position : SV_POSITION, float2 local : TEXCOORD0) : SV_TARGET {
    float2 pixel = position.xy;
    float4 background = background_texture.SampleLevel(
        point_sampler, pixel / output_size, 0.0);
    float2 local_f = floor(local);
    int2 local_pixel = int2(local_f);
    float4 shape = cursor_texture.Load(int3(local_pixel, 0));
    if (cursor_mode == 0) {
        float alpha = saturate(shape.a);
        return float4(shape.rgb * alpha + background.rgb * (1.0 - alpha), 1.0);
    }

    uint3 background_bits = uint3(
        (uint)round(saturate(background.r) * 255.0),
        (uint)round(saturate(background.g) * 255.0),
        (uint)round(saturate(background.b) * 255.0));
    if (cursor_mode == 1) {
        uint3 shape_bits = uint3(
            (uint)round(saturate(shape.r) * 255.0),
            (uint)round(saturate(shape.g) * 255.0),
            (uint)round(saturate(shape.b) * 255.0));
        // DXGI_MASKED_COLOR uses alpha zero for replacement and nonzero
        // alpha for XOR.  Preserve the raw alpha in the normalized shape so
        // this distinction remains explicit in the shader.
        if (shape.a < 0.5) background_bits = shape_bits;
        else background_bits ^= shape_bits;
    } else {
        uint and_mask = (uint)round(saturate(shape.r) * 255.0);
        uint xor_mask = (uint)round(saturate(shape.g) * 255.0);
        uint3 and_bits = uint3(and_mask, and_mask, and_mask);
        uint3 xor_bits = uint3(xor_mask, xor_mask, xor_mask);
        background_bits = (background_bits & and_bits) ^ xor_bits;
    }
    return float4((float3)background_bits / 255.0, 1.0);
}
)";
}

std::string HResultDetail(const char* operation, HRESULT hr) {
    std::ostringstream text;
    text << operation << " failed with HRESULT 0x" << std::hex
         << static_cast<uint32_t>(hr);
    return text.str();
}

template <typename T>
void ReleaseInterface(T*& value) noexcept {
    if (value) value->Release();
    value = nullptr;
}

}  // namespace

Composer::~Composer() {
    Reset();
    device_ = nullptr;
    context_ = nullptr;
}

bool Composer::Initialize(ID3D11Device* device, ID3D11DeviceContext* context,
    uint32_t output_width, uint32_t output_height, std::string& detail) {
    Reset();
    if (!device || !context || output_width == 0 || output_height == 0) {
        detail = "DXGI cursor composer requires a valid device, context, and output size";
        return false;
    }
    device_ = device;
    context_ = context;
    output_width_ = output_width;
    output_height_ = output_height;
    output_format_ = DXGI_FORMAT_B8G8R8A8_UNORM;
    if (!EnsurePipeline(detail)) {
        Reset();
        return false;
    }
    // Allocate the output target during startup so a resource failure makes
    // Desktop AUTO take its normal exact-monitor WGC fallback path.
    if (!EnsureOutputResources(
            output_width_, output_height_, output_format_, detail)) {
        Reset();
        return false;
    }
    return true;
}

void Composer::ReleaseComResources() noexcept {
    ReleaseInterface(constants_);
    ReleaseInterface(sampler_);
    ReleaseInterface(pixel_shader_);
    ReleaseInterface(vertex_shader_);
    ReleaseInterface(cursor_srv_);
    ReleaseInterface(cursor_texture_);
    ReleaseInterface(background_srv_);
    ReleaseInterface(background_source_);
    ReleaseInterface(output_rtv_);
    ReleaseInterface(output_texture_);
}

void Composer::Reset() noexcept {
    ReleaseComResources();
    InvalidateGeneration();
    output_width_ = 0;
    output_height_ = 0;
    output_format_ = DXGI_FORMAT_UNKNOWN;
    device_ = nullptr;
    context_ = nullptr;
}

void Composer::InvalidateGeneration() noexcept {
    InvalidateBackground();
    normalized_shape_.clear();
    shape_width_ = 0;
    shape_height_ = 0;
    shape_ready_ = false;
    visible_ = false;
    position_ = {};
    last_update_time_ = 0;
    ReleaseInterface(cursor_srv_);
    ReleaseInterface(cursor_texture_);
}

void Composer::InvalidateBackground() noexcept {
    ReleaseInterface(background_srv_);
    ReleaseInterface(background_source_);
}

bool Composer::ValidateShape(
    const DXGI_OUTDUPL_POINTER_SHAPE_INFO& info,
    uint32_t buffer_size, std::string& detail) const {
    const uint32_t type = static_cast<uint32_t>(info.Type);
    if (type != kShapeTypeColor && type != kShapeTypeMonochrome
        && type != kShapeTypeMaskedColor) {
        detail = "DXGI cursor shape type is unsupported";
        return false;
    }
    if (info.Width == 0 || info.Height == 0
        || info.Width > kMaxShapeWidth || info.Height > kMaxShapeHeight) {
        detail = "DXGI cursor shape dimensions exceed the safe limit";
        return false;
    }
    if (type == kShapeTypeMonochrome && (info.Height & 1u) != 0) {
        detail = "DXGI monochrome cursor shape has an odd mask height";
        return false;
    }
    const uint32_t logical_height = type == kShapeTypeMonochrome
        ? info.Height / 2u : info.Height;
    if (logical_height == 0 || logical_height > kMaxShapeHeight) {
        detail = "DXGI cursor shape logical height is invalid";
        return false;
    }
    const uint64_t required = static_cast<uint64_t>(info.Pitch)
        * static_cast<uint64_t>(info.Height);
    const uint64_t minimum_pitch = type == kShapeTypeMonochrome
        ? (static_cast<uint64_t>(info.Width) + 7u) / 8u
        : static_cast<uint64_t>(info.Width) * 4u;
    if (info.Pitch < minimum_pitch || required == 0
        || required > kMaxShapeBytes || required > buffer_size) {
        detail = "DXGI cursor shape buffer has invalid pitch or size";
        return false;
    }
    return true;
}

bool Composer::BuildNormalizedShape(
    const DXGI_OUTDUPL_POINTER_SHAPE_INFO& info,
    const std::vector<uint8_t>& source, std::string& detail) {
    const uint32_t buffer_size = static_cast<uint32_t>(source.size());
    if (!ValidateShape(info, buffer_size, detail)) return false;
    const uint32_t type = static_cast<uint32_t>(info.Type);
    const uint32_t logical_height = type == kShapeTypeMonochrome
        ? info.Height / 2u : info.Height;
    const uint64_t pixels = static_cast<uint64_t>(info.Width) * logical_height;
    if (pixels > std::numeric_limits<size_t>::max() / 4u) {
        detail = "DXGI cursor shape pixel count overflows the normalized buffer";
        return false;
    }

    std::vector<uint8_t> normalized(static_cast<size_t>(pixels) * 4u, 0);
    if (type == kShapeTypeMonochrome) {
        const size_t row_bytes = static_cast<size_t>(info.Pitch);
        for (uint32_t y = 0; y < logical_height; ++y) {
            const uint8_t* and_row = source.data() + row_bytes * y;
            const uint8_t* xor_row = source.data() + row_bytes
                * (static_cast<size_t>(y) + logical_height);
            for (uint32_t x = 0; x < info.Width; ++x) {
                const uint8_t bit = static_cast<uint8_t>(0x80u >> (x & 7u));
                const uint8_t and_value = (and_row[x / 8u] & bit) ? 255u : 0u;
                const uint8_t xor_value = (xor_row[x / 8u] & bit) ? 255u : 0u;
                uint8_t* pixel = normalized.data()
                    + (static_cast<size_t>(y) * info.Width + x) * 4u;
                pixel[0] = and_value;
                pixel[1] = xor_value;
                pixel[2] = 0;
                pixel[3] = 255;
            }
        }
        shape_mode_ = ShapeMode::Monochrome;
    } else {
        for (uint32_t y = 0; y < logical_height; ++y) {
            const uint8_t* row = source.data()
                + static_cast<size_t>(info.Pitch) * y;
            for (uint32_t x = 0; x < info.Width; ++x) {
                const uint8_t* source_pixel = row + static_cast<size_t>(x) * 4u;
                uint8_t* pixel = normalized.data()
                    + (static_cast<size_t>(y) * info.Width + x) * 4u;
                // DXGI's COLOR and MASKED_COLOR data is BGRA.  The normalized
                // texture uses logical RGBA channels for the shader.
                pixel[0] = source_pixel[2];
                pixel[1] = source_pixel[1];
                pixel[2] = source_pixel[0];
                pixel[3] = source_pixel[3];
            }
        }
        shape_mode_ = type == kShapeTypeMaskedColor
            ? ShapeMode::MaskedColor : ShapeMode::Color;
    }

    normalized_shape_ = std::move(normalized);
    shape_width_ = info.Width;
    shape_height_ = logical_height;
    shape_ready_ = true;
    ReleaseInterface(cursor_srv_);
    ReleaseInterface(cursor_texture_);
    return true;
}

bool Composer::FetchShape(IDXGIOutputDuplication* duplication,
    uint32_t reported_size, std::string& detail) {
    if (!duplication) {
        detail = "DXGI cursor shape requested without an output duplication";
        return false;
    }
    if (reported_size > kMaxShapeBytes) {
        detail = "DXGI cursor shape buffer exceeds the safe limit";
        return false;
    }

    uint32_t required = 0;
    DXGI_OUTDUPL_POINTER_SHAPE_INFO shape_info{};
    std::vector<uint8_t> shape(static_cast<size_t>(reported_size));
    void* buffer = shape.empty() ? nullptr : shape.data();
    HRESULT hr = duplication->GetFramePointerShape(
        reported_size, buffer, &required, &shape_info);
    if (hr == DXGI_ERROR_MORE_DATA) {
        if (required == 0 || required > kMaxShapeBytes) {
            detail = "DXGI cursor shape retry size exceeds the safe limit";
            return false;
        }
        shape.assign(static_cast<size_t>(required), 0);
        shape_info = {};
        hr = duplication->GetFramePointerShape(
            required, shape.data(), &required, &shape_info);
    }
    if (FAILED(hr)) {
        detail = HResultDetail("IDXGIOutputDuplication::GetFramePointerShape", hr);
        return false;
    }
    const uint32_t actual_size = required == 0
        ? static_cast<uint32_t>(shape.size()) : required;
    if (actual_size > shape.size()) {
        detail = "DXGI cursor shape API returned more bytes than its bounded buffer";
        return false;
    }
    shape.resize(actual_size);
    return BuildNormalizedShape(shape_info, shape, detail);
}

bool Composer::ObserveFrame(IDXGIOutputDuplication* duplication,
    const DXGI_OUTDUPL_FRAME_INFO& frame_info, std::string& detail) {
    if (!device_ || !context_) {
        detail = "DXGI cursor composer is not initialized";
        return false;
    }

    const int64_t timestamp = frame_info.LastMouseUpdateTime.QuadPart;
    const bool has_new_position = timestamp != 0 && timestamp > last_update_time_;
    if (has_new_position) {
        last_update_time_ = timestamp;
        position_ = frame_info.PointerPosition.Position;
        // The desktop-duplication position is already output-local for this
        // selected output.  Visible=false means the OS already included the
        // cursor or the pointer is otherwise absent from the image; drawing it
        // again would create a duplicate cursor.
        visible_ = frame_info.PointerPosition.Visible != FALSE;
    }

    const bool shape_update = frame_info.PointerShapeBufferSize != 0;
    const bool missing_shape = !shape_ready_ && timestamp != 0 && visible_;
    if (shape_update || missing_shape) {
        if (!FetchShape(duplication, frame_info.PointerShapeBufferSize, detail)) {
            return false;
        }
    }
    if (visible_ && !shape_ready_) {
        detail = "DXGI cursor is visible but no valid pointer shape is available";
        return false;
    }
    return true;
}

bool Composer::EnsureOutputResources(uint32_t width, uint32_t height,
    DXGI_FORMAT format, std::string& detail) {
    if (output_texture_ && output_width_ == width && output_height_ == height
        && output_format_ == format) {
        return true;
    }
    ReleaseInterface(output_rtv_);
    ReleaseInterface(output_texture_);
    D3D11_TEXTURE2D_DESC description{};
    description.Width = width;
    description.Height = height;
    description.MipLevels = 1;
    description.ArraySize = 1;
    description.Format = format;
    description.SampleDesc.Count = 1;
    description.Usage = D3D11_USAGE_DEFAULT;
    description.BindFlags = kReplayTextureBindFlags;
    HRESULT hr = device_->CreateTexture2D(
        &description, nullptr, &output_texture_);
    if (FAILED(hr) || !output_texture_) {
        detail = HResultDetail("ID3D11Device::CreateTexture2D(cursor output)", hr);
        output_texture_ = nullptr;
        return false;
    }
    hr = device_->CreateRenderTargetView(
        output_texture_, nullptr, &output_rtv_);
    if (FAILED(hr) || !output_rtv_) {
        detail = HResultDetail(
            "ID3D11Device::CreateRenderTargetView(cursor output)", hr);
        ReleaseInterface(output_texture_);
        output_rtv_ = nullptr;
        return false;
    }
    output_width_ = width;
    output_height_ = height;
    output_format_ = format;
    return true;
}

bool Composer::EnsureBackgroundView(ID3D11Texture2D* background,
    std::string& detail) {
    if (!background) {
        detail = "DXGI cursor compositor received a null background texture";
        return false;
    }
    if (background_source_ == background && background_srv_) return true;
    ReleaseInterface(background_srv_);
    ReleaseInterface(background_source_);
    HRESULT hr = device_->CreateShaderResourceView(
        background, nullptr, &background_srv_);
    if (FAILED(hr) || !background_srv_) {
        detail = HResultDetail(
            "ID3D11Device::CreateShaderResourceView(cursor background)", hr);
        background_srv_ = nullptr;
        return false;
    }
    background->AddRef();
    background_source_ = background;
    return true;
}

bool Composer::EnsureCursorTexture(std::string& detail) {
    if (cursor_texture_ && cursor_srv_) return true;
    if (!shape_ready_ || shape_width_ == 0 || shape_height_ == 0
        || normalized_shape_.empty()) {
        detail = "DXGI cursor texture requested without a normalized shape";
        return false;
    }
    D3D11_TEXTURE2D_DESC description{};
    description.Width = shape_width_;
    description.Height = shape_height_;
    description.MipLevels = 1;
    description.ArraySize = 1;
    description.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    description.SampleDesc.Count = 1;
    description.Usage = D3D11_USAGE_DEFAULT;
    description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA initial{};
    initial.pSysMem = normalized_shape_.data();
    initial.SysMemPitch = shape_width_ * 4u;
    HRESULT hr = device_->CreateTexture2D(
        &description, &initial, &cursor_texture_);
    if (FAILED(hr) || !cursor_texture_) {
        detail = HResultDetail(
            "ID3D11Device::CreateTexture2D(cursor shape)", hr);
        cursor_texture_ = nullptr;
        return false;
    }
    hr = device_->CreateShaderResourceView(
        cursor_texture_, nullptr, &cursor_srv_);
    if (FAILED(hr) || !cursor_srv_) {
        detail = HResultDetail(
            "ID3D11Device::CreateShaderResourceView(cursor shape)", hr);
        ReleaseInterface(cursor_texture_);
        cursor_srv_ = nullptr;
        return false;
    }
    return true;
}

bool Composer::EnsurePipeline(std::string& detail) {
    if (vertex_shader_ && pixel_shader_ && sampler_ && constants_) return true;
    ID3DBlob* vertex_blob = nullptr;
    ID3DBlob* error_blob = nullptr;
    HRESULT hr = D3DCompile(CursorVertexShader(), std::strlen(CursorVertexShader()),
        "fthr_dxgi_cursor_vs", nullptr, nullptr, "main", "vs_5_0",
        D3DCOMPILE_ENABLE_STRICTNESS, 0, &vertex_blob, &error_blob);
    if (FAILED(hr) || !vertex_blob) {
        detail = HResultDetail("D3DCompile(cursor vertex shader)", hr);
        if (error_blob) error_blob->Release();
        if (vertex_blob) vertex_blob->Release();
        return false;
    }
    hr = device_->CreateVertexShader(
        vertex_blob->GetBufferPointer(), vertex_blob->GetBufferSize(),
        nullptr, &vertex_shader_);
    vertex_blob->Release();
    if (FAILED(hr) || !vertex_shader_) {
        detail = HResultDetail("ID3D11Device::CreateVertexShader(cursor)", hr);
        if (error_blob) error_blob->Release();
        return false;
    }
    if (error_blob) error_blob->Release();

    ID3DBlob* pixel_blob = nullptr;
    error_blob = nullptr;
    hr = D3DCompile(CursorPixelShader(), std::strlen(CursorPixelShader()),
        "fthr_dxgi_cursor_ps", nullptr, nullptr, "main", "ps_5_0",
        D3DCOMPILE_ENABLE_STRICTNESS, 0, &pixel_blob, &error_blob);
    if (FAILED(hr) || !pixel_blob) {
        detail = HResultDetail("D3DCompile(cursor pixel shader)", hr);
        if (error_blob) error_blob->Release();
        if (pixel_blob) pixel_blob->Release();
        return false;
    }
    hr = device_->CreatePixelShader(
        pixel_blob->GetBufferPointer(), pixel_blob->GetBufferSize(),
        nullptr, &pixel_shader_);
    pixel_blob->Release();
    if (FAILED(hr) || !pixel_shader_) {
        detail = HResultDetail("ID3D11Device::CreatePixelShader(cursor)", hr);
        if (error_blob) error_blob->Release();
        return false;
    }
    if (error_blob) error_blob->Release();

    D3D11_SAMPLER_DESC sampler_description{};
    sampler_description.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sampler_description.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler_description.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler_description.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler_description.MaxLOD = D3D11_FLOAT32_MAX;
    hr = device_->CreateSamplerState(&sampler_description, &sampler_);
    if (FAILED(hr) || !sampler_) {
        detail = HResultDetail("ID3D11Device::CreateSamplerState(cursor)", hr);
        return false;
    }

    D3D11_BUFFER_DESC constant_description{};
    constant_description.ByteWidth = 32;
    constant_description.Usage = D3D11_USAGE_DEFAULT;
    constant_description.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    hr = device_->CreateBuffer(
        &constant_description, nullptr, &constants_);
    if (FAILED(hr) || !constants_) {
        detail = HResultDetail("ID3D11Device::CreateBuffer(cursor constants)", hr);
        return false;
    }
    return true;
}

bool Composer::Compose(ID3D11Texture2D* background,
    ID3D11Texture2D** composed_texture, std::string& detail) {
    if (!composed_texture) {
        detail = "DXGI cursor compositor received a null output pointer";
        return false;
    }
    *composed_texture = nullptr;
    if (!background) {
        detail = "DXGI cursor compositor received a null background texture";
        return false;
    }
    if (!visible_) {
        *composed_texture = background;
        return true;
    }
    if (!shape_ready_) {
        detail = "DXGI cursor is visible but no valid pointer shape is available";
        return false;
    }

    D3D11_TEXTURE2D_DESC background_description{};
    background->GetDesc(&background_description);
    if (background_description.Width == 0 || background_description.Height == 0
        || background_description.SampleDesc.Count != 1) {
        detail = "DXGI cursor background texture has an invalid description";
        return false;
    }
    const int64_t cursor_left = position_.x;
    const int64_t cursor_top = position_.y;
    const int64_t cursor_right = cursor_left + shape_width_;
    const int64_t cursor_bottom = cursor_top + shape_height_;
    if (cursor_right <= 0 || cursor_bottom <= 0
        || cursor_left >= static_cast<int64_t>(background_description.Width)
        || cursor_top >= static_cast<int64_t>(background_description.Height)) {
        *composed_texture = background;
        return true;
    }
    if (!EnsureOutputResources(background_description.Width,
            background_description.Height, background_description.Format, detail)
        || !EnsureBackgroundView(background, detail)
        || !EnsureCursorTexture(detail)
        || !EnsurePipeline(detail)) {
        return false;
    }

    struct Constants {
        float output_size[2];
        float cursor_position[2];
        float cursor_size[2];
        uint32_t cursor_mode;
        uint32_t unused;
    } constants{};
    constants.output_size[0] = static_cast<float>(background_description.Width);
    constants.output_size[1] = static_cast<float>(background_description.Height);
    constants.cursor_position[0] = static_cast<float>(position_.x);
    constants.cursor_position[1] = static_cast<float>(position_.y);
    constants.cursor_size[0] = static_cast<float>(shape_width_);
    constants.cursor_size[1] = static_cast<float>(shape_height_);
    constants.cursor_mode = static_cast<uint32_t>(shape_mode_);
    context_->UpdateSubresource(constants_, 0, nullptr, &constants, 0, 0);

    // Keep the replay cache immutable.  The output texture is the only
    // render target used by the cursor pass, and starts from a complete GPU
    // copy of the clean replay background on every visible-cursor frame.
    if (output_texture_ != background) {
        context_->CopyResource(output_texture_, background);
    }

    D3D11_VIEWPORT viewport{};
    viewport.Width = static_cast<float>(background_description.Width);
    viewport.Height = static_cast<float>(background_description.Height);
    viewport.MaxDepth = 1.0f;
    ID3D11RenderTargetView* render_target = output_rtv_;
    ID3D11ShaderResourceView* resources[2] = {background_srv_, cursor_srv_};
    context_->OMSetRenderTargets(1, &render_target, nullptr);
    context_->OMSetBlendState(nullptr, nullptr, 0xffffffffu);
    context_->RSSetViewports(1, &viewport);
    context_->IASetInputLayout(nullptr);
    context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    context_->VSSetShader(vertex_shader_, nullptr, 0);
    context_->PSSetShader(pixel_shader_, nullptr, 0);
    context_->PSSetSamplers(0, 1, &sampler_);
    context_->VSSetConstantBuffers(0, 1, &constants_);
    context_->PSSetConstantBuffers(0, 1, &constants_);
    context_->PSSetShaderResources(0, 2, resources);
    context_->Draw(4, 0);

    // The capture engine owns this immediate context.  Leave every binding
    // touched above clear so the following encoder/readback path cannot retain
    // a cursor SRV or the compositor RTV.
    ID3D11ShaderResourceView* null_resources[2] = {nullptr, nullptr};
    ID3D11SamplerState* null_sampler[1] = {nullptr};
    ID3D11Buffer* null_constants[1] = {nullptr};
    context_->PSSetShaderResources(0, 2, null_resources);
    context_->PSSetSamplers(0, 1, null_sampler);
    context_->VSSetConstantBuffers(0, 1, null_constants);
    context_->PSSetConstantBuffers(0, 1, null_constants);
    context_->VSSetShader(nullptr, nullptr, 0);
    context_->PSSetShader(nullptr, nullptr, 0);
    context_->OMSetRenderTargets(0, nullptr, nullptr);
    *composed_texture = output_texture_;
    return true;
}

}  // namespace fthr::dxgi_cursor
