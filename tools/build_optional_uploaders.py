#!/usr/bin/env python3
"""Build and seal the dormant Windows upload package.

The uploader is frozen as a one-shot
executable, optionally signed, wrapped in integrity-declared ``.fthrplugin``
archives, and then bound into Core through generated SHA-256 constants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
UPLOADER_PLUGIN_VERSION = '1.4.0'
UPLOADER_TERMS_VERSION = '2026-10-15-v3'
UPLOADER_PRIVACY_VERSION = '2026-10-15-v3'


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
    if package.name == 'uploader':
        command.extend(['--hidden-import', 'keyring.backends.Windows'])
    command.append(str(package.source_file))
    _run(command, f'Build {package.executable}')
    executable = dist / package.executable
    if not executable.is_file():
        raise FileNotFoundError(f'PyInstaller did not create {executable}')
    return executable


def _seal(package: Package, executable: Path) -> Path:
    destination = PACKAGES / package.bundle
    licenses = (sorted(path for path in (ROOT / 'licenses' / 'optional-uploader').rglob('*')
                       if path.is_file()) if package.name == 'uploader' else [])
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
        }, *[{
            'path': 'payload/' + path.relative_to(ROOT).as_posix(),
            'sha256': _sha256(path),
            'size': path.stat().st_size,
        } for path in licenses]],
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
        for license_file in licenses:
            archive.write(license_file, 'payload/' + license_file.relative_to(ROOT).as_posix())
        archive.write(executable, f'payload/{package.executable}')
    os.replace(temporary, destination)
    print(f'Sealed {destination.relative_to(ROOT)} ({_sha256(destination)})')
    return destination


def _write_core_manifest(hashes: dict[str, str]) -> None:
    text = f'''"""Generated release bindings for dormant optional uploader packages."""

UPLOADER_PLUGIN_ID = {UPLOADER_PLUGIN_ID!r}
UPLOADER_PLUGIN_VERSION = {UPLOADER_PLUGIN_VERSION!r}
UPLOADER_TERMS_VERSION = {UPLOADER_TERMS_VERSION!r}
UPLOADER_PRIVACY_VERSION = {UPLOADER_PRIVACY_VERSION!r}
EXPECTED_UPLOADER_BUNDLE_SHA256 = {hashes['uploader']!r}

'''
    CORE_MANIFEST.write_text(text, encoding='utf-8')


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
