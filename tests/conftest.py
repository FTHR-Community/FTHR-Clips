"""Shared pytest setup for the uninstalled FTHR_UI source tree.

Add the UI directory to sys.path centrally so test imports do not depend
on collection order.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Widget tests construct real QWidgets. On a headless CI runner Qt aborts
# without a platform plugin; offscreen is the supported answer and is harmless
# on a desktop. setdefault so a developer can still force xcb/windows to watch
# a test render.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

for _p in (ROOT / 'FTHR_UI', ROOT / 'tools'):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture(autouse=True)
def _isolate_windows_videos_storage(monkeypatch, tmp_path):
    """Keep Windows known-folder tests away from a developer's real Videos."""
    from core import settings_manager

    monkeypatch.setattr(
        settings_manager, '_windows_videos_directory',
        lambda: tmp_path / 'Videos', raising=False)


@pytest.fixture(autouse=True)
def _isolate_linux_desktop_hotkeys(monkeypatch, tmp_path):
    """Keep Linux hotkey tests away from the developer's real desktop.

    HotkeyManager writes Hyprland binds to paths fixed at import time,
    runs `hyprctl reload` and binds the private hotkey socket. Any test that
    reaches set_hotkey() or register_all() on a Hyprland session would
    otherwise rewrite ~/.config/hypr and reload the live compositor.
    """
    if sys.platform == 'win32':
        return
    monkeypatch.delenv('HYPRLAND_INSTANCE_SIGNATURE', raising=False)
    # The hotkey socket lives under $XDG_RUNTIME_DIR/fthr; keep it private
    # to the test so a running FTHR keeps its socket.
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'xdg-runtime'))
    hypr = tmp_path / 'hypr'
    for name in ('core.hotkey_manager', 'FTHR_UI.core.hotkey_manager'):
        module = sys.modules.get(name)
        if module is None:
            if name != 'core.hotkey_manager':
                continue
            import core.hotkey_manager as module
        monkeypatch.setattr(module, '_FTHR_HYPR_CONF', hypr / 'fthr-hotkeys.conf')
        monkeypatch.setattr(module, '_HYPR_CONF', hypr / 'hyprland.conf')
        monkeypatch.setattr(module, '_HYPR_LUA_CONF', hypr / 'hyprland.lua')
        monkeypatch.setattr(
            module, '_HYPR_CUSTOM_LUA', hypr / 'custom' / 'keybinds.lua')
