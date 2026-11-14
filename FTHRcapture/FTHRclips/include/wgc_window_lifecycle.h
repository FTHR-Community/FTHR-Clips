#pragma once

#include <cstdint>
#include <atomic>
#include <string>

#include <d3d11.h>
#include <roapi.h>
#include <winrt/base.h>

namespace fthr::wgc {

// Initializes the current thread for WinRT MTA use and balances every
// successful RoInitialize result (including S_FALSE) at scope exit. The guard
// must be destroyed on the same thread on which it was constructed.
class WinRtApartmentGuard {
public:
    WinRtApartmentGuard() noexcept;
    WinRtApartmentGuard(const WinRtApartmentGuard&) = delete;
    WinRtApartmentGuard& operator=(const WinRtApartmentGuard&) = delete;
    WinRtApartmentGuard(WinRtApartmentGuard&&) = delete;
    WinRtApartmentGuard& operator=(WinRtApartmentGuard&&) = delete;
    ~WinRtApartmentGuard();

    HRESULT Result() const noexcept;
    bool Succeeded() const noexcept;

private:
    HRESULT result_ = E_UNEXPECTED;
    bool must_uninitialize_ = false;
};

struct ItemClosedSignal {
    std::atomic<bool> closed{false};
};

enum class WindowState : uint8_t {
    Active,
    Paused,
    Closed,
};

WindowState ClassifyWindowState(
    bool hwnd_valid, bool iconic, bool renderable_window) noexcept;

enum class FrameAction : uint8_t {
    Discard,
    Copy,
    CopyValidRect,
    Normalize,
    RecreatePool,
    RequiresReconfigure,
};

FrameAction ClassifyFrameAction(
    uint32_t content_width,
    uint32_t content_height,
    uint32_t pool_width,
    uint32_t pool_height,
    uint32_t texture_width,
    uint32_t texture_height,
    uint32_t canvas_width,
    uint32_t canvas_height,
    bool fit) noexcept;

bool RequiresEncoderReconfigureForResize(
    uint32_t source_width,
    uint32_t source_height,
    uint32_t canvas_width,
    uint32_t canvas_height,
    uint32_t output_width,
    uint32_t output_height,
    bool crop_enabled) noexcept;

enum class RecreateFailureAction : uint8_t {
    RetryWithinBudget,
    ReconfigureRequired,
};

RecreateFailureAction ClassifyRecreateFailure(HRESULT result) noexcept;

class RecreateAttemptBudget {
public:
    explicit RecreateAttemptBudget(uint32_t maximum_failures) noexcept;
    bool CanAttempt() const noexcept;
    void RecordFailure() noexcept;
    void ObserveStableFrameProgress() noexcept;
    uint32_t Attempts() const noexcept;

private:
    uint32_t maximum_failures_;
    uint32_t attempts_ = 0;
    uint32_t consecutive_stable_frames_ = 0;
};

// Owns the canonical fixed-size WGC canvas. The normal path is a GPU copy;
// changed source geometry is normalized by D3D11 VideoProcessor using only
// ContentSize as its source rectangle. Unsupported VP configurations fail.
class OwnedCanvasNormalizer {
public:
    OwnedCanvasNormalizer() = default;
    OwnedCanvasNormalizer(const OwnedCanvasNormalizer&) = delete;
    OwnedCanvasNormalizer& operator=(const OwnedCanvasNormalizer&) = delete;
    ~OwnedCanvasNormalizer();

    bool Initialize(ID3D11Device* device,
                    uint32_t canvas_width,
                    uint32_t canvas_height,
                    uint32_t fps,
                    std::string& error);
    bool Copy(ID3D11DeviceContext* context,
              ID3D11Texture2D* source,
              uint32_t content_width,
              uint32_t content_height,
              uint32_t pool_width,
              uint32_t pool_height,
              bool fit,
              std::string& error);
    ID3D11Texture2D* Texture() const noexcept;
    void ResetProcessor() noexcept;

private:
    bool EnsureProcessor(uint32_t input_width,
                         uint32_t input_height,
                         bool fit,
                         std::string& error);
    void ReleaseProcessor() noexcept;

    winrt::com_ptr<ID3D11Device> device_;
    winrt::com_ptr<ID3D11VideoDevice> video_device_;
    winrt::com_ptr<ID3D11VideoContext> video_context_;
    winrt::com_ptr<ID3D11VideoProcessorEnumerator> processor_enumerator_;
    winrt::com_ptr<ID3D11VideoProcessor> processor_;
    winrt::com_ptr<ID3D11Texture2D> canvas_;
    winrt::com_ptr<ID3D11VideoProcessorOutputView> output_view_;
    uint32_t canvas_width_ = 0;
    uint32_t canvas_height_ = 0;
    uint32_t input_width_ = 0;
    uint32_t input_height_ = 0;
    uint32_t fps_ = 0;
    bool fit_ = false;
    bool source_device_validated_ = false;
};

}  // namespace fthr::wgc
