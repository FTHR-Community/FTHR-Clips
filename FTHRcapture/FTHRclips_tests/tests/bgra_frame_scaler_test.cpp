#include "bgra_frame_scaler.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <wrl/client.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <vector>

extern "C" {
#include <libswscale/swscale.h>
}

namespace {

using Microsoft::WRL::ComPtr;
struct Pixel { uint8_t b, g, r, a; };
static_assert(sizeof(Pixel) == 4);

int checks = 0;
void Check(bool value, const char* message, const std::string& detail = {}) {
    ++checks;
    if (value) return;
    std::cerr << "FAILED: " << message;
    if (!detail.empty()) std::cerr << " (" << detail << ')';
    std::cerr << '\n';
    std::exit(1);
}

ComPtr<ID3D11Texture2D> Texture(ID3D11Device* device, uint32_t width, uint32_t height,
                              UINT bindings, const std::vector<Pixel>* pixels = nullptr) {
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = bindings;
    D3D11_SUBRESOURCE_DATA input{};
    if (pixels) {
        input.pSysMem = pixels->data();
        input.SysMemPitch = width * sizeof(Pixel);
    }
    ComPtr<ID3D11Texture2D> texture;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, pixels ? &input : nullptr, &texture)),
        "create BGRA test texture");
    return texture;
}

std::vector<Pixel> Readback(ID3D11Device* device, ID3D11DeviceContext* context,
                            ID3D11Texture2D* texture, uint32_t width, uint32_t height) {
    D3D11_TEXTURE2D_DESC desc{};
    texture->GetDesc(&desc);
    Check(desc.Width == width && desc.Height == height && desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM,
        "scaled texture preserves target geometry and format");
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    Check(SUCCEEDED(device->CreateTexture2D(&desc, nullptr, &staging)), "create BGRA test readback");
    context->CopyResource(staging.Get(), texture);
    D3D11_MAPPED_SUBRESOURCE mapped{};
    Check(SUCCEEDED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped)), "map BGRA test output");
    std::vector<Pixel> result(static_cast<size_t>(width) * height);
    for (uint32_t y = 0; y < height; ++y) {
        std::memcpy(result.data() + static_cast<size_t>(y) * width,
            static_cast<const uint8_t*>(mapped.pData) + static_cast<size_t>(y) * mapped.RowPitch,
            width * sizeof(Pixel));
    }
    context->Unmap(staging.Get(), 0);
    return result;
}

std::vector<Pixel> Scale(ID3D11Device* device, ID3D11DeviceContext* context,
                         fthr::BgraFrameScaler& scaler, ID3D11Texture2D* source,
                         uint32_t width, uint32_t height) {
    ID3D11Texture2D* output = nullptr;
    std::string error;
    const bool scaled = scaler.Scale(source, output, error);
    Check(scaled && output, "scale BGRA fixture", error);
    return Readback(device, context, output, width, height);
}

void CheckerDownscale(ID3D11Device* device, ID3D11DeviceContext* context) {
    std::vector<Pixel> pixels(64 * 64);
    for (uint32_t y = 0; y < 64; ++y) {
        for (uint32_t x = 0; x < 64; ++x) {
            const auto value = static_cast<uint8_t>(((x / 2 + y / 2) % 2) * 255);
            pixels[y * 64 + x] = {value, value, value, 0};
        }
    }
    auto source = Texture(device, 64, 64, 0, &pixels);
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(64, 64, 32, 32, false, geometry), "checker geometry");
    fthr::BgraFrameScaler scaler;
    std::string error;
    Check(!scaler.Initialize(device, context, {}, error), "empty scaler geometry is rejected");
    const bool initialized = scaler.Initialize(device, context, geometry, error);
    Check(initialized, "initialize checker scaler", error);
    const auto result = Scale(device, context, scaler, source.Get(), 32, 32);
    bool antialiased = true;
    for (uint32_t y = 2; y < 30; ++y) {
        for (uint32_t x = 2; x < 30; ++x) {
            const int expected = (x + y) % 2 ? 159 : 96;
            const auto pixel = result[y * 32 + x];
            antialiased &= std::abs(pixel.b - expected) <= 1 && pixel.b == pixel.g
                && pixel.g == pixel.r && pixel.a == 255;
        }
    }
    Check(antialiased, "2x shrink uses widened triangle rather than plain bilinear sampling");

    ID3D11Texture2D* missing = source.Get();
    auto wrong_size = Texture(device, 62, 64, 0);
    Check(!scaler.Scale(wrong_size.Get(), missing, error) && !missing,
        "a mismatched source fails without publishing stale output");
    Check(fthr::BuildCaptureScaleGeometry(64, 64, 2, 2, false, geometry)
        && !scaler.Initialize(device, context, geometry, error), "oversized tap footprint is rejected");
    Check(!scaler.Scale(source.Get(), missing, error) && !missing,
        "failed reinitialization clears the previous scaler");
    const bool reinitialized = fthr::BuildCaptureScaleGeometry(64, 64, 32, 32, false, geometry)
        && scaler.Initialize(device, context, geometry, error);
    Check(reinitialized, "reinitialize after rejecting unsupported geometry", error);
    const auto repeated = Scale(device, context, scaler, source.Get(), 32, 32);
    Check(std::memcmp(result.data(), repeated.data(), result.size() * sizeof(Pixel)) == 0,
        "reinitialized scaler reproduces the previous output");
}

void FitAndRefresh(ID3D11Device* device, ID3D11DeviceContext* context) {
    std::vector<Pixel> input(64 * 36, {29, 111, 215, 0});
    auto source = Texture(device, 64, 36, D3D11_BIND_SHADER_RESOURCE, &input);
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(64, 36, 32, 32, true, geometry), "Fit geometry");
    fthr::BgraFrameScaler scaler;
    std::string error;
    const bool initialized = scaler.Initialize(device, context, geometry, error);
    Check(initialized, "initialize Fit scaler", error);
    for (const auto color : {Pixel{29, 111, 215, 0}, Pixel{220, 59, 12, 127}}) {
        std::fill(input.begin(), input.end(), color);
        context->UpdateSubresource(source.Get(), 0, nullptr, input.data(), 64 * sizeof(Pixel), 0);
        const auto result = Scale(device, context, scaler, source.Get(), 32, 32);
        bool expected = true;
        for (uint32_t y = 0; y < 32; ++y) {
            const bool inside = y >= geometry.destination.top
                && y < geometry.destination.top + geometry.destination.height;
            for (uint32_t x = 0; x < 32; ++x) {
                const auto pixel = result[y * 32 + x];
                expected &= pixel.b == (inside ? color.b : 0) && pixel.g == (inside ? color.g : 0)
                    && pixel.r == (inside ? color.r : 0) && pixel.a == 255;
            }
        }
        Check(expected, "Fit refresh preserves constant full-range colors independently of input alpha and clears bars");
    }
}

void NonintegerCroppedInput(ID3D11Device* device, ID3D11DeviceContext* context) {
    constexpr uint32_t canvas_w = 72, canvas_h = 32, src_w = 64, src_h = 24;
    constexpr uint32_t out_w = 28, out_h = 40, left = 3, top = 4;
    std::vector<Pixel> pixels(canvas_w * canvas_h + 16);
    for (uint32_t y = 0; y < canvas_h; ++y) {
        for (uint32_t x = 0; x < canvas_w; ++x) {
            const uint8_t checker = static_cast<uint8_t>(((x / 2 + y / 2) % 2) * 100);
            pixels[y * canvas_w + x] = {static_cast<uint8_t>(checker + x),
                static_cast<uint8_t>(checker + y * 2), static_cast<uint8_t>(x * 3), 0};
        }
    }
    auto canvas = Texture(device, canvas_w, canvas_h, 0, &pixels);
    auto source = Texture(device, src_w, src_h, 0);
    const D3D11_BOX box{left, top, 0, left + src_w, top + src_h, 1};
    context->CopySubresourceRegion(source.Get(), 0, 0, 0, 0, canvas.Get(), 0, &box);
    fthr::CaptureScaleGeometry geometry;
    Check(fthr::BuildCaptureScaleGeometry(src_w, src_h, out_w, out_h, false, geometry), "mixed-axis crop geometry");
    fthr::BgraFrameScaler scaler;
    std::string error;
    const bool initialized = scaler.Initialize(device, context, geometry, error);
    Check(initialized, "initialize noninteger scaler", error);
    const auto result = Scale(device, context, scaler, source.Get(), out_w, out_h);

    // Keep input chroma at full resolution, like the shader's RGB taps. The
    // legacy default packed-BGRA path subsamples color before scaling.
    SwsContext* reference = sws_getContext(src_w, src_h, AV_PIX_FMT_BGRA,
        out_w, out_h, AV_PIX_FMT_BGRA, SWS_BILINEAR | SWS_FULL_CHR_H_INP,
        nullptr, nullptr, nullptr);
    Check(reference != nullptr, "initialize full-chroma mixed-axis bilinear reference");
    constexpr int target_pitch = 128;
    std::vector<uint8_t> target(target_pitch * out_h + 64);
    const uint8_t* input_planes[] = {reinterpret_cast<const uint8_t*>(pixels.data() + top * canvas_w + left),
        nullptr, nullptr, nullptr};
    const int input_strides[] = {canvas_w * sizeof(Pixel), 0, 0, 0};
    uint8_t* output_planes[] = {target.data(), nullptr, nullptr, nullptr};
    const int output_strides[] = {target_pitch, 0, 0, 0};
    const int lines = sws_scale(reference, input_planes, input_strides, 0, src_h, output_planes, output_strides);
    sws_freeContext(reference);
    Check(lines == out_h, "full-chroma mixed-axis bilinear reference scales all lines");
    int maximum = 0;
    for (uint32_t y = 0; y < out_h; ++y) {
        for (uint32_t x = 0; x < out_w; ++x) {
            const auto actual = result[y * out_w + x];
            const auto* expected = target.data() + y * target_pitch + x * sizeof(Pixel);
            maximum = std::max({maximum, std::abs(actual.b - expected[0]),
                std::abs(actual.g - expected[1]), std::abs(actual.r - expected[2])});
        }
    }
    Check(maximum <= 2, "noninteger mixed-axis crop agrees with full-chroma antialiased RGB reference",
        "maximum channel difference=" + std::to_string(maximum));
}

} // namespace

int RunBgraFrameScalerTests() {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    Check(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context)),
        "create WARP scaler test device");
    CheckerDownscale(device.Get(), context.Get());
    FitAndRefresh(device.Get(), context.Get());
    NonintegerCroppedInput(device.Get(), context.Get());
    std::cout << "BGRA frame scaler checks: " << checks << '\n';
    return checks;
}
