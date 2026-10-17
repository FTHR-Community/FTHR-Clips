"""Guard clip identity and worker ownership when fast scrolling reuses cards."""
from types import SimpleNamespace
import os
import time

from ui.clip_grid import ClipGrid


def make_grid(tmp_path, monkeypatch):
    monkeypatch.setattr(ClipGrid, '_load_clips', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    return ClipGrid(settings)


def test_recycled_card_resets_clip_identity_and_share_state(qapp, tmp_path, monkeypatch):
    grid = make_grid(tmp_path, monkeypatch)
    first = str(tmp_path / 'first.mp4')
    second = str(tmp_path / 'second.mp4')
    grid._upload_enabled = lambda: True
    grid._imported_files.add(first)
    card = grid._create_thumbnail(first, 240, ready_override=True)
    card.set_upload_info({'url': 'https://example.com/first'})
    card.set_upload_status('Uploading', feedback='Started')
    grid._release_virtual_card(first)
    reused = grid._create_thumbnail(second, 240, ready_override=True)
    assert reused is card
    assert reused.file_path == second
    assert not reused.imported
    assert not reused.upload_link
    assert not reused._upload_status
    assert reused.share_btn.isEnabled()
    uploads = []
    grid.clip_upload_requested.connect(uploads.append)
    reused.share_btn.click()
    assert uploads == [second]
    assert first not in grid._thumb_widgets
    assert len(grid.thumbnails) == 1
    grid.shutdown()
    grid.deleteLater()


def test_duration_option_uses_source_duration_after_card_recycling(qapp, qtbot, tmp_path, monkeypatch):
    from pathlib import Path
    from PySide6.QtGui import QPixmap
    from core.clip_metadata_manager import ClipMetadataManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    grid = make_grid(tmp_path, monkeypatch)
    qtbot.addWidget(grid)
    grid._metadata_manager = ClipMetadataManager()
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    path = str(clip)
    grid._metadata_manager.set_editor_draft(path, {
        'trim_start': 0, 'trim_end': 1,
        'segments': [[0, .5, False], [.5, 1, True]]})
    card = grid._create_thumbnail(path, 240, ready_override=True)
    card.set_source_duration(60)
    card._thumb_pixmap = QPixmap(100, 56)
    card._thumb_pixmap.fill()
    card._thumbnail_ready = True
    assert card.duration_label.text() == '1:00'
    grid._release_virtual_card(path)
    original_get = grid._sm.get
    grid._sm.get = lambda key, default=None: True if key == 'grid_show_edited_duration' else original_get(key, default)
    recycled = grid._create_thumbnail(path, 240, ready_override=True)
    grid._start_thumbnail_worker(path)
    assert recycled is card
    assert recycled._source_duration == 60
    assert recycled.duration_label.text() == '0:30'
    grid._sm.get = original_get
    grid.refresh_duration_labels()
    assert recycled.duration_label.text() == '1:00'
    grid.shutdown()


def test_date_boundaries_keep_card_parent_and_correct_geometry(qapp, qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea
    from ui.clip_grid import ClipThumbnail

    grid = make_grid(tmp_path, monkeypatch)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *_: None)
    for index in range(160):
        path = tmp_path / f'clip-{index}.mp4'
        path.touch()
        timestamp = time.time() - (index // 8) * 86400
        os.utime(path, (timestamp, timestamp))
    grid._records = grid._records_from_paths({str(p) for p in tmp_path.glob('*.mp4')})
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.resize(1440, 900)
    qtbot.addWidget(scroll)
    scroll.show()
    grid._apply_current_index()
    qapp.processEvents()
    viewport_lookups = []
    lookup = grid._scroll_viewport
    monkeypatch.setattr(grid, '_scroll_viewport',
                        lambda: viewport_lookups.append(1) or lookup())
    before = len(viewport_lookups)
    grid._update_virtualized_cards()
    # One lookup per frame regardless of the number of date sections.
    assert len(viewport_lookups) == before + 1
    for value in (1200, 3000, 5000, 0):
        scroll.verticalScrollBar().setValue(value)
        grid._update_virtualized_cards()
        qapp.processEvents()
        qtbot.wait(250)  # Let the debounced viewport resize position host-owned cards.
        for path, card in grid._thumb_widgets.items():
            assert card.parentWidget() is grid._sections_host
            canvas = grid._virtual_sections[grid._card_virtual_sections[path]]
            origin = canvas.mapTo(grid._sections_host, QPoint(0, 0))
            assert card.y() >= origin.y()
            assert card.y() + card.height() <= origin.y() + canvas.height()
    grid._apply_current_index()
    qtbot.wait(100)
    # Rebuilding the index must dispose host-owned cards, including the pool.
    assert len(grid._sections_host.findChildren(ClipThumbnail)) <= 96
    grid.shutdown()


def test_composed_thumbnail_cache_survives_recycling_and_tracks_theme(qapp, qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QPixmap, QPixmapCache
    from ui.style import Colors

    grid = make_grid(tmp_path, monkeypatch)
    qtbot.addWidget(grid)
    path = str(tmp_path / 'clip.mp4')
    card = grid._create_thumbnail(path, 240, ready_override=True)
    image = QPixmap(480, 270)
    image.fill(QColor('red'))
    card._thumb_pixmap = image
    card._thumbnail_ready = True
    QPixmapCache.clear()
    card._render_thumbnail()
    first_key = card.thumb_label.pixmap().cacheKey()
    grid._release_virtual_card(path)
    recycled = grid._create_thumbnail(path, 240, ready_override=True)
    assert recycled is card
    grid._start_thumbnail_worker(path)
    assert card.thumb_label.pixmap().cacheKey() == first_key
    card.resize_card(card._card_width + 40)
    resized_key = card.thumb_label.pixmap().cacheKey()
    assert resized_key != first_key
    monkeypatch.setattr(Colors, 'CARD_BG', '#333333')
    card._render_thumbnail()
    assert card.thumb_label.pixmap().cacheKey() != resized_key
    replacement = QPixmap(480, 270)
    replacement.fill(QColor('blue'))
    previous_key = card.thumb_label.pixmap().cacheKey()
    card._thumb_pixmap = replacement
    card._render_thumbnail()
    assert card.thumb_label.pixmap().cacheKey() != previous_key
    grid.shutdown()


def test_scroll_defers_decode_and_offscreen_cancel_keeps_owner(qapp, tmp_path, monkeypatch):
    grid = make_grid(tmp_path, monkeypatch)
    path = str(tmp_path / 'clip.mp4')
    grid._create_thumbnail(path, 240, ready_override=True)
    submitted = []
    monkeypatch.setattr(grid._thread_pool, 'start', submitted.append)
    grid._on_library_scrolled()
    grid._start_thumbnail_worker(path)
    assert submitted == []
    assert path in grid._deferred_thumbnail_paths
    grid._scroll_settle_timer.stop()
    grid._resume_scroll_thumbnails()
    assert len(submitted) == 1
    worker = submitted[0]
    grid._release_virtual_card(path)
    assert worker.cancel_event.is_set()
    assert path in grid._thumbnail_jobs_inflight
    grid._create_thumbnail(path, 240, ready_override=True)
    grid._start_thumbnail_worker(path)
    assert len(submitted) == 1
    owner_generation, fingerprint = grid._thumbnail_job_owners[path]
    grid._on_thumb_diagnostic(0, 0, False, path, owner_generation, fingerprint)
    assert len(submitted) == 2
    assert grid._thumbnail_workers[path] is submitted[1]
    # No QRunnable was actually started; complete its terminal ownership.
    generation, fingerprint = grid._thumbnail_job_owners[path]
    grid._on_thumb_diagnostic(0, 0, False, path, generation, fingerprint)
    grid.shutdown()
    grid.deleteLater()


def test_scroll_card_creation_yields_and_completes_next_frame(qapp, qtbot, tmp_path, monkeypatch):
    from ui import clip_grid

    grid = make_grid(tmp_path, monkeypatch)
    qtbot.addWidget(grid)
    grid._records = {str(index): None for index in range(100)}
    paths = [str(tmp_path / f'clip-{index}.mp4') for index in range(12)]
    grid._add_section('date', paths, 0)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *_: None)
    clock = [100.0]
    create = grid._create_thumbnail

    def slow_create(*args, **kwargs):
        card = create(*args, **kwargs)
        clock[0] += .010
        return card

    monkeypatch.setattr(grid, '_create_thumbnail', slow_create)
    fake_clock = SimpleNamespace(**vars(time))
    fake_clock.perf_counter = lambda: clock[0]
    monkeypatch.setattr(clip_grid, 'time', fake_clock)
    grid._scrolling = True
    grid._virtual_update_timer.stop()
    grid._update_virtualized_cards()
    assert len(grid._thumb_widgets) == 1
    assert grid._virtual_update_timer.isActive()
    # Subsequent frames make progress instead of restarting old work.
    grid._virtual_update_timer.stop()
    grid._update_virtualized_cards()
    assert len(grid._thumb_widgets) == 2
    grid._scrolling = False
    grid._update_virtualized_cards()
    assert set(grid._thumb_widgets) == set(paths)
    grid.shutdown()
