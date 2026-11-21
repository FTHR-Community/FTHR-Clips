"""Cancellable media discovery for the library index.

Keep traversal and metadata reads off the Qt thread; records contain no
Qt objects and can be scanned independently of the UI.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from core.clip_files import (
    VIDEO_SUFFIXES,
    is_fthr_temporary_dir,
    is_pending_restore_directory,
    is_library_media_path,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
DEFAULT_BATCH_SIZE = 128
DEFAULT_MAX_WATCHED_DIRECTORIES = 256

_CAPTURE_NAME = re.compile(
    r'(?P<group>.+)_clip_from_\d{2}[A-Za-z]{3}\d{4}_\d{2}-\d{2}-\d{2}\.mp4', re.I)
_SCREENSHOT_NAME = re.compile(r'screenshot_from_\d{8}_\d{6}_\d{6}(?:_\d+)?\.png', re.I)


def recorded_media_details(path, file_identity, recorded_media=None, *, inspect_native=False):
    """Return display-only capture evidence; never use this to authorize deletion.

    Older captures predate the registry. Their generated names and embedded
    capture UUID boxes still identify them after moving a library folder.
    """
    # Ordinary scans have no registry mapping; their caller already computed
    # the canonical identity, so avoid a second realpath normalization.
    known = (recorded_media.get(canonical_media_path(path))
             if recorded_media else None)
    if isinstance(known, Mapping) and known.get('identity') == file_identity:
        group = known.get('group')
        return True, group if isinstance(group, str) and group else Path(path).parent.name
    match = _CAPTURE_NAME.fullmatch(Path(path).name)
    if match:
        group = match.group('group')
        return True, 'Desktop' if group.casefold() == 'desktop' else group
    if _SCREENSHOT_NAME.fullmatch(Path(path).name):
        return True, None
    if inspect_native and Path(path).suffix.casefold() == '.mp4':
        from core.capture_highlights import CaptureHighlightError, read_capture_highlights
        try:
            if read_capture_highlights(path, expected_identity=file_identity) is not None:
                return True, Path(path).parent.name
        except (CaptureHighlightError, OSError, ValueError):
            pass
    return False, None


def canonical_media_path(path: str | os.PathLike[str]) -> str:
    """Return a stable, case-insensitive identity for a filesystem path."""

    return os.path.normcase(os.path.normpath(os.path.realpath(os.fspath(path))))


def normalized_root(path: str | os.PathLike[str]) -> str:
    """Normalize a configured root without requiring it to exist."""

    return os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _is_reparse_or_link(entry: os.DirEntry[str]) -> bool:
    """Do not follow links/reparse points while traversing user-selected roots."""

    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
    # A vanished or inaccessible entry is non-fatal; the aggregate scan
    # continues and records the failure count in ``ScanStats``.
    except OSError:
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _root_is_imported(identity: str, imported_roots: tuple[str, ...]) -> bool:
    """Compare a path already canonicalized by the current scan."""
    return any(identity == root or identity.startswith(root + os.sep)
               for root in imported_roots)


@dataclass(frozen=True)
class LibraryRecord:
    """Lightweight metadata used by the view; it does not hold media data."""

    path: str
    identity: str
    root_ids: tuple[str, ...]
    imported: bool
    kind: str
    size: int
    mtime_ns: int
    st_dev: int | None = None
    st_ino: int | None = None
    recorded: bool = False
    recorded_group: str | None = None

    @property
    def file_identity(self) -> dict[str, int] | None:
        values = (self.st_dev, self.st_ino, self.size, self.mtime_ns)
        if not all(type(value) is int for value in values):
            return None
        return dict(zip(('st_dev', 'st_ino', 'size', 'mtime_ns'), values, strict=True))

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1_000_000_000

    @property
    def fingerprint(self) -> str:
        return f"{self.size}:{self.mtime_ns}"


@dataclass
class ScanStats:
    roots: int = 0
    directories_visited: int = 0
    entries_seen: int = 0
    candidate_files: int = 0
    accepted_files: int = 0
    duplicate_files: int = 0
    skipped_reparse_points: int = 0
    inaccessible_entries: int = 0
    vanished_entries: int = 0
    watched_directories: int = 0
    cancelled: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "root_count": self.roots,
            "directories_visited": self.directories_visited,
            "entries_seen": self.entries_seen,
            "candidate_file_count": self.candidate_files,
            "accepted_file_count": self.accepted_files,
            "duplicate_file_count": self.duplicate_files,
            "skipped_reparse_point_count": self.skipped_reparse_points,
            "inaccessible_entry_count": self.inaccessible_entries,
            "vanished_entry_count": self.vanished_entries,
            "watched_directory_count": self.watched_directories,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class LibraryScanResult:
    all_records: tuple[LibraryRecord, ...]
    records: tuple[LibraryRecord, ...]
    imported_paths: frozenset[str]
    watched_directories: tuple[str, ...]
    stats: ScanStats


def scan_library(
    primary_root: str | os.PathLike[str],
    import_roots: Iterable[str | os.PathLike[str]] = (),
    *,
    filter_: str = "all",
    sort_: str = "newest",
    cancel_event: Event | None = None,
    on_batch: Callable[[tuple[LibraryRecord, ...]], None] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_watched_directories: int = DEFAULT_MAX_WATCHED_DIRECTORIES,
    recorded_media: Mapping | None = None,
    active_output_paths: Iterable[str | os.PathLike[str]] = (),
) -> LibraryScanResult:
    """Discover media without following symlinks or Windows reparse points.

    Merge file aliases while preserving their ``root_ids``. ``on_batch`` gets
    bounded snapshots until cancellation; the final records are globally sorted.
    Callbacks run on the worker and must be lightweight and avoid Qt mutations.
    """

    # Ownership is supplied by the live recorder, not inferred from names or
    # missing metadata. After a crash, orphan files remain discoverable.
    active_outputs = frozenset(canonical_media_path(path) for path in active_output_paths)
    primary = normalized_root(primary_root)
    raw_roots: list[tuple[str, bool]] = [(primary, False)]
    seen_roots = {canonical_media_path(primary)}
    for raw in import_roots:
        root = normalized_root(raw)
        root_id = canonical_media_path(root)
        if root_id in seen_roots:
            continue
        seen_roots.add(root_id)
        raw_roots.append((root, True))

    # Compare canonical identities, not display paths, so case and aliasing
    # cannot change whether a file is classified as imported.
    imported_roots = tuple(
        canonical_media_path(root) for root, imported in raw_roots if imported)
    stats = ScanStats(roots=len(raw_roots))
    watched: list[str] = []
    watched_ids: set[str] = set()
    records: dict[str, LibraryRecord] = {}
    # The root identity is part of the visited key.  A configured imported
    # child may overlap the primary root and still needs to contribute root
    # coverage to each shared media record.
    visited_dirs: set[tuple[str, tuple[int, int] | str]] = set()
    cancelled = lambda: bool(cancel_event and cancel_event.is_set())
    pending_batch: list[LibraryRecord] = []
    requested_batch_size = max(1, int(batch_size))

    def selected_for_filter(record: LibraryRecord) -> bool:
        if filter_ == "clips":
            return record.kind == "video"
        if filter_ == "screenshots":
            return record.kind == "image"
        if filter_ == "imported":
            return record.imported
        return True

    def publish_pending() -> None:
        if not pending_batch or on_batch is None or cancelled():
            return
        snapshot = tuple(pending_batch)
        pending_batch.clear()
        on_batch(snapshot)

    def add_watch(path: str) -> None:
        if len(watched) >= max(0, int(max_watched_directories)):
            return
        identity = canonical_media_path(path)
        if identity in watched_ids:
            return
        watched_ids.add(identity)
        watched.append(path)

    def visit(root: str, root_id: str, imported: bool) -> None:
        stack = [root]
        while stack and not cancelled():
            current = stack.pop()
            if is_pending_restore_directory(current):
                continue
            current_id = canonical_media_path(current)
            try:
                info = os.stat(current, follow_symlinks=False)
                file_key = (int(getattr(info, "st_dev", 0)),
                            int(getattr(info, "st_ino", 0)))
                if file_key == (0, 0):
                    file_key = current_id
            except OSError:
                stats.inaccessible_entries += 1
                continue
            visit_key = (root_id, file_key)
            if visit_key in visited_dirs:
                continue
            visited_dirs.add(visit_key)
            stats.directories_visited += 1
            # Windows directory notifications hold handles that can prevent a rename.
            # External folders use the library's periodic refresh on that platform.
            if os.name != 'nt' or not (imported or _root_is_imported(current_id, imported_roots)):
                add_watch(current)
            try:
                entries = os.scandir(current)
            except OSError:
                stats.inaccessible_entries += 1
                continue
            try:
                for entry in entries:
                    if cancelled():
                        break
                    stats.entries_seen += 1
                    name = entry.name
                    if _is_reparse_or_link(entry):
                        stats.skipped_reparse_points += 1
                        continue
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        # A directory entry may vanish between scandir and its
                        # type query; keep the rest of the subtree usable.
                        stats.vanished_entries += 1
                        continue
                    if is_directory:
                        if name in {"Exported", "Shared"} or is_fthr_temporary_dir(name):
                            continue
                        stack.append(entry.path)
                        continue
                    # Filter extensions before stat calls to skip unrelated files cheaply.
                    if not is_library_media_path(name):
                        continue
                    stats.candidate_files += 1
                    try:
                        file_info = entry.stat(follow_symlinks=False)
                        # Windows DirEntry caches omit volume/file IDs. Only
                        # accepted media candidates need the full stat evidence.
                        if os.name == 'nt' and (not file_info.st_dev or not file_info.st_ino):
                            file_info = os.stat(entry.path, follow_symlinks=False)
                        if not stat.S_ISREG(file_info.st_mode):
                            continue
                    except OSError:
                        stats.vanished_entries += 1
                        continue
                    display_path = os.path.abspath(os.path.normpath(entry.path))
                    identity = canonical_media_path(display_path)
                    if identity in active_outputs:
                        continue
                    suffix = Path(name).suffix.casefold()
                    kind = "video" if suffix in VIDEO_SUFFIXES else "image"
                    prior = records.get(identity)
                    imported_here = imported or _root_is_imported(
                        identity, imported_roots)
                    file_identity = {
                        'st_dev': int(file_info.st_dev), 'st_ino': int(file_info.st_ino),
                        'size': int(file_info.st_size), 'mtime_ns': int(file_info.st_mtime_ns)}
                    recorded, recorded_group = recorded_media_details(
                        display_path, file_identity, recorded_media,
                        inspect_native=imported_here)
                    imported_here = imported_here and not recorded
                    root_ids = (root_id,)
                    if prior is not None:
                        stats.duplicate_files += 1
                        root_ids = tuple(dict.fromkeys(prior.root_ids + (root_id,)))
                        imported_here = prior.imported or imported_here
                        # Keep the newest observed stat if aliases race with a
                        # rename, but never create a second visible record.
                        if prior.size == int(file_info.st_size) and prior.mtime_ns == int(file_info.st_mtime_ns):
                            records[identity] = LibraryRecord(
                                prior.path, identity, root_ids, imported_here,
                                prior.kind, prior.size, prior.mtime_ns,
                                int(file_info.st_dev), int(file_info.st_ino),
                                recorded, recorded_group)
                            continue
                    record = LibraryRecord(
                        display_path,
                        identity,
                        root_ids,
                        imported_here,
                        kind,
                        int(file_info.st_size),
                        int(file_info.st_mtime_ns),
                        int(file_info.st_dev),
                        int(file_info.st_ino),
                        recorded,
                        recorded_group,
                    )
                    records[identity] = record
                    if selected_for_filter(record) and on_batch is not None:
                        pending_batch.append(record)
                        if len(pending_batch) >= requested_batch_size:
                            publish_pending()
            except OSError:
                # Some filesystems surface permission/disconnect errors while
                # advancing the iterator rather than when opening it.
                stats.inaccessible_entries += 1
            finally:
                entries.close()

    for root, imported in raw_roots:
        if cancelled():
            break
        if not os.path.isdir(root):
            stats.inaccessible_entries += 1
            continue
        visit(root, canonical_media_path(root), imported)

    # Publish a short tail as soon as traversal ends.  It is intentionally
    # done before sorting so a huge library never waits for a complete list
    # before its first visible records can reach the caller.
    if not cancelled():
        publish_pending()
    stats.cancelled = cancelled()
    stats.watched_directories = len(watched)
    selected = list(records.values())
    if filter_ == "clips":
        selected = [record for record in selected if record.kind == "video"]
    elif filter_ == "screenshots":
        selected = [record for record in selected if record.kind == "image"]
    elif filter_ == "imported":
        selected = [record for record in selected if record.imported]
    if sort_ == "oldest":
        selected.sort(key=lambda record: record.mtime_ns)
    elif sort_ == "longest":
        selected.sort(key=lambda record: record.size, reverse=True)
    else:
        selected.sort(key=lambda record: record.mtime_ns, reverse=True)

    stats.accepted_files = len(selected)
    result = LibraryScanResult(
        all_records=tuple(records.values()),
        records=tuple(selected),
        imported_paths=frozenset(record.path for record in selected if record.imported),
        watched_directories=tuple(watched),
        stats=stats,
    )
    return result
