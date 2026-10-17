"""Catch idle repaint loops and retain event-driven library updates."""
from types import SimpleNamespace

from PySide6.QtWidgets import QScrollArea

from ui import clip_grid


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
        assert 1 <= len(updates) <= 2
        updates.clear()
        qtbot.wait(150)
        assert updates == []
    finally:
        grid.shutdown()
