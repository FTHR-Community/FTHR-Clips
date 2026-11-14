"""Conservative ownership rules for destructive library actions."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Iterable


class MediaOwnership(Enum):
    FTHR_OWNED = auto()
    LINKED_IMPORTED = auto()
    EXTERNAL = auto()


@dataclass(frozen=True)
class DeleteResult:
    deleted: bool
    ownership: MediaOwnership
    reason: str = ''


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(str(_resolved(path))))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        # pathlib uses ValueError to report that path is outside root.
        return False


def classify_media_path(
    path: str | os.PathLike[str],
    owned_root: str | os.PathLike[str],
    imported_roots: Iterable[str | os.PathLike[str]],
) -> MediaOwnership:
    resolved = _resolved(path)
    owned = _resolved(owned_root)
    inside_owned = _within(resolved, owned)
    for imported_root in imported_roots:
        imported = _resolved(imported_root)
        # Linking a broad folder (e.g. Videos) must not turn the app's
        # nested capture directory into imported media. An explicit import
        # of the owned root itself or a subfolder still protects its files.
        if inside_owned and imported != owned and _within(owned, imported):
            continue
        if _within(resolved, imported):
            return MediaOwnership.LINKED_IMPORTED
    if inside_owned:
        return MediaOwnership.FTHR_OWNED
    return MediaOwnership.EXTERNAL


def media_file_identity(path):
    absolute = Path(os.path.abspath(os.fspath(path)))
    for parent in (absolute.parent, *absolute.parent.parents):
        parent_info = os.lstat(parent)
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or getattr(parent_info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise OSError('Linked or reparse-point parent folders block deletion')
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise OSError('Linked or non-regular files cannot be deleted through the library')
    return {'st_dev': info.st_dev, 'st_ino': info.st_ino,
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def delete_media(path, *, metadata_manager, expected_identity=None):
    """Delete only under the metadata protection guard; ownership is separate."""
    guard = getattr(metadata_manager, 'delete_guard', None)
    if not callable(guard):
        raise OSError('Library protection is unavailable; the file was kept')
    with guard(path, expected_identity=expected_identity):
        Path(path).unlink()


def generic_delete_owned_media(
    path: str | os.PathLike[str],
    owned_root: str | os.PathLike[str],
    imported_roots: Iterable[str | os.PathLike[str]],
    *, metadata_manager=None, expected_identity=None,
) -> DeleteResult:
    ownership = classify_media_path(path, owned_root, imported_roots)
    if ownership is not MediaOwnership.FTHR_OWNED:
        return DeleteResult(False, ownership, 'Linked or external media is protected')
    try:
        delete_media(path, metadata_manager=metadata_manager, expected_identity=expected_identity)
    except FileNotFoundError:
        return DeleteResult(False, ownership, 'The clip no longer exists')
    except (OSError, ValueError, TypeError) as exc:
        return DeleteResult(False, ownership, str(exc))
    return DeleteResult(True, ownership)


def add_import_root(
    roots: Iterable[str | os.PathLike[str]], path: str | os.PathLike[str]
) -> list[str]:
    result = [str(_resolved(root)) for root in roots]
    keys = {_path_key(root) for root in result}
    resolved = str(_resolved(path))
    if _path_key(resolved) not in keys:
        result.append(resolved)
    return result


def remove_import_root(
    roots: Iterable[str | os.PathLike[str]], path: str | os.PathLike[str]
) -> list[str]:
    remove_key = _path_key(path)
    return [str(_resolved(root)) for root in roots if _path_key(root) != remove_key]
