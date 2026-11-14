"""Album drag gestures retain central selection and use guarded logical copies."""
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QEvent, QMimeData, QUrl
from PySide6.QtGui import QMouseEvent
from ui import clip_grid
from ui.library_album_sidebar import AlbumSidebar
from ui.library_album_drag import AlbumDragMimeData

pytest_plugins = ('test_library_multiselect',)


def test_control_click_enters_selection_without_opening(selection_library, qtbot):
    grid = selection_library.grid
    card = next(iter(grid._thumb_widgets.values()))
    clicked = []
    card.clicked.connect(clicked.append)
    qtbot.mouseClick(card._thumb_frame, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    assert grid.selected_records == (grid._record_for_path(card.file_path),)
    assert grid.select_mode_btn.text() == 'Done selecting'
    assert clicked == []


def test_press_does_not_open_or_toggle_before_release(selection_library, qtbot):
    grid = selection_library.grid
    card = next(iter(grid._thumb_widgets.values()))
    clicked = []
    card.clicked.connect(clicked.append)
    qtbot.mousePress(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(20, 30))
    assert clicked == []
    qtbot.mouseRelease(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(20, 30))
    assert clicked == [card.file_path]
    grid._set_selection_mode(True)
    grid._select_card(card.file_path, Qt.KeyboardModifier.NoModifier)
    before = grid.selected_records
    qtbot.mousePress(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(20, 30))
    assert grid.selected_records == before
    qtbot.mouseRelease(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(20, 30))
    assert grid.selected_records == ()


def test_selected_drag_uses_central_snapshot_and_cancellation_keeps_selection(
        selection_library, monkeypatch):
    grid = selection_library.grid
    records = tuple(grid._filtered_records())
    grid.select_all_results()
    before = grid.selected_records
    observed = []

    class Drag(clip_grid.QDrag):
        def __init__(self, parent):
            super().__init__(parent)
            assert parent is grid
        def setMimeData(self, mime):
            self.mime = mime
        def setPixmap(self, pixmap):
            assert not pixmap.isNull()
        def setHotSpot(self, point):
            pass
        def exec(self, actions, default):
            assert actions == default == Qt.DropAction.CopyAction
            observed.append(grid._album_drag_lease[1].records)
            return Qt.DropAction.IgnoreAction
        def deleteLater(self):
            pass

    monkeypatch.setattr(clip_grid, 'QDrag', Drag, raising=False)
    grid._start_album_drag(records[0].path, records[0])
    assert observed == [before]
    assert grid.selected_records == before
    assert grid._album_drag_lease is None


def _during_drag(monkeypatch, grid, callback):
    class Drag(clip_grid.QDrag):
        def exec(self, actions, default):
            assert actions == default == Qt.DropAction.CopyAction
            callback(self.mimeData())
            return Qt.DropAction.IgnoreAction
    monkeypatch.setattr(clip_grid, 'QDrag', Drag)


def _album(grid, manager):
    manager.migrate(target_version=4)
    album = manager.create_album('Drop target')
    grid._refresh_album_catalog()
    return album.album_id


@pytest.mark.parametrize('selected', [False, True])
def test_drop_adds_exact_immutable_records_idempotently_without_media_changes(
        selection_library, monkeypatch, qtbot, selected):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    ordered = tuple(grid._filtered_records())
    grid.select_all_results()
    if not selected:
        grid._select_card(ordered[0].path, Qt.KeyboardModifier.ControlModifier)
    before = grid.selected_records
    expected = before if selected else (ordered[0],)
    from pathlib import Path
    original = {r.path: Path(r.path).read_bytes() for r in ordered}
    summaries = []
    grid.bulk_albums_finished.connect(summaries.append)

    def drop(mime):
        assert mime.urls() == []
        assert grid.can_drop_album_records(album_id, mime, grid)
        assert grid.drop_album_records(album_id, mime, grid)
        assert grid._bulk_job['records'] == expected
    for _index in range(2):
        with monkeypatch.context() as patch:
            _during_drag(patch, grid, drop)
            grid._start_album_drag(ordered[0].path, ordered[0])
        qtbot.waitUntil(lambda: grid._bulk_job is None)
        assert grid.selected_records == before
    assert set(summaries[0]['changed']) == {r.path for r in expected}
    assert summaries[0]['failed'] == ()
    assert summaries[1]['changed'] == ()
    assert set(summaries[1]['unchanged']) == {r.path for r in expected}
    assert {r.path: Path(r.path).read_bytes() for r in ordered} == original


@pytest.mark.parametrize('change', [
    'forged', 'urls', 'wrong_source', 'missing_album', 'catalog', 'record',
    'query', 'revision', 'mutation', 'locked', 'shutdown', 'not_ready',
])
def test_invalid_drop_is_rejected_without_membership_write(
        selection_library, monkeypatch, change):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    record = tuple(grid._filtered_records())[0]
    writes = []
    monkeypatch.setattr(grid, 'start_bulk_album_membership', lambda *a, **k: writes.append((a, k)))

    def check(mime):
        source, target = grid, album_id
        if change == 'forged':
            mime = AlbumDragMimeData()
        elif change == 'urls':
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(record.path)])
        elif change == 'wrong_source':
            source = grid.parentWidget()
        elif change == 'missing_album':
            target = 'missing'
        elif change == 'catalog':
            grid._albums = ()
        elif change == 'record':
            grid._records_by_path[record.path] = replace(record, st_ino=record.st_ino + 1)
        elif change == 'query':
            grid._search_criteria = replace(grid._search_criteria, text='different')
        elif change == 'revision':
            grid._album_drag_revision += 1
        elif change == 'mutation':
            grid._mutation_paths.add(record.path)
        elif change == 'locked':
            grid._bulk_job = {'kind': 'test'}
        elif change == 'shutdown':
            grid._shutdown_started = True
        elif change == 'not_ready':
            grid._thumb_widgets[record.path].set_ready(False)
        assert not grid.can_drop_album_records(target, mime, source)
        assert not grid.drop_album_records(target, mime, source)

    _during_drag(monkeypatch, grid, check)
    try:
        grid._start_album_drag(record.path, record)
        assert writes == []
    finally:
        grid._bulk_job = None
        grid._mutation_paths.clear()
        grid._shutdown_started = False


def test_gesture_threshold_cancelled_drag_never_opens_or_deselects(
        selection_library, qtbot, monkeypatch):
    grid = selection_library.grid
    card = next(iter(grid._thumb_widgets.values()))
    grid.select_all_results()
    before = grid.selected_records
    opened, drags = [], []
    card.clicked.connect(opened.append)
    monkeypatch.setattr(clip_grid.QApplication, 'startDragDistance', staticmethod(lambda: 13))
    _during_drag(monkeypatch, grid, lambda mime: drags.append(grid._album_drag_lease[1].records))
    start = QPoint(40, 40)
    qtbot.mousePress(card._thumb_frame, Qt.MouseButton.LeftButton, pos=start)
    for distance in (12, 13):
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(start + QPoint(distance, 0)),
                            QPointF(start + QPoint(distance, 0)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        clip_grid.QApplication.sendEvent(card._thumb_frame, event)
        assert len(drags) == (0 if distance == 12 else 1)
        assert grid.selected_records == before
    qtbot.mouseRelease(card._thumb_frame, Qt.MouseButton.LeftButton, pos=start)
    assert drags == [before]
    assert opened == []
    assert grid.selected_records == before


@pytest.mark.parametrize('operation', ['release', 'drag'])
def test_recycled_card_cannot_activate_old_press(selection_library, qtbot, monkeypatch, operation):
    grid = selection_library.grid
    cards = tuple(grid._thumb_widgets.values())
    card, other = cards[0], cards[1]
    opened, drags = [], []
    card.clicked.connect(opened.append)
    card._album_drag_handler = lambda *args: drags.append(args)
    qtbot.mousePress(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
    card.rebind(other.file_path, imported=other.imported, upload_enabled=False,
                uploaded=False, upload_info=None, ready=True, card_width=300)
    if operation == 'drag':
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(100, 40), QPointF(100, 40),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        clip_grid.QApplication.sendEvent(card._thumb_frame, event)
    qtbot.mouseRelease(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
    assert opened == drags == []
    assert grid.selected_records == ()


class DropEvent:
    def __init__(self, mime, source, actions=Qt.DropAction.CopyAction):
        self.mime, self.origin, self.actions = mime, source, actions
        self.accepted = False
        self.action = None
    def mimeData(self):
        return self.mime
    def source(self):
        return self.origin
    def possibleActions(self):
        return self.actions
    def setDropAction(self, action):
        self.action = action
    def accept(self):
        self.accepted = True
    def ignore(self):
        self.accepted = False


def test_sidebar_highlights_only_valid_target_and_drop_does_not_navigate(
        selection_library, qtbot, monkeypatch):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    sidebar = AlbumSidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_state(grid.album_navigation_state())
    sidebar.set_drop_handlers(grid.can_drop_album_records, grid.drop_album_records)
    grid.album_drag_finished.connect(sidebar.clear_drop_targets)
    button = sidebar.button_for_album(album_id)
    activated = []
    sidebar.album_activated.connect(activated.append)
    record = tuple(grid._filtered_records())[0]

    def drop(mime):
        event = DropEvent(mime, grid)
        button.dragEnterEvent(event)
        assert event.accepted and button.property('albumDropTarget')
        assert not button.isChecked()
        button.dragLeaveEvent(event)
        assert not button.property('albumDropTarget')
        button.dragMoveEvent(event)
        assert button.property('albumDropTarget')
        button.dropEvent(event)
        assert event.accepted and event.action == Qt.DropAction.CopyAction
        assert not button.property('albumDropTarget')
        assert activated == []
    _during_drag(monkeypatch, grid, drop)
    grid._start_album_drag(record.path, record)
    qtbot.waitUntil(lambda: grid._bulk_job is None)
    assert not button.property('albumDropTarget')


@pytest.mark.parametrize('change', ['end', 'locked', 'removed', 'invalid', 'move_only'])
def test_sidebar_clears_highlight_on_state_change_or_cancellation(
        selection_library, qtbot, monkeypatch, change):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    sidebar = AlbumSidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_state(grid.album_navigation_state())
    sidebar.set_drop_handlers(grid.can_drop_album_records, grid.drop_album_records)
    grid.album_drag_finished.connect(sidebar.clear_drop_targets)
    button = sidebar.button_for_album(album_id)
    record = tuple(grid._filtered_records())[0]
    writes = []
    monkeypatch.setattr(grid, 'start_bulk_album_membership', lambda *a, **k: writes.append(True))

    def check(mime):
        event = DropEvent(mime, grid)
        button.dragEnterEvent(event)
        assert button.property('albumDropTarget')
        if change == 'end':
            return
        if change in ('locked', 'removed'):
            state = grid.album_navigation_state()
            if change == 'locked':
                state['locked'] = True
            else:
                state['albums'] = ()
            sidebar.set_state(state)
        else:
            if change == 'invalid':
                event.mime = QMimeData()
            else:
                event.actions = Qt.DropAction.MoveAction
            button.dragMoveEvent(event)
        assert not button.property('albumDropTarget')
        button.dropEvent(event)
        assert not event.accepted
    _during_drag(monkeypatch, grid, check)
    grid._start_album_drag(record.path, record)
    assert not button.property('albumDropTarget')
    assert writes == []


@pytest.mark.parametrize('selection_library', [1000], indirect=True)
def test_offscreen_selection_drag_hover_is_ram_only(selection_library, monkeypatch):
    import builtins
    import os
    from pathlib import Path

    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    grid.select_all_results()
    expected = grid.selected_records
    assert len(expected) == 1000
    assert len(grid._thumb_widgets) <= 96
    record = expected[0]

    def forbidden(*args, **kwargs):
        pytest.fail('Drag hover must use the published RAM snapshot only')

    def check(mime):
        assert grid._album_drag_lease[1].records == expected
        with monkeypatch.context() as guard:
            guard.setattr(builtins, 'open', forbidden)
            guard.setattr(os, 'stat', forbidden)
            guard.setattr(Path, 'stat', forbidden)
            guard.setattr(grid, '_filtered_records', forbidden)
            guard.setattr(grid, '_readiness_checker', forbidden)
            guard.setattr(manager, 'album_bindings', forbidden)
            assert grid.can_drop_album_records(album_id, mime, grid)
        assert grid.selected_records == expected
    _during_drag(monkeypatch, grid, check)
    grid._start_album_drag(record.path, record)
    assert grid.selected_records == expected


def test_filter_roundtrip_invalidates_old_lease(selection_library, monkeypatch):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    record = tuple(grid._filtered_records())[0]
    def check(mime):
        assert grid.can_drop_album_records(album_id, mime, grid)
        grid._apply_query_index()
        assert not grid.can_drop_album_records(album_id, mime, grid)
    _during_drag(monkeypatch, grid, check)
    grid._start_album_drag(record.path, record)


def test_any_pending_mutation_rejects_hover_without_resolving_paths(selection_library, monkeypatch):
    import os
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    first, unrelated = tuple(grid._filtered_records())[:2]
    def forbidden(*args, **kwargs):
        pytest.fail('Pending mutation refusal must not resolve filesystem paths')
    def check(mime):
        grid._mutation_paths.add(unrelated.path)
        with monkeypatch.context() as guard:
            guard.setattr(clip_grid, 'canonical_media_path', forbidden)
            guard.setattr(os.path, 'realpath', forbidden)
            assert not grid.can_drop_album_records(album_id, mime, grid)
    _during_drag(monkeypatch, grid, check)
    try:
        grid._start_album_drag(first.path, first)
    finally:
        grid._mutation_paths.clear()


def test_repeated_valid_hover_keeps_highlight_without_style_rebuild(
        selection_library, qtbot, monkeypatch):
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    sidebar = AlbumSidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_state(grid.album_navigation_state())
    sidebar.set_drop_handlers(grid.can_drop_album_records, grid.drop_album_records)
    button = sidebar.button_for_album(album_id)
    style = button.style()
    polish, unpolish = style.polish, style.unpolish
    calls = []
    def track_polish(widget):
        if widget is button:
            calls.append('polish')
        return polish(widget)
    def track_unpolish(widget):
        if widget is button:
            calls.append('unpolish')
        return unpolish(widget)
    monkeypatch.setattr(style, 'polish', track_polish)
    monkeypatch.setattr(style, 'unpolish', track_unpolish)
    record = tuple(grid._filtered_records())[0]
    def check(mime):
        event = DropEvent(mime, grid)
        button.dragEnterEvent(event)
        initial = tuple(calls)
        assert initial == ('unpolish', 'polish')
        for _ in range(10):
            button.dragMoveEvent(event)
            assert event.accepted and button.property('albumDropTarget')
        assert tuple(calls) == initial
    _during_drag(monkeypatch, grid, check)
    grid._start_album_drag(record.path, record)


@pytest.mark.parametrize('page,viewer', [(1, None), (2, None), (0, object()), (0, None)])
def test_main_drop_guard_does_not_change_page_or_editor(
        selection_library, monkeypatch, page, viewer):
    from main import MainWindow
    grid, manager = selection_library.grid, selection_library.manager
    album_id = _album(grid, manager)
    host = SimpleNamespace(clip_grid=grid, main_stack=SimpleNamespace(currentIndex=lambda: page),
                           _active_clip_viewer=viewer)
    host._can_drop_album_clips = MethodType(MainWindow._can_drop_album_clips, host)
    record = tuple(grid._filtered_records())[0]
    def check(mime):
        assert MainWindow._drop_album_clips(host, album_id, mime, grid) is (page == 0 and viewer is None)
        assert host.main_stack.currentIndex() == page
        assert host._active_clip_viewer is viewer
    _during_drag(monkeypatch, grid, check)
    grid._start_album_drag(record.path, record)
