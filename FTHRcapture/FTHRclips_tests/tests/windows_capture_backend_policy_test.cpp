#include "windows_capture_backend_policy.h"
#include "replay_encoder.h"

#include <cstring>
#include <cstdlib>
#include <iostream>

int RunDxgiCursorTests();

namespace {

int backend_policy_checks = 0;

void CheckBackendPolicy(bool condition, const char* message) {
    ++backend_policy_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

void DesktopAutomaticModePreservesSameMonitorFallback() {
    const auto plan = fthr::PlanWindowsCaptureStartup(
        false, false, false, fthr::WindowsCaptureBackendPreference::Auto);
    CheckBackendPolicy(plan.supported
            && plan.primary == fthr::WindowsCaptureBackend::DxgiDesktop
            && plan.fallback == fthr::WindowsCaptureBackend::WgcMonitor
            && !plan.focus_gated,
        "desktop Auto tries DXGI then exact-monitor WGC");
}

void ExplicitDesktopBackendNeverFallsBack() {
    const auto dxgi = fthr::PlanWindowsCaptureStartup(
        false, false, false, fthr::WindowsCaptureBackendPreference::Dxgi);
    CheckBackendPolicy(dxgi.supported
            && dxgi.primary == fthr::WindowsCaptureBackend::DxgiDesktop
            && dxgi.fallback == fthr::WindowsCaptureBackend::None,
        "forced DXGI initializes only desktop duplication");

    const auto wgc = fthr::PlanWindowsCaptureStartup(
        false, false, false, fthr::WindowsCaptureBackendPreference::Wgc);
    CheckBackendPolicy(wgc.supported
            && wgc.primary == fthr::WindowsCaptureBackend::WgcMonitor
            && wgc.fallback == fthr::WindowsCaptureBackend::None,
        "forced WGC initializes only monitor capture");
}

void WindowModeRequiresAnExactWindowCaptureTarget() {
    const auto missing = fthr::PlanWindowsCaptureStartup(
        true, false, false, fthr::WindowsCaptureBackendPreference::Auto);
    CheckBackendPolicy(!missing.supported
            && missing.failure
                == fthr::WindowsCapturePolicyFailure::MissingWindowTarget,
        "Window mode without an HWND is unavailable, never desktop capture");

    const auto window = fthr::PlanWindowsCaptureStartup(
        true, true, false, fthr::WindowsCaptureBackendPreference::Auto);
    CheckBackendPolicy(window.supported
            && window.primary == fthr::WindowsCaptureBackend::WgcWindow
            && window.fallback == fthr::WindowsCaptureBackend::None,
        "normal Window mode uses WGC window capture only");

    const auto forced_wgc = fthr::PlanWindowsCaptureStartup(
        true, true, false, fthr::WindowsCaptureBackendPreference::Wgc);
    CheckBackendPolicy(forced_wgc.supported
            && forced_wgc.primary == fthr::WindowsCaptureBackend::WgcWindow
            && forced_wgc.fallback == fthr::WindowsCaptureBackend::None,
        "forced WGC preserves the selected window target");

    const auto forced_dxgi = fthr::PlanWindowsCaptureStartup(
        true, true, false, fthr::WindowsCaptureBackendPreference::Dxgi);
    CheckBackendPolicy(!forced_dxgi.supported
            && forced_dxgi.failure
                == fthr::WindowsCapturePolicyFailure::WindowRequiresWgc,
        "forced DXGI cannot pretend to capture a selected window");
}

void ProtectedWindowsPauseWithoutChangingTheCaptureSource() {
    const auto automatic = fthr::PlanWindowsCaptureStartup(
        true, true, true, fthr::WindowsCaptureBackendPreference::Auto);
    CheckBackendPolicy(automatic.supported
            && automatic.primary == fthr::WindowsCaptureBackend::WgcMonitor
            && automatic.fallback == fthr::WindowsCaptureBackend::None
            && automatic.focus_gated,
        "protected Window targets pause monitor capture when another app is foreground");

    const auto forced_dxgi = fthr::PlanWindowsCaptureStartup(
        true, true, true, fthr::WindowsCaptureBackendPreference::Dxgi);
    CheckBackendPolicy(!forced_dxgi.supported
            && forced_dxgi.failure
                == fthr::WindowsCapturePolicyFailure::WindowRequiresWgc,
        "protected targets retain the supported WGC monitor backend");
}

void MissingCaptureTargetsHaveStableStartupErrorIdentity() {
    CheckBackendPolicy(
        std::strcmp(fthr::ReplayStartupErrorName(
                        fthr::ReplayStartupError::CaptureTargetUnavailable),
                    "CAPTURE_TARGET_UNAVAILABLE") == 0,
        "capture target startup failure maps to a clear source-specific code");
}

void WgcSizeMismatchPreservesSourceSpecificRecoveryPolicy() {
    CheckBackendPolicy(
        fthr::WgcSizeMismatchHealthFlags(true)
            == (fthr::CAPTURE_HEALTH_BACKEND_FAILED
                | fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED),
        "monitor WGC size mismatch requests backend restart and reconfiguration");
    CheckBackendPolicy(
        fthr::WgcSizeMismatchHealthFlags(false)
            == (fthr::CAPTURE_HEALTH_BACKEND_FAILED
                | fthr::CAPTURE_HEALTH_RECONFIGURE_REQUIRED),
        "window WGC size mismatch requests a fresh generation while preserving HWND identity");
}

void RotationPolicyAcceptsOnlyIdentityOrUnspecified() {
    CheckBackendPolicy(fthr::IsSupportedDxgiOutputRotation(0u),
        "unspecified output rotation remains supported");
    CheckBackendPolicy(fthr::IsSupportedDxgiOutputRotation(1u),
        "identity output rotation remains supported");
    CheckBackendPolicy(!fthr::IsSupportedDxgiOutputRotation(2u)
            && !fthr::IsSupportedDxgiOutputRotation(3u)
            && !fthr::IsSupportedDxgiOutputRotation(4u),
        "rotated output modes are rejected before capture startup");
}

}  // namespace

int RunWindowsCaptureBackendPolicyTests() {
    auto parsed = fthr::WindowsCaptureBackendPreference::Auto;
    CheckBackendPolicy(fthr::ParseWindowsCaptureBackendPreference("dxgi", parsed)
            && parsed == fthr::WindowsCaptureBackendPreference::Dxgi,
        "production DXGI argument selects strict desktop duplication");
    CheckBackendPolicy(fthr::ParseWindowsCaptureBackendPreference("wgc", parsed)
            && parsed == fthr::WindowsCaptureBackendPreference::Wgc,
        "production WGC argument is explicit");
    CheckBackendPolicy(fthr::ParseWindowsCaptureBackendPreference("auto", parsed)
            && parsed == fthr::WindowsCaptureBackendPreference::Auto,
        "production default keeps automatic capture policy");
    CheckBackendPolicy(!fthr::ParseWindowsCaptureBackendPreference("invalid", parsed)
            && !fthr::ParseWindowsCaptureBackendPreference(nullptr, parsed)
            && parsed == fthr::WindowsCaptureBackendPreference::Auto,
        "invalid backend never silently changes capture preference");
    DesktopAutomaticModePreservesSameMonitorFallback();
    ExplicitDesktopBackendNeverFallsBack();
    WindowModeRequiresAnExactWindowCaptureTarget();
    ProtectedWindowsPauseWithoutChangingTheCaptureSource();
    MissingCaptureTargetsHaveStableStartupErrorIdentity();
    WgcSizeMismatchPreservesSourceSpecificRecoveryPolicy();
    RotationPolicyAcceptsOnlyIdentityOrUnspecified();
    const auto excluded = fthr::PlanWindowsCaptureStartup(false, false, false,
        fthr::WindowsCaptureBackendPreference::Auto, false);
    CheckBackendPolicy(excluded.supported
            && excluded.primary == fthr::WindowsCaptureBackend::WgcMonitor
            && excluded.fallback == fthr::WindowsCaptureBackend::None,
        "cursor exclusion uses WGC with no cursor-leaking DXGI fallback");
    const auto forced_excluded = fthr::PlanWindowsCaptureStartup(false, false, false,
        fthr::WindowsCaptureBackendPreference::Dxgi, false);
    CheckBackendPolicy(!forced_excluded.supported
            && forced_excluded.failure == fthr::WindowsCapturePolicyFailure::CursorExclusionRequiresWgc,
        "forced DXGI cannot promise cursor exclusion");
    const int cursor_checks = RunDxgiCursorTests();
    std::cout << "Windows capture backend policy tests: "
              << backend_policy_checks << " checks passed; DXGI cursor suite "
              << cursor_checks << " checks passed" << std::endl;
    return backend_policy_checks + cursor_checks;
}
