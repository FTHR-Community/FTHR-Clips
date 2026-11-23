"""Deterministic Stage 7 library discovery and bounded-view guards."""

from __future__ import annotations

import threading
import time
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.library_index import scan_library


def test_recursive_scan_deduplicates_overlapping_roots_and_keeps_coverage(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "games" / "season" / "matches"
    nested.mkdir(parents=True)
    clip = nested / "match.mp4"
    clip.write_bytes(b"fixture")

    result = scan_library(tmp_path, [tmp_path / "games"])

    assert len(result.records) == 1
    record = result.records[0]
    assert record.path == str(clip)
    assert record.imported is True
    assert len(record.root_ids) == 2
    assert result.stats.duplicate_files >= 1


def test_scan_skips_symlinks_without_following_a_loop(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"fixture")
    link = tmp_path / "loop"
    try:
        link.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this Windows test host")

    result = scan_library(tmp_path)

    assert len(result.records) == 1
    assert result.stats.skipped_reparse_point_count >= 1


def test_scan_cancellation_does_not_publish_partial_batches(tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"fixture")
    cancelled = threading.Event()
    cancelled.set()
    batches: list[tuple[object, ...]] = []

    result = scan_library(tmp_path, cancel_event=cancelled,
                          on_batch=batches.append)

    assert result.stats.cancelled is True
    assert result.records == ()
    assert batches == []


def test_discovery_batches_are_published_before_traversal_completes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first bounded batch must not wait for a complete directory walk."""
    for index in range(5):
        (tmp_path / f"clip-{index}.mp4").write_bytes(b"fixture")

    original_scandir = os.scandir
    directory_finished = False

    class _ScanHandle:
        def __init__(self, handle):
            self._handle = handle

        def __iter__(self):
            return iter(self._handle)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def close(self):
            nonlocal directory_finished
            directory_finished = True
            return self._handle.close()

    def wrapped_scandir(path):
        return _ScanHandle(original_scandir(path))

    monkeypatch.setattr(os, "scandir", wrapped_scandir)
    batches: list[tuple[object, ...]] = []

    def on_batch(batch):
        # The callback runs while the directory iterator is still active.
        assert directory_finished is False
        batches.append(batch)

    result = scan_library(tmp_path, batch_size=1, on_batch=on_batch)

    assert result.stats.cancelled is False
    assert len(batches) == 5
    assert all(0 < len(batch) <= 1 for batch in batches)


def test_cancellation_during_discovery_stops_future_batches(tmp_path: Path) -> None:
    for index in range(32):
        (tmp_path / f"clip-{index}.mp4").write_bytes(b"fixture")
    cancelled = threading.Event()
    batches: list[tuple[object, ...]] = []

    def on_batch(batch):
        batches.append(batch)
        cancelled.set()

    result = scan_library(
        tmp_path, batch_size=1, cancel_event=cancelled, on_batch=on_batch)

    assert result.stats.cancelled is True
    assert len(batches) == 1
    assert len(result.records) <= len(batches)


def test_large_synthetic_library_has_lightweight_records(tmp_path: Path) -> None:
    """10k candidates exercise traversal; only 5k are accepted media."""
    for index in range(5_000):
        (tmp_path / f"clip-{index:05d}.mp4").write_bytes(b"")
        (tmp_path / f"note-{index:05d}.txt").write_bytes(b"")

    result = scan_library(tmp_path, batch_size=128)

    assert len(result.all_records) == 5_000
    assert len(result.records) == 5_000
    assert result.stats.entries_seen == 10_000
    assert result.stats.accepted_files == 5_000
    assert all(record.size == 0 for record in result.records)


def test_repeated_rescans_keep_identity_and_fingerprint_stable(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"one")
    first = scan_library(tmp_path).records[0]
    second = scan_library(tmp_path).records[0]
    assert first.identity == second.identity
    assert first.fingerprint == second.fingerprint

    time.sleep(0.002)
    clip.write_bytes(b"changed")
    third = scan_library(tmp_path).records[0]
    assert third.identity == first.identity
    assert third.fingerprint != first.fingerprint


def test_thumbnail_queue_and_materialization_limits_are_explicit() -> None:
    from ui import clip_grid

    assert clip_grid._MAX_THUMBNAIL_QUEUE == 128
    assert clip_grid._MAX_MATERIALIZED_CARDS == 96


def test_thumbnail_cache_pruning_removes_complete_old_generations(
    tmp_path: Path,
) -> None:
    from core.library_cache import prune_thumbnail_cache

    for index in range(3):
        (tmp_path / f"generation-{index}.jpg").write_bytes(b"thumb")
        (tmp_path / f"generation-{index}.meta-v3").write_text(
            "1 32 18 30 0 0", encoding="utf-8")
        os_time = time.time() - (3 - index)
        for path in tmp_path.glob(f"generation-{index}.*"):
            os.utime(path, (os_time, os_time))

    result = prune_thumbnail_cache(tmp_path, max_bytes=1024, max_entries=1)

    assert result["entries"] == 2
    assert (tmp_path / "generation-2.jpg").exists()
    assert not (tmp_path / "generation-0.jpg").exists()


def test_fingerprint_bound_cache_changes_after_media_mutation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ui import library_thumbnails

    media = tmp_path / "unicode [take] 'one'.mp4"
    media.write_bytes(b"one")
    first = library_thumbnails._get_cached_thumb_path(str(media))
    first_failed = library_thumbnails._get_negative_cache_path(first)
    media.write_bytes(b"a different payload")
    second = library_thumbnails._get_cached_thumb_path(str(media))
    second_failed = library_thumbnails._get_negative_cache_path(second)

    assert first != second
    assert first_failed != second_failed


def test_corrupt_thumbnail_is_not_a_cache_hit(tmp_path: Path) -> None:
    from ui.library_thumbnails import _valid_cached_thumbnail

    corrupt = tmp_path / "bad.jpg"
    corrupt.write_bytes(b"not a jpeg")
    assert _valid_cached_thumbnail(str(corrupt)) is False


def test_thumbnail_cache_uses_valid_indexed_identity_and_rechecks_replacement(tmp_path, monkeypatch):
    from core.library_index import LibraryRecord, canonical_media_path
    from ui import library_thumbnails

    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original')
    stat = media.stat()
    record = LibraryRecord(str(media), canonical_media_path(media), (), False, 'video',
                           stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
    expected = library_thumbnails._get_cached_thumb_path(str(media))
    resolves = []

    def resolve(path):
        resolves.append(path)
        return canonical_media_path(path)

    monkeypatch.setattr(library_thumbnails, 'canonical_media_path', resolve)
    assert library_thumbnails._get_cached_thumb_path(str(media), indexed=record) == expected
    assert not resolves
    media.write_bytes(b'replacement payload')
    assert library_thumbnails._get_cached_thumb_path(str(media), indexed=record) != expected
    assert resolves == [str(media)]


def test_thumbnail_uses_existing_sidecar_after_cancelled_jpeg(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    from ui import library_thumbnails

    media = tmp_path / 'clip.mp4'
    media.touch()
    monkeypatch.setattr(library_thumbnails, 'THUMB_CACHE_DIR', str(tmp_path))
    cache_path = library_thumbnails._get_cached_thumb_path(str(media))
    library_thumbnails._write_cached_duration(
        library_thumbnails._get_cached_duration_path(cache_path), 13.25, 1920, 1080, 60)
    probes = []
    monkeypatch.setattr(library_thumbnails, '_probe_with_owned_process', lambda *_: probes.append(True))
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    monkeypatch.setattr(library_thumbnails, '_decode_thumbnail_with_owned_process', lambda *_: image)
    worker = library_thumbnails._ThumbnailWorker(str(media))
    results = []
    worker.signals.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert not probes
    assert results == [(str(media), cache_path, 13)]
    assert library_thumbnails.get_cached_clip_metadata(str(media))[0] == 13.25


def test_motion_pixels_then_authoritative_sidecar_only_with_interruption(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    from ui import library_thumbnails

    media = tmp_path / 'clip.mp4'
    media.touch()
    monkeypatch.setattr(library_thumbnails, 'THUMB_CACHE_DIR', str(tmp_path))
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    decodes = []
    monkeypatch.setattr(library_thumbnails, '_decode_thumbnail_with_owned_process',
                        lambda *_: decodes.append(True) or image)
    info = SimpleNamespace(duration_seconds=13.25, width=1920, height=1080,
                           average_fps=60, video_bitrate_bps=1, total_bitrate_bps=1)
    probes = []

    def probe(_source, cancel):
        probes.append(True)
        if len(probes) == 1:
            cancel.set()
        return info

    monkeypatch.setattr(library_thumbnails, '_probe_with_owned_process', probe)
    cache_path = library_thumbnails._get_cached_thumb_path(str(media))
    preview = library_thumbnails._ThumbnailWorker(str(media))
    preview._thumbnail_motion_preview = True
    preview.run()
    jpeg = Path(cache_path).read_bytes()
    assert len(decodes) == 1 and not probes
    assert library_thumbnails.get_cached_clip_metadata(str(media)) is None
    disk_result = []
    cache_reader = library_thumbnails._CachedThumbnailWorker(str(media), True, threading.Event())
    cache_reader.signals.finished.connect(lambda *args: disk_result.append(args))
    cache_reader.run()
    assert len(disk_result) == 1 and disk_result[0][2] is None
    interrupted = library_thumbnails._ThumbnailWorker(str(media))
    interrupted.run()
    assert library_thumbnails.get_cached_clip_metadata(str(media)) is None
    completed = library_thumbnails._ThumbnailWorker(str(media))
    completed.run()
    assert len(probes) == 2 and len(decodes) == 1 and Path(cache_path).read_bytes() == jpeg
    assert library_thumbnails.get_cached_clip_metadata(str(media))[0] == 13.25


def test_motion_preview_does_not_publish_if_source_is_replaced(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    from ui import library_thumbnails

    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'old')
    monkeypatch.setattr(library_thumbnails, 'THUMB_CACHE_DIR', str(tmp_path))
    old_cache = library_thumbnails._get_cached_thumb_path(str(media))
    old_stat = media.stat()

    def decode(*_args):
        replacement = tmp_path / 'replacement.mp4'
        replacement.write_bytes(b'new')
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        replacement.replace(media)
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        return image

    monkeypatch.setattr(library_thumbnails, '_decode_thumbnail_with_owned_process', decode)
    worker = library_thumbnails._ThumbnailWorker(str(media))
    worker._thumbnail_motion_preview = True
    results, terminals = [], []
    worker.signals.finished.connect(lambda *args: results.append(args))
    worker.signals.diagnostic_finished.connect(lambda *args: terminals.append(args))
    worker.run()
    assert len(terminals) == 1 and not results
    assert not Path(old_cache).exists()
    assert library_thumbnails._get_cached_thumb_path(str(media)) == old_cache


def test_safe_tree_honours_cancellation(tmp_path: Path) -> None:
    from core.clip_files import iter_safe_tree

    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "clip.mp4").write_bytes(b"fixture")
    cancel = threading.Event()
    cancel.set()
    assert list(iter_safe_tree(tmp_path, cancel_event=cancel)) == []


def test_large_section_uses_bounded_virtual_canvas(
        qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    from ui.clip_grid import ClipGrid

    for index in range(120):
        (tmp_path / f"clip-{index:03d}.mp4").write_bytes(b"")
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "clips_directory": str(tmp_path),
        "imported_clip_folders": [],
    }.get(key, default)
    # Keep this geometry test deterministic and independent of an installed
    # FFmpeg binary; thumbnail process tests exercise that worker separately.
    monkeypatch.setattr(ClipGrid, "_start_thumbnail_worker", lambda *args: None)
    grid = ClipGrid(settings)
    qtbot.waitUntil(lambda: len(grid._records) == 120, timeout=4000)
    qapp.processEvents()

    assert len(grid._filtered_records()) == 120
    assert len(grid._thumb_widgets) <= 96
    assert grid._virtual_sections
    assert not grid._section_grids
    assert grid._all_count_label.accessibleName().endswith("(120)")
    grid.shutdown()
    grid.deleteLater()


def test_owned_thumbnail_process_is_cancelled_and_reaped(monkeypatch) -> None:
    from core import media_process
    from ui.library_thumbnails import _run_owned_media_process

    cancelled = threading.Event()
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    started = media_process._media_process_started
    owned = []

    def cancel_owned_child(process):
        started(process)
        if process.args is command:
            with media_process._MEDIA_PROCESS_LOCK:
                assert media_process._MEDIA_PROCESS_OWNERS[id(process)] is process
            owned.append(process)
            cancelled.set()

    monkeypatch.setattr(media_process, "_media_process_started", cancel_owned_child)
    result = _run_owned_media_process(command, cancelled, timeout_seconds=3.0)

    assert result is None
    assert len(owned) == 1
    assert owned[0].returncode is not None
    with media_process._MEDIA_PROCESS_LOCK:
        assert id(owned[0]) not in media_process._MEDIA_PROCESS_OWNERS


def test_cancelled_thumbnail_worker_does_not_publish_failure_generation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation is terminal cleanup, not a negative-cache failure."""
    from ui import library_thumbnails

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture")
    cache = tmp_path / "thumb.jpg"
    cancelled = threading.Event()
    monkeypatch.setattr(library_thumbnails, "THUMB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(library_thumbnails, "_get_cached_thumb_path",
                        lambda _path, **_kwargs: str(cache))
    monkeypatch.setattr(library_thumbnails, "is_completed_video_path", lambda _path: True)
    monkeypatch.setattr(
        library_thumbnails, "_probe_with_owned_process",
        lambda _path, _cancel: SimpleNamespace(
            duration_seconds=12.0, width=1920, height=1080,
            average_fps=60.0, video_bitrate_bps=1, total_bitrate_bps=1),
    )

    def cancel_during_decode(_path, _cancel):
        cancelled.set()
        return None

    monkeypatch.setattr(library_thumbnails, "_decode_thumbnail_with_owned_process",
                        cancel_during_decode)
    worker = library_thumbnails._ThumbnailWorker(str(media), cancelled)
    finished: list[tuple] = []
    diagnostics: list[tuple] = []
    worker.signals.finished.connect(lambda *args: finished.append(args))
    worker.signals.diagnostic_finished.connect(
        lambda *args: diagnostics.append(args))

    worker.run()

    assert finished == []
    assert len(diagnostics) == 1
    assert not Path(str(cache)[:-4] + ".failed").exists()


def test_thumbnail_cache_prune_failure_still_releases_terminal_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional cache maintenance cannot suppress diagnostic_finished."""
    from PySide6.QtGui import QImage
    from ui import library_thumbnails

    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'fixture')
    cache = tmp_path / 'thumb.jpg'
    monkeypatch.setattr(library_thumbnails, 'THUMB_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(library_thumbnails, '_get_cached_thumb_path',
                        lambda _path, **_kwargs: str(cache))
    monkeypatch.setattr(library_thumbnails, 'is_completed_video_path', lambda _path: True)
    monkeypatch.setattr(
        library_thumbnails, '_probe_with_owned_process',
        lambda _path, _cancel: SimpleNamespace(
            duration_seconds=12.0, width=1920, height=1080,
            average_fps=60.0, video_bitrate_bps=1, total_bitrate_bps=1),
    )
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    monkeypatch.setattr(library_thumbnails, '_decode_thumbnail_with_owned_process',
                        lambda _path, _cancel: image)
    monkeypatch.setattr(
        library_thumbnails, 'maybe_prune_thumbnail_cache',
        lambda _path: (_ for _ in ()).throw(RuntimeError('prune failed')))

    worker = library_thumbnails._ThumbnailWorker(str(media), threading.Event())
    diagnostics: list[tuple] = []
    worker.signals.diagnostic_finished.connect(
        lambda *args: diagnostics.append(args))
    worker.run()

    assert len(diagnostics) == 1


def test_startup_partial_cleanup_runs_off_calling_thread(tmp_path: Path) -> None:
    from core.clip_files import start_stale_partial_cleanup

    partial = tmp_path / "game_clip_from_old.mp4.partial"
    partial.write_bytes(b"partial")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(partial, (old, old))
    completed = threading.Event()
    thread, cancel = start_stale_partial_cleanup(
        tmp_path, on_complete=lambda _result: completed.set())
    assert thread is not threading.current_thread()
    assert completed.wait(2.0)
    thread.join(timeout=1.0)
    assert not cancel.is_set()
    assert not partial.exists()


def test_upload_interval_scan_is_async_and_non_overlapping(qapp, qtbot, tmp_path: Path) -> None:
    from unittest.mock import MagicMock
    import core.upload_manager as upload_module

    clip = tmp_path / "old.mp4"
    clip.write_bytes(b"fixture")
    old = time.time() - 120
    os.utime(clip, (old, old))
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "upload_enabled": True,
        "upload_mode": "interval",
    }.get(key, default)
    manager = upload_module.UploadManager(settings)
    manager.is_enabled = lambda: True
    manager.clips_directory = lambda: tmp_path
    manager.is_uploaded = lambda _path: False
    enqueued: list[str] = []
    manager.enqueue_upload = lambda path: enqueued.append(path)
    manager._interval_scan()
    manager._interval_scan()  # Must not launch a second walker.
    qtbot.waitUntil(lambda: bool(enqueued), timeout=2000)
    assert enqueued == [str(clip)]
    manager.stop()
