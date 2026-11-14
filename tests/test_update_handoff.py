"""Exercise the real Windows wait/hash helper without running an installer."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.update_handoff import handoff_script


def test_rejects_invalid_handoff_inputs(tmp_path):
    with pytest.raises(ValueError):
        handoff_script(tmp_path / 'setup.exe', 'bad', [123])
    with pytest.raises(ValueError):
        handoff_script(tmp_path / 'setup.exe', 'a' * 64, [-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows helper')
@pytest.mark.parametrize('bad_hash', [False, True])
def test_helper_waits_for_exit_and_checks_hash_without_running_setup(tmp_path, bad_hash):
    installer = tmp_path / "setup ' test.exe"
    installer.write_bytes(b'not an executable; test payload only')
    marker = tmp_path / 'would-launch.txt'
    owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])
    try:
        digest = '0' * 64 if bad_hash else hashlib.sha256(installer.read_bytes()).hexdigest()
        script = handoff_script(installer, digest, [owner.pid])
        # Override only the external action and error UI; execute wait/hash logic unchanged.
        script = ("function Start-Process { param($FilePath) "
                  + "[IO.File]::WriteAllText('" + str(marker).replace("'", "''")
                  + "', $FilePath) }\n" + script)
        script = script.replace('$shell = New-Object -ComObject WScript.Shell',
            '$shell = [pscustomobject]@{}; '
            '$shell | Add-Member ScriptMethod Popup { param($a,$b,$c,$d) 0 }')
        script_path = tmp_path / 'test-helper.ps1'
        script_path.write_text(script, encoding='utf-8-sig')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        helper = subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive',
                                   '-ExecutionPolicy', 'Bypass', '-File', str(script_path)],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        # Process ownership must be gone before a launch marker can be written.
        while owner.poll() is None:
            if marker.exists():
                assert owner.poll() is not None
            try:
                owner.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        assert helper.wait(timeout=15) == (1 if bad_hash else 0)
        assert marker.exists() is not bad_hash
        if bad_hash:
            assert 'hash changed' in (tmp_path / 'installer-handoff.log').read_text(encoding='utf-8-sig')
        else:
            assert marker.read_text() == str(installer)
    finally:
        if owner.poll() is None:
            owner.terminate()
        owner.wait(timeout=5)


def test_app_requests_full_exit_and_cancels_handoff_if_editor_blocks(monkeypatch, tmp_path):
    import main
    path = tmp_path / 'setup.exe'
    host = SimpleNamespace(
        app_update_controller=SimpleNamespace(release=SimpleNamespace(sha256='a' * 64)),
        engine_process=SimpleNamespace(pid=123, poll=lambda: None),
        capture_card=SimpleNamespace(_proc=SimpleNamespace(pid=456, poll=lambda: None)),
        _shutdown_requested=False)
    host.request_full_exit = lambda: None
    with pytest.raises(RuntimeError, match='editor details'):
        main.MainWindow._request_update_installation(host, path)
    assert host._pending_update_install is None
    host.request_full_exit = lambda: setattr(host, '_shutdown_requested', True)
    main.MainWindow._request_update_installation(host, path)
    assert host._pending_update_install == (path, 'a' * 64, [os.getpid(), 123, 456])
    calls = []
    monkeypatch.setattr('core.update_handoff.launch_after_exit', lambda *args: calls.append(args))
    main.MainWindow._handoff_update_after_shutdown(host)
    assert len(calls) == 1
    main.MainWindow._handoff_update_after_shutdown(host)
    assert len(calls) == 1
    host._pending_update_install = (path, 'a' * 64, [os.getpid()])
    host._shutdown_uncertain_owner = True
    main.MainWindow._handoff_update_after_shutdown(host)
    assert len(calls) == 1


def test_installer_blocks_all_running_fthr_instances_before_file_changes():
    source = (Path(__file__).resolve().parents[1] / 'installer_windows.iss').read_text()
    barrier = source.split('function PrepareToInstall', 1)[1].split('procedure InitializeWizard', 1)[0]
    assert 'CheckForMutexes(FTHRMutex)' in barrier
    assert 'power button' in barrier
