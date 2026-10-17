"""Check absolute, cached helper resolution and missing-tool diagnostics."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from core import linux_tools


@pytest.fixture(autouse=True)
def clear_cache():
    linux_tools.reset_cache()
    yield
    linux_tools.reset_cache()


def _make_fake_tool(directory, name, body='#!/bin/sh\necho fake\n'):
    """Create an executable stand-in and return its path."""
    if sys.platform == 'win32':
        name = name + '.bat'
        body = '@echo fake\r\n'
    p = directory / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# Resolution

@pytest.mark.skipif(sys.platform == 'win32',
                    reason='Linux helper tools are not resolved on Windows')
def test_tool_found_on_path(tmp_path, monkeypatch):
    fake = _make_fake_tool(tmp_path, 'grim')
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()
    assert linux_tools.available('grim')
    assert linux_tools.path('grim') == str(fake)
    assert os.path.isabs(linux_tools.path('grim')), \
        'callers must get an absolute path, not a bare name'


def test_missing_tool_reports_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))     # empty dir
    linux_tools.reset_cache()
    assert not linux_tools.available('grim')
    assert linux_tools.path('grim') is None
    assert bool(linux_tools.tool('grim')) is False


def test_missing_tool_message_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()
    msg = linux_tools.missing_message('grim')
    assert 'grim' in msg
    assert 'Wayland screenshots' in msg, 'the message must say what breaks'
    assert 'package manager' in msg, 'the message must say what to do'


def test_require_raises_with_context(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()
    with pytest.raises(linux_tools.ToolMissing) as exc:
        linux_tools.require('hyprctl')
    assert exc.value.name == 'hyprctl'
    assert 'hyprctl' in str(exc.value)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX PATH semantics')
def test_earlier_path_entry_wins_and_is_visible(tmp_path, monkeypatch):
    """A PATH override must appear explicitly in the resolved helper path."""
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    shadow = _make_fake_tool(first, 'xdotool', '#!/bin/sh\necho shadow\n')
    _make_fake_tool(second, 'xdotool', '#!/bin/sh\necho real\n')
    monkeypatch.setenv('PATH', f'{first}{os.pathsep}{second}')
    linux_tools.reset_cache()
    assert linux_tools.path('xdotool') == str(shadow)
    assert str(shadow) in linux_tools.report(), \
        'the resolved path must appear in the diagnostic report'


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX PATH semantics')
def test_non_executable_file_is_not_a_tool(tmp_path, monkeypatch):
    p = tmp_path / 'grim'
    p.write_text('#!/bin/sh\n')
    p.chmod(0o644)                                # readable, not executable
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()
    assert not linux_tools.available('grim')


# Caching

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX PATH semantics')
def test_resolution_is_cached(tmp_path, monkeypatch):
    _make_fake_tool(tmp_path, 'grim')
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()
    first = linux_tools.path('grim')
    monkeypatch.setenv('PATH', '')                # PATH pulled away
    assert linux_tools.path('grim') == first, 'result should come from cache'
    linux_tools.reset_cache()
    assert linux_tools.path('grim') is None, 'reset_cache must re-resolve'


# Classification and reporting

def test_required_and_optional_are_distinguished():
    assert linux_tools.tool('grim').required is True
    assert linux_tools.tool('xdg-open').required is False


def test_report_lists_every_known_tool():
    rep = linux_tools.report()
    if sys.platform == 'win32':
        assert 'not applicable on Windows' in rep
        return
    for name in ('hyprctl', 'xdotool', 'xprop', 'xrandr', 'grim', 'nc', 'xdg-open'):
        assert name in rep


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux helper report')
def test_report_does_not_claim_feature_helpers_block_startup(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    linux_tools.reset_cache()

    report = linux_tools.report()

    hyprctl_line = next(
        line for line in report.splitlines() if 'hyprctl' in line)
    assert 'feature-only' in hyprctl_line
    assert 'required' not in hyprctl_line


def test_windows_never_resolves_linux_tools(monkeypatch, tmp_path):
    """A same-named .exe on Windows must not be picked up by accident."""
    monkeypatch.setattr(linux_tools.sys, 'platform', 'win32')
    linux_tools.reset_cache()
    assert linux_tools.path('nc') is None
    assert linux_tools.path('grim') is None


# Integration: the generated hotkey command must carry resolved values

@pytest.mark.skipif(sys.platform == 'win32', reason='Linux hotkey socket')
def test_generated_socket_command_uses_resolved_nc_and_private_path(
        tmp_path, monkeypatch):
    from core import linux_runtime
    from core.hotkey_manager import HotkeyManager

    fake_nc = _make_fake_tool(tmp_path, 'nc')
    monkeypatch.setenv('PATH', str(tmp_path))
    run = tmp_path / 'run-user'
    run.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(run))
    linux_tools.reset_cache()

    # Call the method unbound against a plain stand-in. HotkeyManager is a
    # QObject, so __new__ without __init__ raises on any attribute access, and
    # a real __init__ would install global keyboard hooks. socket_command only
    # reads an optional _socket_path, so a namespace is a faithful receiver.
    cmd = HotkeyManager.socket_command(SimpleNamespace(), 'save_clip')

    assert str(fake_nc) in cmd, 'bind must invoke the resolved nc'
    assert ' nc -U' not in cmd, 'bare `nc` must not survive in generated binds'
    assert '/tmp/fthr_hotkey.sock' not in cmd
    assert str(linux_runtime.hotkey_socket_path(create_dir=False)) in cmd


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux hotkey socket')
@pytest.mark.parametrize('compositor', ['kwin', 'gnome', 'generic'])
def test_displayed_hotkey_instructions_use_private_socket(
        tmp_path, monkeypatch, compositor):
    from core import linux_runtime
    from core.hotkey_manager import HotkeyManager

    _make_fake_tool(tmp_path, 'nc')
    monkeypatch.setenv('PATH', str(tmp_path))
    run = tmp_path / 'run-user'
    run.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(run))
    linux_tools.reset_cache()

    instructions = HotkeyManager.setup_instructions(
        SimpleNamespace(), compositor)

    assert '/tmp/fthr_hotkey.sock' not in instructions
    assert str(linux_runtime.hotkey_socket_path(create_dir=False)) in instructions
    for action in ('save_clip', 'save_screenshot'):
        assert action in instructions
