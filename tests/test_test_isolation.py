"""The test session must never touch the developer's real home or desktop."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import conftest

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32', reason='home isolation is set up off Windows')


def _outside_real_home(path) -> bool:
    real = Path(conftest.REAL_HOME)
    resolved = Path(os.path.realpath(path))
    return resolved != real and real not in resolved.parents


def test_each_test_gets_a_private_home(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    assert Path.home().is_relative_to(base)
    assert os.environ['HOME'] == str(Path.home())
    assert _outside_real_home(Path.home())
    for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                'XDG_STATE_HOME'):
        assert Path(os.environ[var]).is_relative_to(Path.home())
    assert 'HYPRLAND_INSTANCE_SIGNATURE' not in os.environ


def test_hotkey_state_and_binds_stay_in_tmp(tmp_path, tmp_path_factory):
    from core import hotkey_manager

    manager = hotkey_manager.HotkeyManager()
    base = tmp_path_factory.getbasetemp()
    assert manager.config_file.is_relative_to(base)
    assert manager._portal_cleared_file().is_relative_to(base)
    for name in ('_FTHR_HYPR_CONF', '_HYPR_CONF', '_HYPR_LUA_CONF',
                 '_HYPR_CUSTOM_LUA'):
        assert getattr(hotkey_manager, name).is_relative_to(tmp_path)
    assert Path(hotkey_manager.linux_runtime.hotkey_socket_path()).is_relative_to(
        tmp_path)


def test_import_time_paths_are_outside_the_real_home():
    from core import diagnostics, field_diagnostics, playback_proxy, single_instance
    from ui import clip_grid

    for path in (single_instance._LOCK_FILE, diagnostics.LOG_DIR,
                 field_diagnostics.DEFAULT_ROOT, playback_proxy.CACHE_DIR,
                 clip_grid.THUMB_CACHE_DIR):
        assert _outside_real_home(path), path


def test_no_portal_session_or_cached_compositor():
    from core import compositor, portal_shortcuts

    assert portal_shortcuts.is_available() is False
    assert compositor._COMPOSITOR is None
