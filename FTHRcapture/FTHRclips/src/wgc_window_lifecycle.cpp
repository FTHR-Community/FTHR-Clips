#include "wgc_window_lifecycle.h"

#include <algorithm>
#include <sstream>

#pragma comment(lib, "runtimeobject.lib")

namespace fthr::wgc {
namespace {

std::string HResultDetail(const char* operation, HRESULT result) {
    std::ostringstream detail;
    detail << operation << " failed with HRESULT 0x" << std::hex
           << static_cast<uint32_t>(result);
    return detail.str();
}

}  // namespace

WinRtApartmentGuard::WinRtApartmentGuard() noexcept
    : result_(RoInitialize(RO_INIT_MULTITHREADED)),
      must_uninitialize_(SUCCEEDED(result_)) {}

WinRtApartmentGuard::~WinRtApartmentGuard() {
    if (must_uninitialize_) RoUninitialize();
}

HRESULT WinRtApartmentGuard::Result() const noexcept {
    return result_;
}

bool WinRtApartmentGuard::Succeeded() const noexcept {
    return SUCCEEDED(result_);
}

WindowState ClassifyWindowState(
    bool hwnd_valid, bool iconic, bool renderable_window) noexcept {
    if (!hwnd_valid) return WindowState::Closed;
    if (iconic || !renderable_window) return WindowState::Paused;
    return WindowState::Active;
}

FrameAction ClassifyFrameAction(
    uint32_t content_width,
    uint32_t content_height,
    uint32_t pool_width,
    uint32_t pool_height,
    uint32_t texture_width,
    uint32_t texture_height,
    uint32_t canvas_width,
    uint32_t canvas_height,
    bool fit) noexcept {
    if (content_width == 0 || content_height == 0
        || pool_width == 0 || pool_height == 0
        || texture_width == 0 || texture_height == 0
        || canvas_width == 0 || canvas_height == 0) {
        return FrameAction::Discard;
    }
    if (fit) {
        const uint64_t content_aspect = static_cast<uint64_t>(content_width)
            * canvas_height;
        const uint64_t canvas_aspect = static_cast<uint64_t>(canvas_width)
            * content_height;
        const uint64_t difference = content_aspect > canvas_aspect
            ? content_aspect - canvas_aspect
            : canvas_aspect - content_aspect;
        const uint64_t rounding_tolerance = (std::max)(canvas_width, canvas_height)
            + (std::max)(content_width, content_height);
        if (difference > rounding_tolerance) {
            return FrameAction::RequiresReconfigure;
        }
    }
    if (content_width > pool_width || content_height > pool_height) {
        return FrameAction::RecreatePool;
    }
    // ContentSize is the only valid source rectangle. Do not inspect padding
    // even when the underlying surface happens to be larger than that rect.
    if (content_width > texture_width || content_height > texture_height) {
        return FrameAction::RecreatePool;
    }
    if (content_width == canvas_width && content_height == canvas_height
        && texture_width == canvas_width && texture_height == canvas_height) {
        return FrameAction::Copy;
    }
    if (content_width == canvas_width && content_height == canvas_height) {
        return FrameAction::CopyValidRect;
    }
    return FrameAction::Normalize;
}

bool RequiresEncoderReconfigureForResize(
    uint32_t source_width,
    uint32_t source_height,
    uint32_t canvas_width,
    uint32_t canvas_height,
    uint32_t output_width,
    uint32_t output_height,
    bool crop_enabled) noexcept {
    if (source_width == 0 || source_height == 0
        || canvas_width == 0 || canvas_height == 0) {
        return false;
    }
    if (source_width == canvas_width && source_height == canvas_height) {
        return false;
    }
    // Native/source-preset output geometry follows the source and cannot be
    // kept truthful after a ContentSize transition without a new generation.
    if (output_width == 0 || output_height == 0) return true;
    const bool grows_beyond_canvas = source_width > canvas_width
        || source_height > canvas_height;
    if (!grows_beyond_canvas) return false;
    // A fixed crop is expressed against the startup source geometry. Re-resolve
    // it on growth rather than risk dropping newly available ROI detail.
    if (crop_enabled) return true;
    const uint32_t useful_width = (std::min)(source_width, output_width);
    const uint32_t useful_height = (std::min)(source_height, output_height);
    return useful_width > canvas_width || useful_height > canvas_height;
}

RecreateFailureAction ClassifyRecreateFailure(HRESULT result) noexcept {
    return result == RPC_E_WRONG_THREAD
        ? RecreateFailureAction::ReconfigureRequired
        : RecreateFailureAction::RetryWithinBudget;
}

RecreateAttemptBudget::RecreateAttemptBudget(
    uint32_t maximum_failures) noexcept
    : maximum_failures_(maximum_failures) {}

bool RecreateAttemptBudget::CanAttempt() const noexcept {
    return attempts_ < maximum_failures_;
}

void RecreateAttemptBudget::RecordFailure() noexcept {
    if (attempts_ < maximum_failures_) ++attempts_;
    consecutive_stable_frames_ = 0;
}

void RecreateAttemptBudget::ObserveStableFrameProgress() noexcept {
    constexpr uint32_t kStableFramesBeforeReset = 30;
    if (consecutive_stable_frames_ < kStableFramesBeforeReset) {
        ++consecutive_stable_frames_;
    }
    if (consecutive_stable_frames_ >= kStableFramesBeforeReset) {
        attempts_ = 0;
    }
}

uint32_t RecreateAttemptBudget::Attempts() const noexcept {
    return attempts_;
}

OwnedCanvasNormalizer::~OwnedCanvasNormalizer() = default;

bool OwnedCanvasNormalizer::Initialize(
    ID3D11Device* device,
    uint32_t canvas_width,
    uint32_t canvas_height,
    uint32_t fps,
    std::string& error) {
    if (!device || canvas_width == 0 || canvas_height == 0 || fps == 0) {
        error = "invalid device or canvas geometry";
        return false;
    }
    ResetProcessor();
    canvas_ = nullptr;
    device_.copy_from(device);
    canvas_width_ = canvas_width;
    canvas_height_ = canvas_height;
    fps_ = fps;
    source_device_validated_ = false;

    D3D11_TEXTURE2D_DESC description{};
    description.Width = canvas_width_;
    description.Height = canvas_height_;
    description.MipLevels = 1;
    description.ArraySize = 1;
    description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    description.SampleDesc.Count = 1;
    description.Usage = D3D11_USAGE_DEFAULT;
    // The owned canvas can feed the cross-adapter downscaler directly.
    description.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    const HRESULT result = device_->CreateTexture2D(
        &description, nullptr, canvas_.put());
    if (FAILED(result) || !canvas_) {
        error = HResultDetail("ID3D11Device::CreateTexture2D(WGC owned canvas)", result);
        return false;
    }
    return true;
}

bool OwnedCanvasNormalizer::Copy(
    ID3D11DeviceContext* context,
    ID3D11Texture2D* source,
    uint32_t content_width,
    uint32_t content_height,
    uint32_t pool_width,
    uint32_t pool_height,
    bool fit,
    std::string& error) {
    if (!context || !source || !canvas_) {
        error = "WGC source, context, or owned canvas is unavailable";
        return false;
    }
    D3D11_TEXTURE2D_DESC source_description{};
    source->GetDesc(&source_description);
    if (source_description.ArraySize != 1 || source_description.MipLevels != 1) {
        error = "WGC source texture must have one array slice and one mip level";
        return false;
    }
    // WGC's frame pool is created from this same D3D11 device, and callers
    // supply that device's immediate context. Keep the invariant explicit.
    if (!source_device_validated_) {
        winrt::com_ptr<ID3D11Device> source_device;
        source->GetDevice(source_device.put());
        winrt::com_ptr<ID3D11Device> context_device;
        context->GetDevice(context_device.put());
        if (source_device.get() != device_.get()
            || context_device.get() != device_.get()) {
            error = "WGC source texture/context must belong to the frame-pool D3D11 device";
            return false;
        }
        // The frame pool and every frame it yields share its creation device,
        // including after Recreate with the same WinRT D3D device.
        source_device_validated_ = true;
    }
    const auto action = ClassifyFrameAction(
        content_width, content_height, pool_width, pool_height,
        source_description.Width, source_description.Height,
        canvas_width_, canvas_height_, fit);
    if (action == FrameAction::Discard) {
        error = "WGC frame has empty or invalid content bounds";
        return false;
    }
    if (action == FrameAction::RecreatePool) {
        error = "WGC ContentSize exceeds the active frame-pool texture bounds";
        return false;
    }
    if (action == FrameAction::RequiresReconfigure) {
        error = "WGC source aspect changed in FIT mode; a fresh encoder geometry is required";
        return false;
    }
    if (source_description.Format != DXGI_FORMAT_B8G8R8A8_UNORM
        || source_description.SampleDesc.Count != 1) {
        error = "WGC source texture must be single-sample BGRA8";
        return false;
    }
    if (action == FrameAction::Copy) {
        context->CopyResource(canvas_.get(), source);
        return true;
    }
    if (action == FrameAction::CopyValidRect) {
        const D3D11_BOX valid_content{
            0, 0, 0,
            content_width, content_height, 1};
        context->CopySubresourceRegion(
            canvas_.get(), 0, 0, 0, 0, source, 0, &valid_content);
        return true;
    }

    if (!EnsureProcessor(source_description.Width,
                         source_description.Height, fit, error)) return false;
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
    input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    input_description.Texture2D.MipSlice = 0;
    input_description.Texture2D.ArraySlice = 0;
    winrt::com_ptr<ID3D11VideoProcessorInputView> input_view;
    HRESULT result = video_device_->CreateVideoProcessorInputView(
        source, processor_enumerator_.get(), &input_description,
        input_view.put());
    if (FAILED(result) || !input_view) {
        error = HResultDetail(
            "ID3D11VideoDevice::CreateVideoProcessorInputView(WGC)", result);
        return false;
    }

    uint32_t destination_width = canvas_width_;
    uint32_t destination_height = canvas_height_;
    if (fit) {
        const uint64_t source_aspect = static_cast<uint64_t>(content_width)
            * canvas_height_;
        const uint64_t canvas_aspect = static_cast<uint64_t>(canvas_width_)
            * content_height;
        if (source_aspect > canvas_aspect) {
            destination_height = static_cast<uint32_t>(
                static_cast<uint64_t>(canvas_width_) * content_height
                / content_width);
        } else if (source_aspect < canvas_aspect) {
            destination_width = static_cast<uint32_t>(
                static_cast<uint64_t>(canvas_height_) * content_width
                / content_height);
        }
        destination_width = (std::max)(1u, destination_width);
        destination_height = (std::max)(1u, destination_height);
    }
    const uint32_t destination_left = (canvas_width_ - destination_width) / 2;
    const uint32_t destination_top = (canvas_height_ - destination_height) / 2;
    const RECT source_rect{0, 0,
        static_cast<LONG>(content_width), static_cast<LONG>(content_height)};
    const RECT destination_rect{
        static_cast<LONG>(destination_left),
        static_cast<LONG>(destination_top),
        static_cast<LONG>(destination_left + destination_width),
        static_cast<LONG>(destination_top + destination_height)};
    video_context_->VideoProcessorSetStreamSourceRect(
        processor_.get(), 0, TRUE, &source_rect);
    video_context_->VideoProcessorSetStreamDestRect(
        processor_.get(), 0, TRUE, &destination_rect);
    D3D11_VIDEO_PROCESSOR_STREAM stream{};
    stream.Enable = TRUE;
    stream.pInputSurface = input_view.get();
    result = video_context_->VideoProcessorBlt(
        processor_.get(), output_view_.get(), 0, 1, &stream);
    if (FAILED(result)) {
        error = HResultDetail("ID3D11VideoContext::VideoProcessorBlt(WGC)", result);
        return false;
    }
    return true;
}

ID3D11Texture2D* OwnedCanvasNormalizer::Texture() const noexcept {
    return canvas_.get();
}

void OwnedCanvasNormalizer::ResetProcessor() noexcept {
    ReleaseProcessor();
    video_context_ = nullptr;
    video_device_ = nullptr;
    input_width_ = 0;
    input_height_ = 0;
}

bool OwnedCanvasNormalizer::EnsureProcessor(
    uint32_t input_width, uint32_t input_height, bool fit, std::string& error) {
    if (processor_ && input_width_ == input_width
        && input_height_ == input_height && fit_ == fit) {
        return true;
    }
    ReleaseProcessor();
    if (!video_device_) {
        HRESULT result = device_->QueryInterface(
            __uuidof(ID3D11VideoDevice),
            reinterpret_cast<void**>(video_device_.put()));
        if (FAILED(result) || !video_device_) {
            error = HResultDetail("ID3D11Device::QueryInterface(ID3D11VideoDevice)", result);
            return false;
        }
    }
    if (!video_context_) {
        winrt::com_ptr<ID3D11DeviceContext> context;
        device_->GetImmediateContext(context.put());
        if (!context) {
            error = "ID3D11Device::GetImmediateContext returned null";
            return false;
        }
        HRESULT result = context->QueryInterface(
            __uuidof(ID3D11VideoContext),
            reinterpret_cast<void**>(video_context_.put()));
        if (FAILED(result) || !video_context_) {
            error = HResultDetail(
                "ID3D11DeviceContext::QueryInterface(ID3D11VideoContext)", result);
            return false;
        }
    }

    D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
    content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    content.InputFrameRate = {fps_, 1};
    content.InputWidth = input_width;
    content.InputHeight = input_height;
    content.OutputFrameRate = {fps_, 1};
    content.OutputWidth = canvas_width_;
    content.OutputHeight = canvas_height_;
    content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
    HRESULT result = video_device_->CreateVideoProcessorEnumerator(
        &content, processor_enumerator_.put());
    if (FAILED(result) || !processor_enumerator_) {
        error = HResultDetail(
            "ID3D11VideoDevice::CreateVideoProcessorEnumerator(WGC)", result);
        return false;
    }
    UINT input_support = 0;
    UINT output_support = 0;
    result = processor_enumerator_->CheckVideoProcessorFormat(
        DXGI_FORMAT_B8G8R8A8_UNORM, &input_support);
    if (FAILED(result)) {
        error = HResultDetail(
            "WGC VideoProcessor BGRA input format support", result);
        ReleaseProcessor();
        return false;
    }
    if (!(input_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT)) {
        std::ostringstream detail;
        detail << "WGC VideoProcessor BGRA input format unsupported (support mask 0x"
               << std::hex << input_support << ')';
        error = detail.str();
        ReleaseProcessor();
        return false;
    }
    result = processor_enumerator_->CheckVideoProcessorFormat(
        DXGI_FORMAT_B8G8R8A8_UNORM, &output_support);
    if (FAILED(result)) {
        error = HResultDetail(
            "WGC VideoProcessor BGRA output format support", result);
        ReleaseProcessor();
        return false;
    }
    if (!(output_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT)) {
        std::ostringstream detail;
        detail << "WGC VideoProcessor BGRA output format unsupported (support mask 0x"
               << std::hex << output_support << ')';
        error = detail.str();
        ReleaseProcessor();
        return false;
    }
    result = video_device_->CreateVideoProcessor(
        processor_enumerator_.get(), 0, processor_.put());
    if (FAILED(result) || !processor_) {
        error = HResultDetail("ID3D11VideoDevice::CreateVideoProcessor(WGC)", result);
        ReleaseProcessor();
        return false;
    }
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
    output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    output_description.Texture2D.MipSlice = 0;
    result = video_device_->CreateVideoProcessorOutputView(
        canvas_.get(), processor_enumerator_.get(), &output_description,
        output_view_.put());
    if (FAILED(result) || !output_view_) {
        error = HResultDetail(
            "ID3D11VideoDevice::CreateVideoProcessorOutputView(WGC)", result);
        ReleaseProcessor();
        return false;
    }
    video_context_->VideoProcessorSetStreamFrameFormat(
        processor_.get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    const RECT target_rect{0, 0, static_cast<LONG>(canvas_width_),
        static_cast<LONG>(canvas_height_)};
    video_context_->VideoProcessorSetOutputTargetRect(
        processor_.get(), TRUE, &target_rect);
    D3D11_VIDEO_COLOR background{};
    background.RGBA = {0.f, 0.f, 0.f, 1.f};
    video_context_->VideoProcessorSetOutputBackgroundColor(
        processor_.get(), FALSE, &background);
    video_context_->VideoProcessorSetStreamAutoProcessingMode(
        processor_.get(), 0, FALSE);
    input_width_ = input_width;
    input_height_ = input_height;
    fit_ = fit;
    return true;
}

void OwnedCanvasNormalizer::ReleaseProcessor() noexcept {
    output_view_ = nullptr;
    processor_ = nullptr;
    processor_enumerator_ = nullptr;
}

}  // namespace fthr::wgc
