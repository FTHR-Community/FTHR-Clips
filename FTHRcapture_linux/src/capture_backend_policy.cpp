#include "capture_backend_policy.h"

#include <cctype>
#include <string>

namespace fthr {

const char* BackendTypeName(BackendType type) {
    switch (type) {
    case BackendType::WlrScreencopy:    return "wlr-screencopy";
    case BackendType::ExtImageCopy:     return "ext-image-copy-capture";
    case BackendType::PortalScreenCast: return "ScreenCast portal";
    case BackendType::X11Grab:          return "x11grab";
    }
    return "unknown";
}

std::vector<BackendType> BackendCandidates(bool has_wayland) {
    if (has_wayland) {
        return {BackendType::WlrScreencopy, BackendType::ExtImageCopy,
                BackendType::PortalScreenCast};
    }
    return {BackendType::X11Grab};
}

BackendType RecoveryCandidate(BackendType last_good) {
    return last_good;
}

bool ParseBackendOverride(const char* value, BackendType& out) {
    if (!value || !*value) return false;
    std::string lowered;
    for (const char* c = value; *c; ++c)
        lowered.push_back(static_cast<char>(
            std::tolower(static_cast<unsigned char>(*c))));
    if (lowered == "wlr")    { out = BackendType::WlrScreencopy;    return true; }
    if (lowered == "ext")    { out = BackendType::ExtImageCopy;     return true; }
    if (lowered == "portal") { out = BackendType::PortalScreenCast; return true; }
    if (lowered == "x11")    { out = BackendType::X11Grab;          return true; }
    return false;
}

} // namespace fthr
