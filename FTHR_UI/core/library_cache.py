"""Bounded disk cache helpers for library metadata and thumbnails."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path


DEFAULT_MAX_CACHE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_CACHE_ENTRIES = 8_192
_prune_lock = threading.Lock()
_last_prune_at = 0.0


def _prune_thumbnail_cache_unlocked(
    cache_dir: str | os.PathLike[str], *,
    max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
    max_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
) -> dict[str, int]:
    """Remove oldest complete cache generations until both limits hold."""

    root = Path(cache_dir)
    groups: dict[str, list[Path]] = {}
    sizes: dict[str, int] = {}
    newest: dict[str, float] = {}
    try:
        root.mkdir(parents=True, exist_ok=True)
        # DirEntry retains the directory enumeration's metadata on Windows.
        # Path.is_file() followed by Path.stat() reopened every cache entry,
        # producing thousands of filesystem calls during library startup.
        with os.scandir(root) as entries:
            for entry in entries:
                if '.tmp-' in entry.name:
                    continue
                try:
                    if not entry.is_file():
                        continue
                    info = entry.stat()
                except OSError:
                    # Concurrent cache eviction may remove this entry during enumeration.
                    continue
                path = Path(entry.path)
                key = path.stem.split('.meta-', 1)[0]
                groups.setdefault(key, []).append(path)
                sizes[key] = sizes.get(key, 0) + int(info.st_size)
                newest[key] = max(newest.get(key, 0), float(info.st_mtime_ns))
    # Cache maintenance is best-effort and must not fail library discovery.
    except OSError:
        # Cache pressure must never turn a media card into a hard failure.
        return {'entries': 0, 'bytes': 0, 'removed_entries': 0,
                'removed_bytes': 0}

    bytes_total = sum(sizes.values())
    removed_entries = 0
    removed_bytes = 0
    limit_bytes = max(0, int(max_bytes))
    limit_entries = max(1, int(max_entries))
    for key in sorted(newest, key=newest.get):
        if (bytes_total <= limit_bytes and len(groups) <= limit_entries):
            break
        for path in groups[key]:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                # A concurrent thumbnail writer or another process owns it.
                continue
            removed_entries += 1
            removed_bytes += int(size)
            bytes_total -= int(size)
        groups.pop(key, None)

    return {
        'entries': sum(len(group) for group in groups.values()),
        'bytes': max(0, bytes_total),
        'removed_entries': removed_entries,
        'removed_bytes': removed_bytes,
    }


def prune_thumbnail_cache(
        cache_dir: str | os.PathLike[str], *,
        max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        max_entries: int = DEFAULT_MAX_CACHE_ENTRIES) -> dict[str, int]:
    """Prune one cache as an atomic maintenance operation.

    Locking the complete enumeration/stat/eviction transaction prevents two
    thumbnail workers from making decisions against different snapshots and
    accidentally evicting a generation another worker just published.
    """
    with _prune_lock:
        return _prune_thumbnail_cache_unlocked(
            cache_dir, max_bytes=max_bytes, max_entries=max_entries)


def maybe_prune_thumbnail_cache(cache_dir: str | os.PathLike[str], *, force: bool = False):
    """Coalesce cache maintenance so it never runs for every decoded frame."""

    global _last_prune_at
    now = time.monotonic()
    with _prune_lock:
        if not force and now - _last_prune_at < 30.0:
            return None
        _last_prune_at = now
        return _prune_thumbnail_cache_unlocked(cache_dir)
