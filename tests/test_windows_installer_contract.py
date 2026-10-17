from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_optional_uploader_gate_checks_reviewed_licences_and_payload(tmp_path, monkeypatch):
    import hashlib
    import json
    import zipfile
    from tools import verify_windows_installer_lifecycle as gate

    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    notice = tmp_path / 'licenses' / 'optional-uploader' / 'library' / 'LICENSE'
    notice.parent.mkdir(parents=True)
    notice.write_bytes(b'Reviewed dependency licence')
    payload = {
        'payload/FTHR Uploader.exe': b'MZ test executable',
        'payload/licenses/optional-uploader/library/LICENSE': notice.read_bytes(),
    }
    manifest = {
        'plugin_id': 'test-uploader',
        'entrypoint': 'FTHR Uploader.exe',
        'files': [{'path': path, 'size': len(data),
                   'sha256': hashlib.sha256(data).hexdigest()}
                  for path, data in payload.items()],
    }
    package = tmp_path / 'uploader.fthrplugin'

    def seal(*, extra=False, corrupt=False):
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('TERMS_OF_SERVICE.txt', 'Terms')
            archive.writestr('PRIVACY_POLICY.txt', 'Privacy')
            for path, data in payload.items():
                archive.writestr(path, b'corrupted' if corrupt else data)
            if extra:
                archive.writestr('payload/unreviewed.exe', b'MZ')

    seal()
    assert gate.optional_package_valid(package, 'test-uploader', 'FTHR Uploader.exe')
    seal(extra=True)
    assert not gate.optional_package_valid(package, 'test-uploader', 'FTHR Uploader.exe')
    seal(corrupt=True)
    assert not gate.optional_package_valid(package, 'test-uploader', 'FTHR Uploader.exe')


def test_windows_installer_lifecycle_source_contract_passes():
    completed = subprocess.run(
        [sys.executable, 'tools/verify_windows_installer_lifecycle.py'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'stable existing AppId is retained' in completed.stdout
    assert 'installer never names the user clip root as a deletion target' in completed.stdout
