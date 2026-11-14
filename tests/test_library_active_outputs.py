"""Live recording ownership gates discovery without hiding recoverable orphans."""
from types import SimpleNamespace

import pytest

from core.library_index import canonical_media_path, scan_library
from ui import clip_grid


def test_active_output_hidden_from_overlapping_roots_and_batches(tmp_path):
    recording_dir = tmp_path / 'Recordings'
    recording_dir.mkdir()
    active = recording_dir / 'FTHR_recording_active.mp4'
    active.write_bytes(b'growing')
    completed = recording_dir / 'completed.mp4'
    completed.write_bytes(b'finished')
    batches = []
    result = scan_library(tmp_path, [recording_dir, tmp_path], batch_size=1,
                          on_batch=batches.extend, active_output_paths=[active])
    assert {record.path for record in result.all_records} == {str(completed)}
    assert {record.path for record in batches} == {str(completed)}
    assert str(active) not in result.imported_paths
    # A new process has no live owner: recovery/import of an orphan still works.
    assert {record.path for record in scan_library(tmp_path).records} == {
        str(active), str(completed)}


@pytest.fixture
def grid(qapp, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(clip_grid.ClipGrid, '_load_clips', lambda *_: None)
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [str(tmp_path)],
    }.get(key, default))
    widget = clip_grid.ClipGrid(settings, autostart=False)
    qtbot.addWidget(widget)
    yield widget
    widget.shutdown()


def test_reservation_removes_card_and_rejects_stale_batch_and_upsert(grid, tmp_path):
    active = tmp_path / 'active.mp4'
    active.write_bytes(b'growing')
    records = scan_library(tmp_path).records
    grid._replace_records({record.identity: record for record in records})
    grid._apply_current_index()
    old_generation = grid._scan_generation
    cancelled = []
    worker = SimpleNamespace(generation=old_generation,
                             cancel=lambda: cancelled.append(True))
    grid._active_scan_worker = worker
    grid.set_active_output_paths([active])
    assert cancelled == [True]
    assert grid._records == {}
    assert str(active) not in grid._thumb_widgets
    grid._on_scan_batch(old_generation, records)
    grid.upsert_saved_clip(str(active), ready=True)
    assert grid._records == {}
    assert str(active) not in grid._pending_saved_clips
    # Even a current-generation callback cannot publish an active path.
    worker.generation = grid._scan_generation
    grid._on_scan_batch(grid._scan_generation, records)
    assert grid._records == {}
    grid._active_scan_worker = None


def test_release_republishes_finalized_file_without_restart(grid, tmp_path):
    path = tmp_path / 'recording.mp4'
    path.write_bytes(b'growing')
    grid.set_active_output_paths([path])
    assert grid._collect_media_files() == set()
    path.write_bytes(b'finalized-media')
    grid.set_active_output_paths([])
    assert grid._collect_media_files() == {str(path)}
    record = grid._records[canonical_media_path(path)]
    assert record.size == len(b'finalized-media')


def test_worker_takes_immutable_ownership_snapshot(tmp_path):
    path = tmp_path / 'active.mp4'
    path.write_bytes(b'growing')
    owners = {str(path)}
    worker = clip_grid._FileCollectWorker(str(tmp_path), [], 'all', 'newest',
                                          active_output_paths=owners)
    owners.clear()
    worker.run()
    assert worker.result.all_records == ()


def test_queued_scan_completion_cannot_resurrect_reserved_output(grid, tmp_path, monkeypatch):
    active = tmp_path / 'active.mp4'
    active.write_bytes(b'growing')
    completed = tmp_path / 'completed.mp4'
    completed.write_bytes(b'done')
    queued = []
    monkeypatch.setattr(grid._scan_thread_pool, 'start', queued.append)
    grid._scanning_started = True
    grid._begin_scan('background_refresh')
    old_worker = queued[0]
    # Model a worker that already finished, with its delivery waiting in Qt.
    old_worker.result = scan_library(tmp_path)
    grid.set_active_output_paths([active])
    old_worker.signals.finished.emit(
        {str(active), str(completed)}, [], set(), [str(tmp_path)])
    assert canonical_media_path(active) not in grid._records
    grid._start_pending_scan()
    fresh_worker = queued[-1]
    assert fresh_worker is not old_worker
    fresh_worker.run()
    assert set(grid._records) == {canonical_media_path(completed)}
