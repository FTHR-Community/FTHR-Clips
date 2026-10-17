#include "capture_backend.h"
#include "backend_wlr.h"
#include "backend_ext.h"
#if FTHR_PORTAL_BACKEND
#include "backend_portal.h"
#endif
#if FTHR_X11_CAPTURE
#include "backend_x11.h"
#endif
#include <cstdlib>
#include <iostream>

namespace fthr {

std::unique_ptr<ICaptureBackend> CreateBestBackend(
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure) {
    const auto cancelled = [running] {
        return running && !running->load();
    };
    const auto fail = [failure](const std::string& reason, bool retry_pointless) {
        if (!failure) return;
        failure->reason = reason;
        failure->retry_pointless = retry_pointless;
    };
    if (failure) *failure = BackendFailure{};
    if (cancelled()) return nullptr;

    bool has_wayland = (std::getenv("WAYLAND_DISPLAY") != nullptr);

    if (has_wayland) {
        // 1. wlr-screencopy (a protocol commonly provided by wlroots compositors)
        auto wlr = std::make_unique<WlrBackend>(running);
        if (wlr->Initialize(cfg)) {
            std::cerr << "[Backend] Using wlr-screencopy" << std::endl;
            return wlr;
        }
        if (cancelled()) return nullptr;

        // 2. ext-image-copy-capture-v1 (when the compositor advertises it)
        auto ext = std::make_unique<ExtBackend>(running);
        if (ext->Initialize(cfg)) {
            std::cerr << "[Backend] Using ext-image-copy-capture-v1" << std::endl;
            return ext;
        }
        if (cancelled()) return nullptr;

        // 3. ScreenCast portal + PipeWire (KWin and other compositors that
        //    export capture only through xdg-desktop-portal). Last because
        //    the desktop may ask the user to pick a screen.
        std::string portal_reason =
            "the ScreenCast portal backend is not compiled into this engine";
#if FTHR_PORTAL_BACKEND
        auto portal = std::make_unique<PortalBackend>(running);
        if (portal->Initialize(cfg)) {
            std::cerr << "[Backend] Using ScreenCast portal" << std::endl;
            return portal;
        }
        if (cancelled()) return nullptr;
        portal_reason = portal->FailureReason();
        if (portal->RetryIsPointless()) {
            fail(portal_reason, true);
            std::cerr << "[Backend] ScreenCast portal: " << portal_reason << std::endl;
            return nullptr;
        }
#endif

        std::cerr << "[Backend] No Wayland capture backend available; "
                     "refusing XWayland/x11grab fallback. The compositor "
                     "advertises neither wlr-screencopy nor "
                     "ext-image-copy-capture, and " << portal_reason << std::endl;
        fail("No Wayland capture path is available: the compositor advertises "
             "neither wlr-screencopy nor ext-image-copy-capture, and "
             + portal_reason, false);
        return nullptr;
    }

    // Native X11 only. The UI resolves the selected connector through RandR
    // and passes a physical root-window rectangle. The engine process itself
    // is the cancellation boundary for XCB calls that ignore AVIOInterruptCB.
    if (cancelled()) return nullptr;
#if FTHR_X11_CAPTURE
    auto x11 = std::make_unique<X11Backend>(running);
    if (x11->Initialize(cfg)) {
        std::cerr << "[Backend] Using x11grab" << std::endl;
        return x11;
    }
#else
    std::cerr << "[Backend] Native X11 capture disabled at build time" << std::endl;
#endif

    std::cerr << "[Backend] No capture backend available on this system" << std::endl;
    fail("No capture backend is available on this system", false);
    return nullptr;
}

} // namespace fthr
