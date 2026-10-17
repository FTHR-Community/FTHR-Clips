"""Resolve and cache absolute paths for Linux helper programs.

Log resolved paths at startup so PATH overrides and missing tools are
visible. Callers use argument lists and handle missing helpers per feature.
On Windows, resolution reports these Linux tools as unavailable.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

# Tools FTHR may invoke, with what breaks when they are absent.
# 'required' is relative to the feature, not to the application.
_TOOLS: dict[str, tuple[bool, str]] = {
    # name:            (required_for_its_feature, what it is used for)
    'hyprctl':  (True,  'Hyprland hotkey binds and window/game detection'),
    'xdotool':  (True,  'window focus and game detection on X11/XWayland'),
    'xprop':    (False, 'fullscreen detection on X11 (falls back to geometry)'),
    'xrandr':   (True,  'selected-monitor geometry for native X11 capture'),
    'grim':     (True,  'Wayland screenshots'),
    'nc':       (True,  'the hotkey socket client used by compositor binds'),
    'xdg-open': (False, 'opening the clips folder in a file manager'),
    'wmctrl':   (False, 'alternative window listing when xdotool is absent'),
}


@dataclass(frozen=True)
class Tool:
    name: str
    path: Optional[str]
    required: bool
    purpose: str

    @property
    def available(self) -> bool:
        return self.path is not None

    def __bool__(self) -> bool:
        return self.available


_cache: dict[str, Tool] = {}


def _resolve(name: str) -> Tool:
    required, purpose = _TOOLS.get(name, (False, 'unspecified'))
    # On Windows none of these exist and no caller asks for them; short-circuit
    # so a stray call cannot pick up some unrelated same-named .exe.
    path = None if sys.platform == 'win32' else shutil.which(name)
    return Tool(name=name, path=path, required=required, purpose=purpose)


def tool(name: str) -> Tool:
    """Resolve *name* once and cache it. Never raises."""
    cached = _cache.get(name)
    if cached is None:
        cached = _resolve(name)
        _cache[name] = cached
    return cached


def path(name: str) -> Optional[str]:
    """Absolute path of *name*, or None. Use this to build argv[0]."""
    return tool(name).path


def available(name: str) -> bool:
    return tool(name).available


def require(name: str) -> str:
    """Absolute path, or raise ToolMissing with a message worth showing a user."""
    t = tool(name)
    if t.path is None:
        raise ToolMissing(name, t.purpose)
    return t.path


def missing_message(name: str) -> str:
    """A sentence the UI can show verbatim."""
    t = tool(name)
    return (f"'{name}' is not installed or not on PATH. "
            f"It is needed for {t.purpose}. "
            f"Install it with your distribution's package manager.")


class ToolMissing(RuntimeError):
    def __init__(self, name: str, purpose: str) -> None:
        super().__init__(f"required tool '{name}' not found on PATH "
                         f"(needed for {purpose})")
        self.name = name
        self.purpose = purpose


def reset_cache() -> None:
    """Drop the cache. Tests use this after manipulating PATH."""
    _cache.clear()


def report() -> str:
    """Human-readable resolution table for the log and for bug reports."""
    if sys.platform == 'win32':
        return '[Tools] Linux helper tools not applicable on Windows.'
    lines = ['[Tools] External Linux helper resolution:']
    for name in sorted(_TOOLS):
        t = tool(name)
        # These dependencies gate only the named feature. Calling them
        # "required" in the startup report made KDE users reasonably assume
        # that a missing Hyprland helper prevented FTHR from opening.
        kind = 'feature-only' if t.required else 'optional'
        where = t.path if t.path else 'NOT FOUND'
        lines.append(f'[Tools]   {name:<9} {kind:<12} {where}')
    lines.append(f'[Tools]   PATH={os.environ.get("PATH", "")}')
    return '\n'.join(lines)
