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
    'tests', 'tools',
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
}
START_HERE = """# FTHR Clips
## 2.0.0 Alpha · Source edition

**Source snapshot: 16 October 2026**

The complete, editable Windows app source in one compact folder.
Python / PySide6 interface · C++ capture engine · Optional uploader

Extract the entire **FTHRClips-2.0.0-Alpha** folder before opening or building it.

### Find your way around

| Start here | What you will find |
| --- | --- |
| [README.md](README.md) | App overview and features |
| [BUILDING.md](BUILDING.md) | Prerequisites, setup and complete build instructions |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Project layout and development conventions |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Changes in this release |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Current limits and hardware checks |
| [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | Licensing and third-party notices |

### What is in this snapshot

- Hardware-accelerated exports with Very fast, High quality and Lossless options.
- Export and share workflows, optional simultaneous exports and live progress bars.
- The Discord share-window fix and matching app window controls.
- The simplified interface performance descriptions.
- App and uploader source, native engine, original assets, tests and build tools.
- Pinned dependency lists, dependency download manifests, FFmpeg headers and import libraries.

### Build it

You will need Windows, Python 3.14, Visual Studio 2022 with the Desktop C++
workload, and Inno Setup 6 if you want to make an installer.

From the extracted folder, install the pinned dependencies and fetch the
verified build inputs:

```powershell
python -m pip install -r requirements-alpha.txt -r requirements-dev.txt
python tools/fetch_third_party.py --all
```

Follow **BUILDING.md** to compile the native capture engine and playback bridge
before launching the app. Run `launch_windows.bat --diagnose` and confirm it
selects this source checkout, then use `launch_windows.bat` to start it.
The installer build command is `python tools/build_windows_installer.py`;
BUILDING.md also covers optional packages, verification and code signing.

### Compact without cutting corners

Source and assets are preserved byte for byte, without minification or quality
loss. The archive leaves out generated programs and installers, downloadable
runtime binaries, caches, local backups, recordings and personal settings.
The separate marketing website is not part of this app-source package.

**SOURCE_MANIFEST.json** lists the byte size and SHA-256 hash of every other file
in this folder. Every entry is checked after the ZIP is created.
Standard ZIP Deflate compression keeps it compatible with Windows' built-in
extractor; no special archive app is required.
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
