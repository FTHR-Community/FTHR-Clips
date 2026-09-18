#!/usr/bin/env python3
"""Build and seal the optional Linux upload extension bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = ROOT / 'build' / 'optional-uploader-linux'
DIST = BUILD_ROOT / 'dist'
WORK = BUILD_ROOT / 'work'
SPEC = BUILD_ROOT / 'spec'
BUNDLE = ROOT / 'plugin-packages' / 'FTHR-Uploader-linux.fthrplugin'
MANIFEST_MODULE = ROOT / 'FTHR_UI' / 'core' / 'uploader_bundle_manifest.py'
ENTRYPOINT = 'FTHR-Uploader'
PLUGIN_ID = 'com.fthrclips.uploader'
PLUGIN_VERSION = '1.1.0'
TERMS_VERSION = '2026-08-24-v1'
PRIVACY_VERSION = '2026-08-24-v1'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def bind_linux_bundle_hash(existing: str, bundle_hash: str) -> str:
    """Update only the generated Linux hash while preserving other bindings."""
    linux_hash_line = (
        f'EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = {bundle_hash!r}')
    if 'EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256' in existing:
        return re.sub(
            r"^EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = .*?$",
            linux_hash_line,
            existing,
            count=1,
            flags=re.MULTILINE)
    marker = f'EXPECTED_UPLOADER_BUNDLE_SHA256 = {bundle_hash!r}'
    if marker not in existing:
        raise ValueError('uploader manifest is missing the Windows bundle hash')
    return existing.replace(marker, marker + '\n' + linux_hash_line, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    if sys.platform == 'win32':
        parser.error('This builder is for the Linux AppImage only.')

    for path in (DIST, WORK, SPEC):
        path.mkdir(parents=True, exist_ok=True)
    run([
        args.python, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--console', '--name', ENTRYPOINT,
        '--paths', str(ROOT / 'FTHR_Uploader'),
        '--paths', str(ROOT / 'FTHR_Hardware_ID'),
        '--distpath', str(DIST), '--workpath', str(WORK),
        '--specpath', str(SPEC),
        str(ROOT / 'FTHR_Uploader' / 'uploader_service.py'),
    ])
    executable = DIST / ENTRYPOINT
    if not executable.is_file():
        raise FileNotFoundError(executable)
    executable.chmod(executable.stat().st_mode | 0o111)

    payload_hash = sha256(executable)
    manifest = {
        'schema_version': 1,
        'plugin_id': PLUGIN_ID,
        'plugin_version': PLUGIN_VERSION,
        'terms_version': TERMS_VERSION,
        'privacy_version': PRIVACY_VERSION,
        'entrypoint': ENTRYPOINT,
        'activation': 'in_app_explicit_consent_only',
        'files': [{
            'path': f'payload/{ENTRYPOINT}',
            'sha256': payload_hash,
            'size': executable.stat().st_size,
        }],
    }
    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    temporary = BUNDLE.with_suffix('.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, indent=2, sort_keys=True))
        archive.write(ROOT / 'FTHR_Uploader' / 'TERMS_OF_SERVICE.txt', 'TERMS_OF_SERVICE.txt')
        archive.write(ROOT / 'FTHR_Uploader' / 'PRIVACY_POLICY.txt', 'PRIVACY_POLICY.txt')
        archive.write(executable, f'payload/{ENTRYPOINT}')
    os.replace(temporary, BUNDLE)
    bundle_hash = sha256(BUNDLE)

    existing = MANIFEST_MODULE.read_text(encoding='utf-8')
    MANIFEST_MODULE.write_text(
        bind_linux_bundle_hash(existing, bundle_hash), encoding='utf-8')
    print(f'Built {BUNDLE} ({bundle_hash})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
