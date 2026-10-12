#pragma once

#include "wayland_dispatch.h"

namespace fthr {

// Kept apart from backend_ext.h so tests need neither the generated protocol
// headers nor FFmpeg. Only a plain timeout may repeat: the compositor is
// allowed to hold a frame request until the screen changes, whereas every
// other result means the connection or session is gone.
constexpr bool ShouldRepeatLastFrame(WaylandWaitResult result,
                                     bool have_last_frame) noexcept {
    return result == WaylandWaitResult::Timeout && have_last_frame;
}

} // namespace fthr
