#!/usr/bin/env python3
"""Fetch manifest-pinned FFmpeg and the Microsoft VC++ redistributable.

--ffmpeg installs the Windows runtime; --ffmpeg-linux installs matching Linux
headers and libraries; --vcredist fetches the Windows prerequisite. --all
selects this platform's inputs. Verify FFmpeg files against manifest hashes
and reject mismatches before packaging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'tools' / 'ffmpeg_manifest.json'
FFMPEG_DIR = ROOT / 'FTHRcapture' / 'FTHRclips' / 'third_party' / 'ffmpeg'
REDIST_DIR = ROOT / 'redist'

# AUDIT-014: the Linux engine must NOT link the distribution's FFmpeg, which is
# a GPL build on every mainstream distro. This is the LGPL runtime it is built
# against and shipped with instead.
MANIFEST_LINUX = ROOT / 'tools' / 'ffmpeg_manifest_linux.json'
FFMPEG_LINUX_DIR = ROOT / 'FTHRcapture_linux' / 'third_party' / 'ffmpeg'
APPIMAGE_MANIFEST = ROOT / 'tools' / 'appimage_tool_manifest.json'
APPIMAGE_OUTPUT_DIR = ROOT / 'build_output'

VCREDIST_URL = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
VCREDIST_METADATA = REDIST_DIR / 'vc_redist.x64.json'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    print(f'  downloading {url}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dest.open('wb') as out:
        total = int(resp.headers.get('Content-Length') or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f'\r  {pct:3d}%  {done / 1e6:.1f} / {total / 1e6:.1f} MB',
                      end='', flush=True)
        print()


def _verify_download(path: Path, asset: dict[str, object]) -> None:
    """Verify a downloaded asset's exact byte count and SHA-256."""
    expected_size = int(asset['size_bytes'])
    expected_hash = str(asset['sha256']).lower()
    if not path.is_file():
        raise RuntimeError(f'downloaded asset is missing: {path}')
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f'{path.name} size mismatch: expected {expected_size}, got {actual_size}')
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f'{path.name} sha256 mismatch: expected {expected_hash}, got {actual_hash}')


def _verified_appimage_asset(
    asset: dict[str, object], target: Path, *, force: bool = False
) -> None:
    """Install one manifest-pinned AppImage asset with atomic replacement."""
    if target.is_file():
        try:
            _verify_download(target, asset)
        except RuntimeError as exc:
            print(f'  cached {target.name} is invalid: {exc}')
        else:
            print(f'  cached {target.name} passed size and sha256 verification.')
            if not force:
                return

    url = str(asset['url'])
    if not url.startswith('https://'):
        raise RuntimeError(f'refusing non-HTTPS AppImage asset URL: {url}')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f'.{target.name}.', suffix='.download', dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        _download(url, temp)
        _verify_download(temp, asset)
        temp.chmod(temp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(temp, target)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f'could not install verified {target.name}: {exc}') from exc


def fetch_appimage_tools(force: bool) -> int:
    """Fetch and verify the pinned appimagetool and type-2 runtime."""
    try:
        manifest = json.loads(APPIMAGE_MANIFEST.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'could not read AppImage tool manifest: {exc}') from exc
    if not isinstance(manifest, dict):
        raise RuntimeError('AppImage tool manifest must contain a JSON object')
    if manifest.get('platform') != 'linux' or manifest.get('arch') != 'x86_64':
        raise RuntimeError('AppImage tool manifest is not for Linux x86_64')
    assets: dict[str, dict[str, object]] = {}
    for key in ('appimagetool', 'runtime'):
        asset = manifest.get(key)
        if not isinstance(asset, dict):
            raise RuntimeError(f'AppImage tool manifest is missing {key}')
        filename = asset.get('filename')
        url = asset.get('url')
        size_bytes = asset.get('size_bytes')
        sha256 = asset.get('sha256')
        if not isinstance(filename, str) or not filename:
            raise RuntimeError(f'AppImage tool manifest has invalid {key} filename')
        if Path(filename).name != filename:
            raise RuntimeError(f'invalid AppImage asset filename: {filename!r}')
        if not isinstance(url, str) or not url.startswith('https://'):
            raise RuntimeError(f'AppImage tool manifest has invalid {key} URL')
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
            raise RuntimeError(f'AppImage tool manifest has invalid {key} size_bytes')
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', sha256):
            raise RuntimeError(f'AppImage tool manifest has invalid {key} sha256')
        assets[key] = asset

    # Validate both records before touching the network. A malformed runtime
    # entry must not allow the first asset to be fetched successfully.
    for key, asset in assets.items():
        filename = str(asset['filename'])
        target = APPIMAGE_OUTPUT_DIR / filename
        print(f'Fetching verified {key} {asset.get("version", "unknown")}')
        _verified_appimage_asset(asset, target, force=force)
    return 0


def _windows_powershell() -> str:
    """Prefer the inbox Windows PowerShell Authenticode implementation.

    A developer's PATH can resolve ``powershell.exe`` to a bundled PowerShell
    Core runtime whose Security module lacks the Windows trust provider.  The
    inbox binary is the one that owns the native Authenticode provider.
    """
    candidate = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
        'System32/WindowsPowerShell/v1.0/powershell.exe')
    return str(candidate) if candidate.is_file() else 'powershell.exe'


def _windows_powershell_module_path() -> str:
    system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    program_files = Path(os.environ.get('ProgramFiles', r'C:\Program Files'))
    return ';'.join((
        str(program_files / 'WindowsPowerShell' / 'Modules'),
        str(system_root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'Modules'),
    ))


def _verify_vcredist(path: Path) -> dict[str, str]:
    """Verify Microsoft's serviced redistributable before packaging.

    Require HTTPS and a trusted Microsoft Corporation signature, then record
    the exact local hash and version. The stable URL changes with updates,
    so a permanent hash pin would reject serviced binaries.
    """
    with path.open('rb') as fh:
        if fh.read(2) != b'MZ':
            raise RuntimeError('downloaded file is not a Windows executable')

    details: dict[str, str] = {
        'source_url': VCREDIST_URL,
        'sha256': _sha256(path),
        'bytes': str(path.stat().st_size),
        'authenticode_status': 'not-checked-off-windows',
        'signer_subject': '',
        'file_version': '',
        'product_version': '',
    }
    if os.name != 'nt':
        return details

    # Pass the path in a one-process environment variable rather than splicing
    # it into a PowerShell command string. A source-tree path with
    # spaces/apostrophes cannot alter the verification command this way.
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        "$target = $env:FTHR_VCREDIST_VERIFY_PATH; "
        "$signature = Get-AuthenticodeSignature -LiteralPath $target; "
        "$item = Get-Item -LiteralPath $target; "
        "[PSCustomObject]@{status=[string]$signature.Status; "
        "subject=if ($signature.SignerCertificate) {[string]$signature.SignerCertificate.Subject} else {''}; "
        "file_version=[string]$item.VersionInfo.FileVersion; "
        "product_version=[string]$item.VersionInfo.ProductVersion} | ConvertTo-Json -Compress"
    )
    env = os.environ.copy()
    # Do not let a bundled PowerShell Core module path shadow Windows
    # PowerShell's native Authenticode provider.
    env['PSModulePath'] = _windows_powershell_module_path()
    env['FTHR_VCREDIST_VERIFY_PATH'] = str(path.resolve())
    result = subprocess.run([_windows_powershell(), '-NoProfile', '-Command', command],
                            capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or 'could not inspect VC++ Authenticode signature')
    try:
        signature = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('could not parse VC++ Authenticode metadata') from exc
    status = str(signature.get('status', ''))
    subject = str(signature.get('subject', ''))
    if status != 'Valid' or 'Microsoft Corporation' not in subject:
        raise RuntimeError(
            'VC++ redistributable does not have a valid Microsoft Corporation '
            f'Authenticode signature (status={status!r}, subject={subject!r})')
    details.update({
        'authenticode_status': status,
        'signer_subject': subject,
        'file_version': str(signature.get('file_version', '')),
        'product_version': str(signature.get('product_version', '')),
    })
    return details


def _ensure_linux_ffmpeg_tool_rpaths(bin_dir: Path) -> None:
    """Make bundled FFmpeg CLI tools find libraries beside the archive.

    BtbN's Linux archive currently embeds the linker flag ``-Wl:../lib`` as
    DT_RPATH. That is a literal, invalid loader path rather than a path
    relative to the executable. Patch only the two CLI entry points; library
    bytes remain covered by the manifest hashes.
    """
    patchelf = shutil.which('patchelf')
    if patchelf is None:
        raise RuntimeError(
            'patchelf is required to repair the bundled Linux FFmpeg CLI RPATH; '
            'install it before fetching FFmpeg')

    for name in ('ffmpeg', 'ffprobe'):
        tool = bin_dir / name
        if not tool.is_file():
            raise RuntimeError(f'Linux FFmpeg archive is missing bin/{name}')
        subprocess.run(
            [patchelf, '--force-rpath', '--set-rpath', '$ORIGIN/../lib', str(tool)],
            check=True,
            capture_output=True,
            text=True,
        )


def _ensure_linux_ffmpeg_aliases(
    lib_dir: Path, soname_map: dict[str, str]
) -> None:
    """Restore and verify FFmpeg development and SONAME aliases after extraction.

    Missing aliases can select system libraries or break runtime loading.
    Prefer symlinks, then hard links, then byte copies for portability.
    """
    for versioned_name, soname in soname_map.items():
        source = lib_dir / versioned_name
        source_digest = _sha256(source)
        linker_name = soname.split('.so.', 1)[0] + '.so'
        for alias_name in (soname, linker_name):
            alias = lib_dir / alias_name
            same_file = alias.is_file() and os.path.samefile(alias, source)
            if same_file or (alias.is_file() and _sha256(alias) == source_digest):
                continue
            alias.unlink(missing_ok=True)
            try:
                alias.symlink_to(source.name)
            except OSError:
                try:
                    os.link(source, alias)
                except OSError:
                    shutil.copy2(source, alias)
            same_file = alias.is_file() and os.path.samefile(alias, source)
            if not same_file and (
                not alias.is_file() or _sha256(alias) != source_digest
            ):
                raise RuntimeError(
                    f'could not materialize FFmpeg library alias: {alias_name}')


def fetch_ffmpeg(force: bool) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    src = manifest['source']
    expected = manifest['shipped_files_sha256']
    bin_dir = FFMPEG_DIR / 'bin'

    if bin_dir.is_dir() and not force:
        have = {p.name: p for p in bin_dir.iterdir() if p.is_file()}
        if set(expected) <= set(have):
            bad = [n for n, digest in expected.items()
                   if _sha256(have[n]) != digest]
            if not bad:
                print(f'FFmpeg {manifest["version"]} already present and verified.')
                return 0
            print(f'  present but {len(bad)} file(s) failed sha256: {bad}')

    print(f'FFmpeg {manifest["version"]} ({manifest["license"]}) '
          f'from {src["project"]}')

    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / src['asset']
        _download(src['url'], archive)

        digest = _sha256(archive)
        if digest != src['sha256']:
            print(f'ERROR: archive sha256 mismatch\n'
                  f'  expected {src["sha256"]}\n'
                  f'  got      {digest}\n'
                  f'This is NOT the build the licence paperwork describes. '
                  f'Refusing to install it.', file=sys.stderr)
            return 1
        print('  archive sha256 OK')

        with zipfile.ZipFile(archive) as zf:
            # BtbN archives nest everything under a single top-level directory.
            members = [n for n in zf.namelist() if not n.endswith('/')]
            root_prefix = members[0].split('/', 1)[0] + '/'
            staged = Path(td) / 'staged'
            for name in members:
                rel = name[len(root_prefix):] if name.startswith(root_prefix) else name
                if not rel:
                    continue
                target = staged / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as fsrc, target.open('wb') as fdst:
                    shutil.copyfileobj(fsrc, fdst)

        staged_bin = staged / 'bin'
        missing = [n for n in expected if not (staged_bin / n).is_file()]
        if missing:
            print(f'ERROR: archive is missing expected files: {missing}',
                  file=sys.stderr)
            return 1
        bad = [n for n, d in expected.items() if _sha256(staged_bin / n) != d]
        if bad:
            print(f'ERROR: per-file sha256 mismatch: {bad}', file=sys.stderr)
            return 1
        print(f'  all {len(expected)} binaries verified against the manifest')

        if bin_dir.exists():
            shutil.rmtree(bin_dir)
        shutil.copytree(staged_bin, bin_dir)

        # Headers and import libs are tracked in git; only replace them if the
        # clone is missing them (e.g. a shallow export).
        for sub in ('include', 'lib'):
            dest = FFMPEG_DIR / sub
            if not dest.exists() and (staged / sub).exists():
                shutil.copytree(staged / sub, dest)
                print(f'  restored missing {sub}/')

        lic = FFMPEG_DIR / 'LICENSE.txt'
        if not lic.exists():
            for cand in ('LICENSE.txt', 'LICENSE'):
                if (staged / cand).is_file():
                    shutil.copy2(staged / cand, lic)
                    break

    print(f'FFmpeg installed into {bin_dir.relative_to(ROOT)}')
    return 0


def fetch_ffmpeg_linux(force: bool) -> int:
    """Install the pinned Linux FFmpeg headers and runtime from one archive.

    Compile-time and bundled libraries must agree on the ABI.
    """
    manifest = json.loads(MANIFEST_LINUX.read_text(encoding='utf-8'))
    src = manifest['source']
    expected = manifest['shipped_files_sha256']
    lib_dir = FFMPEG_LINUX_DIR / 'lib'

    if lib_dir.is_dir() and not force:
        have = {p.name: p for p in lib_dir.iterdir() if p.is_file()}
        if set(expected) <= set(have):
            bad = [n for n, d in expected.items() if _sha256(have[n]) != d]
            if not bad:
                _ensure_linux_ffmpeg_tool_rpaths(FFMPEG_LINUX_DIR / 'bin')
                _ensure_linux_ffmpeg_aliases(lib_dir, manifest['soname_map'])
                print(f'LGPL FFmpeg {manifest["version"]} already present and verified.')
                return 0
            print(f'  present but {len(bad)} file(s) failed sha256: {bad}')

    print(f'FFmpeg {manifest["version"]} ({manifest["license"]}) '
          f'from {src["project"]} — Linux')

    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / src['asset']
        _download(src['url'], archive)

        digest = _sha256(archive)
        if digest != src['sha256']:
            print('ERROR: archive sha256 mismatch\n'
                  f'  expected {src["sha256"]}\n'
                  f'  got      {digest}\n'
                  'This is NOT the build the licence paperwork describes. '
                  'Refusing to install it.', file=sys.stderr)
            return 1
        print('  archive sha256 OK')

        staged = Path(td) / 'staged'
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.issym()]
            root_prefix = members[0].name.split('/', 1)[0] + '/'
            for m in members:
                rel = m.name[len(root_prefix):] if m.name.startswith(root_prefix) else m.name
                if not rel or rel.startswith(('doc/', 'man/', 'presets/')):
                    continue
                m2 = tf.getmember(m.name)
                m2.name = rel
                # filter='data' refuses absolute paths and traversal (py3.12+).
                try:
                    tf.extract(m2, staged, filter='data')
                except TypeError:            # older tarfile without `filter`
                    tf.extract(m2, staged)

        staged_lib = staged / 'lib'
        missing = [n for n in expected if not (staged_lib / n).is_file()]
        if missing:
            print(f'ERROR: archive is missing expected libraries: {missing}',
                  file=sys.stderr)
            return 1
        bad = [n for n, d in expected.items() if _sha256(staged_lib / n) != d]
        if bad:
            print(f'ERROR: per-file sha256 mismatch: {bad}', file=sys.stderr)
            return 1
        print(f'  all {len(expected)} libraries verified against the manifest')

        if FFMPEG_LINUX_DIR.exists():
            shutil.rmtree(FFMPEG_LINUX_DIR)
        FFMPEG_LINUX_DIR.mkdir(parents=True)
        for sub in ('lib', 'include', 'bin'):
            if (staged / sub).exists():
                shutil.copytree(staged / sub, FFMPEG_LINUX_DIR / sub,
                                symlinks=True)
        for lic in ('LICENSE.txt', 'LICENSE'):
            if (staged / lic).is_file():
                shutil.copy2(staged / lic, FFMPEG_LINUX_DIR / 'LICENSE.txt')
                break

        _ensure_linux_ffmpeg_tool_rpaths(FFMPEG_LINUX_DIR / 'bin')
        _ensure_linux_ffmpeg_aliases(lib_dir, manifest['soname_map'])

    print(f'LGPL FFmpeg installed into {FFMPEG_LINUX_DIR.relative_to(ROOT)}')
    print(f'  build the engine with: '
          f'-DFTHR_FFMPEG_ROOT={FFMPEG_LINUX_DIR.relative_to(ROOT)}')
    return 0


def fetch_vcredist(force: bool) -> int:
    dest = REDIST_DIR / 'vc_redist.x64.exe'
    downloaded = False
    if dest.is_file() and not force:
        print(f'vc_redist.x64.exe already present ({dest.stat().st_size / 1e6:.1f} MB).')
    else:
        print('MSVC 2022 x64 redistributable (Microsoft, redistributable licence)')
        _download(VCREDIST_URL, dest)
        downloaded = True
    try:
        metadata = _verify_vcredist(dest)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'ERROR: VC++ redistributable verification failed: {exc}',
              file=sys.stderr)
        if downloaded:
            dest.unlink(missing_ok=True)
        return 1
    REDIST_DIR.mkdir(parents=True, exist_ok=True)
    VCREDIST_METADATA.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'Verified {dest.relative_to(ROOT)} '
          f'({dest.stat().st_size / 1e6:.1f} MB, sha256 {metadata["sha256"]})')
    if metadata['authenticode_status'] == 'Valid':
        print(f'  Authenticode: {metadata["signer_subject"]}; '
              f'FileVersion={metadata["file_version"] or "unknown"}')
    else:
        print('  Authenticode: not checked (non-Windows host); verify on Windows before packaging.')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ffmpeg', action='store_true',
                    help='Windows LGPL FFmpeg runtime')
    ap.add_argument('--ffmpeg-linux', action='store_true',
                    help='Linux LGPL FFmpeg (headers + libs) — AUDIT-014')
    ap.add_argument('--appimagetool-linux', action='store_true',
                    help='pinned Linux AppImage tool and type-2 runtime')
    ap.add_argument('--vcredist', action='store_true')
    ap.add_argument('--all', action='store_true',
                    help='everything for the current platform')
    ap.add_argument('--force', action='store_true',
                    help='re-download even if the files are already present')
    args = ap.parse_args()

    if not (args.ffmpeg or args.ffmpeg_linux or args.appimagetool_linux
            or args.vcredist or args.all):
        ap.print_help()
        return 2

    # all is platform-aware: fetching the Windows runtime on Linux (or the
    # reverse) downloads 150 MB nobody can use.
    on_windows = sys.platform == 'win32'
    rc = 0
    if args.ffmpeg or (args.all and on_windows):
        rc |= fetch_ffmpeg(args.force)
    if args.ffmpeg_linux or (args.all and not on_windows):
        rc |= fetch_ffmpeg_linux(args.force)
    if args.appimagetool_linux or (args.all and not on_windows):
        try:
            rc |= fetch_appimage_tools(args.force)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f'ERROR: AppImage tool preparation failed: {exc}',
                  file=sys.stderr)
            rc |= 1
    if args.vcredist or (args.all and on_windows):
        rc |= fetch_vcredist(args.force)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
