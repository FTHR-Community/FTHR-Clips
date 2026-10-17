"""Shared pytest setup for the uninstalled FTHR_UI source tree.

Add the UI directory to sys.path centrally so test imports do not depend
on collection order.
"""

import atexit
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Widget tests construct real QWidgets. On a headless CI runner Qt aborts
# without a platform plugin; offscreen is the supported answer and is harmless
# on a desktop. setdefault so a developer can still force xcb/windows to watch
# a test render.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# Never let a test run read or write the developer's real home. FTHR computes
# many paths from the home directory at import time (logs, the single-instance
# lock, diagnostics, thumbnails, playback cache, Hyprland binds) and
# main.py writes its log as soon as it is imported, which happens during
# collection, before any fixture runs. So the session home is replaced here,
# at conftest import, and every test then gets its own home below.
# XDG_RUNTIME_DIR stays: Wayland clients find the compositor through it.
if sys.platform != 'win32':
    REAL_HOME = os.path.realpath(os.path.expanduser('~'))
    SESSION_HOME = tempfile.mkdtemp(prefix='fthr-test-home-')
    atexit.register(shutil.rmtree, SESSION_HOME, True)
    os.environ['HOME'] = SESSION_HOME
    for _var, _sub in (('XDG_CONFIG_HOME', '.config'),
                       ('XDG_DATA_HOME', '.local/share'),
                       ('XDG_CACHE_HOME', '.cache'),
                       ('XDG_STATE_HOME', '.local/state')):
        os.environ[_var] = os.path.join(SESSION_HOME, _sub)
    # A live compositor must never be reloaded or queried by a test.
    os.environ.pop('HYPRLAND_INSTANCE_SIGNATURE', None)

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
def _isolate_linux_desktop_hotkeys(monkeypatch, tmp_path, tmp_path_factory):
    """Keep Linux hotkey tests away from the developer's real desktop.

    HotkeyManager writes Hyprland binds to paths fixed at import time,
    runs `hyprctl reload` and binds the private hotkey socket. Any test that
    reaches set_hotkey() or register_all() on a Hyprland session would
    otherwise rewrite ~/.config/hypr and reload the live compositor.
    """
    if sys.platform == 'win32':
        return
    # Outside tmp_path: several tests create tmp_path / 'home' themselves or
    # assert on exactly what tmp_path contains.
    home = tmp_path_factory.mktemp('home')
    monkeypatch.setenv('HOME', str(home))
    for var, sub in (('XDG_CONFIG_HOME', '.config'),
                     ('XDG_DATA_HOME', '.local/share'),
                     ('XDG_CACHE_HOME', '.cache'),
                     ('XDG_STATE_HOME', '.local/state')):
        monkeypatch.setenv(var, str(home / sub))
    # Path.home() and expanduser('~') follow HOME on POSIX, so tests that
    # set HOME or patch Path.home() themselves still win.
    monkeypatch.delenv('HYPRLAND_INSTANCE_SIGNATURE', raising=False)
    # No real D-Bus session: the GlobalShortcuts portal answers "absent"
    # unless a test stubs it in, and cached answers never leak between
    # tests (both import names of each module are reset).
    for name in ('core.portal_shortcuts', 'FTHR_UI.core.portal_shortcuts'):
        module = sys.modules.get(name)
        if module is None and name == 'core.portal_shortcuts':
            import core.portal_shortcuts as module
        if module is not None:
            monkeypatch.setattr(module, '_availability', None)
            monkeypatch.setattr(module, 'is_available', lambda: False)
    for name in ('core.compositor', 'FTHR_UI.core.compositor'):
        module = sys.modules.get(name)
        if module is None and name == 'core.compositor':
            import core.compositor as module
        if module is not None:
            monkeypatch.setattr(module, '_COMPOSITOR', None)
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
        # Bind the hotkey server on a private socket, seen only through the
        # hotkey module, so a running FTHR keeps its socket. Path queries
        # (create_dir=False, used for displayed commands) still go to the
        # real resolver, and XDG_RUNTIME_DIR itself must stay: Wayland
        # clients (the engine tests) find the compositor through it.
        real_runtime = module.linux_runtime
        runtime = types.ModuleType(real_runtime.__name__)
        runtime.__getattr__ = lambda name, _real=real_runtime: getattr(_real, name)
        runtime.hotkey_socket_path = (
            lambda create_dir=True, _real=real_runtime:
            str(tmp_path / 'hotkey.sock') if create_dir
            else _real.hotkey_socket_path(create_dir=False))
        monkeypatch.setattr(module, 'linux_runtime', runtime)
