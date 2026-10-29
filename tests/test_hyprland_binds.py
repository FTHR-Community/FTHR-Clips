from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from FTHR_UI.core import hotkey_manager
from FTHR_UI.core.hotkey_manager import HotkeyManager

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


# Which file the binds go to, and how the main config loads it (#91)

@pytest.fixture
def hypr(tmp_path, monkeypatch):
    """Every Hyprland config path under tmp, Hyprland detected, no session."""
    d = tmp_path / 'hypr'
    d.mkdir()
    paths = SimpleNamespace(
        dir=d,
        conf=d / 'hyprland.conf',
        lua=d / 'hyprland.lua',
        custom=d / 'custom' / 'keybinds.lua',
        fthr_conf=d / 'fthr-hotkeys.conf',
        fthr_lua=d / 'fthr-hotkeys.lua',
    )
    monkeypatch.setattr(hotkey_manager, '_HYPR_CONF', paths.conf)
    monkeypatch.setattr(hotkey_manager, '_HYPR_LUA_CONF', paths.lua)
    monkeypatch.setattr(hotkey_manager, '_HYPR_CUSTOM_LUA', paths.custom)
    monkeypatch.setattr(hotkey_manager, '_FTHR_HYPR_CONF', paths.fthr_conf)
    monkeypatch.setattr(hotkey_manager, '_FTHR_HYPR_LUA', paths.fthr_lua)
    monkeypatch.setitem(sys.modules, 'core.compositor',
                        SimpleNamespace(detect_compositor=lambda: 'hyprland'))
    monkeypatch.delenv('HYPRLAND_INSTANCE_SIGNATURE', raising=False)
    return paths


def test_plain_hyprland_lua_gets_own_file_and_one_require_line(manager, hypr):
    user = 'hl.config({ general = { gaps_in = 5 } })\n'
    hypr.lua.write_text(user)

    changed, target, line = manager._apply_hyprland_lua_config()

    assert changed is True and target == hypr.fthr_lua
    assert line == 'pcall(require, "fthr-hotkeys")'
    binds = hypr.fthr_lua.read_text()
    assert binds.startswith(hotkey_manager._FTHR_LUA_BEGIN)
    assert 'hl.bind("F9", hl.dsp.exec_cmd(' in binds
    assert 'description = "FTHR Clips: save_clip"' in binds
    main = hypr.lua.read_text()
    assert main.startswith(user), 'existing config is only appended to'
    assert main.count(line) == 1
    assert hotkey_manager._FTHR_LUA_REQUIRE_COMMENT in main
    assert not hypr.custom.exists()

    assert manager._apply_hyprland_lua_config() == (False, target, line)
    assert hypr.lua.read_text().count(line) == 1


def test_end4_config_that_requires_custom_keybinds_keeps_that_file(manager, hypr):
    hypr.lua.write_text('require("custom.keybinds")\n')
    hypr.custom.parent.mkdir()
    hypr.custom.write_text('hl.bind("SUPER + Q", hl.dsp.exec_cmd("kitty"))\n')

    changed, target, line = manager._apply_hyprland_lua_config()

    assert changed is True and target == hypr.custom
    assert line == 'require("custom.keybinds")'
    assert 'FTHR Clips: save_clip' in hypr.custom.read_text()
    assert 'kitty' in hypr.custom.read_text()
    assert hypr.lua.read_text() == 'require("custom.keybinds")\n'
    assert not hypr.fthr_lua.exists()


def test_slash_form_require_also_counts_as_loading_custom_keybinds(manager, hypr):
    hypr.lua.write_text('require("custom/keybinds")\n')
    hypr.custom.parent.mkdir()
    hypr.custom.write_text('')
    assert manager._apply_hyprland_lua_config()[1] == hypr.custom


def test_commented_out_custom_require_does_not_count(manager, hypr):
    hypr.lua.write_text('-- require("custom.keybinds")\n')
    hypr.custom.parent.mkdir()
    hypr.custom.write_text('')
    assert manager._apply_hyprland_lua_config()[1] == hypr.fthr_lua


def test_unloaded_custom_keybinds_loses_stale_fthr_block(manager, hypr):
    # Earlier builds wrote here even when hyprland.lua never loaded it.
    hypr.lua.write_text('hl.config({})\n')
    hypr.custom.parent.mkdir()
    hypr.custom.write_text('-- mine\n')
    manager._write_hyprland_lua_config(hypr.custom)
    assert 'FTHR Clips' in hypr.custom.read_text()

    manager._apply_hyprland_lua_config()

    assert hypr.custom.read_text() == '-- mine\n'
    assert 'FTHR Clips: save_clip' in hypr.fthr_lua.read_text()


def test_incomplete_stale_block_is_left_alone(manager, hypr):
    hypr.lua.write_text('hl.config({})\n')
    hypr.custom.parent.mkdir()
    stale = f'-- mine\n{hotkey_manager._FTHR_LUA_BEGIN}\nhl.bind("F9", x)\n'
    hypr.custom.write_text(stale)
    manager._apply_hyprland_lua_config()
    assert hypr.custom.read_text() == stale


def test_existing_require_or_manual_socket_binds_are_not_duplicated(manager, hypr):
    hypr.lua.write_text('require("fthr-hotkeys")\n')
    assert manager._apply_hyprland_lua_config()[2] == 'pcall(require, "fthr-hotkeys")'
    assert hypr.lua.read_text() == 'require("fthr-hotkeys")\n'

    manual = f'hl.bind("F9", hl.dsp.exec_cmd("echo -n save_clip | nc -U {SOCK}"))\n'
    hypr.lua.write_text(manual)
    # Hand-made Lua binds cannot be seen in `hyprctl binds`: no load line, no check.
    assert manager._apply_hyprland_lua_config()[2] is None
    assert hypr.lua.read_text() == manual


def test_unreadable_hyprland_lua_is_never_edited(manager, hypr):
    hypr.lua.mkdir()  # exists(), but read_text() raises
    changed, target, _ = manager._apply_hyprland_lua_config()
    assert changed is True and target == hypr.fthr_lua
    assert hypr.lua.is_dir()


def test_require_line_skipped_when_bind_file_could_not_be_written(manager, hypr, monkeypatch):
    hypr.lua.write_text('hl.config({})\n')
    monkeypatch.setattr(manager, '_write_hyprland_lua_config',
                        lambda path=None: False, raising=False)
    manager._apply_hyprland_lua_config()
    assert hypr.lua.read_text() == 'hl.config({})\n'


def test_apply_routes_by_main_config(manager, hypr):
    hypr.conf.write_text('monitor=,preferred,auto,1\n')
    manager._apply_compositor_config()
    assert f'source = {hypr.fthr_conf}' in hypr.conf.read_text()
    assert not hypr.fthr_lua.exists()

    hypr.lua.write_text('hl.config({})\n')
    manager._apply_compositor_config()
    assert 'pcall(require, "fthr-hotkeys")' in hypr.lua.read_text()
    assert hypr.fthr_lua.exists()


# Reload, then check `hyprctl binds` (never a real hyprctl)

class _FakeRun:
    def __init__(self, binds=None, returncode=0, stdout=None, exc=None):
        self.calls = []
        self.binds, self.returncode, self.stdout, self.exc = binds, returncode, stdout, exc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.exc is not None:
            raise self.exc
        if argv[1:] == ['binds', '-j']:
            out = self.stdout if self.stdout is not None else json.dumps(self.binds or [])
            return SimpleNamespace(returncode=self.returncode, stdout=out, stderr='')
        return SimpleNamespace(returncode=0, stdout='ok', stderr='')


@pytest.fixture
def live(manager, hypr, monkeypatch):
    """A faked live session: fake hyprctl, synchronous threads, captured signals."""
    monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE', 'test')
    fake = _FakeRun()
    monkeypatch.setattr(hotkey_manager, 'subprocess', SimpleNamespace(
        run=fake, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(hotkey_manager.linux_tools, 'path',
                        lambda name: '/fake/hyprctl' if name == 'hyprctl' else None)
    monkeypatch.setattr(hotkey_manager.time, 'sleep', lambda s: None)
    threads = []

    class SyncThread:
        def __init__(self, target=None, **kwargs):
            threads.append(kwargs.get('name'))
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(hotkey_manager.threading, 'Thread', SyncThread)
    emitted = []
    manager.error_occurred = SimpleNamespace(emit=lambda *a: emitted.append(a))
    return SimpleNamespace(run=fake, threads=threads, emitted=emitted)


def _reloads(fake):
    return [c for c in fake.calls if c[1:] == ['reload']]


def test_lua_bind_found_by_description_no_warning(manager, hypr, live):
    hypr.lua.write_text('hl.config({})\n')
    live.run.binds = [{'dispatcher': '__lua', 'arg': '7',
                       'description': 'FTHR Clips: save_clip'}]
    manager._apply_compositor_config()
    assert len(_reloads(live.run)) == 1
    assert ['/fake/hyprctl', 'binds', '-j'] in live.run.calls
    assert live.emitted == []


def test_conf_bind_found_by_socket_in_arg_no_warning(manager, hypr, live):
    hypr.conf.write_text('')
    live.run.binds = [{'dispatcher': 'exec', 'description': '',
                       'arg': f'echo -n "save_clip" | /usr/bin/nc -U {SOCK}'}]
    manager._apply_compositor_config()
    assert live.emitted == []


def test_unloaded_binds_warn_with_file_and_line_once(manager, hypr, live):
    hypr.lua.mkdir()  # unreadable main config: FTHR cannot add the line itself
    live.run.binds = [{'dispatcher': 'exec', 'arg': 'kitty', 'description': ''}]
    manager._apply_compositor_config()
    assert len(live.emitted) == 1
    title, detail, level = live.emitted[0]
    assert title == 'HYPRLAND HOTKEYS NOT LOADED' and level == 'warning'
    assert str(hypr.fthr_lua) in detail
    assert 'pcall(require, "fthr-hotkeys")' in detail
    assert str(hypr.lua) in detail
    # Checked every time (startup too), reported once.
    manager._apply_compositor_config()
    assert len(live.emitted) == 1
    binds_calls = [c for c in live.run.calls if c[1:] == ['binds', '-j']]
    assert len(binds_calls) == 2 * manager._HYPR_VERIFY_ATTEMPTS


def test_conf_warning_names_source_line(manager, hypr, live):
    live.run.binds = []
    manager._apply_compositor_config()
    (_, detail, _), = live.emitted
    assert f'source = {hypr.fthr_conf}' in detail


def test_check_runs_on_startup_without_reload(manager, hypr, live):
    hypr.lua.write_text('hl.config({})\n')
    live.run.binds = [{'description': 'FTHR Clips: save_clip'}]
    manager._apply_compositor_config()
    live.run.calls.clear()
    manager._apply_compositor_config()  # nothing changed
    assert _reloads(live.run) == []
    assert live.run.calls == [['/fake/hyprctl', 'binds', '-j']]


@pytest.mark.parametrize('fake', [
    _FakeRun(returncode=1),
    _FakeRun(stdout='not json'),
    _FakeRun(stdout='{"error": 1}'),
])
def test_hyprctl_failures_are_tolerated(manager, hypr, live, fake, monkeypatch):
    hypr.lua.write_text('hl.config({})\n')
    real = live.run

    def run(argv, **kw):
        return (fake if argv[1:] == ['binds', '-j'] else real)(argv, **kw)

    monkeypatch.setattr(hotkey_manager.subprocess, 'run', run)
    manager._apply_compositor_config()
    assert live.emitted == []


def test_missing_hyprctl_skips_reload_warning_and_check(manager, hypr, live, monkeypatch):
    monkeypatch.setattr(hotkey_manager.linux_tools, 'path', lambda name: None)
    hypr.lua.write_text('hl.config({})\n')
    manager._apply_compositor_config()
    assert live.run.calls == []
    assert [e[0] for e in live.emitted] == ['HOTKEY CONFIG ERROR']


def test_no_check_without_bound_keys(manager, hypr, live):
    for action in manager.hotkeys:
        manager.hotkeys[action] = ''
    manager._apply_compositor_config()
    assert [c for c in live.run.calls if c[1:] == ['binds', '-j']] == []
    assert live.emitted == []


def test_no_session_means_no_thread(manager, hypr, monkeypatch):
    started = []
    monkeypatch.setattr(hotkey_manager.threading, 'Thread',
                        lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))
    hypr.lua.write_text('hl.config({})\n')
    manager._apply_compositor_config()
    assert started == []


# The socket client the binds run (nc without -U delivers nothing)

def test_hyprland_reports_unusable_socket_client(manager, monkeypatch):
    monkeypatch.setitem(sys.modules, 'core.compositor',
                        SimpleNamespace(detect_compositor=lambda: 'hyprland'))
    monkeypatch.setattr(hotkey_manager.linux_tools, 'unix_socket_client_problem',
                        lambda: 'install openbsd-netcat or socat')
    emitted = []
    manager.error_occurred = SimpleNamespace(emit=lambda *a: emitted.append(a))
    manager._keyboard_failed = True
    manager._portal = None
    manager._warn_if_linux_hotkeys_dead()
    assert emitted == [('GLOBAL HOTKEYS UNAVAILABLE',
                        'install openbsd-netcat or socat', 'error')]

    emitted.clear()
    monkeypatch.setattr(hotkey_manager.linux_tools, 'unix_socket_client_problem',
                        lambda: None)
    manager._warn_if_linux_hotkeys_dead()
    assert emitted == []


def test_socket_command_uses_socat_when_nc_lacks_unix(manager, monkeypatch):
    monkeypatch.setattr(hotkey_manager.linux_tools, 'unix_socket_client',
                        lambda: ('socat', '/usr/bin/socat'))
    assert manager.socket_command('save_clip') == (
        f'echo -n "save_clip" | /usr/bin/socat - UNIX-CONNECT:{SOCK}')
    monkeypatch.setattr(hotkey_manager.linux_tools, 'unix_socket_client',
                        lambda: ('nc', '/usr/bin/nc'))
    monkeypatch.setattr(hotkey_manager.linux_tools, 'path', lambda name: '/usr/bin/nc')
    assert manager.socket_command('save_clip') == (
        f'echo -n "save_clip" | /usr/bin/nc -U {SOCK}')
