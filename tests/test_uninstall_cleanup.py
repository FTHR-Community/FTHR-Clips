"""Exercise the installed Windows PowerShell cleanup against isolated paths."""

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest


HELPER = Path(__file__).resolve().parents[1] / 'tools' / 'uninstall_cleanup.ps1'
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(
    sys.platform != 'win32' or POWERSHELL is None, reason='Requires Windows PowerShell')


def _write(path, data=b'fixture'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _junction(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def sandbox(tmp_path):
    paths = {name: tmp_path / name for name in ('install', 'home', 'local', 'temp')}
    for path in paths.values():
        path.mkdir()
    return paths


def _run(sandbox, script, *, timeout=20):
    env = dict(os.environ, FTHR_TEST_SCRIPT=str(HELPER))
    env.update({f'FTHR_TEST_{name.upper()}': str(path) for name, path in sandbox.items()})
    return subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
         "$ErrorActionPreference = 'Stop'; . $env:FTHR_TEST_SCRIPT; " + script],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


CLEANUP = ('Invoke-FthrCleanup $env:FTHR_TEST_INSTALL $env:FTHR_TEST_HOME '
           '$env:FTHR_TEST_LOCAL $env:FTHR_TEST_TEMP')


def test_cleanup_removes_owned_data_and_preserves_media(sandbox, tmp_path):
    install, home, local, temp = (sandbox[name] for name in ('install', 'home', 'local', 'temp'))
    state = home / '.fthr'
    clips = state / 'nested library'
    recording = local / 'FTHR Clips' / 'plugins'
    replay_spool = tmp_path / 'replay spool'
    segment_name = 'fthr-replay-v1-0123abcd-' + '-'.join(['1234567890abcdef'] * 3) + '.seg'
    segment_bytes = b'FTHRDS01' + struct.pack('<II', 1, 56) + bytes(40)
    settings = _write(state / 'settings.json', json.dumps({
        'clips_directory': str(clips),
        'recording_directory': str(recording),
        'replay_storage': {'folder': str(replay_spool)},
    }).encode())
    removed = [
        settings,
        _write(state / 'logs' / 'fthr.log'),
        _write(state / 'plugins' / 'theme' / 'asset.png'),
        _write(state / 'playback' / ('a' * 64 + '.mp4')),
        _write(state / 'thumbnails' / ('b' * 32 + '.jpg')),
        _write(state / 'clip_metadata.sqlite3.covers' / 'clip' /
               'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.png'),
        _write(local / 'FTHR Clips' / 'cache' / 'qmlcache' / 'item'),
        _write(local / 'cache' / 'FTHRClips' / 'rnnoise' / 'model.rnnn'),
        _write(local / 'CrashDumps' / 'FTHRClips.exe.123.dmp'),
        _write(install / 'logs' / 'old.log'),
        _write(temp / 'fthr-feature-q1234_ab' / 'cached.png'),
        _write(replay_spool / segment_name, segment_bytes),
    ]
    preserved = [
        _write(clips / 'opaque.sidecar'),
        _write(recording / 'unknown-recording-format'),
        _write(state / 'plugins' / 'misplaced.mp4'),
        _write(state / 'misplaced.mp4.fthr-audio.json'),
        _write(state / 'Screenshots' / 'capture.png'),
        _write(state / 'FTHR_CLIPS_BACKUP' / 'manifest.json'),
        _write(install / 'cache' / 'recording.mkv.partial'),
        _write(install / 'FTHRClips.exe'),
        _write(temp / segment_name, bytes(56)),
        _write(temp / 'unrelated' / 'fthr.log'),
        _write(local / 'cache' / 'unrelated' / 'model.rnnn'),
        _write(local / 'CrashDumps' / 'OtherApp.exe.123.dmp'),
    ]

    result = _run(sandbox, CLEANUP)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [path for path in removed if path.exists()]
    assert all(path.exists() for path in preserved)


def test_cleanup_never_traverses_junctions(sandbox, tmp_path):
    state = sandbox['home'] / '.fthr'
    target = _write(tmp_path / 'untouched' / 'logs' / 'external.log')
    root_link = sandbox['install'] / 'logs'
    ancestor_link = sandbox['local'] / 'cache'
    nested_link = state / 'plugin-link'
    targets = [target, _write(target.parents[1] / 'FTHRClips' / 'rnnoise' / 'model.rnnn')]
    for link in (root_link, ancestor_link, nested_link):
        _junction(link, target.parents[1])
    _write(state / 'logs' / 'fthr.log')

    result = _run(sandbox, CLEANUP)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(path.read_bytes() == b'fixture' for path in targets)
    assert not nested_link.exists()
    assert not state.exists()


def test_cleanup_failure_retains_settings_for_retry(sandbox):
    state = sandbox['home'] / '.fthr'
    settings = _write(state / 'settings.json', b'{}')
    log = _write(state / 'logs' / 'locked.log')
    script = (
        "$lock = [IO.File]::Open($env:FTHR_TEST_HOME + '\\.fthr\\logs\\locked.log', "
        "[IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None); "
        f"try {{ {CLEANUP} }} finally {{ $lock.Dispose() }}"
    )

    assert _run(sandbox, script).returncode != 0
    assert log.is_file()
    assert settings.is_file()
    result = _run(sandbox, CLEANUP)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not state.exists()


def test_process_discovery_scopes_runtime_paths_and_excludes_uninstaller(sandbox):
    script = r"""
    function Get-CimInstance {
        if ($args -notcontains "SessionId = $((Get-Process -Id $PID).SessionId)") {
            throw 'Missing process session filter'
        }
        $install = $env:FTHR_TEST_INSTALL
        $local = $env:FTHR_TEST_LOCAL
        $paths = @(
            "$install\FTHRClips.exe"
            "$install\_internal\engine\FTHRClips.exe"
            "$install\unins000.exe"
            "${install}2\FTHRClips.exe"
            "$local\FTHR Clips\plugins\uploader\1.2.0\FTHR Uploader.exe"
            "$local\Other App\FTHR Uploader.exe"
        )
        for ($index = 0; $index -lt $paths.Count; $index++) {
            [pscustomobject]@{ ExecutablePath = $paths[$index]; ProcessId = $index }
        }
    }
    $found = @(Get-FthrProcesses $env:FTHR_TEST_INSTALL $env:FTHR_TEST_LOCAL)
    if (($found.ProcessId -join ',') -ne '0,1,4') {
        throw "Wrong runtime processes: $($found.ProcessId -join ',')"
    }
    """
    result = _run(sandbox, script)
    assert result.returncode == 0, result.stdout + result.stderr


def test_shutdown_requests_main_window_and_waits_for_helpers(sandbox):
    script = r"""
    $script:polls = 0
    $script:requests = 0
    $script:sleeps = 0
    function Get-FthrProcesses($InstallRoot, $LocalRoot) {
        $script:polls++
        if ($script:polls -le 2) {
            [pscustomobject]@{ ExecutablePath = "$InstallRoot\FTHRClips.exe"; ProcessId = 17 }
        }
        if ($script:polls -le 3) {
            [pscustomobject]@{
                ExecutablePath = "$InstallRoot\_internal\engine\FTHRClips.exe"
                ProcessId = 18
            }
        }
    }
    function Send-FthrShutdown($ProcessIds, $Executable) {
        $script:requests++
        if (($ProcessIds -join ',') -ne '17' -or $Executable -ne "$env:FTHR_TEST_INSTALL\FTHRClips.exe") {
            throw 'Shutdown targeted a process other than the installed main executable'
        }
        return 17
    }
    function Start-Sleep { $script:sleeps++ }
    Wait-FthrShutdown $env:FTHR_TEST_INSTALL $env:FTHR_TEST_LOCAL
    if ($script:polls -ne 4 -or $script:sleeps -ne 3 -or $script:requests -ne 1) {
        throw 'Shutdown did not wait for the installed helpers to finish'
    }
    """
    result = _run(sandbox, script)
    assert result.returncode == 0, result.stdout + result.stderr
