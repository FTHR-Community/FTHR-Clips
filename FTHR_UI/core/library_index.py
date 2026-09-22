"""Cancellable media discovery for the library index.

Keep traversal and metadata reads off the Qt thread; records contain no
Qt objects and can be scanned independently of the UI.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from core.clip_files import (
    VIDEO_SUFFIXES,
    is_fthr_temporary_dir,
    is_library_media_path,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
DEFAULT_BATCH_SIZE = 128
DEFAULT_MAX_WATCHED_DIRECTORIES = 256


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


def _root_is_imported(path: str, imported_roots: tuple[str, ...]) -> bool:
    identity = canonical_media_path(path)
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

    @property
    def skipped_reparse_point_count(self) -> int:
        return self.skipped_reparse_points

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
) -> LibraryScanResult:
    """Discover media without following symlinks or Windows reparse points.

    Merge file aliases while preserving their ``root_ids``. ``on_batch`` gets
    bounded snapshots until cancellation; the final records are globally sorted.
    Callbacks run on the worker and must be lightweight and avoid Qt mutations.
    """

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
                        if not stat.S_ISREG(file_info.st_mode):
                            continue
                    except OSError:
                        stats.vanished_entries += 1
                        continue
                    display_path = os.path.abspath(os.path.normpath(entry.path))
                    identity = canonical_media_path(display_path)
                    suffix = Path(name).suffix.casefold()
                    kind = "video" if suffix in VIDEO_SUFFIXES else "image"
                    prior = records.get(identity)
                    imported_here = imported or _root_is_imported(
                        display_path, imported_roots)
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
                                prior.kind, prior.size, prior.mtime_ns)
                            continue
                    record = LibraryRecord(
                        display_path,
                        identity,
                        root_ids,
                        imported_here,
                        kind,
                        int(file_info.st_size),
                        int(file_info.st_mtime_ns),
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
