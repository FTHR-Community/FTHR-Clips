"""The AppRun written by build_linux.sh must keep a caller's QT_QPA_PLATFORM."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32' or shutil.which('sh') is None,
    reason='runs the generated POSIX AppRun script')


def _apprun_script() -> str:
    text = (ROOT / 'build_linux.sh').read_text(encoding='utf-8')
    match = re.search(r"<<'APPRUN_EOF'\n(.*?)\nAPPRUN_EOF\n", text, re.S)
    assert match, 'AppRun heredoc not found in build_linux.sh'
    return match.group(1)


def _run_apprun(tmp_path: Path, env: dict[str, str]) -> str:
    apprun = tmp_path / 'AppRun'
    apprun.write_text(_apprun_script(), encoding='utf-8')
    # Stand-in for the PyInstaller binary: print the platform the app sees.
    stub = tmp_path / 'FTHRClips'
    stub.write_text('#!/bin/sh\nprintf "%s" "${QT_QPA_PLATFORM:-unset}"\n', encoding='utf-8')
    for path in (apprun, stub):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # FTHR_SCOPED skips the systemd-run re-exec; keep the test in-process.
    base = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'FTHR_SCOPED': '1'}
    base.update(env)
    result = subprocess.run(
        [str(apprun)], env=base, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_wayland_session_defaults_to_the_wayland_plugin(tmp_path):
    assert _run_apprun(tmp_path, {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}) == 'wayland'


def test_x11_session_defaults_to_xcb(tmp_path):
    assert _run_apprun(tmp_path, {'DISPLAY': ':0'}) == 'xcb'


def test_explicit_platform_is_preserved(tmp_path):
    assert _run_apprun(
        tmp_path, {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0',
                   'QT_QPA_PLATFORM': 'xcb'}) == 'xcb'
    assert _run_apprun(
        tmp_path, {'DISPLAY': ':0', 'QT_QPA_PLATFORM': 'offscreen'}) == 'offscreen'
