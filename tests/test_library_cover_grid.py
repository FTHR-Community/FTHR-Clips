"""Focused GUI contracts for the grid's separate cover image state."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from PySide6.QtGui import QImage, QPixmap

import ui.clip_cover_dialog as cover_dialog
from ui.clip_grid import ClipThumbnail, ClipGrid
from core.library_covers import CoverCommittedError


def test_custom_cover_is_separate_from_normal_thumbnail(qtbot, tmp_path):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'clip')
    card = ClipThumbnail(
        str(path), is_video=True, defer_image_load=True,
        metadata_manager=None)
    qtbot.addWidget(card)
    cover = QPixmap(QImage(32, 18, QImage.Format.Format_RGB32))
    card.set_image_thumbnail(QImage(32, 18, QImage.Format.Format_RGB32))
    card.set_custom_cover(cover, 'cover-v1')
    assert card._thumbnail_ready
    assert card._cover_cache_key == 'cover-v1'
    card.clear_custom_cover()
    card._render_thumbnail()
    assert not card._thumb_pixmap.isNull()


def test_cover_cache_key_binds_revision_clip_and_identity():
    grid = ClipGrid.__new__(ClipGrid)
    record = SimpleNamespace(file_identity={
        'st_dev': 1, 'st_ino': 2, 'size': 3, 'mtime_ns': 4})
    binding = {'clip_id': '123e4567-e89b-12d3-a456-426614174000',
               'identity': dict(record.file_identity),
               'reference': {'revision': '123e4567-e89b-12d3-a456-426614174001'}}
    first = grid._cover_cache_key_for(record, binding)
    assert first is not None
    record.file_identity['size'] = 5
    second = grid._cover_cache_key_for(record, binding)
    assert second is None


def test_cover_cache_key_rejects_stale_binding_identity():
    grid = ClipGrid.__new__(ClipGrid)
    record = SimpleNamespace(file_identity={
        'st_dev': 1, 'st_ino': 2, 'size': 3, 'mtime_ns': 4})
    binding = {
        'clip_id': '123e4567-e89b-12d3-a456-426614174000',
        'identity': {'st_dev': 1, 'st_ino': 2, 'size': 2, 'mtime_ns': 4},
        'reference': {'revision': '123e4567-e89b-12d3-a456-426614174001'},
    }
    assert grid._cover_cache_key_for(record, binding) is None


def test_probe_worker_uses_stream_frame_boundary(monkeypatch):
    channel = cover_dialog._CoverResultChannel()
    results = []
    terminals = []
    channel.result.connect(lambda *args: results.append(args))
    channel.terminal.connect(lambda job_id: terminals.append(job_id))
    monkeypatch.setattr(cover_dialog, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(
        cover_dialog, 'run_media_process',
        lambda *_args, **_kwargs: (
            0,
            b'{"format":{"duration":"2.35"},'
            b'"streams":[{"duration":"2.35",'
            b'"avg_frame_rate":"30/1","nb_frames":"71"}]}'))
    worker = cover_dialog._ProbeWorker(
        4, 'clip.mp4', channel, threading.Event(), job_id=9)
    worker.run()
    assert len(results) == 1
    assert results[0][2] == {'duration': 2.35, 'last_frame': 2.316}
    assert terminals == [9]


def test_save_worker_reports_committed_publication_fault():
    channel = cover_dialog._CoverResultChannel()
    results = []
    terminals = []
    channel.result.connect(lambda *args: results.append(args))
    channel.terminal.connect(lambda job_id: terminals.append(job_id))

    class Manager:
        def set_library_cover(self, *_args, **_kwargs):
            raise CoverCommittedError(
                'saved; reload needed',
                reference={'version': 1},
            )

    worker = cover_dialog._SaveWorker(
        5, Manager(), 'clip.mp4', b'png',
        {'st_dev': 1, 'st_ino': 2, 'st_size': 3, 'st_mtime_ns': 4},
        '123e4567-e89b-12d3-a456-426614174000', None,
        channel, threading.Event(), job_id=10)
    worker.run()
    assert len(results) == 1
    assert results[0][2]['committed'] is True
    assert results[0][2]['reload_needed'] is True
    assert terminals == [10]
