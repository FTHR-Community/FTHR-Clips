"""Folder-sidebar behavior backed by the grid's album UUID snapshot."""
from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from main import MainWindow
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QButtonGroup, QToolButton
from ui import clip_grid
from ui import library_album_sidebar

pytest_plugins = ("test_library_tag_grid",)


@pytest.fixture(autouse=True)
def _register_application_fonts(qapp):
    font_dir = Path(clip_grid.__file__).resolve().parents[1] / "assets" / "fonts"
    if font_dir.is_dir() and not any(
            "Oswald" in family for family in QFontDatabase.families()):
        for font_path in sorted(font_dir.glob("Oswald-*.ttf")):
            QFontDatabase.addApplicationFont(str(font_path))


@pytest.fixture
def album_sidebar(qtbot):
    group = QButtonGroup()
    group.setExclusive(True)
    clips_button = QToolButton()
    clips_button.setCheckable(True)
    group.addButton(clips_button, 0)
    sidebar = library_album_sidebar.AlbumSidebar(navigation_group=group)
    qtbot.addWidget(sidebar)
    sidebar.resize(220, 720)
    sidebar.show()
    qtbot.waitUntil(sidebar.isVisible)
    yield SimpleNamespace(sidebar=sidebar, group=group, clips_button=clips_button)


def _sidebar_state(albums, *, selected_id=None, locked=False, unavailable=None):
    return {
        "albums": tuple((str(album_id), str(name)) for album_id, name in albums),
        "selected_id": selected_id,
        "locked": locked,
        "unavailable": unavailable,
    }


def _publish_grid_catalog(grid):
    grid._refresh_album_catalog()
    return grid.album_navigation_state()


def test_folder_click_and_plus_emit_stable_uuid_commands(album_sidebar, qtbot):
    state = album_sidebar
    sidebar = state.sidebar
    first, second = "album-first", "album-second"
    sidebar.set_state(_sidebar_state(((first, "First folder"),
                                      (second, "Second folder")),
                                     selected_id=first))
    selected = []
    create = []
    sidebar.album_activated.connect(selected.append)
    sidebar.create_requested.connect(lambda: create.append(True))

    assert sidebar.button_for_album(first).isChecked()
    assert not state.clips_button.isChecked()
    sidebar.button_for_album(second).click()
    sidebar.create_button.click()

    assert selected == [second]
    assert create == [True]
    assert sidebar.button_for_album(second).isChecked()
    assert not sidebar.button_for_album(first).isChecked()
    assert not state.clips_button.isChecked()


def test_passive_state_updates_reuse_folder_button_by_uuid(album_sidebar):
    sidebar = album_sidebar.sidebar
    first, second = "stable-first", "stable-second"
    sidebar.set_state(_sidebar_state(((first, "Old name"), (second, "Second")),
                                    selected_id=first))
    original = sidebar.button_for_album(first)
    sidebar.set_state(_sidebar_state(((second, "Second"),
                                     (first, "Renamed folder")),
                                    selected_id=first))

    assert sidebar.button_for_album(first) is original
    assert sidebar.button_for_album(first).toolTip() == "Renamed folder"
    assert sidebar.button_for_album(first).accessibleName() == "Album folder: Renamed folder"
    assert sidebar.button_for_album(first).isChecked()
    assert sidebar.button_for_album(second).toolTip() == "Second"


@pytest.mark.parametrize(("action_name", "signal_name"), [
    ("Rename", "rename_requested"),
    ("Delete", "delete_requested"),
])
def test_folder_context_actions_emit_the_captured_uuid(
        album_sidebar, monkeypatch, action_name, signal_name):
    sidebar = album_sidebar.sidebar
    album_id = "captured-album-uuid"
    sidebar.set_state(_sidebar_state(((album_id, "Captured folder"),)))
    answers = []
    emitted = []
    signal = getattr(sidebar, signal_name)
    signal.connect(emitted.append)

    class AnswerMenu(library_album_sidebar.QMenu):
        def exec(self, _position):
            action = next(item for item in self.actions()
                          if item.text() == action_name)
            answers.append(action.isEnabled())
            return action

    monkeypatch.setattr(library_album_sidebar, "QMenu", AnswerMenu)
    sidebar.button_for_album(album_id).customContextMenuRequested.emit(QPoint(2, 2))

    assert answers == [True]
    assert emitted == [album_id]


def test_locked_sidebar_disables_folder_mutations_and_keeps_selection(album_sidebar):
    sidebar = album_sidebar.sidebar
    album_id = "locked-album"
    sidebar.set_state(_sidebar_state(((album_id, "Locked folder"),),
                                    selected_id=album_id,
                                    locked=True,
                                    unavailable="Album catalog unavailable"))

    assert sidebar.button_for_album(album_id).isEnabled() is False
    assert sidebar.create_button.isEnabled() is False
    assert sidebar.button_for_album(album_id).isChecked()


def test_folder_names_are_elided_visually_but_preserved_accessibly(album_sidebar):
    sidebar = album_sidebar.sidebar
    album_id = "long-album"
    name = "A very long album folder name " * 8
    sidebar.set_state(_sidebar_state(((album_id, name),), selected_id=album_id))
    button = sidebar.button_for_album(album_id)
    button.ensurePolished()

    assert button.width() == 78
    assert button.text() != name
    assert button.fontMetrics().horizontalAdvance(button.text()) <= button.width()
    assert button.toolTip() == name
    assert button.accessibleName() == f"Album folder: {name}"


def test_explicit_album_navigation_filters_by_current_membership_and_reset_keeps_sort(
        tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    album = manager.create_album("Explicit folder")
    for path in state.paths[:2]:
        record = grid._records[clip_grid.canonical_media_path(path)]
        assert manager.add_album_member(
            album.album_id, str(path), expected_identity=dict(record.file_identity))
    _publish_grid_catalog(grid)

    criteria = SearchCriteria(
        text="no matching text", tags_all=("missing tag",), kind="screenshots",
        favorites_only=True, sort="oldest")
    saved = manager.create_saved_search("Old recipe", criteria)
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(saved.search_id)
    assert not grid.navigation_paths()

    grid.select_album_view(album.album_id)
    selected = grid.current_search_criteria()
    assert selected.text == ""
    assert selected.tags_all == ()
    assert selected.album_id == album.album_id
    assert selected.kind == "all"
    assert selected.favorites_only is False
    assert selected.sort == "oldest"
    assert grid.album_navigation_state()["selected_id"] == album.album_id
    assert grid._saved_search_id is None
    assert set(grid.navigation_paths()) == {str(path) for path in state.paths[:2]}

    grid.select_album_view(None)
    reset = grid.current_search_criteria()
    assert reset.album_id is None
    assert reset.text == "" and reset.tags_all == ()
    assert reset.kind == "all" and reset.favorites_only is False
    assert reset.sort == "oldest"
    assert len(grid.navigation_paths()) == len(state.paths)


def test_saved_query_passively_highlights_album_without_clearing_composed_criteria(
        tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    album = manager.create_album("Passive query folder")
    path = state.paths[0]
    record = grid._records[clip_grid.canonical_media_path(path)]
    manager.add_album_member(album.album_id, str(path),
                             expected_identity=dict(record.file_identity))
    _publish_grid_catalog(grid)
    criteria = SearchCriteria(text=path.stem, album_id=album.album_id,
                              sort="oldest")
    saved = manager.create_saved_search("Passive recipe", criteria)
    grid.refresh_saved_searches()
    emitted = []
    grid.album_view_changed.connect(emitted.append)

    assert grid.apply_saved_search(saved.search_id)

    assert grid.current_search_criteria() == criteria
    assert grid.album_navigation_state()["selected_id"] == album.album_id
    assert grid.album_navigation_state()["unavailable"] is None
    assert grid.navigation_paths() == [str(path)]
    assert emitted and emitted[-1]["selected_id"] == album.album_id


def test_sidebar_routes_explicit_folder_click_and_tracks_passive_grid_state(
        tag_grid, album_sidebar, qtbot, monkeypatch):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    sidebar = album_sidebar.sidebar
    album = manager.create_album("Routed folder")
    path = state.paths[0]
    record = grid._records[clip_grid.canonical_media_path(path)]
    manager.add_album_member(album.album_id, str(path),
                             expected_identity=dict(record.file_identity))
    _publish_grid_catalog(grid)
    assert grid.select_album_view(album.album_id)
    assert grid.set_search_criteria(SearchCriteria(album_id=album.album_id))
    sidebar.set_state(grid.album_navigation_state())
    grid.album_catalog_changed.connect(
        lambda _catalog: sidebar.set_state(grid.album_navigation_state()))
    grid.album_view_changed.connect(sidebar.set_state)
    sidebar.album_activated.connect(grid.select_album_view)

    counters = {name: 0 for name in (
        "list_albums", "list_saved_searches", "metadata_bindings", "flagged_paths")}
    originals = {name: getattr(manager, name) for name in counters}

    def counted(name):
        def call(*args, **kwargs):
            counters[name] += 1
            return originals[name](*args, **kwargs)
        return call

    for name in counters:
        monkeypatch.setattr(manager, name, counted(name))
    grid.search_edit.setText(path.stem)
    qtbot.wait(220)
    assert sidebar.button_for_album(album.album_id).isChecked()
    assert counters == {name: 0 for name in counters}

    grid.set_search_criteria(SearchCriteria(text="preserved query", sort="oldest"))
    sidebar.button_for_album(album.album_id).click()
    assert grid.album_navigation_state()["selected_id"] == album.album_id
    assert grid.current_search_criteria().text == ""
    assert grid.current_search_criteria().sort == "oldest"
    assert grid.navigation_paths() == [str(path)]


@pytest.mark.parametrize("handler_name", [
    "_open_album_folder",
    "_create_album_folder",
    "_rename_album_folder",
    "_delete_album_folder",
])
@pytest.mark.parametrize("blocker", ["trash_pending", "park_false", "save_error"])
def test_sidebar_navigation_and_folder_actions_preserve_state_when_editor_vetoes(
        tag_grid, album_sidebar, handler_name, blocker):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    album = manager.create_album("Guarded folder")
    _publish_grid_catalog(grid)
    assert grid.select_album_view(album.album_id)
    criteria = SearchCriteria(text="keep this query", album_id=album.album_id,
                              sort="oldest")
    assert grid.set_search_criteria(criteria)
    sidebar = album_sidebar.sidebar
    settings_button = QToolButton()
    settings_button.setCheckable(True)
    album_sidebar.group.addButton(settings_button, 1)
    settings_button.setChecked(True)
    sidebar.set_state(grid.album_navigation_state(), active=False)
    warnings = []
    active_viewer = SimpleNamespace(
        _warn_clip_details_error=lambda error: warnings.append(error))
    main = SimpleNamespace(
        clip_grid=grid,
        album_sidebar=sidebar,
        main_stack=SimpleNamespace(currentIndex=lambda: 1),
        _editor_trash_pending=lambda: blocker == "trash_pending",
        _refresh_editor_sidebar=lambda: MainWindow._sync_album_sidebar(main),
        _active_clip_viewer=(None if blocker == "trash_pending" else active_viewer),
    )
    if blocker == "park_false":
        main._park_active_editor = lambda: False
    elif blocker == "save_error":
        def fail_save():
            raise OSError("synthetic editor save failure")
        main._park_active_editor = fail_save
    main._sync_album_sidebar = MethodType(MainWindow._sync_album_sidebar, main)
    main._set_main_page = MethodType(MainWindow._set_main_page, main)
    before_albums = manager.list_albums()
    before_criteria = grid.current_search_criteria()

    if handler_name == "_create_album_folder":
        result = MainWindow._create_album_folder(main)
    else:
        result = getattr(MainWindow, handler_name)(main, album.album_id)

    assert result is False
    assert main.main_stack.currentIndex() == 1
    assert grid.current_search_criteria() == before_criteria
    assert grid.album_navigation_state()["selected_id"] == album.album_id
    assert sidebar.button_for_album(album.album_id).isChecked() is False
    assert settings_button.isChecked()
    assert manager.list_albums() == before_albums
    assert main._active_clip_viewer is (
        None if blocker == "trash_pending" else active_viewer)
    assert len(warnings) == (1 if blocker == "save_error" else 0)


def test_missing_saved_album_keeps_uuid_and_zero_results_in_passive_projection(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    album = manager.create_album("Will be removed")
    criteria = SearchCriteria(text="constrained", album_id=album.album_id)
    saved = manager.create_saved_search("Missing folder recipe", criteria)
    _publish_grid_catalog(grid)
    grid.refresh_saved_searches()
    manager.delete_album(album.album_id)
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(saved.search_id)

    projection = grid.album_navigation_state()
    assert projection["selected_id"] == album.album_id
    assert projection["unavailable"]
    assert grid.current_search_criteria() == criteria
    assert not grid.navigation_paths()
    assert grid.saved_search_combo.currentData() == saved.search_id


@pytest.mark.parametrize("accepted", [False, True], ids=["cancel", "accept"])
def test_plus_creation_cancel_preserves_query_and_accept_selects_empty_folder(
        tag_grid, monkeypatch, accepted):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    _publish_grid_catalog(grid)
    before = SearchCriteria(text="existing query", sort="oldest")
    assert grid.set_search_criteria(before)
    saved_ids = {item.search_id for item in manager.list_saved_searches()}
    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text",
        lambda *_args, **_kwargs: ("New empty folder", accepted))
    count_before = len(manager.list_albums())

    grid._new_album()

    if not accepted:
        assert len(manager.list_albums()) == count_before
        assert grid.current_search_criteria() == before
        assert grid.album_navigation_state()["selected_id"] is None
        assert {item.search_id for item in manager.list_saved_searches()} == saved_ids
    else:
        album = next(item for item in manager.list_albums()
                     if item.name == "New empty folder")
        criteria = grid.current_search_criteria()
        assert criteria.album_id == album.album_id
        assert criteria.text == "" and criteria.tags_all == ()
        assert criteria.kind == "all" and not criteria.favorites_only
        assert criteria.sort == "oldest"
        assert grid.album_navigation_state()["selected_id"] == album.album_id
        assert manager.album_bindings(album.album_id) == {}
        assert not grid.navigation_paths()


def test_public_rename_and_delete_keep_media_and_missing_query_fail_closed(
        tag_grid, monkeypatch):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    before_bytes = path.read_bytes()
    album = manager.create_album("Target folder")
    record = grid._records[clip_grid.canonical_media_path(path)]
    manager.add_album_member(album.album_id, str(path),
                             expected_identity=dict(record.file_identity))
    _publish_grid_catalog(grid)
    assert grid.select_album_view(album.album_id)
    criteria = SearchCriteria(text="will not match", tags_all=("missing tag",),
                              album_id=album.album_id, sort="oldest")
    saved = manager.create_saved_search("Keep unavailable reference", criteria)
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(saved.search_id)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, "question",
                        lambda *_args, **_kwargs: True)

    grid.rename_album_view(album.album_id, "Renamed folder")
    assert grid.album_navigation_state()["selected_id"] == album.album_id
    assert dict(grid.album_navigation_state()["albums"])[album.album_id] == "Renamed folder"
    assert grid.current_search_criteria() == criteria
    assert grid._saved_search_id == saved.search_id
    assert not grid.navigation_paths()

    grid.delete_album_view(album.album_id)
    projection = grid.album_navigation_state()
    assert projection["selected_id"] == album.album_id
    assert projection["unavailable"]
    assert album.album_id not in dict(projection["albums"])
    assert grid.current_search_criteria() == criteria
    assert grid._saved_search_id == saved.search_id
    assert grid.saved_search_combo.currentData() == saved.search_id
    assert not grid.navigation_paths()
    assert path.read_bytes() == before_bytes
    assert manager.resolve_media(path) is not None

    try:
        grid.rename_album_view(album.album_id, "Stale target")
    except (OSError, ValueError):
        pass
    assert manager.list_albums() == ()

    assert grid.select_album_view(None)
    assert grid.current_search_criteria().album_id is None
    assert grid.current_search_criteria().text == ""
    assert grid.current_search_criteria().tags_all == ()
    assert grid._saved_search_id is None
    assert len(grid.navigation_paths()) == len(state.paths)


def test_album_batch_lock_is_reflected_in_sidebar_and_released_on_cancel(
        tag_grid, qtbot):
    grid = tag_grid.grid
    album = tag_grid.manager.create_album("Locked during batch")
    _publish_grid_catalog(grid)
    records = tuple(grid._records.values())
    completed = []
    grid.bulk_albums_finished.connect(completed.append)

    grid.start_bulk_album_membership(album.album_id, records=records)
    grid._bulk_timer.stop()
    assert grid.album_navigation_state()["locked"] is True
    grid.cancel_bulk_flags()
    grid._bulk_timer.start(0)
    qtbot.waitUntil(lambda: bool(completed), timeout=3000)

    assert completed[0]["cancelled"] is True
    assert grid.album_navigation_state()["locked"] is False


def test_album_sidebar_layout_keeps_tiles_compact_and_prevents_horizontal_scroll(
        qtbot, qapp, tmp_path):
    from PySide6.QtWidgets import QScrollArea

    area = QScrollArea()
    sidebar = library_album_sidebar.AlbumSidebar(area)
    area.setWidget(sidebar)
    albums = tuple((f"album-{index}", f"Folder {index} " + ("W" * 50))
                   for index in range(18))
    sidebar.set_state(_sidebar_state(albums, selected_id="album-17"))
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.resize(1100, 720)
    area.show()
    for width in (1100, 1440):
        area.resize(width, 720 if width == 1100 else 900)
        qapp.processEvents()
        assert area.horizontalScrollBar().maximum() == 0
        assert all(sidebar.button_for_album(album_id).width() == 78
                   for album_id, _name in albums)
        assert sidebar.create_button.width() == 78
        output = tmp_path / f"album-sidebar-{width}.png"
        pixmap = area.grab()
        assert pixmap.width() == width
        assert pixmap.save(str(output))
        assert output.is_file() and output.stat().st_size > 0
