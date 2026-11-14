#include "dxgi_cursor.h"

#include <d3d11.h>
#include <wrl/client.h>

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

namespace {

int cursor_checks = 0;

void CheckCursor(bool condition, const char* message) {
    ++cursor_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

struct FakeDuplication final : IDXGIOutputDuplication {
    ULONG references = 1;
    std::vector<uint8_t> shape;
    DXGI_OUTDUPL_POINTER_SHAPE_INFO shape_info{};
    uint32_t shape_calls = 0;
    uint32_t required_override = 0;
    HRESULT forced_success = S_OK;

    HRESULT STDMETHODCALLTYPE QueryInterface(
        REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IDXGIObject)
            || iid == __uuidof(IDXGIOutputDuplication)) {
            *object = static_cast<IDXGIOutputDuplication*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++references; }
    ULONG STDMETHODCALLTYPE Release() override {
        if (references > 1) --references;
        return references;
    }
    HRESULT STDMETHODCALLTYPE SetPrivateData(
        REFGUID, UINT, const void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(
        REFGUID, const IUnknown*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetPrivateData(
        REFGUID, UINT*, void*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID, void**) override {
        return E_NOTIMPL;
    }
    void STDMETHODCALLTYPE GetDesc(DXGI_OUTDUPL_DESC* description) override {
        if (description) *description = {};
    }
    HRESULT STDMETHODCALLTYPE AcquireNextFrame(
        UINT, DXGI_OUTDUPL_FRAME_INFO*, IDXGIResource**) override {
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE GetFrameDirtyRects(
        UINT, RECT*, UINT*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetFrameMoveRects(
        UINT, DXGI_OUTDUPL_MOVE_RECT*, UINT*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetFramePointerShape(
        UINT buffer_size, void* buffer, UINT* required,
        DXGI_OUTDUPL_POINTER_SHAPE_INFO* info) override {
        if (!required || !info) return E_POINTER;
        ++shape_calls;
        const uint32_t required_size = required_override != 0
            ? required_override : static_cast<uint32_t>(shape.size());
        *required = required_size;
        if (buffer_size < required_size || !buffer) return DXGI_ERROR_MORE_DATA;
        if (forced_success != S_OK) return forced_success;
        std::memcpy(buffer, shape.data(), shape.size());
        *info = shape_info;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE MapDesktopSurface(
        DXGI_MAPPED_RECT*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE UnMapDesktopSurface() override {
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE ReleaseFrame() override { return S_OK; }
};

struct TextureSet {
    ID3D11Texture2D* source = nullptr;
    ID3D11Texture2D* staging = nullptr;
    uint32_t width = 0;
    uint32_t height = 0;

    TextureSet() = default;
    TextureSet(const TextureSet&) = delete;
    TextureSet& operator=(const TextureSet&) = delete;
    TextureSet(TextureSet&& other) noexcept
        : source(other.source), staging(other.staging), width(other.width),
          height(other.height) {
        other.source = nullptr;
        other.staging = nullptr;
    }
    TextureSet& operator=(TextureSet&& other) noexcept {
        if (this == &other) return *this;
        if (staging) staging->Release();
        if (source) source->Release();
        source = other.source;
        staging = other.staging;
        width = other.width;
        height = other.height;
        other.source = nullptr;
        other.staging = nullptr;
        return *this;
    }

    ~TextureSet() {
        if (staging) staging->Release();
        if (source) source->Release();
    }
};

TextureSet MakeBackground(ID3D11Device* device, uint32_t width,
    uint32_t height, const std::vector<uint8_t>& pixels) {
    TextureSet textures;
    textures.width = width;
    textures.height = height;
    D3D11_TEXTURE2D_DESC source_desc{};
    source_desc.Width = width;
    source_desc.Height = height;
    source_desc.MipLevels = 1;
    source_desc.ArraySize = 1;
    source_desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    source_desc.SampleDesc.Count = 1;
    source_desc.Usage = D3D11_USAGE_DEFAULT;
    source_desc.BindFlags = fthr::dxgi_cursor::kReplayTextureBindFlags;
    D3D11_SUBRESOURCE_DATA initial{};
    initial.pSysMem = pixels.data();
    initial.SysMemPitch = width * 4u;
    CheckCursor(SUCCEEDED(device->CreateTexture2D(
        &source_desc, &initial, &textures.source)),
        "WARP source texture creation succeeds");

    D3D11_TEXTURE2D_DESC staging_desc = source_desc;
    staging_desc.Usage = D3D11_USAGE_STAGING;
    staging_desc.BindFlags = 0;
    staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    CheckCursor(SUCCEEDED(device->CreateTexture2D(
        &staging_desc, nullptr, &textures.staging)),
        "WARP staging texture creation succeeds");
    return textures;
}

std::vector<uint8_t> ReadTexture(ID3D11DeviceContext* context,
    ID3D11Texture2D* texture, ID3D11Texture2D* staging,
    uint32_t width, uint32_t height) {
    context->CopyResource(staging, texture);
    D3D11_MAPPED_SUBRESOURCE mapped{};
    CheckCursor(SUCCEEDED(context->Map(
        staging, 0, D3D11_MAP_READ, 0, &mapped)),
        "WARP output readback maps");
    std::vector<uint8_t> pixels(static_cast<size_t>(width) * height * 4u);
    for (uint32_t y = 0; y < height; ++y) {
        std::memcpy(pixels.data() + static_cast<size_t>(y) * width * 4u,
            static_cast<const uint8_t*>(mapped.pData)
                + static_cast<size_t>(y) * mapped.RowPitch,
            static_cast<size_t>(width) * 4u);
    }
    context->Unmap(staging, 0);
    return pixels;
}

void SetColorShape(FakeDuplication& duplication, uint32_t width,
    uint32_t height, const std::vector<uint8_t>& bgra,
    POINT hotspot = {}) {
    duplication.shape = bgra;
    duplication.shape_info = {};
    duplication.shape_info.Width = width;
    duplication.shape_info.Height = height;
    duplication.shape_info.Pitch = width * 4u;
    duplication.shape_info.HotSpot = hotspot;
    duplication.shape_info.Type = DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR;
}

DXGI_OUTDUPL_FRAME_INFO PointerFrame(int64_t timestamp, LONG x, LONG y,
    bool visible, uint32_t shape_size) {
    DXGI_OUTDUPL_FRAME_INFO frame{};
    frame.LastMouseUpdateTime.QuadPart = timestamp;
    frame.PointerPosition.Position = {x, y};
    frame.PointerPosition.Visible = visible ? TRUE : FALSE;
    frame.PointerShapeBufferSize = shape_size;
    return frame;
}

void CreateWarp(ID3D11Device** device, ID3D11DeviceContext** context) {
    D3D_FEATURE_LEVEL level{};
    CheckCursor(SUCCEEDED(D3D11CreateDevice(
        nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
        D3D11_SDK_VERSION, device, &level, context)),
        "WARP device creation succeeds");
}

void ColorAlphaAndMoveHide(ID3D11Device* device,
    ID3D11DeviceContext* context) {
    constexpr uint32_t width = 4;
    constexpr uint32_t height = 4;
    std::vector<uint8_t> background(width * height * 4u, 0);
    for (uint32_t i = 0; i < width * height; ++i) {
        background[i * 4u + 0] = 10;
        background[i * 4u + 1] = 20;
        background[i * 4u + 2] = 30;
        background[i * 4u + 3] = 255;
    }
    TextureSet textures = MakeBackground(device, width, height, background);
    fthr::dxgi_cursor::Composer composer;
    std::string detail;
    CheckCursor(composer.Initialize(device, context, width, height, detail),
        "cursor compositor initializes on WARP");
    FakeDuplication duplication;
    SetColorShape(duplication, 2, 2, {
        0, 0, 255, 128, 0, 255, 0, 255,
        255, 0, 0, 255, 0, 0, 0, 0});
    auto frame = PointerFrame(1, 0, 0, true,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, frame, detail),
        "color pointer metadata is accepted");
    ID3D11Texture2D* output = nullptr;
    CheckCursor(composer.Compose(textures.source, &output, detail)
            && output && output != textures.source,
        "visible color cursor uses a separate output texture");
    const auto pixels = ReadTexture(context, output, textures.staging, width, height);
    CheckCursor(pixels[0] >= 4 && pixels[0] <= 7
            && pixels[1] >= 9 && pixels[1] <= 12
            && pixels[2] >= 140 && pixels[2] <= 146,
        "color cursor alpha blends over the clean background");
    const auto unchanged = ReadTexture(context, textures.source,
        textures.staging, width, height);
    CheckCursor(unchanged == background,
        "cursor composition never mutates the clean background");

    auto moved = PointerFrame(2, 2, 2, true,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, moved, detail),
        "cursor move metadata is accepted");
    CheckCursor(composer.Compose(textures.source, &output, detail),
        "moved cursor composes from a fresh clean background");
    const auto moved_pixels = ReadTexture(context, output, textures.staging,
        width, height);
    CheckCursor(moved_pixels[0] == background[0]
            && moved_pixels[static_cast<size_t>(2 * width + 2) * 4u] != background[
                static_cast<size_t>(2 * width + 2) * 4u],
        "cursor move leaves no stale pixels behind");

    auto hidden = PointerFrame(3, 2, 2, false,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, hidden, detail),
        "hidden cursor metadata is accepted");
    CheckCursor(composer.Compose(textures.source, &output, detail)
            && output == textures.source,
        "hidden cursor passes through the source without double drawing");
}

void MaskedXorAndMonochrome(ID3D11Device* device,
    ID3D11DeviceContext* context) {
    {
        std::vector<uint8_t> background = {0x0f, 0xf0, 0x55, 255};
        for (uint8_t alpha : {uint8_t(0), uint8_t(255)}) {
            TextureSet textures = MakeBackground(device, 1, 1, background);
            fthr::dxgi_cursor::Composer composer;
            std::string detail;
            CheckCursor(composer.Initialize(device, context, 1, 1, detail),
                "masked cursor compositor initializes");
            FakeDuplication duplication;
            duplication.shape = {0xf0, 0x0f, 0xaa, alpha};
            duplication.shape_info = {};
            duplication.shape_info.Width = 1;
            duplication.shape_info.Height = 1;
            duplication.shape_info.Pitch = 4;
            duplication.shape_info.Type =
                DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR;
            const auto frame = PointerFrame(1, 0, 0, true, 4);
            CheckCursor(composer.ObserveFrame(&duplication, frame, detail),
                "masked cursor metadata is accepted");
            ID3D11Texture2D* output = nullptr;
            CheckCursor(composer.Compose(textures.source, &output, detail),
                "masked cursor composes");
            const auto pixels = ReadTexture(
                context, output, textures.staging, 1, 1);
            const std::vector<uint8_t> expected = alpha == 0
                ? std::vector<uint8_t>{0xf0, 0x0f, 0xaa, 255}
                : std::vector<uint8_t>{0xff, 0xff, 0xff, 255};
            CheckCursor(pixels == expected,
                alpha == 0
                    ? "masked color alpha zero replaces RGB"
                    : "masked color alpha 255 applies XOR semantics");
        }
    }
    {
        std::vector<uint8_t> background = {0x0f, 0xf0, 0x55, 255,
                                           0xff, 0x00, 0xaa, 255,
                                           0x33, 0x77, 0x99, 255,
                                           0x80, 0x40, 0x20, 255};
        TextureSet textures = MakeBackground(device, 4, 1, background);
        fthr::dxgi_cursor::Composer composer;
        std::string detail;
        CheckCursor(composer.Initialize(device, context, 4, 1, detail),
            "monochrome cursor compositor initializes");
        FakeDuplication duplication;
        duplication.shape = {0xa0, 0x60};  // AND=1010, XOR=0110: all four combinations.
        duplication.shape_info = {};
        duplication.shape_info.Width = 4;
        duplication.shape_info.Height = 2;
        duplication.shape_info.Pitch = 1;
        duplication.shape_info.Type = DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME;
        const auto frame = PointerFrame(1, 0, 0, true, 2);
        CheckCursor(composer.ObserveFrame(&duplication, frame, detail),
            "monochrome cursor metadata is accepted");
        ID3D11Texture2D* output = nullptr;
        CheckCursor(composer.Compose(textures.source, &output, detail),
            "monochrome cursor composes");
        const auto pixels = ReadTexture(context, output, textures.staging, 4, 1);
        for (uint32_t x = 0; x < 4; ++x) {
            const uint8_t and_mask = (0xa0u & (0x80u >> x)) ? 255u : 0u;
            const uint8_t xor_mask = (0x60u & (0x80u >> x)) ? 255u : 0u;
            const size_t offset = static_cast<size_t>(x) * 4u;
            for (uint32_t channel = 0; channel < 3; ++channel) {
                const uint8_t expected =
                    (background[offset + channel] & and_mask) ^ xor_mask;
                CheckCursor(std::abs(static_cast<int>(pixels[offset + channel])
                        - static_cast<int>(expected)) <= 1,
                    "monochrome cursor applies AND then XOR masks");
            }
        }
    }
}

void BoundsStaleAndReset(ID3D11Device* device,
    ID3D11DeviceContext* context) {
    std::vector<uint8_t> background(3u * 3u * 4u, 0x42);
    for (size_t i = 3; i < background.size(); i += 4) background[i] = 255;
    TextureSet textures = MakeBackground(device, 3, 3, background);
    fthr::dxgi_cursor::Composer composer;
    std::string detail;
    CheckCursor(composer.Initialize(device, context, 3, 3, detail),
        "bounds cursor compositor initializes");
    FakeDuplication duplication;
    SetColorShape(duplication, 2, 2, {
        0, 0, 255, 255, 0, 255, 0, 255,
        255, 0, 0, 255, 255, 255, 255, 255}, POINT{1, 1});
    const auto first = PointerFrame(4, -1, -1, true,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, first, detail),
        "negative cursor position is accepted");
    ID3D11Texture2D* output = nullptr;
    CheckCursor(composer.Compose(textures.source, &output, detail),
        "partially offscreen cursor composes safely");
    const auto clipped = ReadTexture(
        context, output, textures.staging, 3, 3);
    CheckCursor(clipped[0] == 0xff && clipped[1] == 0xff
            && clipped[2] == 0xff
            && clipped[4] == 0x42 && clipped[5] == 0x42
            && clipped[6] == 0x42,
        "offscreen clipping uses the output-local top-left without hotspot subtraction");

    const auto outside = PointerFrame(5, 100, 100, true,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, outside, detail),
        "fully offscreen cursor metadata is accepted");
    CheckCursor(composer.Compose(textures.source, &output, detail)
            && output == textures.source,
        "fully offscreen cursor returns the clean background directly");

    const auto stale = PointerFrame(4, 2, 2, false,
        static_cast<uint32_t>(duplication.shape.size()));
    CheckCursor(composer.ObserveFrame(&duplication, stale, detail),
        "stale timestamp is ignored without failure");
    CheckCursor(composer.Visible(), "stale timestamp preserves the last cursor state");
    const auto zero_time = PointerFrame(0, 2, 2, false, 0);
    CheckCursor(composer.ObserveFrame(&duplication, zero_time, detail),
        "zero timestamp is ignored without failure");
    composer.InvalidateGeneration();
    CheckCursor(!composer.Visible() && !composer.HasShape(),
        "generation reset invalidates position and shape state");
    CheckCursor(composer.Compose(textures.source, &output, detail)
            && output == textures.source,
        "reset cursor state cannot double draw a stale cursor");
}

void PointerOnlyPreservesCursorThroughBackgroundInvalidation(
    ID3D11Device* device, ID3D11DeviceContext* context) {
    const std::vector<uint8_t> background = {10, 20, 30, 255};
    TextureSet textures = MakeBackground(device, 1, 1, background);
    fthr::dxgi_cursor::Composer composer;
    std::string detail;
    CheckCursor(composer.Initialize(device, context, 1, 1, detail),
        "pointer-only compositor initializes");
    FakeDuplication duplication;
    SetColorShape(duplication, 1, 1, {0, 0, 255, 255});
    const auto pointer_only = PointerFrame(1, 0, 0, true, 0);
    CheckCursor(composer.ObserveFrame(&duplication, pointer_only, detail),
        "pointer-only frame fetches the initial shape with bounded retry");
    CheckCursor(duplication.shape_calls == 2,
        "pointer-only initial shape uses one bounded MORE_DATA retry");
    composer.InvalidateBackground();
    const auto image_without_mouse_update = PointerFrame(0, 0, 0, true, 0);
    CheckCursor(composer.ObserveFrame(
            &duplication, image_without_mouse_update, detail),
        "desktop image with zero mouse timestamp keeps pointer metadata");
    ID3D11Texture2D* output = nullptr;
    CheckCursor(composer.Compose(textures.source, &output, detail)
            && output != textures.source,
        "pointer-only cursor survives background invalidation until generation reset");
}

DXGI_OUTDUPL_POINTER_SHAPE_INFO ShapeInfo(
    DXGI_OUTDUPL_POINTER_SHAPE_TYPE type, UINT width, UINT height, UINT pitch) {
    DXGI_OUTDUPL_POINTER_SHAPE_INFO info{};
    info.Type = type;
    info.Width = width;
    info.Height = height;
    info.Pitch = pitch;
    return info;
}

void InvalidShapesAreRejectedWithoutUnboundedAllocation(
    ID3D11Device* device, ID3D11DeviceContext* context) {
    struct InvalidCase {
        DXGI_OUTDUPL_POINTER_SHAPE_INFO info;
        std::vector<uint8_t> bytes;
        uint32_t reported_size;
        const char* message;
    };
    InvalidCase cases[] = {
        {ShapeInfo(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR, 0, 1, 4),
            {0, 0, 0, 255}, 4,
            "zero-width cursor shape is rejected"},
        {ShapeInfo(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR, 1, 0, 4),
            {0, 0, 0, 255}, 4,
            "zero-height cursor shape is rejected"},
        {ShapeInfo(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME, 1, 3, 1),
            {0, 0, 0}, 3,
            "odd monochrome mask height is rejected"},
        {ShapeInfo(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR, 2, 1, 3),
            {0, 0, 0}, 3,
            "color shape pitch below minimum is rejected"},
        {ShapeInfo(DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR, 2, 2, 8),
            {0, 0, 0, 255}, 4,
            "insufficient shape buffer is rejected"},
        {ShapeInfo(static_cast<DXGI_OUTDUPL_POINTER_SHAPE_TYPE>(99), 1, 1, 4),
            {0, 0, 0, 255}, 4, "unsupported cursor shape type is rejected"},
    };
    for (const auto& invalid : cases) {
        fthr::dxgi_cursor::Composer composer;
        std::string detail;
        CheckCursor(composer.Initialize(device, context, 1, 1, detail),
            "malformed-shape compositor initializes");
        FakeDuplication duplication;
        duplication.shape = invalid.bytes;
        duplication.shape_info = invalid.info;
        CheckCursor(!composer.ObserveFrame(
                &duplication,
                PointerFrame(1, 0, 0, true, invalid.reported_size), detail),
            invalid.message);
    }

    {
        fthr::dxgi_cursor::Composer composer;
        std::string detail;
        CheckCursor(composer.Initialize(device, context, 1, 1, detail),
            "bounded-shape compositor initializes");
        FakeDuplication duplication;
        SetColorShape(duplication, 1, 1, {0, 0, 255, 255});
        CheckCursor(!composer.ObserveFrame(
                &duplication,
                PointerFrame(1, 0, 0, true,
                    fthr::dxgi_cursor::Composer::kMaxShapeBytes + 1u), detail),
            "reported cursor shape cap is rejected before allocation");
        CheckCursor(duplication.shape_calls == 0,
            "oversized cursor shape does not call the duplication API");
    }

    {
        fthr::dxgi_cursor::Composer composer;
        std::string detail;
        CheckCursor(composer.Initialize(device, context, 1, 1, detail),
            "MORE_DATA error compositor initializes");
        FakeDuplication duplication;
        SetColorShape(duplication, 1, 1, {0, 0, 255, 255});
        duplication.required_override = fthr::dxgi_cursor::Composer::kMaxShapeBytes + 1u;
        CheckCursor(!composer.ObserveFrame(
                &duplication, PointerFrame(1, 0, 0, true, 0), detail),
            "MORE_DATA retry cap is rejected");
        CheckCursor(duplication.shape_calls == 1,
            "oversized MORE_DATA response does not retry or allocate");
    }
}

// Reproduce the production 1440p-to-1080p video-processor path with every
// compositor result, including the clean background returned for a hidden cursor.
void CursorStatesRemainValidVideoProcessorInputs() {
    using Microsoft::WRL::ComPtr;
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
            nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context))) {
        std::cout << "SKIP: hardware video processor unavailable" << std::endl;
        return;
    }
    ComPtr<ID3D11VideoDevice> video;
    ComPtr<ID3D11VideoContext> video_context;
    CheckCursor(SUCCEEDED(device.As(&video)) && SUCCEEDED(context.As(&video_context)),
        "hardware exposes video processor interfaces");
    D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
    content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    content.InputFrameRate = content.OutputFrameRate = {60, 1};
    content.InputWidth = 2560; content.InputHeight = 1440;
    content.OutputWidth = 1920; content.OutputHeight = 1080;
    content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
    ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
    ComPtr<ID3D11VideoProcessor> processor;
    CheckCursor(SUCCEEDED(video->CreateVideoProcessorEnumerator(&content, &enumerator))
            && SUCCEEDED(video->CreateVideoProcessor(enumerator.Get(), 0, &processor)),
        "1440p-to-1080p video processor initializes");
    // Control reproduces the original failure: an SRV-only clean background
    // is not a legal video-processor input, despite being shader-readable.
    D3D11_TEXTURE2D_DESC incompatible_desc{};
    incompatible_desc.Width = 2560; incompatible_desc.Height = 1440;
    incompatible_desc.MipLevels = incompatible_desc.ArraySize = 1;
    incompatible_desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    incompatible_desc.SampleDesc.Count = 1;
    incompatible_desc.Usage = D3D11_USAGE_DEFAULT;
    incompatible_desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Texture2D> incompatible;
    CheckCursor(SUCCEEDED(device->CreateTexture2D(&incompatible_desc, nullptr, &incompatible)),
        "original SRV-only replay texture initializes");
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC incompatible_view_desc{};
    incompatible_view_desc.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    ComPtr<ID3D11VideoProcessorInputView> incompatible_view;
    CheckCursor(FAILED(video->CreateVideoProcessorInputView(incompatible.Get(),
        enumerator.Get(), &incompatible_view_desc, &incompatible_view)),
        "original SRV-only replay texture reproduces rejected video input view");
    auto background = MakeBackground(device.Get(), 2560, 1440,
        std::vector<uint8_t>(2560u * 1440u * 4u, 128));
    D3D11_TEXTURE2D_DESC output_desc{};
    background.source->GetDesc(&output_desc);
    output_desc.Width = 1920; output_desc.Height = 1080;
    ComPtr<ID3D11Texture2D> output;
    CheckCursor(SUCCEEDED(device->CreateTexture2D(&output_desc, nullptr, &output)),
        "1080p scaling output initializes");
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_view_desc{};
    output_view_desc.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    ComPtr<ID3D11VideoProcessorOutputView> output_view;
    CheckCursor(SUCCEEDED(video->CreateVideoProcessorOutputView(output.Get(),
        enumerator.Get(), &output_view_desc, &output_view)), "scaling output view initializes");
    RECT source_rect{0, 0, 2560, 1440}, target_rect{0, 0, 1920, 1080};
    video_context->VideoProcessorSetStreamFrameFormat(processor.Get(), 0,
        D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    video_context->VideoProcessorSetStreamSourceRect(processor.Get(), 0, TRUE, &source_rect);
    video_context->VideoProcessorSetStreamDestRect(processor.Get(), 0, TRUE, &target_rect);
    video_context->VideoProcessorSetOutputTargetRect(processor.Get(), TRUE, &target_rect);
    fthr::dxgi_cursor::Composer composer;
    std::string detail;
    CheckCursor(composer.Initialize(device.Get(), context.Get(), 2560, 1440, detail),
        "hardware cursor composer initializes");
    FakeDuplication duplication;
    SetColorShape(duplication, 1, 1, {0, 0, 255, 255});
    for (int state = 0; state < 4; ++state) {
        ID3D11Texture2D* input_texture = background.source;
        if (state != 3) { // state 3 explicitly bypasses cursor capture.
            CheckCursor(composer.ObserveFrame(&duplication,
                PointerFrame(state + 1, state == 2 ? 3000 : 20, 20,
                    state != 1, state == 0 ? 4 : 0), detail), "cursor state metadata accepted");
            CheckCursor(composer.Compose(background.source, &input_texture, detail),
                "cursor state composes for scaling");
        }
        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC view_desc{};
        view_desc.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        ComPtr<ID3D11VideoProcessorInputView> view;
        CheckCursor(SUCCEEDED(video->CreateVideoProcessorInputView(input_texture,
            enumerator.Get(), &view_desc, &view)),
            "visible/hidden/outside/disabled cursor supports video input view");
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE; stream.pInputSurface = view.Get();
        CheckCursor(SUCCEEDED(video_context->VideoProcessorBlt(processor.Get(),
            output_view.Get(), 0, 1, &stream)),
            "visible/hidden/outside/disabled cursor scales 1440p to 1080p");
    }
    context->Flush();
    std::cout << "Hardware cursor scaling: all four cursor states passed" << std::endl;
}

}  // namespace

int RunDxgiCursorTests() {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    CreateWarp(&device, &context);
    ColorAlphaAndMoveHide(device, context);
    MaskedXorAndMonochrome(device, context);
    BoundsStaleAndReset(device, context);
    PointerOnlyPreservesCursorThroughBackgroundInvalidation(device, context);
    InvalidShapesAreRejectedWithoutUnboundedAllocation(device, context);
    context->Release();
    device->Release();
    CursorStatesRemainValidVideoProcessorInputs();
    std::cout << "DXGI cursor tests: " << cursor_checks << " checks passed"
              << std::endl;
    return cursor_checks;
}
