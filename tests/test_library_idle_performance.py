"""Catch idle repaint loops and retain event-driven library updates."""
import os
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QScrollArea

from ui import clip_grid


@pytest.mark.parametrize('section_count', (1, 66))
def test_global_virtualization_is_progressive_and_visible_first(
        qtbot, monkeypatch, tmp_path, section_count):
    """Many small date groups must share the same bounded viewport budget."""
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1100, 600)
    scroll.setWidgetResizable(True)
    grid = clip_grid.ClipGrid(settings, autostart=False)
    scroll.setWidget(grid)
    scroll.show()
    qtbot.wait(20)

    now = time.time_ns()
    files = []
    records = {}
    for index in range(66):
        path = tmp_path / f'clip-{index:03}.mp4'
        path.touch()
        if section_count == 66:
            stamp = now - index * 86_400 * 1_000_000_000
            os.utime(path, ns=(stamp, stamp))
        raw = str(path)
        info = path.stat()
        identity = clip_grid.canonical_media_path(raw)
        records[identity] = clip_grid.LibraryRecord(
            raw, identity, (), False, 'video', int(info.st_size),
            int(info.st_mtime_ns), int(info.st_dev), int(info.st_ino))
        files.append(raw)
    grid._records = records
    grid._records_by_path = {record.path: record for record in records.values()}

    try:
        for start in range(0, len(files), 66 // section_count):
            group = files[start:start + 66 // section_count]
            grid._add_section(f'day-{start}', group, start)
        assert not grid._section_grids
        assert len(grid._virtual_sections) == section_count

        # Advance the real update path with a deterministic slow card build.
        # This proves one expired non-scrolling tick still makes progress.
        clock = [100.0]
        original_create = grid._create_thumbnail

        def slow_create(*args, **kwargs):
            card = original_create(*args, **kwargs)
            clock[0] += .010
            return card

        monkeypatch.setattr(grid, '_create_thumbnail', slow_create)
        monkeypatch.setattr(clip_grid.time, 'perf_counter', lambda: clock[0])
        grid._virtual_update_timer.stop()
        grid._update_virtualized_cards()
        assert grid._thumb_widgets
        assert len(grid._thumb_widgets) < len(files)
        assert grid._virtual_update_timer.isActive()

        bar = scroll.verticalScrollBar()
        for _ in range(160):
            grid._virtual_update_timer.stop()
            grid._update_virtualized_cards()
            if grid._virtual_initialized and not grid._virtual_update_timer.isActive():
                break
        assert grid._virtual_initialized
        assert len(grid._thumb_widgets) <= clip_grid._MAX_MATERIALIZED_CARDS
        viewport = grid._scroll_viewport()
        visible_paths = []
        for key in tuple(grid._virtual_sections):
            visible = grid._virtual_section_plan(
                key, viewport, include_overscan=False)
            visible_paths.extend(path for path, _, _ in visible)
        if visible_paths:
            assert all(path in grid._thumb_widgets for path in visible_paths)
        assert visible_paths

        bar.setValue(bar.maximum())
        grid._scrolling = False
        for _ in range(160):
            grid._virtual_update_timer.stop()
            grid._update_virtualized_cards()
            if not grid._virtual_update_timer.isActive():
                break
        visible_paths = []
        for key in tuple(grid._virtual_sections):
            visible = grid._virtual_section_plan(
                key, viewport, include_overscan=False)
            visible_paths.extend(path for path, _, _ in visible)
        if visible_paths:
            assert all(path in grid._thumb_widgets for path in visible_paths)
        assert visible_paths
    finally:
        grid.shutdown()


def _library(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    updates = []
    original = clip_grid.ClipGrid._update_virtualized_cards

    def counted(self, *args):
        updates.append(True)
        return original(self, *args)

    monkeypatch.setattr(clip_grid.ClipGrid, '_update_virtualized_cards', counted)
    for index in range(240):
        (tmp_path / f'clip-{index:03}.mp4').touch()
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1100, 650)
    scroll.setWidgetResizable(True)
    grid = clip_grid.ClipGrid(settings)
    scroll.setWidget(grid)
    scroll.show()
    qtbot.waitUntil(lambda: grid._active_scan_worker is None)
    grid.refresh_timer.stop()
    grid._debounce_timer.stop()
    grid._watcher.blockSignals(True)
    qtbot.wait(350)

    def _ready_for_idle_measurement():
        if (len(grid._records) != 240 or grid._active_scan_worker is not None
                or grid._pending_scan_request is not None or grid._resize_timer.isActive()
                or grid._virtual_update_timer.isActive() or grid._scrolling
                or not grid._virtual_initialized or not grid._virtual_sections):
            return False
        bottom = max(canvas.mapTo(grid, QPoint(0, canvas.height())).y()
                     for canvas in grid._virtual_sections.values())
        return (bottom + grid.layout.contentsMargins().bottom()
                <= scroll.verticalScrollBar().maximum() + scroll.viewport().height())

    # Measure idle work only after the initial scan and layout event chain.
    qtbot.waitUntil(_ready_for_idle_measurement)
    updates.clear()
    return grid, scroll, updates


def test_large_library_has_no_recurring_layout_work_while_idle(qtbot, monkeypatch, tmp_path):
    grid, _scroll, updates = _library(qtbot, monkeypatch, tmp_path)
    try:
        assert len(grid._records) == 240
        qtbot.wait(300)
        assert updates == []
        assert len(grid._thumb_widgets) <= clip_grid._MAX_MATERIALIZED_CARDS
    finally:
        grid.shutdown()


def test_hidden_library_trims_offscreen_images_and_recovers_on_resume(
        qtbot, monkeypatch, tmp_path):
    from PySide6.QtGui import QPixmap, QColor
    from PySide6.QtCore import QRect, QPoint
    grid, scroll, _updates = _library(qtbot, monkeypatch, tmp_path)
    try:
        thumbnail = QPixmap(320, 180)
        thumbnail.fill(QColor('teal'))
        visible, offscreen = [], []
        for card in grid._thumb_widgets.values():
            card._thumb_pixmap = QPixmap(thumbnail)
            card._thumbnail_ready = True
            card._render_thumbnail()
            bounds = QRect(card.mapTo(scroll.viewport(), QPoint()), card.size())
            (visible if bounds.intersects(scroll.viewport().rect()) else offscreen).append(card)
        assert visible and offscreen
        grid.set_background_paused(True)
        grid._trim_hidden_memory()
        assert all(not card._thumb_pixmap.isNull() for card in visible)
        assert all(card._thumb_pixmap.isNull() and not card._thumbnail_ready for card in offscreen)
        requests = []
        monkeypatch.setattr(grid, '_start_thumbnail_worker', requests.append)
        grid.set_background_paused(False)
        assert all(card.file_path in requests for card in offscreen)
    finally:
        grid.shutdown()


def test_scroll_burst_updates_final_view_and_then_settles(qtbot, monkeypatch, tmp_path):
    grid, scroll, updates = _library(qtbot, monkeypatch, tmp_path)
    try:
        before = set(grid._thumb_widgets)
        bar = scroll.verticalScrollBar()
        for step in range(1, 41):
            bar.setValue(bar.maximum() * step // 40)
        qtbot.wait(100)
        qtbot.waitUntil(lambda: not grid._virtual_update_timer.isActive()
                       and not grid._scrolling)
        # Input is coalesced, then new card creation may yield across frames.
        # It must finish with bounded work and return to zero idle updates.
        assert 1 <= len(updates) <= len(grid._thumb_widgets) + 1
        assert set(grid._thumb_widgets) != before
        assert len(grid._thumb_widgets) <= clip_grid._MAX_MATERIALIZED_CARDS
        updates.clear()
        qtbot.wait(150)
        assert updates == []
        bar.setValue(0)
        qtbot.wait(100)
        qtbot.waitUntil(lambda: not grid._virtual_update_timer.isActive()
                       and not grid._scrolling)
        assert set(grid._thumb_widgets) == before
    finally:
        grid.shutdown()


def test_paused_library_cancels_pending_layout_and_resumes_once(qtbot, monkeypatch, tmp_path):
    grid, scroll, updates = _library(qtbot, monkeypatch, tmp_path)
    try:
        grid._request_virtual_update()
        grid.set_background_paused(True)
        assert not grid._virtual_update_timer.isActive()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        qtbot.wait(100)
        assert updates == []
        grid.set_background_paused(False)
        qtbot.wait(100)
        # Returning to a new viewport may span several coalesced 4 ms ticks;
        # every callback must still make materialization progress.
        assert 1 <= len(updates) <= len(grid._thumb_widgets)
        updates.clear()
        qtbot.waitUntil(lambda: grid._virtual_initialized
                        and not grid._virtual_update_timer.isActive()
                        and not grid._scrolling, timeout=2000)
        updates.clear()
        qtbot.wait(150)
        assert updates == []
    finally:
        grid.shutdown()


def test_repeated_show_and_parent_attach_delivers_each_scroll_once_and_detaches_old_bar(
        qtbot, monkeypatch, tmp_path):
    calls = []
    target = {'grid': None}
    original = clip_grid.ClipGrid._on_library_scrolled

    def counted(self, *args):
        if self is target['grid']:
            calls.append(args[0])
        return original(self, *args)

    monkeypatch.setattr(clip_grid.ClipGrid, '_on_library_scrolled', counted)
    grid, first_scroll, updates = _library(qtbot, monkeypatch, tmp_path)
    target['grid'] = grid

    def settled():
        return (
            not grid._virtual_update_timer.isActive()
            and not grid._resize_timer.isActive()
            and not grid._scrolling
        )

    try:
        for _ in range(3):
            first_scroll.hide()
            first_scroll.show()
            grid.hide()
            grid.show()
            qtbot.waitUntil(settled)
        first_bar = first_scroll.verticalScrollBar()
        first_emissions = []
        first_bar.valueChanged.connect(first_emissions.append)
        calls.clear()
        updates.clear()
        before = set(grid._thumb_widgets)
        first_bar.setValue(first_bar.maximum())
        qtbot.waitUntil(settled)
        assert first_emissions and calls == first_emissions
        assert updates and set(grid._thumb_widgets) != before

        second_scroll = QScrollArea()
        qtbot.addWidget(second_scroll)
        second_scroll.resize(1100, 650)
        second_scroll.setWidgetResizable(True)
        assert first_scroll.takeWidget() is grid
        second_scroll.setWidget(grid)
        second_scroll.show()
        qtbot.waitUntil(lambda: second_scroll.verticalScrollBar().maximum() > 0)
        qtbot.waitUntil(settled)
        calls.clear()
        updates.clear()
        first_bar.setRange(0, 500)
        first_bar.setValue(250)
        qtbot.wait(120)
        assert calls == [] and updates == []

        second_bar = second_scroll.verticalScrollBar()
        second_bar.setValue(0)
        qtbot.waitUntil(settled)
        second_emissions = []
        second_bar.valueChanged.connect(second_emissions.append)
        calls.clear()
        updates.clear()
        second_bar.setValue(second_bar.maximum())
        qtbot.waitUntil(settled)
        assert second_emissions and calls == second_emissions
        assert updates
    finally:
        grid.shutdown()


def test_scroll_attach_before_recovery_does_not_start_scans_or_watchers(qtbot, monkeypatch, tmp_path):
    calls, updates, scans = [], [], []
    on_scroll = clip_grid.ClipGrid._on_library_scrolled
    update = clip_grid.ClipGrid._update_virtualized_cards

    def counted_scroll(self, *args):
        calls.append(args[0])
        return on_scroll(self, *args)

    def counted_update(self, *args):
        updates.append(True)
        return update(self, *args)

    monkeypatch.setattr(clip_grid.ClipGrid, '_on_library_scrolled', counted_scroll)
    monkeypatch.setattr(clip_grid.ClipGrid, '_update_virtualized_cards', counted_update)
    monkeypatch.setattr(clip_grid.ClipGrid, '_begin_scan', lambda *args: scans.append(args))
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    grid = clip_grid.ClipGrid(settings, autostart=False)
    grid.setFixedHeight(1500)
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1100, 600)
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.show()
    try:
        scroll.hide()
        scroll.show()
        qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)
        qtbot.waitUntil(lambda: not grid._virtual_update_timer.isActive())
        emissions = []
        bar = scroll.verticalScrollBar()
        bar.valueChanged.connect(emissions.append)
        calls.clear()
        updates.clear()
        bar.setValue(bar.maximum())
        qtbot.waitUntil(lambda: bool(calls) and not grid._scrolling
                       and not grid._virtual_update_timer.isActive())
        assert emissions and calls == emissions
        assert updates
        grid._on_filter_changed(2)
        grid._on_sort_changed(1)
        grid.set_background_paused(True)
        grid.set_background_paused(False)
        assert not grid._scanning_started
        assert scans == []
        assert not grid.refresh_timer.isActive()
        assert not grid._watcher.directories()
    finally:
        grid.shutdown()
