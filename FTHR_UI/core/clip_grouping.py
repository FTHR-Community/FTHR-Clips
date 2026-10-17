"""Stable, safe folder names for clips captured from application windows.

The window title is useful UI text, but it is not a durable application
identity: games commonly change it for versions, multiplayer state, or maps.
This module keeps the grouping decision pure so manual selection, automatic
game detection, and background saves use the same policy without touching the
capture engine or shared-memory contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping


_INVALID_FOLDER_CHARS = r'\\/:*?"<>|'
_RESERVED_FOLDER_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}

# These hosts execute many unrelated applications.  Their executable stem is
# therefore not a sufficient game identity.
_GENERIC_HOST_STEMS = {
    'java', 'javaw', 'python', 'pythonw', 'python3', 'python3w',
    'node', 'nodejs', 'dotnet',
}

# A browser executable always wins over a title that merely contains the word
# "Minecraft" (for example, "Minecraft - YouTube - Firefox").
_BROWSER_STEMS = {
    'brave', 'chrome', 'chromium', 'firefox', 'msedge', 'opera',
    'vivaldi', 'waterfox',
}

# Keep title-only matching conservative.  This accepts the observed Java
# launcher titles and common Minecraft mode suffixes, but not arbitrary text
# after the word Minecraft.
_MINECRAFT_TITLE = re.compile(
    r'^\s*minecraft(?:\s+\d+(?:\.\d+){1,3})?'
    r'(?:\s*-\s*(?:multiplayer|singleplayer|realms|lan|demo)'
    r'(?:\s*\([^)]*)?)?\s*$',
    re.IGNORECASE,
)


def sanitize_clip_folder_name(name: str) -> str:
    """Apply the legacy Windows-safe folder-name policy exactly once."""

    invalid = _INVALID_FOLDER_CHARS
    cleaned = ''.join(c for c in str(name)
                      if c not in invalid and ord(c) >= 32).strip('. ')
    cleaned = cleaned[:32].rstrip('. ')
    if cleaned.upper() in _RESERVED_FOLDER_NAMES:
        cleaned = f'{cleaned}_game'
    return cleaned or 'Unknown'


def _text(source: Mapping[str, object], key: str) -> str:
    value = source.get(key, '')
    return value.strip() if isinstance(value, str) else ''


def _executable_stem(source: Mapping[str, object]) -> str:
    exe_name = _text(source, 'exe_name')
    exe_path = _text(source, 'exe_path')
    raw = exe_name or (
        exe_path.replace('\\', '/').rsplit('/', 1)[-1]
        if exe_path else ''
    )
    if not raw:
        return ''
    raw = raw.replace('\\', '/').rsplit('/', 1)[-1]
    return Path(raw).stem.casefold()


def _title_is_minecraft(title: str) -> bool:
    return bool(title and _MINECRAFT_TITLE.fullmatch(title))


def clip_group_for_source(source: Mapping[str, object] | None) -> str:
    """Return the future-save clip folder for a window source record.

    Normal executables use their stable executable stem.  Generic runtime
    hosts (Java/Python/etc.) use a descriptive title so unrelated applications
    are not merged.  Credible Minecraft launcher titles are the one explicit
    generic-host family that is canonicalised to ``Minecraft``.
    """

    if not isinstance(source, Mapping):
        return 'Unknown'

    title = _text(source, 'title')
    display_name = _text(source, 'display_name')
    stem = _executable_stem(source)
    is_browser = stem in _BROWSER_STEMS
    if not is_browser and _title_is_minecraft(title):
        # A title-only source is accepted because the pattern is deliberately
        # strict; an executable-backed source additionally requires a known
        # Java/Minecraft launcher rather than an arbitrary process.
        if (not stem or stem in _GENERIC_HOST_STEMS
                or stem in {'minecraft', 'minecraftlauncher'}):
            return 'Minecraft'

    if stem and stem not in _GENERIC_HOST_STEMS:
        return sanitize_clip_folder_name(Path(stem).name)

    # Generic hosts and metadata-unavailable sources retain descriptive title
    # text.  ``display_name`` is the only value available to legacy settings.
    return sanitize_clip_folder_name(title or display_name or 'Unknown')


__all__ = ['clip_group_for_source', 'sanitize_clip_folder_name']
