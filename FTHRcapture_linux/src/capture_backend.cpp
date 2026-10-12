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

namespace {

void Fail(BackendFailure* failure, const std::string& reason,
          BackendFailureKind kind) {
    if (!failure) return;
    failure->reason = reason;
    failure->kind = kind;
    failure->retry_pointless = kind == BackendFailureKind::UserDeclined;
}

} // namespace

std::unique_ptr<ICaptureBackend> CreateBackend(
    BackendType type,
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure,
    bool strict_output) {
    if (failure) *failure = BackendFailure{};
    if (running && !running->load()) return nullptr;

    std::unique_ptr<ICaptureBackend> backend;
    switch (type) {
    case BackendType::WlrScreencopy:
        backend = std::make_unique<WlrBackend>(running);
        break;
    case BackendType::ExtImageCopy:
        backend = std::make_unique<ExtBackend>(running);
        break;
    case BackendType::PortalScreenCast:
#if FTHR_PORTAL_BACKEND
        backend = std::make_unique<PortalBackend>(running);
        break;
#else
        Fail(failure, "the ScreenCast portal backend is not compiled into this engine",
             BackendFailureKind::Error);
        return nullptr;
#endif
    case BackendType::X11Grab:
#if FTHR_X11_CAPTURE
        backend = std::make_unique<X11Backend>(running);
        break;
#else
        Fail(failure, "Native X11 capture disabled at build time",
             BackendFailureKind::Error);
        return nullptr;
#endif
    }

    backend->SetStrictOutput(strict_output);
    if (backend->Initialize(cfg)) {
        std::cerr << "[Backend] Using " << BackendTypeName(type) << std::endl;
        return backend;
    }
    if (running && !running->load()) return nullptr;
    Fail(failure, backend->FailureReason(), backend->InitFailureKind());
    return nullptr;
}

std::unique_ptr<ICaptureBackend> CreateBestBackend(
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure) {
    if (failure) *failure = BackendFailure{};
    const auto cancelled = [running] { return running && !running->load(); };
    if (cancelled()) return nullptr;

    const bool has_wayland = std::getenv("WAYLAND_DISPLAY") != nullptr;

    // A forced backend is a diagnostic aid: it never falls back, so a failure
    // is reported for that backend alone.
    if (const char* forced = std::getenv("FTHR_CAPTURE_BACKEND")) {
        BackendType type;
        if (ParseBackendOverride(forced, type)) {
            std::cerr << "[Backend] FTHR_CAPTURE_BACKEND forces "
                      << BackendTypeName(type) << std::endl;
            BackendFailure f;
            auto backend = CreateBackend(type, cfg, running, &f, false);
            if (!backend && !cancelled()) {
                Fail(failure,
                     std::string(BackendTypeName(type)) + " was forced by "
                     "FTHR_CAPTURE_BACKEND and could not start"
                     + (f.reason.empty() ? "" : ": " + f.reason),
                     f.kind == BackendFailureKind::None ? BackendFailureKind::Error
                                                        : f.kind);
            }
            return backend;
        }
        std::cerr << "[Backend] Ignoring unknown FTHR_CAPTURE_BACKEND value '"
                  << forced << "'" << std::endl;
    }

    std::string portal_reason =
        "the ScreenCast portal backend is not compiled into this engine";
    for (const BackendType type : BackendCandidates(has_wayland)) {
        BackendFailure f;
        auto backend = CreateBackend(type, cfg, running, &f, false);
        if (backend) return backend;
        if (cancelled()) return nullptr;
        if (type == BackendType::PortalScreenCast) {
            if (!f.reason.empty()) portal_reason = f.reason;
            if (f.kind == BackendFailureKind::UserDeclined) {
                std::cerr << "[Backend] ScreenCast portal: " << f.reason << std::endl;
                Fail(failure, f.reason, BackendFailureKind::UserDeclined);
                return nullptr;
            }
        }
    }

    if (has_wayland) {
        std::cerr << "[Backend] No Wayland capture backend available; "
                     "refusing XWayland/x11grab fallback. The compositor "
                     "advertises neither wlr-screencopy nor "
                     "ext-image-copy-capture, and " << portal_reason << std::endl;
        Fail(failure,
             "No Wayland capture path is available: the compositor advertises "
             "neither wlr-screencopy nor ext-image-copy-capture, and "
             + portal_reason, BackendFailureKind::Error);
        return nullptr;
    }

    std::cerr << "[Backend] No capture backend available on this system" << std::endl;
    Fail(failure, "No capture backend is available on this system",
         BackendFailureKind::Error);
    return nullptr;
}

} // namespace fthr
