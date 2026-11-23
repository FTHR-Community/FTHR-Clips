"""Guard clip identity and worker ownership when fast scrolling reuses cards."""
from types import SimpleNamespace
import os
import time

import pytest

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


def test_scroll_cancels_full_decode_and_checks_cache_once_per_path(qapp, tmp_path, monkeypatch):
    grid = make_grid(tmp_path, monkeypatch)
    path = str(tmp_path / 'clip.mp4')
    grid._create_thumbnail(path, 240, ready_override=True)
    submitted = []
    transitions = []
    grid.scroll_activity_changed.connect(transitions.append)
    monkeypatch.setattr(grid._thread_pool, 'start', submitted.append)
    grid._start_thumbnail_worker(path)
    full = submitted[0]
    grid._on_library_scrolled()
    grid._start_thumbnail_worker(path)
    assert transitions == [True]
    assert full.cancel_event.is_set() and len(submitted) == 1
    assert grid._thumbnail_workers[path] is full
    full.signals.diagnostic_finished.emit(0, 0, False)
    assert len(submitted) == 2 and submitted[-1]._thumbnail_cache_only
    submitted[-1].signals.diagnostic_finished.emit(0, 0, False)
    grid._start_thumbnail_worker(path)
    assert len(submitted) == 2 and path in grid._scroll_thumbnail_misses
    grid._scroll_settle_timer.stop()
    grid._resume_scroll_thumbnails()
    assert transitions == [True, False]
    assert len(submitted) == 3 and not submitted[-1]._thumbnail_cache_only
    worker = submitted[-1]
    full.signals.diagnostic_finished.emit(0, 0, False)
    assert grid._thumbnail_workers[path] is worker
    grid._release_virtual_card(path)
    assert worker.cancel_event.is_set()
    assert path in grid._thumbnail_jobs_inflight
    grid._create_thumbnail(path, 240, ready_override=True)
    grid._start_thumbnail_worker(path)
    assert len(submitted) == 3
    worker.signals.diagnostic_finished.emit(0, 0, False)
    assert len(submitted) == 4
    assert grid._thumbnail_workers[path] is submitted[-1]
    # No QRunnable was actually started; complete its terminal ownership.
    submitted[-1].signals.diagnostic_finished.emit(0, 0, False)
    grid.shutdown()
    grid.deleteLater()


@pytest.mark.parametrize('extension', ['mp4', 'png'])
def test_warm_disk_thumbnail_displays_before_scroll_settles(qtbot, tmp_path, monkeypatch, extension):
    from PySide6.QtGui import QColor, QImage
    from ui import library_thumbnails as thumbs

    media = tmp_path / f'clip.{extension}'
    media.touch()
    monkeypatch.setattr(thumbs, 'THUMB_CACHE_DIR', str(tmp_path))
    cache_path = thumbs._get_cached_thumb_path(str(media))
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    assert image.save(cache_path)
    if extension == 'mp4':
        thumbs._write_cached_duration(thumbs._get_cached_duration_path(cache_path), 10, 1920, 1080, 60)
    subprocess_calls = []
    monkeypatch.setattr(thumbs, '_run_owned_media_process', lambda *args: subprocess_calls.append(args))
    grid = make_grid(tmp_path, monkeypatch)
    qtbot.addWidget(grid)
    try:
        card = grid._create_thumbnail(str(media), 300, ready_override=True)
        grid._on_library_scrolled()
        grid._scroll_settle_timer.stop()
        grid._start_thumbnail_worker(str(media))
        qtbot.waitUntil(lambda: card._thumbnail_ready and not grid._thumbnail_jobs_inflight)
        assert grid._scrolling and not subprocess_calls
        assert not card._thumb_pixmap.isNull() and grid._thumbnail_cache_hits == 1
        assert card.metadata_label.text() == ('1080p · 60 FPS' if extension == 'mp4' else 'Screenshot')
    finally:
        grid.shutdown()


def test_scroll_cancels_cover_owner_and_requeues_cover_on_settle(qapp, tmp_path, monkeypatch):
    from core.library_index import LibraryRecord, canonical_media_path

    file = tmp_path / 'clip.mp4'
    file.touch()
    media = str(file)
    stat = file.stat()
    record = LibraryRecord(media, canonical_media_path(media), (), False, 'video',
                           stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
    grid = make_grid(tmp_path, monkeypatch)
    sources, workers = [], []
    grid._records_by_path[media] = record
    grid._cover_bindings_snapshot[record.identity] = {
        'identity': record.file_identity,
        'reference': {'revision': 'one'}, 'clip_id': 'clip',
    }
    asset = str(tmp_path / 'cover.png')
    grid._metadata_manager = SimpleNamespace(
        cover_asset_path_for_read=lambda *_: sources.append(asset) or asset)
    monkeypatch.setattr(grid._cover_decode_pool, 'start', workers.append)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *_: None)
    try:
        card = grid._create_thumbnail(media, 300, ready_override=True)
        original = workers[0]
        grid._on_library_scrolled()
        assert original.cancel_event.is_set()
        grid._apply_cover_to_card(card)
        assert len(sources) == len(workers) == 1
        grid._on_cover_decoded(asset, original.cache_key, None, 'cancelled', original.job_id)
        assert not grid._cover_decode_jobs
        grid._scroll_settle_timer.stop()
        grid._resume_scroll_thumbnails()
        grid._virtual_update_timer.stop()
        grid._update_virtualized_cards()
        assert len(sources) == len(workers) == 2
        assert workers[-1].job_id != original.job_id
    finally:
        grid.shutdown()


def test_cold_catchup_serializes_source_workers_and_keeps_one_cache_slot(qapp, tmp_path, monkeypatch):
    grid = make_grid(tmp_path, monkeypatch)
    sources = [str(tmp_path / f'clip-{index}.mp4') for index in range(3)]
    workers = []
    monkeypatch.setattr(grid._thread_pool, 'start', workers.append)
    try:
        for path in sources:
            grid._create_thumbnail(path, 300, ready_override=True)
        grid._on_library_scrolled()
        assert grid._scroll_settle_timer.interval() == 240
        for path in sources[:2]:
            grid._start_thumbnail_worker(path)
            assert workers[-1]._thumbnail_cache_only
            workers[-1].signals.diagnostic_finished.emit(0, 0, False)
        grid._start_thumbnail_worker(sources[2])
        queued_cache = workers[-1]
        grid._scroll_settle_timer.stop()
        grid._resume_scroll_thumbnails()
        first_full = workers[-1]
        assert grid._thumbnail_catchup and grid._overlap_discovery._scrolling
        assert not first_full._thumbnail_cache_only and first_full.file_path == sources[0]
        assert grid._thumbnail_workers == {sources[2]: queued_cache, sources[0]: first_full}
        assert grid._deferred_thumbnail_paths == {sources[1]}
        queued_cache.signals.diagnostic_finished.emit(0, 0, False)
        assert grid._thumbnail_workers == {sources[0]: first_full}
        assert grid._deferred_thumbnail_paths == set(sources[1:])
        for path in sources:
            worker = grid._thumbnail_workers[path]
            assert not worker._thumbnail_cache_only and len(grid._thumbnail_workers) == 1
            worker.signals.diagnostic_finished.emit(0, 0, False)
        assert grid._thumbnail_catchup and not grid._thumbnail_jobs_inflight
        grid._virtual_update_timer.stop()
        grid._update_virtualized_cards()
        assert not grid._thumbnail_catchup and not grid._overlap_discovery._scrolling
        for path in sources[:2]:
            grid._start_thumbnail_worker(path)
        assert len(grid._thumbnail_workers) == 2
    finally:
        grid.shutdown()
        assert not grid._thumbnail_catchup


def test_slow_scroll_previews_visible_then_ahead_and_fast_scroll_cancels(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    viewport = QWidget()
    viewport.resize(1000, 620)
    qtbot.addWidget(viewport)
    viewport.show()
    grid = make_grid(tmp_path, monkeypatch)
    workers = []
    monkeypatch.setattr(grid._thread_pool, 'start', workers.append)
    monkeypatch.setattr(grid, '_scroll_viewport', lambda: viewport)
    paths = [str(tmp_path / f'clip-{index}.mp4') for index in range(3)]
    try:
        grid._on_library_scrolled()
        grid._scroll_settle_timer.stop()
        for path, y in zip(paths, (120, 640, 1000), strict=True):
            card = grid._create_thumbnail(path, 320, ready_override=True, parent=viewport)
            card.move(0, y)
            card.show()
            grid._scroll_thumbnail_misses[path] = False
        grid._scroll_thumbnail_samples.extend([(1.0, 0), (1.16, 60), (1.32, 120)])
        grid._run_scroll_thumbnail_preview()
        assert len(workers) == 1 and workers[0].file_path == paths[0]
        assert workers[0]._thumbnail_motion_preview
        workers[0].signals.diagnostic_finished.emit(0, 0, False)
        assert len(workers) == 2 and workers[1].file_path == paths[1]
        assert workers[1]._thumbnail_motion_preview
        grid._scroll_thumbnail_samples.clear()
        grid._scroll_thumbnail_samples.extend([(2.0, 0), (2.05, 60), (2.11, 120)])
        grid._run_scroll_thumbnail_preview()
        assert workers[1].cancel_event.is_set()
        workers[1].signals.diagnostic_finished.emit(0, 0, False)
        assert len(workers) == 2 and not grid._thumbnail_jobs_inflight
    finally:
        grid.shutdown()


def test_provisional_thumbnail_state_survives_own_cover_and_resets_on_rebind(qapp, tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage, QPixmap

    grid = make_grid(tmp_path, monkeypatch)
    path = str(tmp_path / 'clip.mp4')
    other_path = str(tmp_path / 'other.mp4')
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    cover = QPixmap(32, 18)
    cover.fill(QColor('blue'))
    try:
        card = grid._create_thumbnail(path, 320, ready_override=True)
        card.set_video_thumbnail('', 0, cached_image=image, provisional=True)
        card.set_custom_cover(cover, 'cover')
        card.clear_custom_cover()
        card._render_thumbnail()
        assert card._thumbnail_ready and card._thumbnail_metadata_pending
        assert card._thumb_pixmap.toImage().pixelColor(0, 0) == QColor('red')
        grid._release_virtual_card(path)
        assert grid._create_thumbnail(other_path, 320, ready_override=True) is card
        assert not card._thumbnail_ready and not card._thumbnail_metadata_pending
        assert card._thumb_pixmap.isNull()
        grid._release_virtual_card(other_path)
        assert grid._create_thumbnail(path, 320, ready_override=True) is card
        grid._on_library_scrolled()
        grid._start_thumbnail_worker(path)
        assert card._thumbnail_ready and card._thumbnail_metadata_pending
        assert not grid._thumbnail_jobs_inflight
    finally:
        grid.shutdown()


@pytest.mark.parametrize('emit_failure_result', [False, True])
def test_failed_authoritative_probe_leaves_preview_without_immediate_retry(
        qapp, tmp_path, monkeypatch, emit_failure_result):
    from PySide6.QtGui import QColor, QImage

    grid = make_grid(tmp_path, monkeypatch)
    path = str(tmp_path / 'clip.mp4')
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    workers = []
    monkeypatch.setattr(grid._thread_pool, 'start', workers.append)
    try:
        card = grid._create_thumbnail(path, 320, ready_override=True)
        card.set_video_thumbnail('', 0, cached_image=image, provisional=True)
        grid._thumbnail_catchup = True
        grid._virtual_initialized = True
        grid._virtual_update_timer.stop()
        grid._overlap_discovery.set_scrolling(True)
        grid._start_thumbnail_worker(path)
        assert len(workers) == 1 and not workers[0]._thumbnail_cache_only
        if emit_failure_result:
            workers[0].signals.finished.emit(path, '', 0)
        workers[0].signals.diagnostic_finished.emit(0, 1, False)
        assert len(workers) == 1 and not grid._thumbnail_jobs_inflight
        assert not grid._thumbnail_catchup and not grid._overlap_discovery._scrolling
        assert card._thumbnail_ready and card._thumbnail_metadata_pending
        assert card._thumb_pixmap.toImage().pixelColor(0, 0) == QColor('red')
        grid._requeue_visible_thumbnail_jobs()
        assert len(workers) == 2
        workers[-1].signals.cached_ready.emit(path, image, (10, 1920, 1080, 60, 1, 1))
        workers[-1].signals.diagnostic_finished.emit(0, 1, True)
        assert not card._thumbnail_metadata_pending and card.duration_label.text() == '0:10'
    finally:
        grid.shutdown()


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
    # Initial materialization is budgeted too; drain the existing coalesced
    # timer path instead of allowing one unbounded callback after scrolling.
    for _ in range(len(paths) + 2):
        grid._virtual_update_timer.stop()
        grid._update_virtualized_cards()
        if set(grid._thumb_widgets) == set(paths):
            break
    assert set(grid._thumb_widgets) == set(paths)
    grid.shutdown()


def test_resize_virtual_conversion_adopts_active_rename_card(
        qapp, qtbot, tmp_path, monkeypatch):
    """A resize must preserve an active rename editor and its focus."""
    from PySide6.QtWidgets import QScrollArea

    grid = make_grid(tmp_path, monkeypatch)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *_: None)
    paths = []
    for index in range(20):
        path = tmp_path / f'clip-{index:02}.mp4'
        path.touch()
        paths.append(str(path))

    grid._add_section('date', paths, 0)
    grid._records = {path: None for path in paths}
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.resize(1100, 600)
    qtbot.addWidget(scroll)
    scroll.show()
    qtbot.wait(30)

    editing = grid._thumb_widgets[paths[0]]
    editing._begin_rename()
    editing.title_edit.setText('draft survives resize')
    editing.title_edit.setFocus()
    assert editing.title_edit.hasFocus()
    old_parent = editing.parentWidget()
    old_generation = editing._library_generation

    monkeypatch.setattr(grid, '_virtual_card_budget', lambda *_: 10)
    grid._virtual_update_timer.stop()
    grid._update_virtualized_cards()
    qapp.processEvents()

    assert not grid._section_grids
    assert all(grid._thumb_widgets[path] is not None for path in paths[:3])
    assert editing.parentWidget() is old_parent
    assert editing._editing_name
    assert editing.title_edit.text() == 'draft survives resize'
    assert editing.title_edit.hasFocus()
    assert editing._library_generation is old_generation
    editing._cancel_rename()
    grid.shutdown()


def test_resize_virtual_conversion_adopts_mutating_popup_card(
        qapp, qtbot, tmp_path, monkeypatch):
    """An active mutation and its popup survive eager-to-virtual conversion."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu, QScrollArea, QApplication

    grid = make_grid(tmp_path, monkeypatch)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *_: None)
    paths = []
    for index in range(20):
        path = tmp_path / f'clip-{index:02}.mp4'
        path.touch()
        paths.append(str(path))
    grid._add_section('date', paths, 0)
    grid._records = {path: None for path in paths}
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.resize(1100, 600)
    qtbot.addWidget(scroll)
    scroll.show()
    qtbot.wait(30)

    card = grid._thumb_widgets[paths[2]]
    token = object()
    card._mutation_busy = True
    card._mutation_token = token
    menu = QMenu(card)
    menu.addAction('Still open')
    menu.popup(card.mapToGlobal(QPoint(4, 4)))
    qtbot.wait(10)
    assert QApplication.activePopupWidget() is menu
    old_parent = card.parentWidget()

    monkeypatch.setattr(grid, '_virtual_card_budget', lambda *_: 10)
    grid._virtual_update_timer.stop()
    grid._update_virtualized_cards()
    qapp.processEvents()

    assert QApplication.activePopupWidget() is menu
    assert grid._thumb_widgets[paths[2]] is card
    assert card.parentWidget() is old_parent
    assert card._mutation_busy
    assert card._mutation_token is token
    point = card.geometry().center()
    hit = card.parentWidget().childAt(point)
    assert hit is card or card.isAncestorOf(hit)

    menu.close()
    card._mutation_busy = False
    grid.shutdown()


def test_virtual_target_budget_prioritizes_visible_paths_globally(
        qapp, qtbot, tmp_path, monkeypatch):
    """Overscan from one section cannot consume the 96-card global target."""
    from PySide6.QtWidgets import QScrollArea, QWidget
    from ui import clip_grid

    grid = make_grid(tmp_path, monkeypatch)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.resize(1100, 600)
    scroll.show()
    qtbot.wait(20)

    canvases = {key: QWidget(grid._sections_host) for key in ('old', 'new')}
    grid._virtual_sections = canvases
    grid._section_files = {key: [] for key in canvases}
    grid._records = {str(index): None for index in range(200)}
    visible = {
        'old': [(f'old-visible-{index}', 0, index) for index in range(2)],
        'new': [(f'new-visible-{index}', 0, index) for index in range(2)],
    }
    overscan = {
        'old': [(f'old-overscan-{index}', 1, index) for index in range(100)],
        'new': [(f'new-overscan-{index}', 1, index) for index in range(100)],
    }
    stale = [path for path, _row, _col in overscan['old'][:96]]
    grid._thumb_widgets = {path: object() for path in stale}
    grid._card_virtual_sections = {path: 'old' for path in stale}
    scheduled = []
    materialized = []
    positioned = []
    released = []

    def plan(key, _viewport, _row_height):
        return visible[key], overscan[key]

    monkeypatch.setattr(grid, '_virtual_section_plans', plan)
    monkeypatch.setattr(grid, '_set_virtual_canvas_height', lambda *_: None)
    def release(path, *, for_reuse):
        assert for_reuse
        released.append(path)
        grid._thumb_widgets.pop(path, None)
        grid._card_virtual_sections.pop(path, None)

    monkeypatch.setattr(grid, '_release_virtual_card', release)

    def materialize(key, wanted, **_kwargs):
        if _kwargs.get('create_missing', True):
            for item in wanted:
                path = item[0]
                scheduled.append((key, path))
                if path not in grid._thumb_widgets:
                    assert len(grid._thumb_widgets) < clip_grid._MAX_MATERIALIZED_CARDS
                    grid._thumb_widgets[path] = object()
                    materialized.append((key, path))
        else:
            positioned.extend((key, item[0]) for item in wanted)
        return True

    monkeypatch.setattr(grid, '_materialize_virtual_section', materialize)
    grid._virtual_update_timer.stop()
    grid._update_virtualized_cards()

    expected_visible = [item[0] for key in ('old', 'new') for item in visible[key]]
    assert [path for _key, path in scheduled] == expected_visible
    assert [path for _key, path in materialized] == expected_visible
    positioned_paths = [path for _key, path in positioned]
    assert len(positioned_paths) == clip_grid._MAX_MATERIALIZED_CARDS
    assert len(set(positioned_paths)) == len(positioned_paths)
    assert set(expected_visible).issubset(positioned_paths)
    assert released == stale[92:]
    assert not any(path.startswith('new-overscan') for _key, path in materialized[:4])

    grid.shutdown()


def test_scroll_reuses_complete_overscan_but_layout_changes_invalidate_it(
        qapp, tmp_path, monkeypatch):
    grid = make_grid(tmp_path, monkeypatch)
    value = 240
    grid._scroll_bar = SimpleNamespace(value=lambda: value)
    grid._scroll_plan = (200, 100)
    grid._virtual_update_timer.stop()
    grid._request_virtual_update(scroll_only=True)
    assert not grid._virtual_update_timer.isActive()
    value = 300
    grid._request_virtual_update(scroll_only=True)
    assert grid._virtual_update_timer.isActive() and grid._scroll_plan is None
    grid._virtual_update_timer.stop()
    grid._scroll_plan = (value, 100)
    grid._request_virtual_update()
    assert grid._virtual_update_timer.isActive() and grid._scroll_plan is None
    grid._scroll_bar = None
    grid.shutdown()
