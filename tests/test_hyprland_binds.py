from __future__ import annotations

import pytest

from FTHR_UI.core import hotkey_manager
from FTHR_UI.core.hotkey_manager import HotkeyManager
import sys

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32', reason='Hyprland binds are a Linux-only path')

SOCK = '/run/user/1000/fthr/hotkey.sock'


@pytest.fixture
def manager():
    m = HotkeyManager.__new__(HotkeyManager)
    m.hotkeys = {
        'save_clip': 'F9', 'save_screenshot': 'F12',
        'start_recording': '', 'stop_recording': '',
        'confirm_game_detection': '', 'dismiss_game_detection': '',
    }
    m._socket_path = SOCK
    return m


def test_conf_includes_every_bound_socket_action_and_skips_empty(manager, tmp_path, monkeypatch):
    manager.hotkeys.update({'save_screenshot': '',
                            'confirm_game_detection': 'F7',
                            'dismiss_game_detection': 'F8'})
    monkeypatch.setattr(hotkey_manager, '_FTHR_HYPR_CONF', tmp_path / 'fthr-hotkeys.conf')
    assert manager._write_hyprland_config() is True
    text = (tmp_path / 'fthr-hotkeys.conf').read_text()
    assert 'confirm_game_detection' in text and 'dismiss_game_detection' in text
    assert 'save_screenshot' not in text
    assert 'bind = , ,' not in text
    assert manager._write_hyprland_config() is False


def test_bind_actions_order_and_filtering(manager):
    manager.hotkeys.update({'stop_recording': 'F10', 'dismiss_game_detection': 'F8'})
    assert manager._hyprland_bind_actions() == [
        'save_clip', 'save_screenshot', 'stop_recording', 'dismiss_game_detection']


def test_lua_returns_changed_flag_and_includes_all_actions(manager, tmp_path):
    manager.hotkeys['confirm_game_detection'] = 'F7'
    path = tmp_path / 'keybinds.lua'
    assert manager._write_hyprland_lua_config(path) is True
    assert 'confirm_game_detection' in path.read_text()
    assert manager._write_hyprland_lua_config(path) is False
    manager.hotkeys['save_clip'] = 'F5'
    assert manager._write_hyprland_lua_config(path) is True


def test_source_line_not_duplicated_when_user_bound_current_socket(manager, tmp_path, monkeypatch):
    conf = tmp_path / 'hyprland.conf'
    conf.write_text(f'bind = , F9, exec, echo -n save_clip | nc -U {SOCK}\n')
    monkeypatch.setattr(hotkey_manager, '_HYPR_CONF', conf)
    manager._ensure_hyprland_source()
    assert 'source =' not in conf.read_text()


def _run_apply(manager, monkeypatch, tmp_path, changed):
    import sys
    import types
    monkeypatch.setitem(sys.modules, 'core.compositor',
                        types.SimpleNamespace(detect_compositor=lambda: 'hyprland'))
    monkeypatch.setattr(hotkey_manager, '_HYPR_CUSTOM_LUA', tmp_path / 'none.lua')
    monkeypatch.setattr(hotkey_manager, '_HYPR_LUA_CONF', tmp_path / 'none2.lua')
    monkeypatch.setattr(manager, '_write_hyprland_config', lambda: changed, raising=False)
    monkeypatch.setattr(manager, '_ensure_hyprland_source', lambda: None, raising=False)
    monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE', 'x')
    started = []

    class FakeThread:
        def __init__(self, *a, **k):
            started.append(1)

        def start(self):
            pass

    monkeypatch.setattr(hotkey_manager.threading, 'Thread', FakeThread)
    manager._apply_compositor_config()
    return started


def test_reload_only_when_config_changed(manager, monkeypatch, tmp_path):
    assert _run_apply(manager, monkeypatch, tmp_path, False) == []
    assert _run_apply(manager, monkeypatch, tmp_path, True) == [1]
