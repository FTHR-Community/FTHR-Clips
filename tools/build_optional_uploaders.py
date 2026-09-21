#!/usr/bin/env python3
"""Build and seal the two dormant Windows upload packages.

The uploader and Hardware Identity are frozen as independent one-shot
executables, optionally signed, wrapped in integrity-declared ``.fthrplugin``
archives, and then bound into Core through generated SHA-256 constants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = ROOT / 'build' / 'optional-uploaders'
PACKAGES = ROOT / 'plugin-packages'
CORE_MANIFEST = ROOT / 'FTHR_UI' / 'core' / 'uploader_bundle_manifest.py'
ICON = ROOT / 'FTHR_UI' / 'assets' / 'favicon.ico'

UPLOADER_PLUGIN_ID = 'com.fthrclips.uploader'
UPLOADER_PLUGIN_VERSION = '1.1.0'
UPLOADER_TERMS_VERSION = '2026-08-24-v1'
UPLOADER_PRIVACY_VERSION = '2026-08-24-v1'
HARDWARE_PLUGIN_ID = 'com.fthrclips.hardware-identity'
HARDWARE_PLUGIN_VERSION = '1.0.0'
HARDWARE_POLICY_VERSION = 'lustful-2026-07-27-hwid-v1'


@dataclass(frozen=True)
class Package:
    name: str
    source_dir: Path
    source_file: Path
    executable: str
    bundle: str
    plugin_id: str
    plugin_version: str
    legal_versions: dict[str, str]


PACKAGE_DEFINITIONS = (
    Package(
        name='uploader',
        source_dir=ROOT / 'FTHR_Uploader',
        source_file=ROOT / 'FTHR_Uploader' / 'uploader_service.py',
        executable='FTHR Uploader.exe',
        bundle='FTHR-Uploader.fthrplugin',
        plugin_id=UPLOADER_PLUGIN_ID,
        plugin_version=UPLOADER_PLUGIN_VERSION,
        legal_versions={
            'terms_version': UPLOADER_TERMS_VERSION,
            'privacy_version': UPLOADER_PRIVACY_VERSION,
        },
    ),
    Package(
        name='hardware-identity',
        source_dir=ROOT / 'FTHR_Hardware_ID',
        source_file=ROOT / 'FTHR_Hardware_ID' / 'hardware_id_service.py',
        executable='FTHR Hardware Identity.exe',
        bundle='FTHR-Hardware-Identity.fthrplugin',
        plugin_id=HARDWARE_PLUGIN_ID,
        plugin_version=HARDWARE_PLUGIN_VERSION,
        legal_versions={'policy_version': HARDWARE_POLICY_VERSION},
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str], label: str) -> None:
    print(f'\n== {label} ==')
    subprocess.run(command, cwd=ROOT, check=True)


def _sign(template: str, target: Path) -> None:
    if '{file}' not in template:
        raise ValueError('sign command must contain the literal {file} placeholder')
    command = [
        part.replace('{file}', str(target))
        for part in shlex.split(template, posix=False)
    ]
    _run(command, f'Sign {target.name}')


def _safe_remove_tree(path: Path) -> None:
    resolved = path.resolve(strict=False)
    build_root = (ROOT / 'build').resolve(strict=False)
    if build_root not in resolved.parents:
        raise ValueError(f'Refusing to remove a path outside build/: {resolved}')
    if resolved.exists():
        shutil.rmtree(resolved)


def _build_executable(package: Package) -> Path:
    package_root = BUILD_ROOT / package.name
    dist = package_root / 'dist'
    work = package_root / 'work'
    spec = package_root / 'spec'
    for directory in (dist, work, spec):
        directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        '-m',
        'PyInstaller',
        '--noconfirm',
        '--clean',
        '--onefile',
        '--console',
        '--name',
        package.executable.removesuffix('.exe'),
        '--paths',
        str(package.source_dir),
        '--distpath',
        str(dist),
        '--workpath',
        str(work),
        '--specpath',
        str(spec),
    ]
    if ICON.is_file():
        command.extend(['--icon', str(ICON)])
    command.append(str(package.source_file))
    _run(command, f'Build {package.executable}')
    executable = dist / package.executable
    if not executable.is_file():
        raise FileNotFoundError(f'PyInstaller did not create {executable}')
    return executable


def _seal(package: Package, executable: Path) -> Path:
    destination = PACKAGES / package.bundle
    manifest = {
        'schema_version': 1,
        'plugin_id': package.plugin_id,
        'plugin_version': package.plugin_version,
        **package.legal_versions,
        'entrypoint': package.executable,
        'activation': 'in_app_explicit_consent_only',
        'files': [{
            'path': f'payload/{package.executable}',
            'sha256': _sha256(executable),
            'size': executable.stat().st_size,
        }],
    }
    PACKAGES.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            'manifest.json',
            json.dumps(manifest, indent=2, sort_keys=True).encode('utf-8'))
        archive.write(package.source_dir / 'TERMS_OF_SERVICE.txt', 'TERMS_OF_SERVICE.txt')
        archive.write(package.source_dir / 'PRIVACY_POLICY.txt', 'PRIVACY_POLICY.txt')
        archive.write(executable, f'payload/{package.executable}')
    os.replace(temporary, destination)
    print(f'Sealed {destination.relative_to(ROOT)} ({_sha256(destination)})')
    return destination


def bind_windows_bundle_hashes(existing: str, hashes: dict[str, str]) -> str:
    """Update only Windows bundle hashes while preserving other bindings."""
    result = existing
    for name, key in (
            ('EXPECTED_UPLOADER_BUNDLE_SHA256', 'uploader'),
            ('EXPECTED_HARDWARE_BUNDLE_SHA256', 'hardware-identity')):
        replacement = f'{name} = {hashes[key]!r}'
        updated, count = re.subn(
            rf'^{re.escape(name)} = [^\r\n]*',
            replacement,
            result,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f'uploader manifest is missing {name}')
        result = updated
    return result


def _write_core_manifest(hashes: dict[str, str]) -> None:
    existing = CORE_MANIFEST.read_text(encoding='utf-8')
    CORE_MANIFEST.write_text(
        bind_windows_bundle_hashes(existing, hashes),
        encoding='utf-8',
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--sign-command',
        help='operator-owned signing command template containing {file}')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('The current optional package entrypoints are Windows executables.')
    try:
        _safe_remove_tree(BUILD_ROOT)
        hashes: dict[str, str] = {}
        for package in PACKAGE_DEFINITIONS:
            executable = _build_executable(package)
            if args.sign_command:
                _sign(args.sign_command, executable)
            bundle = _seal(package, executable)
            hashes[package.name] = _sha256(bundle)
        _write_core_manifest(hashes)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
