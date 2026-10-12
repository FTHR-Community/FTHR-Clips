#include "capture_backend_policy.h"

#include <cassert>
#include <cstring>
#include <vector>

int main() {
    using fthr::BackendType;

    // First-time selection: dialog-free protocol backends before the portal
    // on Wayland; only x11grab elsewhere.
    const auto wayland = fthr::BackendCandidates(true);
    assert((wayland == std::vector<BackendType>{
        BackendType::WlrScreencopy, BackendType::ExtImageCopy,
        BackendType::PortalScreenCast}));
    const auto x11 = fthr::BackendCandidates(false);
    assert((x11 == std::vector<BackendType>{BackendType::X11Grab}));

    // Recovery retries the backend that delivered frames and nothing else,
    // so a transient compositor stall can never reach the portal dialog.
    assert(fthr::RecoveryCandidate(BackendType::WlrScreencopy) == BackendType::WlrScreencopy);
    assert(fthr::RecoveryCandidate(BackendType::PortalScreenCast) == BackendType::PortalScreenCast);

    assert(std::strcmp(fthr::BackendTypeName(BackendType::WlrScreencopy), "wlr-screencopy") == 0);
    assert(std::strcmp(fthr::BackendTypeName(BackendType::ExtImageCopy), "ext-image-copy-capture") == 0);
    assert(std::strcmp(fthr::BackendTypeName(BackendType::PortalScreenCast), "ScreenCast portal") == 0);
    assert(std::strcmp(fthr::BackendTypeName(BackendType::X11Grab), "x11grab") == 0);

    // FTHR_CAPTURE_BACKEND: four names, case-insensitive, anything else ignored.
    BackendType parsed;
    assert(fthr::ParseBackendOverride("ext", parsed) && parsed == BackendType::ExtImageCopy);
    assert(fthr::ParseBackendOverride("PORTAL", parsed) && parsed == BackendType::PortalScreenCast);
    assert(fthr::ParseBackendOverride("wlr", parsed) && parsed == BackendType::WlrScreencopy);
    assert(fthr::ParseBackendOverride("X11", parsed) && parsed == BackendType::X11Grab);
    assert(!fthr::ParseBackendOverride("", parsed));
    assert(!fthr::ParseBackendOverride("x11grab", parsed));
    assert(!fthr::ParseBackendOverride(nullptr, parsed));
    return 0;
}
