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

void WgcMonitorGeometryDebouncesFocusTransitions() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcMonitorGeometryAction;
    const auto start = fthr::WgcMonitorGeometry::Clock::time_point{};
    fthr::WgcMonitorGeometry geometry(1920, 1080);
    CheckBackendPolicy(geometry.Observe(1920, 1080, false, start) == Action::Capture,
        "a static display can use the cached frame while geometry still matches");
    CheckBackendPolicy(geometry.Observe(1600, 900, false, start) == Action::Wait,
        "a mode switch drops incompatible frames before requesting recovery");
    CheckBackendPolicy(geometry.Observe(1920, 1080, false, start + 250ms) == Action::Wait,
        "the old item size alone does not release the frame gate");
    CheckBackendPolicy(geometry.Observe(1920, 1080, true, start + 350ms) == Action::Resume,
        "a matching fresh frame cancels transient recovery");
    CheckBackendPolicy(geometry.Observe(1600, 900, false, start + 1s) == Action::Wait
            && geometry.Observe(1600, 900, false, start + 1400ms) == Action::Reconfigure,
        "a sustained new mode requires a fresh capture generation");
}

void WgcMonitorGeometryCannotWaitIndefinitely() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcMonitorGeometryAction;
    const auto start = fthr::WgcMonitorGeometry::Clock::time_point{};
    fthr::WgcMonitorGeometry geometry(1600, 900);
    CheckBackendPolicy(geometry.Observe(0, 0, false, start) == Action::Wait
            && geometry.Observe(0, 0, false, start + 2s) == Action::Reconfigure,
        "zero-sized content has a bounded wait");
    geometry.Reset();
    for (int step = 0; step < 10; ++step) {
        const int width = step % 2 == 0 ? 1920 : 1280;
        CheckBackendPolicy(geometry.Observe(width, 1080, false, start + step * 200ms)
                == Action::Wait, "alternating modes wait for stable geometry");
    }
    CheckBackendPolicy(geometry.Observe(1920, 1080, false, start + 2s) == Action::Reconfigure,
        "alternating modes cannot keep capture in recovery indefinitely");
}

void ProtectedWgcResizeKeepsPaddedPoolAcrossBothModeChanges() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcProtectedMonitorResizeAction;
    const auto start = fthr::WgcProtectedMonitorResize::Clock::time_point{};
    const fthr::WgcCaptureSize full{1920, 1080};
    const fthr::WgcCaptureSize game{1600, 900};
    fthr::WgcProtectedMonitorResize resize(full);

    const auto initial = resize.Observe(full, full, full, full, true, start);
    CheckBackendPolicy(initial.action == Action::Capture && !initial.source_changed,
        "matching dimensions use the existing monitor source");

    const auto shrink = resize.Observe(full, full, game, full, true, start + 1ms);
    CheckBackendPolicy(shrink.action == Action::Capture && shrink.source_changed
            && resize.AcceptedSource() == game && !shrink.recreate_pool.Positive(),
        "fresh fitting 900p content can arrive ahead of the item size");

    const auto queued = resize.Observe(full, full, full, full, true, start + 2ms);
    CheckBackendPolicy(queued.action == Action::Wait && queued.allow_cached_frame
            && resize.AcceptedSource() == game,
        "the previous source cannot regain authority while the item size is lagging");

    const auto lagging = resize.Observe(full, full, game, full, true, start + 500ms);
    CheckBackendPolicy(lagging.action == Action::Capture && !lagging.source_changed,
        "fitting frames continue while the item size takes time to catch up");
    CheckBackendPolicy(resize.Observe(game, full, game, full, true, start + 501ms).action
            == Action::Capture,
        "matching item and fresh content settle the shrink with the original pool");

    const auto announce = resize.Observe(full, full, game, full, true, start + 1s);
    CheckBackendPolicy(announce.action == Action::Wait && announce.allow_cached_frame
            && !announce.recreate_pool.Positive(),
        "an item-first return to 1080p waits for fresh 1080p content");
    const auto delayed = resize.Observe(full, full, full, full, false, start + 1500ms);
    CheckBackendPolicy(delayed.action == Action::Wait && delayed.allow_cached_frame,
        "the verified-frame bridge covers a half-second mode set without accepting stale content");
    const auto grow = resize.Observe(full, full, full, full, true, start + 1501ms);
    CheckBackendPolicy(grow.action == Action::Capture && grow.source_changed
            && resize.AcceptedSource() == full && !grow.recreate_pool.Positive(),
        "fresh 1080p content resumes capture within the retained pool capacity");
}

void ProtectedWgcResizeVerifiesGrowthAfterPoolRecreation() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcProtectedMonitorResizeAction;
    const auto start = fthr::WgcProtectedMonitorResize::Clock::time_point{};
    const fthr::WgcCaptureSize full{1920, 1080};
    const fthr::WgcCaptureSize game{1600, 900};
    fthr::WgcProtectedMonitorResize resize(game);

    const auto clipped = resize.Observe(game, game, full, game, true, start);
    CheckBackendPolicy(clipped.action == Action::Wait && clipped.recreate_pool == full
            && clipped.allow_cached_frame && resize.Pending() && resize.AcceptedSource() == game,
        "clipped upsized content requests growth without changing the accepted source");
    const auto pool_undersized = resize.Observe(game, game, full, full, true, start + 1ms);
    CheckBackendPolicy(pool_undersized.action == Action::Wait && resize.AcceptedSource() == game,
        "a large texture alone does not verify the current pool can supply the content");
    const auto queue = resize.Observe(full, full, full, full, false, start + 50ms);
    CheckBackendPolicy(queue.action == Action::Wait && queue.allow_cached_frame
            && !queue.recreate_pool.Positive(),
        "a sufficiently large pool and matching item still need a fresh image");
    const auto undersized = resize.Observe(full, full, full, game, true, start + 100ms);
    CheckBackendPolicy(undersized.action == Action::Wait && resize.AcceptedSource() == game,
        "reported pool dimensions do not override the texture bounds");
    const auto verified = resize.Observe(full, full, full, full, true, start + 500ms);
    CheckBackendPolicy(verified.action == Action::Capture && verified.source_changed
            && !resize.Pending() && resize.AcceptedSource() == full,
        "a fresh fully backed frame after pool growth can use the fixed replay canvas");
}

void ProtectedWgcResizeToleratesAStuckItemSize() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcProtectedMonitorResizeAction;
    const auto start = fthr::WgcProtectedMonitorResize::Clock::time_point{};
    const fthr::WgcCaptureSize full{1920, 1080};
    const fthr::WgcCaptureSize game{1600, 900};
    fthr::WgcProtectedMonitorResize resize(full);

    CheckBackendPolicy(resize.Observe(full, full, game, full, true, start).source_changed
            && !resize.Pending(),
        "a verified new source settles immediately when the item size stays old");
    CheckBackendPolicy(resize.Observe(full, full, game, full, true, start + 3s).action
            == Action::Capture,
        "continuous verified frames do not time out on the stale item size");
    const auto idle = resize.Observe(full, full, {}, {}, false, start + 6s);
    CheckBackendPolicy(idle.action == Action::Wait && idle.allow_cached_frame && !resize.Pending(),
        "static content at the verified source keeps normal idle behavior with the stale item");

    const auto likely_queued = resize.Observe(full, full, full, full, true, start + 7s);
    CheckBackendPolicy(likely_queued.action == Action::Wait && resize.Pending()
            && resize.AcceptedSource() == game,
        "one old-sized frame cannot prove a return to the startup display mode");
    CheckBackendPolicy(resize.Observe(full, full, full, full, false, start + 7016ms).action
            == Action::Wait,
        "a repeated timestamp cannot confirm a return to the startup mode");
    CheckBackendPolicy(resize.Observe(full, full, game, full, true, start + 7033ms).action
            == Action::Capture && !resize.Pending(),
        "fresh current-source content resolves the ambiguous frame");
    CheckBackendPolicy(resize.Observe(full, full, full, full, true, start + 8s).action
            == Action::Wait && resize.Pending(),
        "an intervening current-source frame resets return confirmation");
    const auto returned = resize.Observe(full, full, full, full, true, start + 8016ms);
    CheckBackendPolicy(returned.action == Action::Capture && returned.source_changed
            && !resize.Pending() && resize.AcceptedSource() == full,
        "two fresh full-sized frames verify the return even if the item never changed");

    resize.Observe(full, full, game, full, true, start + 9s);
    resize.Observe(full, full, full, full, true, start + 10s);
    const auto unconfirmed = resize.Observe(full, full, {}, {}, false, start + 12s);
    CheckBackendPolicy(unconfirmed.action == Action::Reconfigure
            && !unconfirmed.allow_cached_frame,
        "an ambiguous mode change cannot bridge indefinitely without fresh frames");
}

void ProtectedWgcResizeHasBoundedRecovery() {
    using namespace std::chrono_literals;
    using Action = fthr::WgcProtectedMonitorResizeAction;
    const auto start = fthr::WgcProtectedMonitorResize::Clock::time_point{};
    const fthr::WgcCaptureSize full{1920, 1080};
    const fthr::WgcCaptureSize game{1600, 900};
    const fthr::WgcCaptureSize unsupported{1280, 1024};
    fthr::WgcProtectedMonitorResize resize(game);

    const auto first = resize.Observe(full, game, full, game, true, start);
    const auto throttled = resize.Observe(full, game, full, game, true, start + 99ms);
    const auto retry = resize.Observe(full, game, full, game, true, start + 100ms);
    CheckBackendPolicy(first.recreate_pool == full && !throttled.recreate_pool.Positive()
            && retry.recreate_pool == full,
        "a failed pool growth can retry at a bounded rate");
    const auto failure = resize.Observe(full, game, full, game, true, start + 2s);
    CheckBackendPolicy(failure.action == Action::Reconfigure && !failure.allow_cached_frame
            && !failure.recreate_pool.Positive()
            && std::strcmp(failure.reason, "pool_recreation_timeout") == 0,
        "failed pool growth eventually stops submitting the cached image and falls back");

    resize.Reset();
    CheckBackendPolicy(resize.Observe(unsupported, game, unsupported, unsupported, true, start)
            .action == Action::Wait,
        "a transient incompatible aspect ratio is given time to settle");
    const auto bad_aspect =
        resize.Observe(unsupported, game, unsupported, unsupported, true, start + 400ms);
    CheckBackendPolicy(bad_aspect.action == Action::Reconfigure
            && !bad_aspect.allow_cached_frame
            && std::strcmp(bad_aspect.reason, "unsupported_aspect") == 0,
        "a stable incompatible aspect ratio uses legacy recovery");

    resize.Reset();
    CheckBackendPolicy(resize.Observe({}, game, {}, {}, true, start).action == Action::Wait,
        "zero-sized observations do not replace the accepted source");
    for (int step = 1; step < 10; ++step) {
        const auto item = step % 2 == 0 ? game : unsupported;
        CheckBackendPolicy(resize.Observe(item, game, {}, {}, false, start + step * 200ms)
                .action == Action::Wait,
            "inconsistent item sizes cannot settle recovery without fresh content");
    }
    const auto last = resize.Observe(game, game, {}, {}, false, start + 2s);
    CheckBackendPolicy(last.action == Action::Reconfigure && !last.allow_cached_frame
            && std::strcmp(last.reason, "geometry_timeout") == 0,
        "flapping and zero-sized observations cannot prolong recovery indefinitely");

    resize.Reset();
    const auto focused = resize.Observe(game, game, game, game, true, start + 3s);
    CheckBackendPolicy(focused.action == Action::Capture && !focused.source_changed,
        "a focus reset cancels pending recovery and retains the accepted source geometry");
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
    WgcMonitorGeometryDebouncesFocusTransitions();
    WgcMonitorGeometryCannotWaitIndefinitely();
    ProtectedWgcResizeKeepsPaddedPoolAcrossBothModeChanges();
    ProtectedWgcResizeVerifiesGrowthAfterPoolRecreation();
    ProtectedWgcResizeToleratesAStuckItemSize();
    ProtectedWgcResizeHasBoundedRecovery();
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
