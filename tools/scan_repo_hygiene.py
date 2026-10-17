#!/usr/bin/env python3
"""Scan for credential patterns, user-state files, and oversized binaries.

Default: git-tracked files. --staged checks the index; --all-files walks the
working tree. Exit 0 means no findings; 1 means findings. Pattern checks
cannot prove that a tree contains no secrets.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Anything above this that is not on the allow-list gets flagged. The tracked
# FFmpeg import libs are the largest legitimate binaries at ~500 KB.
MAX_BINARY_BYTES = 2 * 1024 * 1024

# Paths allowed to exceed MAX_BINARY_BYTES, as prefixes.
LARGE_FILE_ALLOWLIST = (
    'licenses/',                    # opencv LICENSE is 181 KB but text
    'installer_assets/',            # Inno wizard bitmaps
    '.github/social_preview.png',
)

SECRET_PATTERNS: list[tuple[str, str]] = [
    ('private key',      r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY'),
    ('AWS access key',   r'\bAKIA[0-9A-Z]{16}\b'),
    ('GitHub token',     r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    ('Slack token',      r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),
    ('Discord bot token', r'\b[MNO][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b'),
    ('Discord webhook',  r'https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+'),
    ('bearer token',     r'\bBearer\s+[A-Za-z0-9_\-.=]{20,}'),
    ('basic auth header', r'\bBasic\s+[A-Za-z0-9+/=]{20,}'),
    ('hardcoded api key', r'''(?i)\bapi[_-]?key\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]'''),
    ('hardcoded secret', r'''(?i)\b(?:client_secret|app_secret)\s*[:=]\s*['"][^'"]{12,}['"]'''),
    ('hardcoded password', r'''(?i)\bpassword\s*[:=]\s*['"][^'"\s]{8,}['"]'''),
    ('url with credentials', r'\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@'),
]

# Runtime state the app writes into ~/.fthr. None of it belongs in the repo.
USER_STATE_NAMES = {
    'settings.json', 'settings.json.corrupt', 'settings.json.tmp',
    'hotkeys.json', 'upload_history.json', 'fthr.log', 'fthr.log.old',
}

SECRET_FILE_SUFFIXES = {'.pem', '.key', '.pfx', '.p12', '.crt', '.cer', '.jks'}

MEDIA_SUFFIXES = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv'}

# Files that legitimately contain the *words* above without holding a secret.
CONTENT_SCAN_SKIP_PREFIXES = (
    'licenses/',
    'THIRD_PARTY_NOTICES.md',
    'LICENSE',
    'tools/scan_repo_hygiene.py',   # this file, with its own patterns
    'FTHRcapture/FTHRclips/third_party/',
)

TEXT_SUFFIXES = {
    '.py', '.pyi', '.md', '.txt', '.json', '.yml', '.yaml', '.toml', '.cfg',
    '.ini', '.sh', '.bat', '.cmd', '.ps1', '.iss', '.c', '.h', '.cpp', '.hpp',
    '.cmake', '.xml', '.spec', '.desktop', '.in', '.def', '.pc', '',
}


class Findings:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, kind: str, path: str, detail: str) -> None:
        self.items.append(f'  [{kind}] {path}: {detail}')

    def __bool__(self) -> bool:
        return bool(self.items)


def git_files(mode: str) -> list[str] | None:
    cmd = {
        'tracked': ['git', 'ls-files'],
        'staged': ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'],
    }[mode]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [line for line in out.splitlines() if line.strip()]


def walk_files() -> list[str]:
    # Mirrors .gitignore closely enough to be useful before `git init`. The
    # authoritative check is the tracked/staged mode — this is the fallback.
    skip_dirs = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache',
                 'build', 'dist', 'Output', 'redist', '.vs', 'node_modules',
                 'x64', 'plugin-packages'}
    skip_prefixes = ('FTHRcapture/FTHRclips/third_party/ffmpeg/bin/',
                     'FTHRcapture_linux/build/')
    out = []
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if rel.parts[0].startswith('dist_old_gpl_'):
            continue
        posix = rel.as_posix()
        if posix.startswith(skip_prefixes):
            continue
        out.append(posix)
    return out


def scan(paths: list[str]) -> Findings:
    f = Findings()
    compiled = [(name, re.compile(pat)) for name, pat in SECRET_PATTERNS]

    for rel in paths:
        p = ROOT / rel
        if not p.is_file():
            continue
        name = Path(rel).name
        suffix = Path(rel).suffix.lower()

        # filename-based
        if name in USER_STATE_NAMES:
            f.add('user data', rel, 'runtime state belongs in ~/.fthr, not the repo')
        if suffix in SECRET_FILE_SUFFIXES:
            f.add('secret file', rel, f'{suffix} key/certificate material')
        if name == '.env' or name.startswith('.env.'):
            if name != '.env.example':
                f.add('secret file', rel, 'environment file')
        if suffix in MEDIA_SUFFIXES:
            f.add('media', rel, 'recorded clip / video — not source')
        if suffix == '.log':
            f.add('log', rel, 'log file')

        size = p.stat().st_size
        if size > MAX_BINARY_BYTES and not rel.startswith(LARGE_FILE_ALLOWLIST):
            f.add('large file', rel,
                  f'{size / 1024 / 1024:.1f} MB exceeds the '
                  f'{MAX_BINARY_BYTES // 1024 // 1024} MB limit — should this be '
                  f'downloaded at build time or shipped as a release artefact?')

        # content
        if rel.startswith(CONTENT_SCAN_SKIP_PREFIXES):
            continue
        if suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for kind, rx in compiled:
            m = rx.search(text)
            if m:
                line = text[:m.start()].count('\n') + 1
                f.add('secret', rel, f'{kind} at line {line}')

    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--staged', action='store_true',
                   help='scan the staged changeset instead of all tracked files')
    g.add_argument('--all-files', action='store_true',
                   help='walk the working tree; use when there is no git repo yet')
    args = ap.parse_args()

    if args.all_files:
        paths, mode = walk_files(), 'working tree'
    else:
        mode = 'staged' if args.staged else 'tracked'
        paths = git_files(mode)
        if paths is None:
            print('No git repository (or git unavailable) — falling back to a '
                  'working-tree walk.')
            paths, mode = walk_files(), 'working tree'

    print(f'Repo hygiene scan — {len(paths)} files ({mode})')
    print()

    findings = scan(paths)
    if findings:
        print(f'{len(findings.items)} finding(s):')
        for item in findings.items:
            print(item)
        print()
        print('RESULT: FAIL — do not commit or release this tree.')
        return 1

    print('RESULT: clean — no secrets, user data or unexpected large binaries.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
