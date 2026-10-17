#!/usr/bin/env python3
"""Fetch verified Windows FFmpeg and Microsoft VC++ runtime build inputs.

Use --ffmpeg, --vcredist, or --all. Reject manifest hash mismatches before packaging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'tools' / 'ffmpeg_manifest.json'
FFMPEG_DIR = ROOT / 'FTHRcapture' / 'FTHRclips' / 'third_party' / 'ffmpeg'
REDIST_DIR = ROOT / 'redist'

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
    ap.add_argument('--vcredist', action='store_true')
    ap.add_argument('--all', action='store_true',
                    help='everything for the current platform')
    ap.add_argument('--force', action='store_true',
                    help='re-download even if the files are already present')
    args = ap.parse_args()

    if not (args.ffmpeg or args.vcredist or args.all):
        ap.print_help()
        return 2

    rc = 0
    if args.ffmpeg or args.all:
        rc |= fetch_ffmpeg(args.force)
    if args.vcredist or args.all:
        rc |= fetch_vcredist(args.force)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
