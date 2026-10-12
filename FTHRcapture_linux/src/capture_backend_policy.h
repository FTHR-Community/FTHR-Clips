#pragma once
// Backend selection rules that need no compositor, kept apart from the
// factory so they can be unit-tested without Wayland or FFmpeg.
#include <vector>

namespace fthr {

enum class BackendType { WlrScreencopy, ExtImageCopy, X11Grab, PortalScreenCast };

const char* BackendTypeName(BackendType type);

// First-time selection order. On Wayland the protocol backends come before
// the ScreenCast portal because they need no dialog; native X11 has only
// x11grab, and an XWayland fallback is deliberately never offered.
std::vector<BackendType> BackendCandidates(bool has_wayland);

// Recovery never re-runs first-time selection. A backend that delivered
// frames is retried alone, so a transient compositor stall can never fall
// through to the portal and its dialog.
BackendType RecoveryCandidate(BackendType last_good);

// FTHR_CAPTURE_BACKEND accepts wlr, ext, portal or x11 (case-insensitive).
// Returns false for null, empty or unknown values so callers ignore them.
bool ParseBackendOverride(const char* value, BackendType& out);

} // namespace fthr
