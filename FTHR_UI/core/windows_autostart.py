"""Truthful, per-user Windows autostart registration for FTHR Clips.

The registry is the authority.  The settings UI never persists a second copy
of this state, because a user or an uninstaller can remove the Run value while
FTHR is not running.
"""

from __future__ import annotations

import sys
from pathlib import Path


RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'FTHRClips'
BACKGROUND_ARGUMENT = '--background'


def build_background_command(executable: str | Path) -> str:
    """Return the correctly quoted Run value for an installed FTHR executable."""

    return f'"{Path(executable)}" {BACKGROUND_ARGUMENT}'


def is_packaged_launch(*, frozen: bool | None = None) -> bool:
    """Only a packaged executable may be registered for automatic startup.

    Registering ``python.exe`` from a checkout starts no application after a
    reboot and is especially confusing when the checkout later moves.  Tests
    can pass ``frozen=`` explicitly without changing the host registry.
    """

    return bool(getattr(sys, 'frozen', False) if frozen is None else frozen)


def read_enabled(*, registry=None, executable: str | Path | None = None,
                 frozen: bool | None = None) -> bool:
    """Return whether the OS currently has FTHR's exact background command."""

    if not is_packaged_launch(frozen=frozen):
        return False
    registry = registry or _winreg()
    if registry is None:
        return False
    expected = build_background_command(executable or sys.executable)
    try:
        key = registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY)
        try:
            value, _kind = registry.QueryValueEx(key, VALUE_NAME)
        finally:
            registry.CloseKey(key)
    except (FileNotFoundError, OSError):
        # Missing/inaccessible HKCU Run state truthfully means disabled.
        return False
    return str(value) == expected


def set_enabled(enabled: bool, *, registry=None,
                executable: str | Path | None = None,
                frozen: bool | None = None) -> bool:
    """Create or remove the exact HKCU Run value, without admin rights.

    Returns the observed state after the attempted change.  Development runs
    intentionally do not alter a person's Windows login configuration.
    """

    if not is_packaged_launch(frozen=frozen):
        return False
    registry = registry or _winreg()
    if registry is None:
        return False
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE)
        try:
            if enabled:
                registry.SetValueEx(
                    key, VALUE_NAME, 0, registry.REG_SZ,
                    build_background_command(executable or sys.executable))
            else:
                try:
                    registry.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    # Deleting an already-removed external entry is success.
                    pass
        finally:
            registry.CloseKey(key)
    except OSError:
        # A denied/unavailable Registry write must not claim success in the UI.
        return False
    return read_enabled(
        registry=registry, executable=executable, frozen=frozen) == bool(enabled)


def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        # A non-Windows interpreter has no legitimate Run registration path.
        return None
