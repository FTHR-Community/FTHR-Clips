#pragma once

#include <cstdint>

namespace fthr {

// Capture backend selection is an internal startup preference. Auto remains
// the production default and does not change the shared-memory contract.
enum class WindowsCaptureBackendPreference : uint32_t {
    Auto = 0,
    Dxgi = 1,
    Wgc = 2,
};

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
    UnknownPreference,
};

struct WindowsCaptureStartupPlan {
    bool supported = false;
    WindowsCaptureBackend primary = WindowsCaptureBackend::None;
    WindowsCaptureBackend fallback = WindowsCaptureBackend::None;
    bool focus_gated = false;
    WindowsCapturePolicyFailure failure =
        WindowsCapturePolicyFailure::None;
};

// Select only startup paths that preserve the user's selected source. The
// only automatic fallback is desktop WGC to DXGI on the same resolved monitor.
constexpr WindowsCaptureStartupPlan PlanWindowsCaptureStartup(
    bool window_mode,
    bool window_target_available,
    bool protected_window,
    WindowsCaptureBackendPreference preference) noexcept {
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
    return {true, WindowsCaptureBackend::WgcMonitor,
            WindowsCaptureBackend::DxgiDesktop, false,
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
