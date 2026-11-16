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

// A cached WGC item-size mismatch has the same source semantics as a fresh
// frame ContentSize mismatch. Both source types require a fresh generation;
// the selected HWND is retained by the caller's identity guard.
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
