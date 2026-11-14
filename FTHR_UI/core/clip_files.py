"""Clip filename contract and conservative stale-partial recovery."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
import os
import stat
import threading
from typing import Callable


VIDEO_SUFFIXES = ('.mp4', '.mkv', '.avi')
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg')
PARTIAL_SUFFIX = '.partial'
_AUDIO_MANIFEST_SUFFIX = '.fthr-audio.json'
_FTHR_CLIP_MARKER = '_clip_from_'
FTHR_TEMP_DIR_PREFIX = '.fthr-'
PENDING_RESTORE_MARKER = '.fthr-restore-pending.json'
DEFAULT_PARTIAL_MAX_AGE_SECONDS = 24 * 60 * 60
_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x0400)


@dataclass(frozen=True)
class PartialCleanupResult:
    """Files removed and secondary failures encountered during recovery."""

    removed: tuple[Path, ...]
    failures: tuple[tuple[Path, str], ...]


def iter_safe_tree(root: str | Path, cancel_event: threading.Event | None = None):
    """Walk below ``root`` without following links or Windows reparse points.

    Bounded-depth scandir traversal avoids junction loops and collecting an
    entire drive into memory during recovery or maintenance.
    """

    stack = [Path(root)]
    visited: set[tuple[int, int] | str] = set()
    while stack and not (cancel_event and cancel_event.is_set()):
        current = stack.pop()
        try:
            info = os.stat(current, follow_symlinks=False)
            key = (int(getattr(info, 'st_dev', 0)), int(getattr(info, 'st_ino', 0)))
            if key == (0, 0):
                key = os.path.normcase(os.path.normpath(str(current)))
        # A disappearing recovery root is simply skipped; callers only remove
        # paths that can be observed safely.
        except OSError:
            continue
        if key in visited:
            continue
        visited.add(key)
        try:
            entries = os.scandir(current)
        # Unreadable directories are non-fatal during stale-file recovery.
        except OSError:
            continue
        try:
            for entry in entries:
                if cancel_event and cancel_event.is_set():
                    break
                child = Path(entry.path)
                try:
                    if entry.is_symlink():
                        continue
                    child_info = entry.stat(follow_symlinks=False)
                    if getattr(child_info, 'st_file_attributes', 0) & _REPARSE_POINT:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(child)
                    else:
                        yield child
                # Individual files can disappear while the maintenance walk runs.
                except OSError:
                    continue
        finally:
            entries.close()


def is_partial_clip_path(path: str | Path) -> bool:
    """Return whether ``path`` is an untrusted/in-progress media path.

    Export staging names use ``.partial.<token>`` before the final extension,
    while the capture engine uses the older ``.mp4.partial`` form.  Neither
    should be visible to the library or be handed to a media consumer.
    """

    name = Path(path).name.casefold()
    return name.endswith(PARTIAL_SUFFIX) or '.partial.' in name


def is_fthr_temporary_dir(path: str | Path) -> bool:
    """Return whether ``path`` is an app-owned post-processing directory."""

    return Path(path).name.casefold().startswith(FTHR_TEMP_DIR_PREFIX)


def is_pending_restore_directory(path: str | Path) -> bool:
    """Return whether a directory is still protected by a restore marker.

    Marker inspection is deliberately no-follow and fail-closed.  A marker
    that appears while a scan is running, or that cannot be inspected, keeps
    the directory hidden until the restore owner has completed cleanup.
    """

    marker = Path(path) / PENDING_RESTORE_MARKER
    try:
        os.lstat(marker)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # An unreadable marker keeps a restore directory hidden fail-closed.
        return True
    return True


def is_completed_video_path(path: str | Path) -> bool:
    """Return whether ``path`` names a supported, completed video clip."""

    candidate = Path(path)
    return (
        not is_partial_clip_path(candidate)
        and candidate.suffix.casefold() in VIDEO_SUFFIXES
    )


def is_library_media_path(path: str | Path) -> bool:
    """Return whether a file may be discovered by the clip library."""

    candidate = Path(path)
    if is_partial_clip_path(candidate):
        return False
    suffix = candidate.suffix.casefold()
    return suffix in VIDEO_SUFFIXES or suffix in IMAGE_SUFFIXES


def is_fthr_owned_partial_path(path: str | Path) -> bool:
    """Return whether ``path`` matches the engine's FTHR clip temp naming."""

    name = Path(path).name.casefold()
    return name.endswith('.mp4.partial') and _FTHR_CLIP_MARKER in name


def is_fthr_owned_orphan_audio_manifest_path(path: str | Path) -> bool:
    """Return whether a sidecar can be safely recovered when its MP4 is absent.

    A paired native save publishes the manifest immediately before the MP4. A
    hard process kill in that narrow window leaves this harmless orphan. The
    marker requirement keeps third-party sidecars outside FTHR's ownership.
    """

    name = Path(path).name.casefold()
    return name.endswith('.mp4' + _AUDIO_MANIFEST_SUFFIX) and _FTHR_CLIP_MARKER in name


def cleanup_stale_partial_clips(
    clips_root: str | Path,
    *,
    minimum_age_seconds: float = DEFAULT_PARTIAL_MAX_AGE_SECONDS,
    now: float | None = None,
    cancel_event: threading.Event | None = None,
    metadata_manager=None,
) -> PartialCleanupResult:
    """Remove FTHR partial clips older than ``minimum_age_seconds``.

    Return removed paths and non-fatal stat/removal failures. ``now`` overrides
    wall-clock time for tests. A negative minimum age raises ValueError.
    """

    if minimum_age_seconds < 0:
        raise ValueError('minimum_age_seconds must be non-negative')

    root = Path(clips_root)
    if not root.is_dir():
        return PartialCleanupResult(removed=(), failures=())

    cutoff = (time.time() if now is None else now) - minimum_age_seconds
    removed: list[Path] = []
    failures: list[tuple[Path, str]] = []

    for candidate in iter_safe_tree(root, cancel_event=cancel_event):
        if (not candidate.name.casefold().endswith('.mp4.partial')
                and not candidate.name.casefold().endswith(
                    '.mp4' + _AUDIO_MANIFEST_SUFFIX)):
            continue
        is_partial = is_fthr_owned_partial_path(candidate)
        is_orphan_manifest = is_fthr_owned_orphan_audio_manifest_path(candidate)
        if (not is_partial and not is_orphan_manifest) or candidate.is_symlink():
            continue
        try:
            if not candidate.is_file() or candidate.stat().st_mtime > cutoff:
                continue
            if is_orphan_manifest:
                media = candidate.with_name(
                    candidate.name[:-len(_AUDIO_MANIFEST_SUFFIX)])
                if media.exists():
                    continue
            if is_orphan_manifest:
                from core.library_ownership import delete_media, media_file_identity
                delete_media(candidate, metadata_manager=metadata_manager,
                             expected_identity=media_file_identity(candidate))
            else:
                candidate.unlink()  # Technical .partial artifacts cannot carry library flags.
            removed.append(candidate)
        except (OSError, ValueError, TypeError) as error:
            failures.append((candidate, str(error)))

    return PartialCleanupResult(
        removed=tuple(removed), failures=tuple(failures))


def start_stale_partial_cleanup(
        clips_root: str | Path, *,
        on_complete: Callable[[PartialCleanupResult], None] | None = None,
        metadata_manager=None,
) -> tuple[threading.Thread, threading.Event]:
    """Run stale-partial recovery outside Qt's main thread.

    The returned event/thread are owned by the caller so shutdown can request
    cancellation and join for a short bounded grace period.
    """
    cancel_event = threading.Event()

    def _run() -> None:
        result = cleanup_stale_partial_clips(
            clips_root, cancel_event=cancel_event, metadata_manager=metadata_manager)
        if on_complete is not None and not cancel_event.is_set():
            try:
                on_complete(result)
            except Exception:
                # Recovery diagnostics are advisory; never take down startup.
                pass

    worker = threading.Thread(
        target=_run, daemon=True, name='fthr-stale-partial-cleanup')
    worker.start()
    return worker, cancel_event
