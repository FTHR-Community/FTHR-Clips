#!/usr/bin/env python3
"""Build and verify the Windows bundle and Inno Setup installer.

Fetch and verify the Microsoft runtime, then run license and lifecycle gates.
For signing, provide a command containing ``{file}`` via --sign-command or
FTHR_SIGN_COMMAND. Use --require-signed to require signed release outputs.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist' / 'FTHRClips'
REDIST = ROOT / 'redist' / 'vc_redist.x64.exe'


def run(command: list[str], *, label: str) -> None:
    print(f'\n== {label} ==')
    print(' '.join(f'"{part}"' if ' ' in part else part for part in command))
    subprocess.run(command, cwd=ROOT, check=True)


def find_iscc(value: str | None) -> Path:
    candidates = [Path(value)] if value else []
    local_app_data = os.environ.get('LOCALAPPDATA')
    candidates.extend([
        Path(os.environ['INNO_SETUP_COMPILER'])
        if os.environ.get('INNO_SETUP_COMPILER') else Path(),
        Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'),
        Path(r'C:\Program Files\Inno Setup 6\ISCC.exe'),
        Path(local_app_data) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe'
        if local_app_data else Path(),
    ])
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        'Inno Setup 6 compiler was not found. Set INNO_SETUP_COMPILER or pass --iscc.')


def signing_targets() -> tuple[Path, Path, Path]:
    return (
        DIST / 'FTHRClips.exe',
        DIST / '_internal' / 'engine' / 'FTHRClips.exe',
        DIST / '_internal' / 'engine' / 'FTHRPlaybackMixer.dll',
    )


def sign(template: str, target: Path) -> None:
    if '{file}' not in template:
        raise ValueError('sign command must contain the literal {file} placeholder')
    command = [part.replace('{file}', str(target))
               for part in shlex.split(template, posix=False)]
    if not command:
        raise ValueError('sign command is empty')
    run(command, label=f'Sign {target.relative_to(ROOT)}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-pyinstaller', action='store_true',
                        help='reuse an existing dist/FTHRClips bundle')
    parser.add_argument('--skip-fetch-vcredist', action='store_true',
                        help='reuse an already verified redist/vc_redist.x64.exe')
    parser.add_argument('--iscc', help='path to Inno Setup ISCC.exe')
    parser.add_argument('--sign-command',
                        help='operator-owned signing command template containing {file}')
    parser.add_argument('--require-signed', action='store_true',
                        help='fail instead of leaving unsigned artifacts for local qualification')
    args = parser.parse_args()

    sign_command = args.sign_command or os.environ.get('FTHR_SIGN_COMMAND')
    if args.require_signed and not sign_command:
        parser.error('--require-signed needs --sign-command or FTHR_SIGN_COMMAND')

    try:
        run([sys.executable, 'tools/generate_installer_assets.py'],
            label='Render current installer artwork')
        run([sys.executable, 'tools/verify_version_consistency.py'],
            label='Version consistency')
        run([sys.executable, 'tools/verify_release_licenses.py', '--tree', '.'],
            label='Source license gate')
        if not args.skip_fetch_vcredist:
            run([sys.executable, 'tools/fetch_third_party.py', '--vcredist'],
                label='Fetch and verify Microsoft VC++ redistributable')
        if not REDIST.is_file():
            raise FileNotFoundError(f'missing prerequisite: {REDIST}')
        if not args.skip_pyinstaller:
            optional_command = [
                sys.executable, 'tools/build_optional_uploaders.py']
            if sign_command:
                optional_command.extend(['--sign-command', sign_command])
            run(optional_command, label='Build sealed optional upload packages')
            run([sys.executable, '-m', 'PyInstaller', 'FTHR.spec', '--clean', '--noconfirm'],
                label='PyInstaller Windows onedir bundle')
        if not DIST.is_dir():
            raise FileNotFoundError(f'missing PyInstaller bundle: {DIST}')
        run([sys.executable, 'tools/verify_release_licenses.py',
             '--windows-dist', str(DIST)],
            label='Windows bundle license gate')
        run([sys.executable, 'tools/verify_windows_installer_lifecycle.py',
             '--bundle', str(DIST), '--vcredist', str(REDIST)],
            label='Installer inputs gate')

        if sign_command:
            for target in signing_targets():
                if not target.is_file():
                    raise FileNotFoundError(f'FTHR signing target is missing: {target}')
                sign(sign_command, target)

        iscc = find_iscc(args.iscc)
        run([str(iscc), 'installer_windows.iss'], label='Inno Setup compile')

        sys.path.insert(0, str(ROOT / 'FTHR_UI'))
        from version import __version__  # noqa: PLC0415
        installer = ROOT / 'Output' / f'FTHRClips-Setup-{__version__}-x64.exe'
        if not installer.is_file():
            raise FileNotFoundError(f'Inno Setup did not produce: {installer}')
        if sign_command:
            sign(sign_command, installer)

        command = [sys.executable, 'tools/verify_windows_installer_lifecycle.py',
                   '--bundle', str(DIST), '--vcredist', str(REDIST),
                   '--installer', str(installer)]
        if args.require_signed:
            command.append('--require-signed')
        run(command, label='Final installer gate')
    except (FileNotFoundError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1

    if sign_command:
        print('\nRESULT: Windows installer built with the supplied signing command.')
    else:
        print('\nRESULT: Windows installer built for local qualification only; it is unsigned and not publishable.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
