"""Create a compact, lossless source ZIP with a verifiable file manifest."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {
    '.github', 'docs', 'FTHR_UI', 'FTHR_Uploader', 'FTHRcapture',
    'FTHRcapture_common', 'installer_assets', 'licenses',
    'tests', 'tools',
    'FTHR_Hardware_ID',
}

SKIP_DIRS = {
    '.git', '.vs', '__pycache__', '.pytest_cache', '.ruff_cache',
    '.mypy_cache', '.venv', 'venv', 'node_modules', 'x64', 'x86',
    'build', 'dist', 'Output', 'Debug', 'Release', 'CFRRelease',
    'CFRReleaseFinal', 'ipch', '.fthr',
    '.agent-team', '.agents', '.codex', '.claude',
}
SKIP_SUFFIXES = {
    '.pyc', '.pyo', '.pyd', '.exe', '.dll', '.obj', '.o', '.iobj',
    '.ipdb', '.pdb', '.idb', '.ilk', '.exp', '.tlog', '.lastbuildstate',
    '.log', '.dmp', '.tmp', '.partial', '.bak', '.old', '.zip',
    '.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv',
    '.pem', '.key', '.pfx', '.p12', '.crt', '.cer',
}
SKIP_NAMES = {
    'HANDOFF.md', 'HANDOFF-MANIFEST.json', 'Desktop.ini', 'desktop.ini',
    'Thumbs.db', '.DS_Store', 'settings.json', 'hotkeys.json',
    'credentials.json', 'upload_history.json',
    'START_HERE.md', 'SOURCE_MANIFEST.json', '-progress',
}
LOCAL_ONLY_NAMES = {
    'agents.md', 'claude.md', 'gemini.md', 'handoff.md', 'handoff-manifest.json',
}
LOCAL_ONLY_DIRS = {'.agent-team', '.agents', '.codex', '.claude'}
# Local review/planning records stay in the workspace, not in source exports.
# Ordinary project documentation and upstream attribution remain included.
LOCAL_ONLY_PATHS = {
    'docs/CODEX-CONTINUATION.md',
    'docs/amd-completion-review-2026-10-18.md',
    'docs/amd-completion-review-2026-10-18.evidence.json',
    'docs/amd-completion-review-2026-10-18.json',
    'docs/amd-nv12-comparison-2026-10-18.md',
    'docs/amd-nv12-comparison-2026-10-18.evidence.json',
    'docs/amd-nv12-comparison-2026-10-18.json',
    'docs/amd-project-assessment-2026-10-18.md',
    'docs/amd-project-assessment-2026-10-18.evidence.json',
    'docs/amd-project-assessment-2026-10-18.json',
    'docs/amd-source-safety-and-gpu-probe-2026-10-18.md',
    'docs/amd-source-safety-and-gpu-probe-2026-10-18.evidence.json',
    'docs/amd-source-safety-and-gpu-probe-2026-10-18.json',
    'docs/amd-test-tooling-2026-10-18.md',
    'docs/amd-test-tooling-2026-10-18.evidence.json',
    'docs/amd-test-tooling-2026-10-18.json',
    'docs/windows-audio-sync-fix-2026-10-19.md',
    'docs/windows-audio-sync-fix-2026-10-19.evidence.json',
    'docs/windows-audio-sync-fix-2026-10-19.json',
    'docs/vxnsin-pr-integration-2026-10-19.md',
    'docs/vxnsin-pr-integration-2026-10-19.evidence.json',
    'docs/vxnsin-pr-integration-2026-10-19.json',
    'docs/editor-upload-fix-2026-10-19.md',
    'docs/editor-upload-fix-2026-10-19.evidence.json',
    'docs/editor-upload-fix-2026-10-19.json',
    'docs/p0-baseline-review-2026-10-19.md',
    'docs/p0-baseline-review-2026-10-19.evidence.json',
    'docs/p0-baseline-review-2026-10-19.json',
    'docs/p0-audio-selection-fix-2026-10-19.md',
    'docs/FEATURE-CANDIDATES-50-2026-10-19.md',
    'docs/FEATURE-IMPLEMENTATION-PLAN-2026-10-19.md',
    'docs/FEATURE-WISHLIST.md',
    'docs/UPDATE-BACKLOG-2026-10-19.md',
}


def source_paths():
    for parent, dirs, files in os.walk(ROOT, followlinks=False):
        base = Path(parent)
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS
            and name.casefold() not in LOCAL_ONLY_DIRS
            and not (base / name).is_symlink()
            and (base != ROOT or name in SOURCE_DIRS))
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(ROOT).as_posix()
            shared_ui_helpers = {
                'FTHR_UI/core/linux_runtime.py',
                'FTHR_UI/core/linux_tools.py',
                'FTHR_UI/ui/frameless_window_linux.py',
            }
            if (rel not in shared_ui_helpers
                    and any(term in rel.lower() for term in ('linux', 'appimage', 'wayland'))):
                continue
            if (name in {'build_linux.sh', 'FTHR_linux.spec'}
                    or (rel.startswith('tools/build_') and name.endswith('_plugin.py'))
                    or (rel.startswith('docs/') and 'plugin' in name.lower())):
                continue
            if (path.is_symlink() or name in SKIP_NAMES
                    or name.casefold() in LOCAL_ONLY_NAMES
                    or rel in LOCAL_ONLY_PATHS
                    or path.suffix.lower() in SKIP_SUFFIXES
                    or name.endswith(('.vcxproj.user', '~'))
                    or (name.startswith('.env') and name != '.env.example')):
                continue
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = 'FTHRClips-2.1.4-Windows/'
    manifest = []
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        contents = [('START_HERE.md', (ROOT / 'START_HERE.md').read_bytes())]
        contents.extend((path.relative_to(ROOT).as_posix(), path.read_bytes())
                        for path in sorted(source_paths()))
        for name, data in contents:
            archive.writestr(prefix + name, data)
            manifest.append({'path': name, 'bytes': len(data),
                             'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr(prefix + 'SOURCE_MANIFEST.json', json.dumps(
            {'format': 1, 'version': '2.1.4',
             'snapshot_date': date.today().isoformat(),
             'compression': 'ZIP Deflate level 9', 'files': manifest},
            indent=2).encode('utf-8'))
    # Verify every compressed member and its exact extracted content.
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f'ZIP integrity check failed: {bad}')
        for entry in manifest:
            data = archive.read(prefix + entry['path'])
            if (len(data) != entry['bytes']
                    or hashlib.sha256(data).hexdigest() != entry['sha256']):
                raise RuntimeError(f'Content verification failed: {entry["path"]}')
    original = sum(entry['bytes'] for entry in manifest)
    print(f'{destination}\n{len(manifest)} files; {original:,} source bytes; '
          f'{destination.stat().st_size:,} ZIP bytes; all hashes verified')


if __name__ == '__main__':
    main()
