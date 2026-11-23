"""Regression tests for composed library search controls and saved queries.

Tests reuse the synthetic schema-5 grid fixture and never start the application.
"""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QLabel
from ui import clip_grid

pytest_plugins = ("test_library_tag_grid",)


@pytest.fixture(autouse=True)
def _register_application_fonts(qapp):
    """Load bundled app fonts before any grid widget is constructed."""
    font_dir = Path(clip_grid.__file__).resolve().parents[1] / "assets" / "fonts"
    if font_dir.is_dir() and not any("Oswald" in family for family in QFontDatabase.families()):
        for font_path in sorted(font_dir.glob("Oswald-*.ttf")):
            QFontDatabase.addApplicationFont(str(font_path))


def _matches(grid):
    return {record.path for record in grid._filtered_records(ordered=False)}


def test_composed_search_ands_text_tags_album_kind_and_favorites(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    screenshot_path = state.paths[0].parent / "cafe-highlight.png"
    screenshot_path.write_bytes(b"synthetic screenshot bytes")
    manager.register_media(manager.inspect_media(screenshot_path))
    imported_video, nonfavorite = state.paths[1], state.paths[2]
    grid._imported_files.add(str(imported_video))
    all_paths = {str(path) for path in state.paths} | {str(screenshot_path)}
    grid._replace_records(grid._records_from_paths(all_paths))
    assert grid._records[clip_grid.canonical_media_path(screenshot_path)].kind == "image"

    album = manager.create_album("Composed search")
    grid._refresh_album_catalog()
    for path in (screenshot_path, imported_video, nonfavorite):
        record = grid._records[clip_grid.canonical_media_path(path)]
        identity = dict(record.file_identity)
        assert manager.set_details(
            str(path), tags=("Caf\u00e9 Tag", "Highlight"),
            description="Night caf\u00e9 highlight", expected_identity=identity)
        assert manager.add_album_member(
            album.album_id, str(path), expected_identity=identity)
    assert manager.set_library_flags(screenshot_path, favorite=True)
    assert manager.set_library_flags(imported_video, favorite=True)
    grid.refresh_library_details()

    screenshot_criteria = SearchCriteria(
        text="  cafe\u0301   NIGHT ", tags_all=(" CAF\u00c9   tag ", "highlight"),
        album_id=album.album_id, kind="screenshots", favorites_only=True)
    grid.set_search_criteria(screenshot_criteria)
    assert _matches(grid) == {str(screenshot_path)}

    imported_criteria = replace(screenshot_criteria, kind="imported")
    grid.set_search_criteria(imported_criteria)
    assert _matches(grid) == {str(imported_video)}


def test_pending_text_debounces_and_new_matching_index_entry_appears(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    grid.start_scanning()
    qtbot.waitUntil(lambda: grid._active_scan_worker is None, timeout=10000)
    qtbot.waitUntil(lambda: bool(grid._thumb_widgets) and not grid._resize_timer.isActive()
                    and not grid._virtual_update_timer.isActive(), timeout=3000)

    apply_count = 0
    original_clear = grid._clear_sections

    def counted_clear():
        nonlocal apply_count
        apply_count += 1
        return original_clear()

    with monkeypatch.context() as patch:
        patch.setattr(grid, "_clear_sections", counted_clear)
        grid.search_edit.setText("  Dynamic   highlight ")
        grid.search_edit.setCursorPosition(10)
        qtbot.wait(80)
        assert apply_count == 0
        assert grid.search_edit.text() == "  Dynamic   highlight "
        assert grid.search_edit.cursorPosition() == 10
        qtbot.waitUntil(lambda: apply_count == 1, timeout=1500)
        assert not _matches(grid)

    new_path = state.paths[0].parent / "dynamic-highlight.mp4"
    new_path.write_bytes(b"new synthetic matching clip")
    manager.register_media(manager.inspect_media(new_path))
    identity = dict(manager.inspect_media(new_path).stat)
    assert manager.set_details(
        str(new_path), tags=("fresh",), description="dynamic highlight",
        expected_identity=identity)
    generation = grid._scan_generation
    grid._begin_scan("foreground_refresh")
    qtbot.waitUntil(
        lambda: grid._scan_generation > generation
        and grid._active_scan_worker is None
        and clip_grid.canonical_media_path(new_path) in grid._records,
        timeout=10000)
    assert _matches(grid) == {str(new_path)}

def test_query_debt_waits_for_card_mutation_and_unchanged_scan(tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid = state.grid
    grid.start_scanning()
    qtbot.waitUntil(lambda: grid._active_scan_worker is None, timeout=10000)
    original_records = dict(grid._records)
    path = state.paths[0]
    assert not grid._prepare_card_mutation(str(path))

    apply_count = 0
    original_clear = grid._clear_sections

    def counted_clear():
        nonlocal apply_count
        apply_count += 1
        return original_clear()

    with monkeypatch.context() as patch:
        patch.setattr(grid, "_clear_sections", counted_clear)
        from core.library_searches import SearchCriteria
        grid.set_search_criteria(SearchCriteria(text=path.stem))
        assert apply_count == 0
        assert grid._details_rebuild_pending
        before_generation = grid._scan_generation
        grid._begin_scan("foreground_refresh")
        qtbot.waitUntil(
            lambda: grid._scan_generation > before_generation
            and grid._active_scan_worker is None, timeout=10000)
        assert grid._records == original_records
        assert apply_count == 0
        assert grid._details_rebuild_pending
        grid._finish_card_mutation(str(path))
        qtbot.waitUntil(lambda: apply_count == 1, timeout=3000)
        assert not grid._details_rebuild_pending
        assert grid.navigation_paths() == [str(path)]


def test_text_debounce_during_tag_batch_is_applied_once_after_cancel(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid = state.grid
    qtbot.wait(250)
    record = grid._records[clip_grid.canonical_media_path(state.paths[0])]
    apply_count = 0
    original_clear = grid._clear_sections

    def counted_clear():
        nonlocal apply_count
        apply_count += 1
        return original_clear()

    with monkeypatch.context() as patch:
        patch.setattr(grid, "_clear_sections", counted_clear)
        grid.start_bulk_tags("add", ("batch marker",), records=(record,))
        grid._bulk_timer.stop()
        grid.search_edit.setText(record.path.rsplit("\\", 1)[-1].removesuffix(".mp4"))
        assert not grid.search_edit.isEnabled()
        qtbot.wait(240)
        assert not grid._search_debounce_timer.isActive()
        assert apply_count == 0
        assert grid._details_rebuild_pending
        assert grid._bulk_job is not None and grid._bulk_job["index"] == 0
        with qtbot.waitSignal(grid.bulk_tags_finished, timeout=3000) as emitted:
            grid.cancel_bulk_flags()
            grid._bulk_timer.start(0)
        assert emitted.args[0]["cancelled"] is True
        qtbot.waitUntil(lambda: apply_count == 1, timeout=3000)
        assert not grid._details_rebuild_pending
        assert grid.navigation_paths() == [record.path]


def test_favorites_query_tracks_flag_publication_and_normal_empty_state(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    record = grid._records[clip_grid.canonical_media_path(path)]
    evidence = dict(record.file_identity)
    grid.refresh_library_details()
    grid.set_search_criteria(SearchCriteria(favorites_only=True))
    assert not _matches(grid)

    assert manager.set_library_flags(path, favorite=True, expected_identity=evidence)
    grid.refresh_library_flags(str(path))
    assert _matches(grid) == {str(path)}
    assert grid.navigation_paths() == [str(path)]

    assert manager.set_library_flags(path, favorite=False, expected_identity=evidence)
    grid.refresh_library_flags(str(path))
    assert not _matches(grid)
    assert not grid.navigation_paths()
    assert "no matching clips" in grid._all_count_label.accessibleName().casefold()


def test_same_album_text_tag_and_sort_changes_reuse_published_album_snapshot(
        tag_grid, qtbot, monkeypatch):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    album = manager.create_album("Stable album snapshot")
    for path in state.paths:
        record = grid._records[clip_grid.canonical_media_path(path)]
        assert manager.add_album_member(
            album.album_id, str(path), expected_identity=dict(record.file_identity))
    grid._refresh_album_catalog(selected_id=album.album_id)
    grid.set_search_criteria(SearchCriteria(album_id=album.album_id))
    assert grid.navigation_paths()

    names = ("album_bindings", "list_albums", "list_saved_searches",
             "metadata_bindings", "flagged_paths")
    calls = {name: 0 for name in names}
    originals = {name: getattr(manager, name) for name in names}

    def tracked(name):
        def call(*args, **kwargs):
            calls[name] += 1
            return originals[name](*args, **kwargs)
        return call

    for name in names:
        monkeypatch.setattr(manager, name, tracked(name))

    grid.search_edit.setText(state.paths[0].stem)
    qtbot.waitUntil(lambda: not grid._search_debounce_timer.isActive(), timeout=1500)
    assert grid.current_search_criteria().album_id == album.album_id
    assert calls == {name: 0 for name in names}

    assert grid.set_search_tags(("criterion tag",))
    assert grid.current_search_criteria().album_id == album.album_id
    assert calls == {name: 0 for name in names}

    grid.sort_combo.setCurrentIndex(1)
    assert grid.current_search_criteria().album_id == album.album_id
    assert grid.current_search_criteria().sort == "oldest"
    assert calls == {name: 0 for name in names}


def test_atomic_criteria_apply_restores_outer_signal_sync_state(tag_grid):
    from core.library_searches import SearchCriteria

    grid = tag_grid.grid
    previous = grid._search_syncing
    grid._search_syncing = True
    try:
        assert grid.set_search_criteria(SearchCriteria(text="nested atomic apply"))
        assert grid.search_edit.text() == "nested atomic apply"
        assert grid._search_syncing is True
    finally:
        grid._search_syncing = previous


def test_missing_album_saved_search_keeps_reason_typed_query_and_controls(
        tag_grid):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    album = manager.create_album("Deleted search album")
    criteria = SearchCriteria(
        text="must remain constrained", tags_all=("known tag",),
        album_id=album.album_id, favorites_only=True, sort="oldest")
    saved = manager.create_saved_search("Missing reference", criteria)
    grid._refresh_album_catalog()
    grid.refresh_saved_searches()

    manager.delete_album(album.album_id)
    assert grid.apply_saved_search(saved.search_id)
    expected = next(item.error for item in manager.list_saved_searches()
                    if item.search_id == saved.search_id)
    assert expected == "Referenced album no longer exists"

    grid.refresh_saved_searches()
    grid.refresh_saved_searches()
    assert grid._saved_search_id == saved.search_id
    assert grid.saved_search_combo.currentData() == saved.search_id
    assert grid._search_criteria == criteria
    assert grid.current_search_criteria() == criteria
    assert grid._search_unavailable_reason == expected
    assert grid.search_status_label.isVisible()
    assert grid.search_status_label.text() == expected
    assert not _matches(grid)
    assert grid.search_edit.isEnabled() is False
    assert grid.tags_filter_btn.isEnabled() is False
    assert grid.favorites_only_check.isEnabled() is False
    actions = grid._saved_search_menu.actions()
    assert actions[0].isEnabled() is False  # New
    assert actions[2].isEnabled() is False  # Update
    assert grid.reset_search_btn.isEnabled()


def test_favorites_snapshot_rejects_same_path_replacement_while_apply_is_gated(
        tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    original = grid._records[clip_grid.canonical_media_path(path)]
    old_identity = dict(original.file_identity)
    assert manager.set_library_flags(
        path, favorite=True, expected_identity=old_identity)
    grid.refresh_library_flags(str(path))
    grid.set_search_criteria(SearchCriteria(favorites_only=True))
    assert _matches(grid) == {str(path)}

    guard_path = str(state.paths[1])
    assert not grid._prepare_card_mutation(guard_path)
    try:
        path.write_bytes(b"replacement file has a different identity and length")
        replacement_identity = dict(manager.inspect_media(path).stat)
        assert replacement_identity != old_identity
        grid.upsert_saved_clip(str(path), ready=True)
        replacement = grid._records[clip_grid.canonical_media_path(path)]
        assert replacement.file_identity == replacement_identity
        assert grid._details_rebuild_pending
        assert str(path) not in _matches(grid)
        assert str(path) not in grid.navigation_paths()
    finally:
        grid._finish_card_mutation(guard_path)

    assert not grid._details_rebuild_pending
    assert str(path) not in _matches(grid)
    assert str(path) not in grid.navigation_paths()


def test_saved_clip_upsert_keeps_query_membership_and_filename_only_match(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid = state.grid
    default_query_save = state.paths[0].parent / "direct-update-no-scan.mp4"
    default_query_save.write_bytes(b"synthetic default-query save")
    default_generation = grid._scan_generation
    grid.upsert_saved_clip(str(default_query_save), ready=False)
    assert clip_grid.canonical_media_path(default_query_save) in grid._records
    assert str(default_query_save) in grid.navigation_paths()
    assert grid._scan_generation == default_generation
    grid.set_search_criteria(SearchCriteria(text="direct update"))
    assert grid.navigation_paths() == [str(default_query_save)]
    assert grid._scan_generation == default_generation

    filtered_out = state.paths[0].parent / "unrelated-local-save.mp4"
    filtered_out.write_bytes(b"synthetic local save")
    grid.set_search_criteria(SearchCriteria(text="no matching token"))
    grid.upsert_saved_clip(str(filtered_out), ready=False)
    assert clip_grid.canonical_media_path(filtered_out) in grid._records
    assert str(filtered_out) not in _matches(grid)
    assert str(filtered_out) not in grid.navigation_paths()

    filename_only = state.paths[0].parent / "fresh-subject-local-save.mp4"
    filename_only.write_bytes(b"synthetic unregistered save")
    grid.set_search_criteria(SearchCriteria(text="fresh subject"))
    grid.upsert_saved_clip(str(filename_only), ready=False)
    assert clip_grid.canonical_media_path(filename_only) in grid._records
    assert _matches(grid) == {str(filename_only)}
    assert grid.navigation_paths() == [str(filename_only)]

    grid._reset_search()
    assert str(filtered_out) in _matches(grid)
    assert str(filename_only) in _matches(grid)
    assert str(filtered_out) in grid.navigation_paths()
    assert str(filename_only) in grid.navigation_paths()


def test_saved_search_apply_and_reset_cancel_pending_debounce(tag_grid, qtbot):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    criteria = SearchCriteria(text="saved needle", tags_all=("saved tag",),
                              kind="imported", favorites_only=True, sort="oldest")
    saved = manager.create_saved_search("Stable recipe", criteria)
    grid.refresh_saved_searches()
    grid.search_edit.setText("stale pending text")
    grid.apply_saved_search(saved.search_id)
    qtbot.wait(250)

    assert grid.current_search_criteria() == criteria
    assert grid.saved_search_combo.currentData() == saved.search_id
    assert grid.search_edit.text() == criteria.text

    grid.search_edit.setText("modified custom query")
    qtbot.waitUntil(
        lambda: grid.current_search_criteria().text == "modified custom query",
        timeout=1000)
    modified = grid.current_search_criteria()
    assert grid.saved_search_combo.currentData() is None
    manager.delete_saved_search(saved.search_id)
    grid.refresh_saved_searches()
    assert grid.current_search_criteria() == modified
    assert grid.saved_search_combo.currentData() is None

    grid.search_edit.setText("pending reset text")
    grid._reset_search()
    qtbot.wait(250)
    assert grid.current_search_criteria() == SearchCriteria()
    assert grid.search_edit.text() == ""


def test_saved_search_dialog_crud_commits_pending_text_and_preserves_query(
        tag_grid, monkeypatch):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    grid.search_edit.setText("  created   from pending text ")
    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text",
        lambda *_args, **_kwargs: ("Dialog recipe", True))
    grid._new_saved_search()
    search_id = grid.saved_search_combo.currentData()
    assert search_id is not None
    saved = next(item for item in manager.list_saved_searches()
                 if item.search_id == search_id)
    assert saved.criteria == SearchCriteria(text="created from pending text")
    assert grid.current_search_criteria() == saved.criteria

    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text",
        lambda *_args, **_kwargs: ("Renamed recipe", True))
    grid._rename_saved_search()
    renamed = next(item for item in manager.list_saved_searches()
                   if item.search_id == search_id)
    assert renamed.name == "Renamed recipe"
    assert grid.current_search_criteria() == saved.criteria

    grid.search_edit.setText("updated current recipe")
    assert grid.current_search_criteria().text == "updated current recipe"
    assert grid.saved_search_combo.currentData() is None
    update_actions = [action for action in grid._saved_search_menu.actions()
                      if "update" in action.text().casefold()]
    assert update_actions
    target_label = " ".join(
        action.text() + " " + action.toolTip() for action in update_actions)
    assert "Renamed recipe" in target_label
    grid._update_saved_search()
    updated = next(item for item in manager.list_saved_searches()
                   if item.search_id == search_id)
    assert updated.criteria.text == "updated current recipe"
    assert grid.saved_search_combo.currentData() is None

    assert grid.apply_saved_search(search_id)
    effective = grid.current_search_criteria()
    monkeypatch.setattr(
        clip_grid.FthrMessageDialog, "question",
        lambda *_args, **_kwargs: True)
    grid._delete_saved_search()
    assert all(item.search_id != search_id for item in manager.list_saved_searches())
    assert grid.saved_search_combo.currentData() is None
    assert grid.current_search_criteria() == effective


@pytest.mark.parametrize("dialog_action", ["rename", "delete"])
def test_saved_search_dialog_revalidates_captured_target(tag_grid, monkeypatch, dialog_action):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    first = manager.create_saved_search("First recipe", SearchCriteria(text="first"))
    second = manager.create_saved_search("Second recipe", SearchCriteria(text="second"))
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(first.search_id)

    def switch_selection(*_args, **_kwargs):
        assert grid.apply_saved_search(second.search_id)
        return ("Must not apply", True)

    if dialog_action == "rename":
        monkeypatch.setattr(clip_grid.FthrInputDialog, "get_text", switch_selection)
        grid._rename_saved_search()
    else:
        def switch_before_confirm(*_args, **_kwargs):
            assert grid.apply_saved_search(second.search_id)
            return True

        monkeypatch.setattr(
            clip_grid.FthrMessageDialog, "question", switch_before_confirm)
        grid._delete_saved_search()

    by_id = {item.search_id: item for item in manager.list_saved_searches()}
    assert by_id[first.search_id].name == "First recipe"
    assert by_id[second.search_id].name == "Second recipe"
    assert grid.saved_search_combo.currentData() == second.search_id
    assert grid.current_search_criteria().text == "second"


def test_saved_search_captures_visible_pending_text(tag_grid):
    grid, manager = tag_grid.grid, tag_grid.manager
    grid.search_edit.setText("  pending   text ")
    criteria = grid.current_search_criteria()
    assert criteria.text == "pending text"
    saved = manager.create_saved_search("Pending query", criteria)
    grid.refresh_saved_searches()
    assert any(item.search_id == saved.search_id
               for item in manager.list_saved_searches())


def test_unavailable_saved_search_and_missing_id_never_broaden_to_all(tag_grid):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    album = manager.create_album("Will disappear")
    criteria = SearchCriteria(text="must stay constrained", album_id=album.album_id)
    saved = manager.create_saved_search("Missing album recipe", criteria)
    grid.refresh_saved_searches()
    manager.delete_album(album.album_id)
    grid.refresh_saved_searches()
    grid.apply_saved_search(saved.search_id)

    assert grid.current_search_criteria().album_id == album.album_id
    assert grid.saved_search_combo.currentData() == saved.search_id
    assert not _matches(grid)
    assert grid.reset_search_btn.isEnabled()
    assert any(
        any(token in label.text().casefold() for token in ("missing", "unavailable"))
        for label in grid.findChildren(QLabel) if label.isVisible())

    grid.apply_saved_search("00000000-0000-0000-0000-000000000000")
    assert not _matches(grid)
    assert grid.reset_search_btn.isEnabled()
    grid._reset_search()
    assert len(_matches(grid)) == len(grid._records)


def test_unavailable_album_search_ignores_pending_custom_and_needs_explicit_reset(
        tag_grid, qtbot):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    album = manager.create_album("Search target removed")
    saved = manager.create_saved_search(
        "Unavailable with pending text",
        SearchCriteria(text="never match all", album_id=album.album_id))
    grid.refresh_saved_searches()
    manager.delete_album(album.album_id)
    grid.refresh_saved_searches()

    grid.search_edit.setText("pending custom text")
    assert grid._search_debounce_timer.isActive()
    grid.apply_saved_search(saved.search_id)
    assert not grid._search_debounce_timer.isActive()
    qtbot.wait(240)
    assert grid._saved_search_id == saved.search_id
    assert grid.current_search_criteria().album_id == album.album_id
    assert not _matches(grid)

    custom_index = grid.saved_search_combo.findData(None)
    assert custom_index >= 0
    grid.saved_search_combo.setCurrentIndex(custom_index)
    qtbot.wait(240)
    assert grid._saved_search_id == saved.search_id
    assert not _matches(grid)
    assert grid.reset_search_btn.isEnabled()
    grid._reset_search()
    assert grid._saved_search_id is None
    assert len(_matches(grid)) == len(grid._records)


def test_removed_active_search_row_keeps_uuid_reason_and_zero_results(tag_grid):
    import sqlite3

    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    saved = manager.create_saved_search(
        "Externally removed", SearchCriteria(text="cannot broaden"))
    grid.refresh_saved_searches()
    assert grid.apply_saved_search(saved.search_id)
    original_criteria = grid.current_search_criteria()

    with sqlite3.connect(manager._database) as connection:
        connection.execute(
            "DELETE FROM saved_searches WHERE search_id=?", (saved.search_id,))
    manager.reload()
    grid.refresh_saved_searches()

    assert grid._saved_search_id == saved.search_id
    assert grid._search_unavailable_reason
    assert grid._search_criteria == original_criteria
    assert not _matches(grid)
    with pytest.raises(ValueError):
        grid.current_search_criteria()
    custom_index = grid.saved_search_combo.findData(None)
    assert custom_index >= 0
    grid.saved_search_combo.setCurrentIndex(custom_index)
    assert grid._saved_search_id == saved.search_id
    assert not _matches(grid)
    assert grid._search_unavailable_reason


def test_unsupported_saved_criteria_stays_selected_and_fails_closed(tag_grid):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    saved = manager.create_saved_search("Future recipe", SearchCriteria(text="needle"))
    with sqlite3.connect(manager._database) as connection:
        connection.execute(
            "UPDATE saved_searches SET criteria_json=? WHERE search_id=?",
            (json.dumps({"version": 2, "text": "needle"}), saved.search_id))
    manager.reload()
    grid.refresh_saved_searches()
    grid.apply_saved_search(saved.search_id)

    loaded = next(item for item in manager.list_saved_searches()
                  if item.search_id == saved.search_id)
    assert loaded.criteria is None and loaded.error
    assert grid.saved_search_combo.currentData() == saved.search_id
    visible_name = (grid.saved_search_combo.currentText()
                    + " " + grid.saved_search_combo.toolTip())
    assert "Future recipe" in visible_name
    assert not _matches(grid)
    assert grid.reset_search_btn.isEnabled()
    with pytest.raises(ValueError):
        grid.current_search_criteria()
    grid._reset_search()
    assert len(_matches(grid)) == len(grid._records)


def test_selected_missing_and_legacy_long_tags_survive_search_criteria(tag_grid):
    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    long_legacy = "Legacy " + ("long " * 17) + "long"
    manager.set(str(path), tag=long_legacy)
    grid.refresh_library_details(str(path))
    missing = "Selected but absent"
    criteria_tags = (missing, long_legacy)

    grid.set_search_tags(criteria_tags)
    current = grid.current_search_criteria()
    assert current.tags_all == tuple(" ".join(value.split()).casefold()
                                     for value in criteria_tags)
    assert len(current.tags_all) == 2
    assert " ".join(long_legacy.split()).casefold() in current.tags_all
    assert missing.casefold() in current.tags_all

    at_limit = tuple(f"Criterion {index}" for index in range(32))
    grid.set_search_tags(at_limit)
    assert len(grid.current_search_criteria().tags_all) == 32
    with pytest.raises((TypeError, ValueError)):
        grid.set_search_tags((*at_limit, "Criterion 33"))


def test_long_nfd_description_matches_and_replacement_loses_old_query_evidence(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    original = manager.inspect_media(path)
    old_identity = dict(original.stat)
    long_description = "Cafe\u0301\n" + ("multiline context " * 40) + "FinalMarker"
    assert manager.set_details(
        str(path), tags=("old unique tag",), description=long_description,
        expected_identity=old_identity)
    assert manager.set_library_flags(
        path, favorite=True, expected_identity=old_identity)
    grid.refresh_library_flags(str(path))

    grid.set_search_criteria(SearchCriteria(text="CAF\u00c9 finalmarker"))
    assert grid.navigation_paths() == [str(path)]

    path.write_bytes(b"replacement with different size and identity")
    grid.upsert_saved_clip(str(path), ready=True)
    fresh_identity = dict(manager.inspect_media(path).stat)
    record = grid._records[clip_grid.canonical_media_path(path)]
    assert record.file_identity == fresh_identity

    grid.set_search_criteria(SearchCriteria(tags_all=("old unique tag",)))
    assert str(path) not in grid.navigation_paths()
    grid.set_search_criteria(SearchCriteria(favorites_only=True))
    assert str(path) not in grid.navigation_paths()


def test_new_registered_replacement_favorite_waits_for_fresh_grid_record(tag_grid):
    from core.library_searches import SearchCriteria

    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    old_record = grid._records[clip_grid.canonical_media_path(path)]
    old_identity = dict(old_record.file_identity)
    path.write_bytes(b"new replacement bytes with a different size")
    replacement = manager.inspect_media(path)
    new_identity = dict(replacement.stat)
    assert new_identity != old_identity
    manager.register_media(replacement)
    assert manager.set_library_flags(
        path, favorite=True, expected_identity=new_identity)
    grid.refresh_library_flags(str(path))
    grid.set_search_criteria(SearchCriteria(favorites_only=True))

    assert not _matches(grid)
    assert str(path) not in grid.navigation_paths()
    grid.upsert_saved_clip(str(path), ready=True)
    assert grid._records[clip_grid.canonical_media_path(path)].file_identity == new_identity
    assert _matches(grid) == {str(path)}
    assert grid.navigation_paths() == [str(path)]


def test_filtered_records_are_ram_only_after_cache_refresh(tag_grid, monkeypatch):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    grid.refresh_library_details()
    counts = {name: 0 for name in (
        "metadata_bindings", "album_bindings", "flagged_paths",
        "list_albums", "list_saved_searches")}
    originals = {name: getattr(manager, name) for name in counts}

    def counted(name):
        def call(*args, **kwargs):
            counts[name] += 1
            return originals[name](*args, **kwargs)
        return call

    for name in counts:
        monkeypatch.setattr(manager, name, counted(name))
    grid.set_search_criteria(SearchCriteria(text="clip"))
    assert counts == {name: 0 for name in counts}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cached query filtering must not perform storage or path I/O")

    with monkeypatch.context() as patch:
        for name in ("metadata_bindings", "get", "get_tags", "flagged_paths"):
            patch.setattr(manager, name, forbidden)
        patch.setattr(clip_grid.os, "stat", forbidden)
        patch.setattr(clip_grid, "canonical_media_path", forbidden)
        assert _matches(grid)


@pytest.mark.parametrize("width", [1100, 1440])
def test_search_controls_and_long_saved_name_fit_with_app_fonts(tag_grid, qtbot, tmp_path, width):
    from core.library_searches import SearchCriteria

    grid, manager = tag_grid.grid, tag_grid.manager
    long_name = "W" * 80
    saved = manager.create_saved_search(long_name, SearchCriteria())
    grid.refresh_saved_searches()
    index = grid.saved_search_combo.findData(saved.search_id)
    assert index >= 0
    grid.saved_search_combo.setCurrentIndex(index)
    tag_grid.area.resize(width, 720)
    qtbot.waitUntil(
        lambda: grid.isVisible() and tag_grid.area.viewport().width() > 0
        and grid.width() == tag_grid.area.viewport().width())
    assert grid.width() <= tag_grid.area.viewport().width()
    assert grid.search_edit.maxLength() == 200
    controls = (grid.search_edit, grid.tags_filter_btn,
                grid.favorites_only_check, grid.saved_search_combo,
                grid.saved_search_manage_btn, grid.reset_search_btn)
    # Saved-query and redundant filter adapters stay hidden after toolbar consolidation.
    controls = tuple(widget for widget in controls if not widget.isHidden())
    rects = [QRect(widget.mapTo(grid, QPoint(0, 0)), widget.size()) for widget in controls]
    assert all(grid.rect().contains(rect) for rect in rects)
    assert all(not left.intersects(right)
               for left, right in zip(rects, rects[1:], strict=False))
    assert grid.saved_search_combo.currentData() == saved.search_id
    assert long_name in grid.saved_search_combo.toolTip()
    image_path = tmp_path / f"search-layout-{width}.png"
    assert not tag_grid.area.grab().isNull()
    assert tag_grid.area.grab().save(str(image_path))
    assert image_path.is_file() and image_path.stat().st_size > 0
