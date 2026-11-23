"""Library toolbar, menu hierarchy and complete selection outline contracts."""
from pathlib import Path
from types import SimpleNamespace
import os

import pytest
from PySide6.QtCore import Qt, QPoint, QRect
from PySide6.QtGui import QColor, QFontDatabase, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QScrollArea, QMenu, QVBoxLayout, QWidget
from ui import clip_grid


@pytest.fixture
def toolbar_grid(qapp, qtbot, tmp_path, monkeypatch):
    for font in (Path(clip_grid.__file__).resolve().parents[1] / 'assets' / 'fonts').glob('*.ttf'):
        QFontDatabase.addApplicationFont(str(font))
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    grid = clip_grid.ClipGrid(settings, autostart=False, toolbar_layout=layout)
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setWidget(grid)
    layout.addWidget(area, 1)
    qtbot.addWidget(page)
    paths = []
    for index in range(30):
        path = tmp_path / 'Arena' / f'Match {index + 1}.mp4'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'synthetic')
        paths.append(str(path))
    grid._replace_records(grid._records_from_paths(set(paths)))
    grid._apply_current_index()
    page.resize(1100, 720)
    page.show()
    qtbot.waitUntil(lambda: bool(grid._thumb_widgets))
    for card in grid._thumb_widgets.values():
        image = QPixmap(480, 270)
        image.fill(QColor('#234754'))
        card._thumb_pixmap = image
        card.set_source_duration(42)
        card.metadata_label.setText('1920 × 1080 · 60 fps')
        card._render_thumbnail()
    yield SimpleNamespace(grid=grid, area=area, page=page, paths=paths)
    grid.shutdown()


def test_render_toolbar(toolbar_grid, qtbot):
    state = toolbar_grid
    prefix = os.environ.get('FTHR_TOOLBAR_SCREENSHOTS')
    for width in (800, 1100, 1500):
        state.page.resize(width, 720)
        qtbot.wait(300)
        if prefix:
            assert state.page.grab().save(f'{prefix}-{width}.png')
    first = next(iter(state.grid._thumb_widgets))
    state.grid._select_card(first, Qt.KeyboardModifier.NoModifier, checkbox=True)
    qtbot.wait(30)
    if prefix:
        card = state.grid._thumb_widgets[first]
        assert card.grab().save(f'{prefix}-selected.png')
        state.page.resize(800, 720)
        qtbot.wait(250)
        assert state.page.grab().save(f'{prefix}-selecting-800.png')


@pytest.mark.parametrize('width', [800, 1100, 1500])
def test_single_row_fits_and_done_exits(toolbar_grid, qtbot, width):
    grid, area = toolbar_grid.grid, toolbar_grid.area
    toolbar_grid.page.resize(width, 720)
    qtbot.wait(100)
    grid._set_selection_mode(True)
    assert grid.select_mode_btn.text() == 'Done'
    controls = (grid._all_count_label, grid.search_edit, grid.reset_search_btn,
                grid.filter_combo, grid.sort_combo, grid.select_mode_btn)
    header = grid._library_toolbar
    rects = [QRect(w.mapTo(header, QPoint()), w.size()) for w in controls]
    assert all(header.rect().contains(rect) for rect in rects)
    assert all(not left.intersects(right) for i, left in enumerate(rects) for right in rects[i + 1:])
    assert len({rect.center().y() for rect in rects}) <= 2
    assert grid.search_edit.width() >= 120
    assert area.horizontalScrollBar().maximum() == 0
    for widget in (grid.tags_filter_btn, grid.favorites_only_check,
                   grid.saved_search_combo, grid.saved_search_manage_btn):
        assert widget.isHidden() and widget.parent() is grid
    grid.select_all_results()
    assert grid.selected_records
    grid.select_mode_btn.setFocus()
    qtbot.keyClick(grid.select_mode_btn, Qt.Key.Key_Space)
    assert not grid._selection_mode and not grid.selected_records
    assert grid.selection_toolbar.isHidden()
    assert all(card.selection_checkbox.isHidden() for card in grid._thumb_widgets.values())


def test_search_and_selection_actions_stay_visible_at_bottom(toolbar_grid, qtbot):
    state = toolbar_grid
    grid, area, page = state.grid, state.area, state.page
    grid.select_all_results()
    QApplication.processEvents()
    qtbot.waitUntil(lambda: area.verticalScrollBar().maximum() > 0)
    controls = (grid.search_edit, grid.selection_count_label, grid.bulk_actions_btn)
    positions = [control.mapTo(page, QPoint()) for control in controls]
    bar = area.verticalScrollBar()
    bar.setValue(bar.maximum())
    viewport = area.viewport()
    qtbot.waitUntil(lambda: grid.y() < 0 and any(
        card.isVisible() and viewport.rect().intersects(
            QRect(card.mapTo(viewport, QPoint()), card.size()))
        for card in grid._thumb_widgets.values()))
    assert all(control.isVisible() for control in controls)
    assert [control.mapTo(page, QPoint()) for control in controls] == positions
    assert positions[-1].y() + controls[-1].height() <= area.y()
    assert grid.selection_count_label.text() == '30 selected'
    prefix = os.environ.get('FTHR_TOOLBAR_SCREENSHOTS')
    if prefix:
        assert page.grab().save(f'{prefix}-scrolled.png')
    grid.clear_selection_btn.setFocus()
    qtbot.keyClick(grid.clear_selection_btn, Qt.Key.Key_Escape)
    assert not grid.selection_toolbar.isVisible()
    assert grid.search_edit.isVisible()


def test_selected_outline_paints_all_edges_and_recycle_clears(toolbar_grid):
    grid = toolbar_grid.grid
    card = next(iter(grid._thumb_widgets.values()))
    size = card.size()
    card.set_selection_state(True, True)
    image = card.grab().toImage()
    accent = QColor(clip_grid.Colors.ACCENT)
    w, h = card.width(), card.height()
    for x, y in ((w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)):
        assert image.pixelColor(x, y) == accent
    assert card._selection_outline.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert card.size() == size
    card.resize_card(300, force=True)
    assert card._selection_outline.geometry() == card.rect()
    card.rebind('other.mp4', imported=False, upload_enabled=False, uploaded=False,
                upload_info=None, ready=True, card_width=300)
    assert card._selection_outline.isHidden() and not card.property('selected')


def test_tags_nested_after_games_preserve_game_and_media(toolbar_grid, monkeypatch):
    grid = toolbar_grid.grid
    monkeypatch.setattr(grid, '_apply_query_index', lambda: None)
    grid._library_details_cache = {'one': {'tags': ('highlight', 'ranked')}}
    grid._on_game_filter_changed('Arena')
    game = grid._game_filter
    calls = []
    grid.filter_combo.game_selected.connect(calls.append)
    class Menu(QMenu):
        def exec(self, *_):
            actions = self.actions()
            labels = [a.text() for a in actions]
            games_index = labels.index('Games')
            tag_action = actions[games_index + 1]
            assert tag_action.text() == 'Tags'
            tags = tag_action.menu()
            tags.aboutToShow.emit()
            chosen = next(a for a in tags.actions() if a.data() == 'highlight')
            chosen.trigger()
            return chosen
    monkeypatch.setattr(clip_grid, 'QMenu', Menu)
    grid.filter_combo.showPopup()
    assert grid._game_filter == game and calls == []
    assert grid._search_tags == ('highlight',)
    assert grid.filter_combo.currentText().startswith('1 tag')
    grid._refresh_tag_filter_menu()
    tags = grid._tag_filter_menu
    assert next(a for a in tags.actions() if a.data() == 'highlight').isChecked()
    second = next(a for a in tags.actions() if a.data() == 'ranked')
    second.trigger()
    assert grid._search_tags == ('highlight', 'ranked')
    clear = next(a for a in tags.actions() if a.text() == 'Clear tags')
    clear.trigger()
    assert grid._search_tags == () and grid._game_filter == game


def test_hidden_favorite_criteria_visible_and_reset_clears(toolbar_grid):
    from core.library_searches import SearchCriteria
    grid = toolbar_grid.grid
    grid.set_search_criteria(SearchCriteria(text='Match', tags_all=('ranked',),
                                          kind='clips', favorites_only=True, sort='oldest'))
    assert 'Favorites' in grid.filter_combo.toolTip()
    assert grid.filter_combo.currentText().startswith('1 tag')
    grid._reset_search()
    assert grid.current_search_criteria() == SearchCriteria()
    assert grid._game_filter is None and not grid._favorites_only
    assert not grid.search_edit.text() and not grid._search_tags


def test_render_filter_menu(toolbar_grid, qtbot, monkeypatch):
    grid = toolbar_grid.grid
    prefix = os.environ.get('FTHR_TOOLBAR_SCREENSHOTS')
    grid._library_details_cache = {'one': {'tags': ('highlight', 'ranked')}}
    monkeypatch.setattr(grid, '_apply_query_index', lambda: None)
    grid._on_game_filter_changed('Arena')
    grid.set_search_tags(('ranked',))
    class Menu(QMenu):
        def exec(self, position):
            self.popup(position)
            qtbot.wait(30)
            if prefix:
                assert self.grab().save(f'{prefix}-filter-menu.png')
            tags = next(a.menu() for a in self.actions() if a.text().startswith('Tags'))
            tags.popup(position + QPoint(self.width(), 0))
            qtbot.wait(30)
            if prefix:
                assert tags.grab().save(f'{prefix}-tags-menu.png')
                assert toolbar_grid.page.grab().save(f'{prefix}-active-tags.png')
            tags.hide()
            self.hide()
            return None
    monkeypatch.setattr(clip_grid, 'QMenu', Menu)
    grid.filter_combo.showPopup()
