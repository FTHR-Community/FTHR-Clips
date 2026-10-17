"""Local filename changes that preserve a clip's adjacent audio manifest."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core.audio_manifest import manifest_path_for


def renamed_clip_path(path: str, name: str) -> str:
    name = name.strip()
    if (not name or name in {'.', '..'} or name.endswith(('.', ' '))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.I)):
        raise ValueError('Choose a valid filename without / \\ : * ? " < > | or a reserved Windows name.')
    source = Path(path)
    destination = source.with_name(name + source.suffix)
    if len(destination.name) > 255:
        raise ValueError('The filename is too long. Choose a shorter name.')
    if destination.exists() and not os.path.samefile(source, destination):
        raise FileExistsError('A file with that name already exists. Choose another name.')
    return str(destination)


def rename_clip(source_path: str, destination_path: str) -> None:
    source, destination = Path(source_path), Path(destination_path)
    # Windows rename refuses to replace another existing file. Also check on
    # other platforms, preserving same-file/case-only renames.
    if destination.exists() and not os.path.samefile(source, destination):
        raise FileExistsError('A file with that name already exists.')
    old_manifest = manifest_path_for(source)
    new_manifest = manifest_path_for(destination)
    manifest_text = old_manifest.read_text(encoding='utf-8') if old_manifest.exists() else None
    manifest = json.loads(manifest_text) if manifest_text is not None else None
    if (manifest is not None and new_manifest.exists()
            and not os.path.samefile(old_manifest, new_manifest)):
        raise FileExistsError('Audio metadata with that name already exists.')
    os.rename(source, destination)
    try:
        if manifest is not None:
            manifest['media_file'] = destination.name
            os.rename(old_manifest, new_manifest)
            temporary = new_manifest.with_name(new_manifest.name + '.rename.tmp')
            try:
                temporary.write_text(json.dumps(manifest, ensure_ascii=True), encoding='utf-8')
                os.replace(temporary, new_manifest)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        # A sidecar failure leaves the original name and valid original binding.
        os.rename(destination, source)
        if new_manifest.exists():
            os.rename(new_manifest, old_manifest)
            old_manifest.write_text(manifest_text, encoding='utf-8')
        raise
