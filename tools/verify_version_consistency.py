#!/usr/bin/env python3
"""Check product-version declarations against FTHR_UI/version.py.

The independently versioned IPC layout is checked by
verify_shared_memory_contract.py. Exit 1 reports product-version drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

from version import __version__ as EXPECTED, windows_file_version  # noqa: E402


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, msg: str) -> None:
        print(f'  OK    {msg}')

    def fail(self, msg: str) -> None:
        self.failed = True
        print(f'  FAIL  {msg}')

    def skip(self, msg: str) -> None:
        print(f'  SKIP  {msg}')


def _read(rel: str) -> str | None:
    p = ROOT / rel
    return p.read_text(encoding='utf-8', errors='replace') if p.is_file() else None


def check_literal_pattern(rep: Report, rel: str, pattern: str, label: str) -> None:
    """Assert a regex with one capture group yields exactly EXPECTED."""
    text = _read(rel)
    if text is None:
        rep.fail(f'{label}: {rel} not found')
        return
    matches = re.findall(pattern, text)
    if not matches:
        rep.fail(f'{label}: no version statement matched in {rel}')
        return
    bad = [m for m in matches if m != EXPECTED]
    if bad:
        rep.fail(f'{label}: {rel} says {sorted(set(bad))}, expected {EXPECTED!r}')
    else:
        rep.ok(f'{label}: {rel} -> {EXPECTED}')


def check_numeric_installer_version(rep: Report) -> None:
    """Keep Setup.exe's numeric PE version tied to version.py as well.

    Inno Setup needs four numeric fields for VERSIONINFO while the product
    version can include ``-alpha``.  ``windows_file_version()`` owns that
    conversion, so a second literal cannot silently drift.
    """
    text = _read('installer_windows.iss')
    if text is None:
        rep.fail('installer numeric version: installer_windows.iss not found')
        return
    matches = re.findall(
        r'(?m)^VersionInfo(?:Product)?Version=(\d+\.\d+\.\d+\.\d+)\s*$', text)
    expected = '.'.join(str(part) for part in windows_file_version())
    if not matches:
        rep.fail('installer numeric version: no VersionInfoVersion directive')
    elif any(value != expected for value in matches):
        rep.fail(f'installer numeric version: {sorted(set(matches))!r}, expected {expected!r}')
    else:
        rep.ok(f'installer numeric version: {expected}')


def check_no_hardcoded(rep: Report, rel: str, label: str) -> None:
    """Assert a file derives the version instead of spelling it out.

    A literal that happens to equal the current version is still a bug: it is
    the mechanism that let the About dialog drift to 'v1.0.0' last time.
    """
    text = _read(rel)
    if text is None:
        rep.fail(f'{label}: {rel} not found')
        return
    # Any x.y.z, with or without a pre-release suffix.
    hits = re.findall(r'\b\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?\b', text)
    hits = [h for h in hits if not _is_allowed_unrelated_version(rel, h)]
    if hits:
        rep.fail(f'{label}: {rel} hardcodes version literal(s) {sorted(set(hits))} '
                 f'— derive from FTHR_UI/version.py instead')
    else:
        rep.ok(f'{label}: {rel} derives the version')


# Version-looking strings in these files that are not the product version.
_UNRELATED = {
    # sip version named in a comment explaining a crash workaround.
    'FTHR_UI/main.py': {'6.15.1'},
    # libavif soname quoted in a comment explaining why the FFmpeg cleanup
    # regex must not be widened back to a glob.
}


def _is_allowed_unrelated_version(rel: str, literal: str) -> bool:
    return literal in _UNRELATED.get(rel, set())


def main() -> int:
    rep = Report()
    print(f'Product version (FTHR_UI/version.py): {EXPECTED}')
    print()

    print('Files that must state the version verbatim:')
    # Inno Setup cannot import Python, so the literal lives here and is gated.
    check_literal_pattern(
        rep, 'installer_windows.iss',
        r'#define\s+MyAppVersion\s+"([^"]+)"', 'installer')
    check_numeric_installer_version(rep)
    check_literal_pattern(
        rep, 'RELEASE_NOTES.md',
        r'(?m)^#+\s*(?:FTHR Clips\s+)?v?(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)',
        'release notes')

    print()
    print('Files that must DERIVE the version:')
    check_no_hardcoded(rep, 'FTHR_UI/main.py', 'UI')
    check_no_hardcoded(rep, 'FTHR.spec', 'Windows spec')

    print()
    print('Built artefacts (informational — absent in a clean checkout):')
    dist_exe = ROOT / 'dist' / 'FTHRClips' / 'FTHRClips.exe'
    if dist_exe.is_file():
        rep.ok(f'dist bundle present: {dist_exe.relative_to(ROOT)} '
               f'(version resource not read here — check Explorer -> Details)')
    else:
        rep.skip('no dist/ bundle to inspect')


    print()
    if rep.failed:
        print('RESULT: version drift detected.')
        return 1
    print('RESULT: all version statements agree.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
