"""Game groups come from recorded clip folders and compose with library search."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.library_searches import SearchCriteria

pytest_plugins = ("test_library_tag_grid",)


def _choose_game(qtbot, grid, name):
    combo = grid.filter_combo
    observation = {}

    def navigate():
        popup = QApplication.activePopupWidget()
        if popup is None:
            return
        observation["main"] = [action.text() for action in popup.actions()]
        games = next(action for action in popup.actions() if action.text() == "Games")
        popup.setActiveAction(games)
        QTest.mouseClick(popup, Qt.MouseButton.LeftButton,
                         pos=popup.actionGeometry(games).center())
        submenu = games.menu()
        QTest.qWait(300)
        observation["submenu_visible"] = submenu.isVisible()
        observation["submenu"] = [action.data() for action in submenu.actions()]
        action = next((action for action in submenu.actions() if action.data() == name), None)
        if action is not None:
            submenu.setActiveAction(action)
            QTest.keyClick(submenu, Qt.Key.Key_Return)
        popup.close()

    QTimer.singleShot(0, navigate)
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton)
    return observation


def test_games_submenu_filters_recorded_groups_without_touching_sort(tag_grid, qtbot, tmp_path):
    grid = tag_grid.grid
    owned = tag_grid.paths[0].parent
    paths = [
        owned / "Apex & Legends" / "highlight-old.mp4",
        owned / "Apex & Legends" / "highlight-new.mp4",
        owned / "Hades" / "highlight.mp4",
        owned / "Desktop" / "desktop.mp4",
        owned / "Screenshots" / "screen.png",
        owned / "Recordings" / "recording.mp4",
        tmp_path / "Linked folder" / "linked.mp4",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"clip")
        os.utime(path, ns=(10_000_000_000, 10_000_000_000 + index * 1_000_000_000))
    grid._replace_records(grid._records_from_paths(
        {str(path) for path in [*paths, *tag_grid.paths]}))

    observation = _choose_game(qtbot, grid, "Apex & Legends")
    assert observation["main"][4:6] == ["Favorites", "Games"]
    assert observation["submenu_visible"]
    assert observation["submenu"] == ["Apex & Legends", "Hades"]
    assert grid.filter_combo.currentText() == "Apex & Legends"
    assert [Path(record.path) for record in grid._filtered_records()] == paths[1::-1]

    grid.sort_combo.setCurrentIndex(1)
    assert [Path(record.path) for record in grid._filtered_records()] == paths[:2]
    grid.search_edit.setText("new")
    qtbot.waitUntil(lambda: grid._search_criteria.text == "new")
    assert [Path(record.path) for record in grid._filtered_records()] == paths[1:2]
    assert not grid._saved_search_menu.actions()[0].isEnabled()

    grid.filter_combo.setCurrentIndex(0)
    assert grid.filter_combo.currentText() == "All clips"
    assert grid._saved_search_menu.actions()[0].isEnabled()
    assert grid._search_criteria.sort == "oldest"


def test_game_filter_refreshes_from_index_and_clears_on_saved_search_or_reset(tag_grid):
    grid, manager = tag_grid.grid, tag_grid.manager
    old = tag_grid.paths[0].parent / "Apex" / "clip.mp4"
    old.parent.mkdir()
    old.write_bytes(b"clip")
    grid._replace_records(grid._records_from_paths({str(old)}))
    grid._on_game_filter_changed("Apex")
    assert [Path(record.path) for record in grid._filtered_records()] == [old]

    renamed = old.with_name("renamed.mp4")
    old.rename(renamed)
    grid._replace_records(grid._records_from_paths({str(renamed)}))
    assert grid._game_groups() == ["Apex"]
    assert [Path(record.path) for record in grid._filtered_records()] == [renamed]

    saved = manager.create_saved_search("All video", SearchCriteria(kind="clips"))
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(saved.search_id)
    assert grid.filter_combo.currentText() == "Clips only"
    grid._on_game_filter_changed("Apex")
    grid._reset_search()
    assert grid.filter_combo.currentText() == "All clips"


def test_game_combines_with_album_and_favorites_only(tag_grid):
    grid, manager = tag_grid.grid, tag_grid.manager
    paths = []
    for group, filename in (("Apex", "favorite"), ("Apex", "ordinary"),
                            ("Apex", "outside-album"), ("Hades", "favorite")):
        path = tag_grid.paths[0].parent / group / f"{filename}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"clip")
        manager.register_media(manager.inspect_media(path))
        paths.append(path)

    album = manager.create_album("Game night")
    for path in (paths[0], paths[1], paths[3]):
        manager.add_album_member(album.album_id, str(path),
                                 expected_identity=manager.inspect_media(path).stat)
    for path in (paths[0], paths[2], paths[3]):
        manager.set_library_flags(path, favorite=True)
    grid._replace_records(grid._records_from_paths({str(path) for path in paths}))
    grid.refresh_library_details()
    grid.set_search_criteria(SearchCriteria(album_id=album.album_id, favorites_only=True))
    grid._on_game_filter_changed("Apex")

    assert [Path(record.path) for record in grid._filtered_records()] == paths[:1]


def test_games_survive_storage_move_and_flattened_folders(tag_grid, tmp_path):
    from core.library_index import scan_library
    grid = tag_grid.grid
    old = tmp_path / "Old library"
    old.mkdir()
    paths = [old / "Valorant_clip_from_08Nov2026_12-00-00.mp4",
             old / "Overwatch_clip_from_08Nov2026_12-01-00.mp4",
             old / "desktop_clip_from_08Nov2026_12-02-00.mp4"]
    for path in paths:
        path.write_bytes(b"clip")
    result = scan_library(grid.clips_dir, [old])
    grid._replace_records({record.identity: record for record in result.all_records})
    assert grid._game_groups() == ["Overwatch", "Valorant"]
    grid._on_game_filter_changed("VALORANT")
    assert [Path(record.path) for record in grid._filtered_records()] == paths[:1]
