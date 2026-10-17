#!/usr/bin/env python3
"""Build and seal the optional Linux upload extension bundle.

The plugin identity, legal document versions, licence inventory and the Core
binding format come from tools/build_optional_uploaders.py, so the Linux and
Windows bundles always describe the same plugin release and neither build
erases the other platform's bundle hash in
FTHR_UI/core/uploader_bundle_manifest.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_optional_uploaders as shared  # noqa: E402

ROOT = shared.ROOT
BUILD_ROOT = ROOT / 'build' / 'optional-uploader-linux'
DIST = BUILD_ROOT / 'dist'
WORK = BUILD_ROOT / 'work'
SPEC = BUILD_ROOT / 'spec'
# Every ELF frozen inside the one-file payload, unpacked so that
# tools/verify_linux_glibc.py can hold the plugin to the same glibc ceiling as
# the AppImage. The compressed payload inside the .fthrplugin is invisible to
# a scan of the AppDir.
PAYLOAD_ELF_DIR = BUILD_ROOT / 'payload-elf'
BUNDLE = ROOT / 'plugin-packages' / 'FTHR-Uploader-linux.fthrplugin'
UPLOADER_DIR = ROOT / 'FTHR_Uploader'
ENTRYPOINT = 'FTHR-Uploader'
_ELF_MAGIC = b'\x7fELF'

# sftp_provider is imported lazily by the service; keyring resolves backends
# through entry points that a frozen executable does not carry, so name the
# Secret Service backend (SecretStorage over jeepney) explicitly.
HIDDEN_IMPORTS = (
    'sftp_provider',
    'keyring.backends.SecretService',
    'secretstorage',
    'jeepney.io.blocking',
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def _remove_build_dir(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if BUILD_ROOT.resolve(strict=False) not in (resolved, *resolved.parents):
        raise ValueError(f'Refusing to remove a path outside {BUILD_ROOT}: {resolved}')
    if resolved.exists():
        shutil.rmtree(resolved)


def extract_payload_elves(executable: Path, destination: Path) -> list[Path]:
    """Unpack every ELF member of a PyInstaller one-file executable."""
    from PyInstaller.archive.readers import CArchiveReader  # noqa: PLC0415

    _remove_build_dir(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    extracted = []
    archive = CArchiveReader(str(executable))
    for name in sorted(archive.toc):
        data = archive.extract(name)
        if not data.startswith(_ELF_MAGIC):
            continue
        target = (root / name).resolve()
        if root not in target.parents:
            raise ValueError(f'unsafe member name in {executable.name}: {name!r}')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        extracted.append(target)
    return extracted


def seal(executable: Path) -> Path:
    licenses = shared.uploader_license_files()
    manifest = {
        'schema_version': 1,
        'plugin_id': shared.UPLOADER_PLUGIN_ID,
        'plugin_version': shared.UPLOADER_PLUGIN_VERSION,
        'terms_version': shared.UPLOADER_TERMS_VERSION,
        'privacy_version': shared.UPLOADER_PRIVACY_VERSION,
        'entrypoint': ENTRYPOINT,
        'activation': 'in_app_explicit_consent_only',
        'files': [{
            'path': f'payload/{ENTRYPOINT}',
            'sha256': sha256(executable),
            'size': executable.stat().st_size,
        }, *[{
            'path': 'payload/' + path.relative_to(ROOT).as_posix(),
            'sha256': sha256(path),
            'size': path.stat().st_size,
        } for path in licenses]],
    }
    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    temporary = BUNDLE.with_suffix('.tmp')
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, indent=2, sort_keys=True))
        archive.write(UPLOADER_DIR / 'TERMS_OF_SERVICE.txt', 'TERMS_OF_SERVICE.txt')
        archive.write(UPLOADER_DIR / 'PRIVACY_POLICY.txt', 'PRIVACY_POLICY.txt')
        for license_file in licenses:
            archive.write(license_file, 'payload/' + license_file.relative_to(ROOT).as_posix())
        archive.write(executable, f'payload/{ENTRYPOINT}')
    os.replace(temporary, BUNDLE)
    return BUNDLE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    if sys.platform == 'win32':
        parser.error('This builder is for the Linux AppImage only.')
    if Path(args.python).resolve() != Path(sys.executable).resolve():
        # The requirement check below inspects this interpreter's packages.
        parser.error('run this script with the interpreter passed as --python')

    try:
        shared.require_uploader_requirements()
        for path in (DIST, WORK, SPEC):
            _remove_build_dir(path)
            path.mkdir(parents=True)
        command = [
            args.python, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--onefile', '--console', '--name', ENTRYPOINT,
            '--paths', str(UPLOADER_DIR),
            '--distpath', str(DIST), '--workpath', str(WORK),
            '--specpath', str(SPEC),
        ]
        for module in HIDDEN_IMPORTS:
            command.extend(['--hidden-import', module])
        command.append(str(UPLOADER_DIR / 'uploader_service.py'))
        run(command)
        executable = DIST / ENTRYPOINT
        if not executable.is_file():
            raise FileNotFoundError(executable)
        executable.chmod(executable.stat().st_mode | 0o111)

        elves = extract_payload_elves(executable, PAYLOAD_ELF_DIR)
        bundle = seal(executable)
        bundle_hash = sha256(bundle)
        shared.write_core_manifest(linux_hash=bundle_hash)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(f'Unpacked {len(elves)} payload ELF file(s) to {PAYLOAD_ELF_DIR} for the glibc gate')
    print(f'Built {bundle} ({bundle_hash})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
