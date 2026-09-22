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
    assert result.stats.skipped_reparse_points >= 1


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
    from ui import clip_grid

    media = tmp_path / "unicode [take] 'one'.mp4"
    media.write_bytes(b"one")
    first = clip_grid._get_cached_thumb_path(str(media))
    first_failed = clip_grid._get_negative_cache_path(first)
    media.write_bytes(b"a different payload")
    second = clip_grid._get_cached_thumb_path(str(media))
    second_failed = clip_grid._get_negative_cache_path(second)

    assert first != second
    assert first_failed != second_failed


def test_corrupt_thumbnail_is_not_a_cache_hit(tmp_path: Path) -> None:
    from ui.clip_grid import _valid_cached_thumbnail

    corrupt = tmp_path / "bad.jpg"
    corrupt.write_bytes(b"not a jpeg")
    assert _valid_cached_thumbnail(str(corrupt)) is False


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
    assert grid._all_count_label.text().endswith("(120)")
    grid.shutdown()
    grid.deleteLater()


def test_owned_thumbnail_process_is_cancelled_and_reaped() -> None:
    from ui.clip_grid import _run_owned_media_process, media_process_snapshot

    cancelled = threading.Event()
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    before = media_process_snapshot()[0]
    try:
        result = _run_owned_media_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cancelled, timeout_seconds=3.0)
    finally:
        timer.cancel()
    assert result is None
    assert media_process_snapshot()[0] == before


def test_cancelled_thumbnail_worker_does_not_publish_failure_generation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation is terminal cleanup, not a negative-cache failure."""
    from ui import clip_grid

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture")
    cache = tmp_path / "thumb.jpg"
    cancelled = threading.Event()
    monkeypatch.setattr(clip_grid, "THUMB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(clip_grid, "_get_cached_thumb_path",
                        lambda _path: str(cache))
    monkeypatch.setattr(clip_grid, "is_completed_video_path", lambda _path: True)
    monkeypatch.setattr(
        clip_grid, "_probe_with_owned_process",
        lambda _path, _cancel: SimpleNamespace(
            duration_seconds=12.0, width=1920, height=1080,
            average_fps=60.0, video_bitrate_bps=1, total_bitrate_bps=1),
    )

    def cancel_during_decode(_path, _cancel):
        cancelled.set()
        return None

    monkeypatch.setattr(clip_grid, "_decode_thumbnail_with_owned_process",
                        cancel_during_decode)
    worker = clip_grid._ThumbnailWorker(str(media), cancelled)
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
    from ui import clip_grid

    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'fixture')
    cache = tmp_path / 'thumb.jpg'
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(clip_grid, '_get_cached_thumb_path',
                        lambda _path: str(cache))
    monkeypatch.setattr(clip_grid, 'is_completed_video_path', lambda _path: True)
    monkeypatch.setattr(
        clip_grid, '_probe_with_owned_process',
        lambda _path, _cancel: SimpleNamespace(
            duration_seconds=12.0, width=1920, height=1080,
            average_fps=60.0, video_bitrate_bps=1, total_bitrate_bps=1),
    )
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    monkeypatch.setattr(clip_grid, '_decode_thumbnail_with_owned_process',
                        lambda _path, _cancel: image)
    monkeypatch.setattr(
        clip_grid, 'maybe_prune_thumbnail_cache',
        lambda _path: (_ for _ in ()).throw(RuntimeError('prune failed')))

    worker = clip_grid._ThumbnailWorker(str(media), threading.Event())
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
