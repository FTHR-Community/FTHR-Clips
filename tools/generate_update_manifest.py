"""Generate a website update manifest from the exact installer being published."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))
from core.app_updates import UpdateError, _parse_manifest  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--url', required=True, help='Public immutable HTTPS .exe URL')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--notes', default='')
    args = parser.parse_args(argv)
    try:
        if args.installer.suffix.lower() != '.exe':
            raise UpdateError('The installer must be an .exe file.')
        digest = hashlib.sha256()
        size = 0
        with args.installer.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        manifest = {
            'schema_version': 1, 'platform': 'windows-x64', 'version': args.version,
            'url': args.url, 'sha256': digest.hexdigest(), 'size': size, 'notes': args.notes,
        }
        _parse_manifest(manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    except (OSError, UpdateError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
