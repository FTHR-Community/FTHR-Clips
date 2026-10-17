"""Exercise actual QRunnable delivery across library hide/reopen cycles."""
from pathlib import Path
import os
import threading
from types import SimpleNamespace

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QImage

from ui import clip_grid


def test_ready_clip_opens_before_thumbnail_and_only_once(qtbot, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    card = clip_grid.ClipThumbnail(str(clip), ready=True)
    qtbot.addWidget(card)
    opened = []
    card.opened.connect(lambda *args: opened.append(args))
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton)
    assert len(opened) == 1
    card.set_video_thumbnail('', 0)
    assert len(opened) == 1
    assert card.duration_label.text() == '--:--'
    card.set_ready(False)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton)
    assert len(opened) == 1


def test_expired_failure_retries_and_metadata_survives_decode_failure(
        qtbot, monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path))
    cache = clip_grid._get_cached_thumb_path(str(clip))
    failed = Path(clip_grid._get_negative_cache_path(cache))
    failed.touch()
    os.utime(failed, (1, 1))
    metadata = SimpleNamespace(duration_seconds=12.5, width=1920, height=1080,
                               average_fps=60, video_bitrate_bps=1,
                               total_bitrate_bps=1)
    monkeypatch.setattr(clip_grid, '_probe_with_owned_process', lambda *_: metadata)
    monkeypatch.setattr(clip_grid, '_decode_thumbnail_with_owned_process', lambda *_: None)
    worker = clip_grid._ThumbnailWorker(str(clip))
    results = []
    worker.signals.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert results == [(str(clip), '', 12)]
    assert clip_grid.get_cached_clip_metadata(str(clip))[0] == 12.5


def test_cancelled_real_worker_recovers_on_reopen(qtbot, monkeypatch, tmp_path):
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path / 'cache'))
    entered = threading.Event()
    metadata = SimpleNamespace(duration_seconds=12.5, width=1920, height=1080,
                               average_fps=60, video_bitrate_bps=1,
                               total_bitrate_bps=1)
    calls = []

    def probe(_path, cancel):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert cancel.wait(4), 'test did not cancel the first worker'
        return metadata

    monkeypatch.setattr(clip_grid, '_probe_with_owned_process', probe)
    image = QImage(16, 9, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    monkeypatch.setattr(clip_grid, '_decode_thumbnail_with_owned_process', lambda *_: image)
    delivered_on = []
    original = clip_grid.ClipThumbnail.set_video_thumbnail

    def install(card, *args):
        delivered_on.append(QThread.currentThread() == card.thread())
        return original(card, *args)

    monkeypatch.setattr(clip_grid.ClipThumbnail, 'set_video_thumbnail', install)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    qtbot.addWidget(grid)
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        for _ in range(3):
            grid.set_background_paused(True)
            grid.set_background_paused(False)
            qtbot.waitUntil(lambda: bool(grid.thumbnails)
                           and grid.thumbnails[0]._thumbnail_ready
                           and not grid._thumbnail_jobs_inflight, timeout=5000)
            assert not grid.thumbnails[0]._thumb_pixmap.isNull()
            assert grid.thumbnails[0].duration_label.text() == '0:12'
            grid._apply_current_index()
        qtbot.waitUntil(lambda: not grid._thumbnail_jobs_inflight, timeout=5000)
        assert delivered_on and all(delivered_on)
        assert grid._thumbnail_workers == {}
    finally:
        grid.shutdown()
