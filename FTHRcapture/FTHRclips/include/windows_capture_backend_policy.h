#pragma once

#include <chrono>
#include <cstdint>
#include <cstring>
#include "shared_memory.h"

namespace fthr {

// Capture backend selection is an internal startup preference. Auto remains
// the production default and does not change the shared-memory contract.
enum class WindowsCaptureBackendPreference : uint32_t {
    Auto = 0,
    Dxgi = 1,
    Wgc = 2,
};

inline bool ParseWindowsCaptureBackendPreference(
    const char* value, WindowsCaptureBackendPreference& preference) noexcept {
    if (!value) return false;
    if (std::strcmp(value, "auto") == 0) {
        preference = WindowsCaptureBackendPreference::Auto;
    } else if (std::strcmp(value, "dxgi") == 0) {
        preference = WindowsCaptureBackendPreference::Dxgi;
    } else if (std::strcmp(value, "wgc") == 0) {
        preference = WindowsCaptureBackendPreference::Wgc;
    } else {
        return false;
    }
    return true;
}

enum class WindowsCaptureBackend : uint32_t {
    None = 0,
    DxgiDesktop,
    WgcMonitor,
    WgcWindow,
};

enum class WindowsCapturePolicyFailure : uint32_t {
    None = 0,
    MissingWindowTarget,
    WindowRequiresWgc,
    CursorExclusionRequiresWgc,
    UnknownPreference,
};

// Desktop Duplication cannot safely normalize rotated outputs in this path.
// DXGI_MODE_ROTATION_UNSPECIFIED is zero and IDENTITY is one in the DXGI
// contract; every rotated mode is rejected during startup so Auto can try the
// exact-monitor WGC fallback before any capture thread starts.
constexpr bool IsSupportedDxgiOutputRotation(uint32_t rotation) noexcept {
    return rotation == 0u || rotation == 1u;
}

struct WindowsCaptureStartupPlan {
    bool supported = false;
    WindowsCaptureBackend primary = WindowsCaptureBackend::None;
    WindowsCaptureBackend fallback = WindowsCaptureBackend::None;
    bool focus_gated = false;
    WindowsCapturePolicyFailure failure =
        WindowsCapturePolicyFailure::None;
};

// A WGC size mismatch that reaches the reconfiguration path requires a
// fresh generation. The caller's identity guard retains the selected HWND.
constexpr uint32_t WgcSizeMismatchHealthFlags(bool monitor_item) noexcept {
    (void)monitor_item;
    return CAPTURE_HEALTH_BACKEND_FAILED | CAPTURE_HEALTH_RECONFIGURE_REQUIRED;
}

enum class WgcMonitorGeometryAction {
    Capture,
    Wait,
    Resume,
    Reconfigure,
};

class WgcMonitorGeometry {
public:
    using Clock = std::chrono::steady_clock;

    WgcMonitorGeometry(int32_t width, int32_t height) noexcept
        : expected_width_(width), expected_height_(height) {}

    bool Matches(int32_t width, int32_t height) const noexcept {
        return width == expected_width_ && height == expected_height_;
    }

    WgcMonitorGeometryAction Observe(int32_t width, int32_t height,
                                    bool fresh_matching_frame,
                                    Clock::time_point now) noexcept {
        const bool matches = Matches(width, height);
        if (matches && !pending_) return WgcMonitorGeometryAction::Capture;
        if (matches && fresh_matching_frame) {
            Reset();
            return WgcMonitorGeometryAction::Resume;
        }
        if (!pending_) {
            pending_ = true;
            started_ = now;
        }
        if (width != width_ || height != height_) {
            width_ = width;
            height_ = height;
            stable_since_ = now;
        }
        // Display modes can briefly expose different dimensions on focus
        // changes. Zero-sized and flapping sources have a bounded wait too.
        if ((!matches && width > 0 && height > 0
                && now - stable_since_ >= std::chrono::milliseconds(400))
            || now - started_ >= std::chrono::seconds(2)) {
            return WgcMonitorGeometryAction::Reconfigure;
        }
        return WgcMonitorGeometryAction::Wait;
    }

    void Reset() noexcept {
        pending_ = false;
        width_ = 0;
        height_ = 0;
    }

private:
    const int32_t expected_width_;
    const int32_t expected_height_;
    int32_t width_ = 0;
    int32_t height_ = 0;
    bool pending_ = false;
    Clock::time_point started_{};
    Clock::time_point stable_since_{};
};

struct WgcCaptureSize {
    int32_t width = 0;
    int32_t height = 0;

    constexpr bool Positive() const noexcept { return width > 0 && height > 0; }

    constexpr bool Covers(WgcCaptureSize content) const noexcept {
        return width >= content.width && height >= content.height;
    }

    constexpr bool operator==(WgcCaptureSize other) const noexcept {
        return width == other.width && height == other.height;
    }

    constexpr bool operator!=(WgcCaptureSize other) const noexcept {
        return !(*this == other);
    }
};

enum class WgcProtectedMonitorResizeAction {
    Capture,
    Wait,
    Reconfigure,
};

struct WgcProtectedMonitorResizeDecision {
    WgcProtectedMonitorResizeAction action;
    WgcCaptureSize recreate_pool{};
    bool source_changed = false;
    bool allow_cached_frame = false;
    const char* reason = nullptr;
};

// Used only for protected SDR monitor capture without cropping. The startup
// source defines the supported display aspect ratio; the encoder's canvas is
// owned by the caller and stays fixed. The caller releases all frame leases
// before growing the pool and reports its actual size on the next observation.
class WgcProtectedMonitorResize {
public:
    using Clock = std::chrono::steady_clock;

    explicit WgcProtectedMonitorResize(WgcCaptureSize initial_source) noexcept
        : initial_source_(initial_source), accepted_source_(initial_source) {}

    WgcCaptureSize AcceptedSource() const noexcept { return accepted_source_; }
    bool Pending() const noexcept { return pending_; }

    // Reset transition state at a focus boundary. The caller must invalidate
    // its cached frame and filter frames queued before focus was restored.
    void Reset() noexcept {
        pending_ = false;
        saw_lagging_frame_ = false;
        requested_pool_ = {};
        unsupported_size_ = {};
    }

    WgcProtectedMonitorResizeDecision Observe(
        WgcCaptureSize item, WgcCaptureSize pool,
        WgcCaptureSize content, WgcCaptureSize texture,
        bool fresh_frame, Clock::time_point now) noexcept {
        using namespace std::chrono_literals;
        using Action = WgcProtectedMonitorResizeAction;

        const bool item_is_lagging = lagging_item_.Positive() && item == lagging_item_;
        const bool item_is_known = item == accepted_source_ || item_is_lagging;

        const bool frame_fits = fresh_frame && Supports(content)
            && texture.Covers(content) && pool.Covers(content);
        // WGC may report the previous item size indefinitely. Continue
        // using fresh current-source frames, but require confirmation when
        // a frame could be either residual imagery or a genuine return.
        const bool ambiguous_return = frame_fits && item_is_lagging && content == item;
        const bool confirmed_return = ambiguous_return && saw_lagging_frame_;
        if (fresh_frame || !item_is_lagging) saw_lagging_frame_ = ambiguous_return;
        if (frame_fits && item.Positive() && (item == content || item_is_known)
            && (!ambiguous_return || confirmed_return)) {
            const bool source_changed = content != accepted_source_;
            if (item == content) {
                lagging_item_ = {};
            } else if (source_changed) {
                lagging_item_ = item;
            }
            accepted_source_ = content;
            Reset();
            return {Action::Capture, {}, source_changed};
        }
        if (!pending_ && !fresh_frame && item_is_known && pool.Covers(accepted_source_)) {
            return {Action::Wait, {}, false, true};
        }

        if (!pending_) {
            pending_ = true;
            started_ = now;
        }

        WgcCaptureSize unsupported_size{};
        if (fresh_frame && content.Positive() && !Supports(content)) {
            unsupported_size = content;
        } else if (item.Positive() && !Supports(item)) {
            unsupported_size = item;
        }
        if (unsupported_size != unsupported_size_) {
            unsupported_size_ = unsupported_size;
            unsupported_since_ = now;
        }
        if (unsupported_size_.Positive() && now - unsupported_since_ >= 400ms) {
            return {Action::Reconfigure, {}, false, false, "unsupported_aspect"};
        }
        if (now - started_ >= 2s) {
            const char* reason = requested_pool_.Positive() && !pool.Covers(requested_pool_)
                ? "pool_recreation_timeout" : "geometry_timeout";
            return {Action::Reconfigure, {}, false, false, reason};
        }

        WgcCaptureSize pool_target{};
        if (!item_is_known && Supports(item)) {
            pool_target = item;
        } else if (item_is_known && fresh_frame && Supports(content)) {
            // The old pool may clip an upsized frame. Use its reported
            // ContentSize to request a larger pool and verify the next frame.
            pool_target = content;
        }

        WgcCaptureSize recreate_pool{};
        if (pool_target.Positive() && !pool.Covers(pool_target)
            && (!requested_pool_.Positive() || now - last_pool_request_ >= 100ms)) {
            requested_pool_ = pool_target;
            last_pool_request_ = now;
            recreate_pool = pool_target;
        }
        // Keep padded pool capacity for a return to the previous display mode.
        return {Action::Wait, recreate_pool, false, true};
    }

private:
    bool Supports(WgcCaptureSize size) const noexcept {
        return size.Positive() && initial_source_.Positive()
            && static_cast<int64_t>(size.width) * initial_source_.height
                == static_cast<int64_t>(size.height) * initial_source_.width;
    }

    const WgcCaptureSize initial_source_;
    WgcCaptureSize accepted_source_;
    WgcCaptureSize lagging_item_{};
    WgcCaptureSize requested_pool_{};
    WgcCaptureSize unsupported_size_{};
    bool pending_ = false;
    bool saw_lagging_frame_ = false;
    Clock::time_point started_{};
    Clock::time_point last_pool_request_{};
    Clock::time_point unsupported_since_{};
};

// Select only startup paths that preserve the user's selected source. The
// The only automatic fallback is desktop DXGI to WGC on the same resolved
// monitor.
constexpr WindowsCaptureStartupPlan PlanWindowsCaptureStartup(
    bool window_mode,
    bool window_target_available,
    bool protected_window,
    WindowsCaptureBackendPreference preference,
    bool capture_cursor = true) noexcept {
    if (preference != WindowsCaptureBackendPreference::Auto
        && preference != WindowsCaptureBackendPreference::Dxgi
        && preference != WindowsCaptureBackendPreference::Wgc) {
        return {false, WindowsCaptureBackend::None,
                WindowsCaptureBackend::None, false,
                WindowsCapturePolicyFailure::UnknownPreference};
    }

    if (window_mode && !window_target_available) {
        return {false, WindowsCaptureBackend::None,
                WindowsCaptureBackend::None, false,
                WindowsCapturePolicyFailure::MissingWindowTarget};
    }

    // Duplication may already contain a software cursor in desktop pixels.
    // Only WGC can guarantee exclusion of the system pointer.
    if (!capture_cursor) {
        if (preference == WindowsCaptureBackendPreference::Dxgi) {
            return {false, WindowsCaptureBackend::None,
                    WindowsCaptureBackend::None, false,
                    WindowsCapturePolicyFailure::CursorExclusionRequiresWgc};
        }
        preference = WindowsCaptureBackendPreference::Wgc;
    }

    if (window_mode) {
        if (preference == WindowsCaptureBackendPreference::Dxgi) {
            return {false, WindowsCaptureBackend::None,
                    WindowsCaptureBackend::None, false,
                    WindowsCapturePolicyFailure::WindowRequiresWgc};
        }
        return {true,
                protected_window ? WindowsCaptureBackend::WgcMonitor
                                 : WindowsCaptureBackend::WgcWindow,
                WindowsCaptureBackend::None,
                protected_window,
                WindowsCapturePolicyFailure::None};
    }

    if (preference == WindowsCaptureBackendPreference::Dxgi) {
        return {true, WindowsCaptureBackend::DxgiDesktop,
                WindowsCaptureBackend::None, false,
                WindowsCapturePolicyFailure::None};
    }
    if (preference == WindowsCaptureBackendPreference::Wgc) {
        return {true, WindowsCaptureBackend::WgcMonitor,
                WindowsCaptureBackend::None, false,
                WindowsCapturePolicyFailure::None};
    }
    return {true, WindowsCaptureBackend::DxgiDesktop,
            WindowsCaptureBackend::WgcMonitor, false,
            WindowsCapturePolicyFailure::None};
}

constexpr const char* WindowsCaptureBackendName(
    WindowsCaptureBackend backend) noexcept {
    switch (backend) {
    case WindowsCaptureBackend::DxgiDesktop:
        return "DXGI_OUTPUT_DUPLICATION";
    case WindowsCaptureBackend::WgcMonitor:
        return "WGC_MONITOR";
    case WindowsCaptureBackend::WgcWindow:
        return "WGC_WINDOW";
    case WindowsCaptureBackend::None:
        return "unavailable:not_reached";
    }
    return "unavailable:unknown";
}

constexpr const char* WindowsCaptureBackendPreferenceName(
    WindowsCaptureBackendPreference preference) noexcept {
    switch (preference) {
    case WindowsCaptureBackendPreference::Auto: return "auto";
    case WindowsCaptureBackendPreference::Dxgi: return "dxgi";
    case WindowsCaptureBackendPreference::Wgc: return "wgc";
    }
    return "unknown";
}

}  // namespace fthr
