"""Create a compact, lossless source ZIP with a verifiable file manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {
    '.github', 'docs', 'FTHR_UI', 'FTHR_Uploader', 'FTHRcapture',
    'FTHRcapture_common', 'installer_assets', 'licenses',
    'tests', 'tools', 'AppDir', 'FTHRcapture_linux',
    'FTHR_Hardware_ID', 'public_html',
}
SKIP_DIRS = {
    '.git', '.vs', '__pycache__', '.pytest_cache', '.ruff_cache',
    '.mypy_cache', '.venv', 'venv', 'node_modules', 'x64', 'x86',
    'build', 'dist', 'Output', 'Debug', 'Release', 'CFRRelease',
    'CFRReleaseFinal', 'ipch', '.fthr',
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
    'START_HERE.md', 'SOURCE_MANIFEST.json',
}
START_HERE = """# FTHR Clips 2.0.0 Alpha — source snapshot (17 October 2026)

This snapshot includes the short replay save fix: save available healthy footage
from 0.5 seconds onward during startup/recovery, even for longer clip requests.

Extract the entire folder before building. Read BUILDING.md for prerequisites,
pinned Python dependencies, native Windows/Linux builds, optional uploader,
verified dependency downloads, packaging and verification commands.

The archive preserves app and helper source, both native engines, shared code,
website, original assets, tests, licenses, build scripts, dependency manifests,
and Windows FFmpeg headers/import libraries. Source files are losslessly stored.
Generated builds, installers, downloadable runtimes, caches and personal data
are omitted. Fetch the verified dependencies using tools/fetch_third_party.py
as described in BUILDING.md; this is not an offline binary bundle.

SOURCE_MANIFEST.json contains the size and SHA-256 of every other archived file.
The ZIP uses maximum standard Deflate compression for Windows compatibility.
After building, tools/update_latest_build_shortcut.ps1 creates a shortcut to
the latest local packaged app.
"""


def source_paths():
    for parent, dirs, files in os.walk(ROOT, followlinks=False):
        base = Path(parent)
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS
            and not (base / name).is_symlink()
            and (base != ROOT or name in SOURCE_DIRS))
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(('AppDir/usr/', 'FTHRcapture_linux/third_party/')) or rel == 'AppDir/AppRun':
                continue
            if (path.is_symlink() or name in SKIP_NAMES
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
    prefix = 'FTHRClips-2.0.0-Alpha/'
    manifest = []
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        contents = [('START_HERE.md', START_HERE.encode('utf-8'))]
        contents.extend((path.relative_to(ROOT).as_posix(), path.read_bytes())
                        for path in sorted(source_paths()))
        for name, data in contents:
            archive.writestr(prefix + name, data)
            manifest.append({'path': name, 'bytes': len(data),
                             'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr(prefix + 'SOURCE_MANIFEST.json', json.dumps(
            {'format': 1, 'version': '2.0.0-alpha',
             'snapshot_date': '2026-10-17',
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
