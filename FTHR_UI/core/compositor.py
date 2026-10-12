"""Detect and cache the compositor/display server.

Results: hyprland, kwin, gnome, x11, wayland-unknown, or none (headless).
"""
import os
import shutil

_COMPOSITOR: str | None = None


def detect_compositor() -> str:
    """Detect and return the compositor string. Result is cached after first call."""
    global _COMPOSITOR
    if _COMPOSITOR is not None:
        return _COMPOSITOR

    has_wayland = (bool(os.environ.get('WAYLAND_DISPLAY'))
                   or os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland')
    has_x11     = bool(os.environ.get('DISPLAY'))

    if not has_wayland and not has_x11:
        _COMPOSITOR = 'none'
        return _COMPOSITOR

    if has_wayland:
        # Hyprland sets a unique env var
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            _COMPOSITOR = 'hyprland'
            return _COMPOSITOR

        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        session = os.environ.get('XDG_SESSION_DESKTOP', '').lower()

        if 'kde' in desktop or 'plasma' in session or 'kde' in session:
            _COMPOSITOR = 'kwin'
            return _COMPOSITOR

        if 'gnome' in desktop or os.environ.get('GNOME_DESKTOP_SESSION_ID'):
            _COMPOSITOR = 'gnome'
            return _COMPOSITOR

        # Last resort, and only when the session names no desktop at all: an
        # installed kwin_wayland says nothing about the running session (Plasma
        # packages are common next to Sway or GNOME).
        if not desktop and not session and shutil.which('kwin_wayland'):
            _COMPOSITOR = 'kwin'
            return _COMPOSITOR

        _COMPOSITOR = 'wayland-unknown'
        return _COMPOSITOR

    # Pure X11
    _COMPOSITOR = 'x11'
    return _COMPOSITOR


def _reset_cache_for_tests() -> None:
    global _COMPOSITOR
    _COMPOSITOR = None


def has_xtools() -> bool:
    """True if xdotool is available (needed for focus/game detection on non-Hyprland)."""
    from core import linux_tools
    return linux_tools.available('xdotool')


def has_wmctrl() -> bool:
    """True if wmctrl is available."""
    from core import linux_tools
    return linux_tools.available('wmctrl')
