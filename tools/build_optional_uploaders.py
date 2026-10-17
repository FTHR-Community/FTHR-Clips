#!/usr/bin/env python3
"""Build and seal the dormant Windows upload package.

The uploader is frozen as a one-shot
executable, optionally signed, wrapped in integrity-declared ``.fthrplugin``
archives, and then bound into Core through generated SHA-256 constants.

This module is also the single source of the uploader plugin version, legal
document versions, licence inventory and Core binding format;
tools/build_linux_uploader.py imports them so the two platform builds cannot
drift apart or erase each other's bundle hash.
"""

from __future__ import annotations

import argparse
import ast
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
from importlib import metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = ROOT / 'build' / 'optional-uploaders'
PACKAGES = ROOT / 'plugin-packages'
CORE_MANIFEST = ROOT / 'FTHR_UI' / 'core' / 'uploader_bundle_manifest.py'
ICON = ROOT / 'FTHR_UI' / 'assets' / 'favicon.ico'
REQUIREMENTS = ROOT / 'requirements-uploader.txt'
LICENSE_ROOT = ROOT / 'licenses' / 'optional-uploader'

UPLOADER_PLUGIN_ID = 'com.fthrclips.uploader'
UPLOADER_PLUGIN_VERSION = '1.4.0'
UPLOADER_TERMS_VERSION = '2026-10-15-v3'
UPLOADER_PRIVACY_VERSION = '2026-10-15-v3'

WINDOWS_BUNDLE_BINDING = 'EXPECTED_UPLOADER_BUNDLE_SHA256'
LINUX_BUNDLE_BINDING = 'EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256'


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


def uploader_license_files() -> list[Path]:
    """Every reviewed uploader licence file; each bundle carries all of them."""
    if not LICENSE_ROOT.is_dir():
        return []
    return sorted(path for path in LICENSE_ROOT.rglob('*') if path.is_file())


def missing_uploader_requirements(requirements: Path = REQUIREMENTS) -> list[str]:
    """Pins from requirements-uploader.txt that this interpreter does not satisfy.

    PyInstaller silently freezes an executable without a module it cannot
    find, so a missing paramiko only shows up when the sealed package runs.
    """
    from packaging.requirements import Requirement  # noqa: PLC0415 (PyInstaller dep)

    problems = []
    for raw in requirements.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            problems.append(f'{requirement.name} is not installed')
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            problems.append(
                f'{requirement.name} {installed} does not match {requirement.specifier}')
    return problems


def require_uploader_requirements() -> None:
    problems = missing_uploader_requirements()
    if problems:
        raise ValueError(
            'the uploader build environment does not match '
            f'{REQUIREMENTS.name}: ' + '; '.join(problems)
            + f'. Run: {Path(sys.executable).name} -m pip install -r {REQUIREMENTS.name}')


def _existing_binding(text: str, name: str) -> str | None:
    match = re.search(rf'^{re.escape(name)} = (.+)$', text, flags=re.MULTILINE)
    if match is None:
        return None
    try:
        value = ast.literal_eval(match.group(1).strip())
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) and value else None


def render_core_manifest(existing: str, *, windows_hash: str | None = None,
                         linux_hash: str | None = None) -> str:
    """Regenerate Core's uploader bindings from this module's constants.

    Each platform builder passes only its own bundle hash. The other
    platform's hash is carried over from ``existing`` so a Windows build does
    not erase the Linux binding and vice versa.
    """
    windows_hash = windows_hash or _existing_binding(existing, WINDOWS_BUNDLE_BINDING)
    linux_hash = linux_hash or _existing_binding(existing, LINUX_BUNDLE_BINDING)
    if not windows_hash:
        raise ValueError(f'uploader manifest is missing {WINDOWS_BUNDLE_BINDING}')
    linux_line = (f'{LINUX_BUNDLE_BINDING} = {linux_hash!r}\n' if linux_hash else '')
    return f'''"""Generated release bindings for dormant optional uploader packages."""

UPLOADER_PLUGIN_ID = {UPLOADER_PLUGIN_ID!r}
UPLOADER_PLUGIN_VERSION = {UPLOADER_PLUGIN_VERSION!r}
UPLOADER_TERMS_VERSION = {UPLOADER_TERMS_VERSION!r}
UPLOADER_PRIVACY_VERSION = {UPLOADER_PRIVACY_VERSION!r}
{WINDOWS_BUNDLE_BINDING} = {windows_hash!r}
{linux_line}
'''


def write_core_manifest(*, windows_hash: str | None = None,
                        linux_hash: str | None = None) -> None:
    existing = CORE_MANIFEST.read_text(encoding='utf-8') if CORE_MANIFEST.is_file() else ''
    CORE_MANIFEST.write_text(
        render_core_manifest(existing, windows_hash=windows_hash, linux_hash=linux_hash),
        encoding='utf-8')


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
    licenses = uploader_license_files() if package.name == 'uploader' else []
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--sign-command',
        help='operator-owned signing command template containing {file}')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('The current optional package entrypoints are Windows executables.')
    try:
        require_uploader_requirements()
        _safe_remove_tree(BUILD_ROOT)
        hashes: dict[str, str] = {}
        for package in PACKAGE_DEFINITIONS:
            executable = _build_executable(package)
            if args.sign_command:
                _sign(args.sign_command, executable)
            bundle = _seal(package, executable)
            hashes[package.name] = _sha256(bundle)
        write_core_manifest(windows_hash=hashes['uploader'])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
