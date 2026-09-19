#pragma once
#include "capture_engine.h"
#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

namespace fthr {

enum class BackendType { WlrScreencopy, ExtImageCopy, X11Grab, PortalScreenCast };

// Raw frame delivered synchronously by CaptureFrame().
// data pointer is valid only until the next CaptureFrame() call.
struct RawFrame {
    const uint8_t* data         = nullptr;
    uint32_t       stride       = 0;
    uint32_t       width        = 0;
    uint32_t       height       = 0;
    int            av_pix_fmt   = 0;   // AV_PIX_FMT_* constant
    int64_t        timestamp_ns = 0;   // CLOCK_MONOTONIC nanoseconds
};

class ICaptureBackend {
public:
    virtual ~ICaptureBackend() = default;

    // One-time setup. Returns false and logs on failure.
    virtual bool Initialize(const CaptureConfig& cfg) = 0;

    // Synchronously waits within the backend's bounded frame deadline. Returns
    // false on timeout, cancellation, disconnect, or another backend error.
    virtual bool CaptureFrame(RawFrame& out) = 0;

    virtual void Shutdown() = 0;

    virtual BackendType Type()       const = 0;
    virtual uint32_t NativeWidth()  const = 0;
    virtual uint32_t NativeHeight() const = 0;
};

// Why CreateBestBackend() returned nullptr, worded for the UI. Empty when
// capture was merely cancelled. retry_pointless marks failures that only user
// action can resolve (a dismissed portal dialog); the engine's bounded
// recovery would otherwise re-open that dialog on every attempt.
struct BackendFailure {
    std::string reason;
    bool retry_pointless = false;
};

// Factory. On Wayland: wlr-screencopy → ext-image-copy-capture → ScreenCast
// portal (PipeWire); the protocol backends come first because they need no
// dialog. Native X11: x11grab. Returns nullptr if no backend works or capture
// cancellation is requested; `failure` (optional) then explains why.
std::unique_ptr<ICaptureBackend> CreateBestBackend(
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure = nullptr);

} // namespace fthr
