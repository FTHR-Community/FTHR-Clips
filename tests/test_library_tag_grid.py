"""Regression tests for cached tag metadata and bulk edits in the clip grid.

The tests use synthetic media and metadata under pytest's temporary profile.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from core.clip_metadata_manager import ClipMetadataManager
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QPixmap
from PySide6.QtWidgets import QScrollArea
from ui import clip_grid
from ui.dialogs import FthrMessageDialog

pytest_plugins = ("test_editor_sessions",)


@pytest.fixture
def tag_grid(qtbot, tmp_path, monkeypatch, request):
    owned = tmp_path / "owned"
    owned.mkdir()
    count = getattr(request, "param", 24)
    paths = [owned / f"clip-{index:03}.mp4" for index in range(count)]
    for path in paths:
        path.write_bytes(f"synthetic:{path.name}".encode())

    manager = ClipMetadataManager(database_path=tmp_path / "metadata.sqlite3")
    manager.migrate(target_version=5)
    for path in paths:
        manager.register_media(manager.inspect_media(path))

    settings = SimpleNamespace(
        get=lambda key, default=None: {
            "clips_directory": str(owned), "imported_clip_folders": [],
        }.get(key, default),
        set=lambda *_args: None,
        save_settings=lambda: None,
    )
    area = QScrollArea()
    area.setWidgetResizable(True)
    grid = clip_grid.ClipGrid(settings, metadata_manager=manager, autostart=False)
    area.setWidget(grid)
    qtbot.addWidget(area)
    monkeypatch.setattr(grid, "_start_thumbnail_worker", lambda *_args: None)
    grid._records = grid._records_from_paths({str(path) for path in paths})
    grid._apply_current_index()
    area.resize(1100, 720)
    area.show()
    qtbot.waitUntil(
        lambda: area.isVisible() and grid.isVisible() and bool(grid._thumb_widgets))
    warnings = []
    monkeypatch.setattr(
        FthrMessageDialog, "warning",
        lambda parent, title, message, **kwargs:
        warnings.append((title, message, kwargs)))
    yield SimpleNamespace(
        area=area, grid=grid, manager=manager, paths=paths, warnings=warnings)
    grid.shutdown()


def _record(state, path):
    return state.grid._records[clip_grid.canonical_media_path(path)]


def _select(state, path, *, toggle=False):
    modifiers = Qt.KeyboardModifier.ControlModifier if toggle else Qt.KeyboardModifier.NoModifier
    state.grid._select_card(str(path), modifiers, checkbox=True)


def _bulk(qtbot, grid, operation, tags, records):
    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000) as emitted:
        grid.start_bulk_tags(operation, tags, records=records)
    return emitted.args[0]


def _answer_menu(monkeypatch, answer):
    class Menu(clip_grid.QMenu):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._safety_timer = QTimer(self)
            self._safety_timer.setSingleShot(True)
            self._safety_timer.timeout.connect(self.close)
            self._safety_timer.start(250)

        def exec(self, position):
            self._safety_timer.stop()
            return answer(self, position)

    monkeypatch.setattr(clip_grid, "QMenu", Menu)


def _open_editor(root, path):
    root._on_clip_opened(str(path), QPixmap(), QRect())
    root.wait_for_editor_page()
    return root._active_clip_viewer.clip_page


@pytest.mark.parametrize("tag_grid", [120], indirect=True)
def test_add_remove_is_idempotent_and_applies_to_offscreen_selected_records(
        tag_grid, qtbot):
    state = tag_grid
    grid, manager = state.grid, state.manager
    first = Path(next(iter(grid._thumb_widgets)))
    offscreen = next(path for path in state.paths
                     if str(path) not in grid._thumb_widgets)
    first_identity = dict(manager.inspect_media(first).stat)
    assert manager.set_details(
        str(first), tags=("Shared",), expected_identity=first_identity)

    _select(state, first)
    _select(state, offscreen, toggle=True)
    captured = tuple(grid.selected_records)
    assert {record.path for record in captured} == {str(first), str(offscreen)}
    assert str(offscreen) not in grid._thumb_widgets

    added = _bulk(qtbot, grid, "add", ("Shared",), captured)
    assert added["operation"] == "add"
    assert {str(path) for path in added["changed"]} == {str(offscreen)}
    assert {str(path) for path in added["unchanged"]} == {str(first)}
    assert manager.get_tags(str(first)) == ("Shared",)
    assert manager.get_tags(str(offscreen)) == ("Shared",)

    repeated = _bulk(qtbot, grid, "add", ("Shared",), captured)
    assert repeated["changed"] == ()
    assert len(repeated["unchanged"]) == 2

    removed = _bulk(qtbot, grid, "remove", ("Shared",), captured)
    assert {str(path) for path in removed["changed"]} == {str(first), str(offscreen)}
    assert manager.get_tags(str(first)) == ()
    assert manager.get_tags(str(offscreen)) == ()


def test_single_record_replace_uses_the_captured_library_identity(tag_grid, qtbot):
    state = tag_grid
    path = state.paths[0]
    record = _record(state, path)
    identity = dict(record.file_identity)
    assert state.manager.set_details(
        str(path), tags=("old", "another"), expected_identity=identity)

    result = _bulk(qtbot, state.grid, "replace", ("new", "two,quoted"), (record,))

    assert result["operation"] == "replace"
    assert {str(item) for item in result["changed"]} == {str(path)}
    assert state.manager.get_tags(str(path)) == ("new", "two,quoted")


def test_single_card_edit_can_register_an_unregistered_record(tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0].parent / "not-yet-registered.mp4"
    path.write_bytes(b"synthetic media not registered by browse")
    assert manager.resolve_media(path) is None

    record = grid._records_from_paths({str(path)})[
        clip_grid.canonical_media_path(path)]
    grid._replace_records({**grid._records, record.identity: record})
    card = grid._create_thumbnail(record.path, 320)
    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text",
        lambda *_args, **_kwargs: ("new tag", True))

    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000) as emitted:
        card._tag_edit_handler(
            record.path, record, card._library_generation,
            grid.library_details_for(record.path), card)

    assert emitted.args[0]["changed"] == (record.path,)
    assert manager.resolve_media(path) is not None
    assert manager.get_tags(path) == ("new tag",)


def test_card_generation_change_while_edit_dialog_is_open_blocks_write(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    original_path, rebound_path = state.paths[:2]
    record = _record(state, original_path)

    # clip-000 may be outside the virtual viewport after the initial layout.
    # Scroll to its real section/row and wait for that target to materialize;
    # the generation/recycling assertions below must exercise a real card.
    section_key = next(
        key for key, files in grid._section_files.items()
        if record.path in files)
    if section_key in grid._virtual_sections:
        index = grid._section_files[section_key].index(record.path)
        row, _column = divmod(index, max(1, grid._current_columns))
        canvas = grid._virtual_sections[section_key]
        target = canvas.mapTo(grid, QPoint(
            0, row * grid._virtual_row_height()))
        state.area.ensureVisible(target.x(), target.y(), 0, 0)
    qtbot.waitUntil(
        lambda: (record.path in grid._thumb_widgets
                 and not grid._virtual_update_timer.isActive()
                 and not grid._resize_timer.isActive()),
        timeout=5000)
    card = grid._thumb_widgets[record.path]

    def change_card_during_dialog(*_args, **_kwargs):
        grid._release_virtual_card(record.path)
        rebound = grid._create_thumbnail(str(rebound_path), 320)
        assert rebound is card
        return "must not be written", True

    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text", change_card_during_dialog)
    card._tag_edit_handler(
        record.path, record, card._library_generation,
        grid.library_details_for(record.path), card)

    assert card.file_path == str(rebound_path)
    assert manager.get_tags(original_path) == ()
    assert manager.get_tags(rebound_path) == ()
    assert grid._bulk_job is None


@pytest.mark.parametrize("mutation", ["album", "single_flag", "bulk_flag"])
def test_legacy_tag_survives_grid_adoption_and_prefills_single_edit(
        tag_grid, qtbot, monkeypatch, tmp_path, mutation):
    import json

    state = tag_grid
    grid, path = state.grid, state.paths[0]
    legacy_path = tmp_path / f"legacy-{mutation}.json"
    legacy_path.write_text(json.dumps({str(path): {
        "tag": "Alt", "description": "legacy description",
    }}), encoding="utf-8")
    manager = ClipMetadataManager(
        database_path=tmp_path / f"legacy-{mutation}.sqlite3",
        legacy_path=legacy_path)
    manager.migrate(target_version=5)
    assert manager.resolve_media(path).state.name == "LEGACY_UNVERIFIED"

    grid._metadata_manager = manager
    for card in grid._thumb_widgets.values():
        card._metadata_manager = manager
    grid._library_details_cache.clear()
    grid.refresh_library_details()
    record = _record(state, path)
    assert grid.library_details_for(record.path) is None

    if mutation == "album":
        album = manager.create_album("Adopt legacy")
        grid._refresh_album_catalog()
        with qtbot.waitSignal(grid.bulk_albums_finished, timeout=5000):
            grid.start_bulk_album_membership(album.album_id, records=(record,))
    elif mutation == "single_flag":
        card = grid._thumb_widgets[record.path]
        card._toggle_favorite(
            expected_path=record.path,
            expected_generation=card._library_generation)
    else:
        _select(state, path)
        with qtbot.waitSignal(grid.bulk_flags_finished, timeout=5000):
            grid.start_bulk_flags(favorite=True)

    cached = grid.library_details_for(record.path)
    assert cached is not None
    assert cached["tags"] == ("Alt",)
    assert cached["description"] == "legacy description"
    assert manager.get_tags(path) == ("Alt",)

    card = grid._thumb_widgets[record.path]
    prompts = []

    def inspect_edit_prompt(*_args, **kwargs):
        prompts.append(kwargs)
        return "", False

    monkeypatch.setattr(
        clip_grid.FthrInputDialog, "get_text", inspect_edit_prompt)
    card._tag_edit_handler(
        record.path, record, card._library_generation, cached, card)
    assert len(prompts) == 1
    assert "Alt" in prompts[0].get("text", "")
    assert manager.get_tags(path) == ("Alt",)


def test_bulk_job_uses_records_captured_before_selection_changes(tag_grid, qtbot):
    state = tag_grid
    grid = state.grid
    first, second, later = state.paths[:3]
    _select(state, first)
    _select(state, second, toggle=True)
    captured = tuple(grid.selected_records)
    grid.clear_selection()
    _select(state, later)
    assert {record.path for record in grid.selected_records} == {str(later)}

    result = _bulk(qtbot, grid, "add", ("Captured",), captured)

    assert {str(path) for path in result["changed"]} == {str(first), str(second)}
    assert state.manager.get_tags(str(first)) == ("Captured",)
    assert state.manager.get_tags(str(second)) == ("Captured",)
    assert state.manager.get_tags(str(later)) == ()


def test_partial_batch_commits_current_record_but_refuses_stale_replacement(
        tag_grid, qtbot):
    import json
    import sqlite3

    state = tag_grid
    current, stale = state.paths[:2]
    current_record, stale_record = _record(state, current), _record(state, stale)
    manager = state.manager
    old_binding = manager.resolve_media(stale)
    assert old_binding is not None and old_binding.clip_id is not None
    old_identity = dict(stale_record.file_identity)
    assert manager.set_details(
        str(stale), tags=("Historical tag",), expected_identity=old_identity)
    state.grid.refresh_library_details()
    old_metadata = manager.get_by_id(old_binding.clip_id)
    with sqlite3.connect(manager._database) as db:
        old_sql_value = db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (old_binding.clip_id,)
        ).fetchone()[0]
    retired = stale.with_suffix(".retired")
    stale.replace(retired)
    stale.write_bytes(b"new same-path replacement")
    replacement = state.grid._records_from_paths({str(stale)})[
        clip_grid.canonical_media_path(stale)]
    assert replacement.file_identity != stale_record.file_identity

    result = _bulk(qtbot, state.grid, "add", ("Current only",),
                   (current_record, stale_record))

    assert {str(path) for path in result["changed"]} == {str(current)}
    assert [str(path) for path, _reason in result["failures"]] == [str(stale)]
    assert manager.get_tags(str(current)) == ("Current only",)
    assert manager.get_by_id(old_binding.clip_id) == old_metadata
    assert manager.get_tags(str(stale)) == ()
    assert clip_grid.canonical_media_path(stale) not in manager.metadata_bindings()
    with sqlite3.connect(manager._database) as db:
        current_sql_value = db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (old_binding.clip_id,)
        ).fetchone()[0]
    assert json.loads(current_sql_value) == json.loads(old_sql_value)
    assert len(state.warnings) == 1
    title, message, options = state.warnings[0]
    assert title == "Some tag changes failed"
    assert message.startswith("1 changed, 0 unchanged, 1 failed\n")
    assert stale.name in message
    assert result["failures"][0][1] in message
    assert options.get("plain_text") is True


@pytest.mark.parametrize("tag_grid", [120], indirect=True)
def test_rebound_card_menu_retains_edit_tags_action(tag_grid, monkeypatch):
    state = tag_grid
    grid = state.grid
    assert grid._thumb_widgets
    old_path = next(iter(grid._thumb_widgets))
    card = grid._thumb_widgets[old_path]
    target_path = next(path for path in state.paths
                       if str(path) not in grid._thumb_widgets)
    target_record = _record(state, target_path)
    grid._release_virtual_card(old_path)
    assert card in grid._recycled_cards
    rebound = grid._create_thumbnail(target_record.path, 320)
    assert rebound is card
    menus = []

    def inspect_menu(menu, *_args, **_kwargs):
        menus.append(tuple(action.text() for action in menu.actions()))

    _answer_menu(monkeypatch, inspect_menu)
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(3, 3), QPoint(12, 12))

    card.contextMenuEvent(event)

    assert card.file_path == target_record.path
    assert len(menus) == 1
    assert any(label.startswith("Edit tags") for label in menus[0])


def test_pending_mutation_and_not_ready_records_fail_closed(tag_grid, qtbot):
    state = tag_grid
    grid = state.grid
    not_ready, pending = state.paths[:2]
    grid.set_readiness_checker(lambda path: clip_grid.canonical_media_path(path)
                               != clip_grid.canonical_media_path(not_ready))
    assert grid._prepare_card_mutation(str(pending)) == ""
    try:
        records = (_record(state, not_ready), _record(state, pending))
        result = _bulk(qtbot, grid, "add", ("Blocked",), records)
    finally:
        grid._finish_card_mutation(str(pending))

    assert result["changed"] == ()
    assert {str(path) for path, _reason in result["failures"]} == {
        str(not_ready), str(pending)}
    assert state.manager.get_tags(str(not_ready)) == ()
    assert state.manager.get_tags(str(pending)) == ()
    assert len(state.warnings) == 1
    title, message, options = state.warnings[0]
    assert title == "Some tag changes failed"
    assert message.startswith("0 changed, 0 unchanged, 2 failed\n")
    for path, reason in result["failures"]:
        assert Path(str(path)).name in message
        assert reason in message
    assert options.get("plain_text") is True


def test_cancelled_bulk_job_reports_cancel_without_partial_false_success(tag_grid, qtbot):
    state = tag_grid
    captured = tuple(state.grid._records.values())

    with qtbot.waitSignal(state.grid.bulk_tags_finished, timeout=5000) as emitted:
        state.grid.start_bulk_tags("add", ("Cancel me",), records=captured)
        state.grid.cancel_bulk_flags()

    result = emitted.args[0]
    assert result["cancelled"] is True
    assert result["changed"] == ()
    assert result["failures"] == ()
    assert all(state.manager.get_tags(record.path) == () for record in captured)


def test_cancel_after_first_tag_commit_reports_partial_success(tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    first, second = state.paths[:2]
    records = (_record(state, first), _record(state, second))
    update = manager.update_tags
    calls = []

    def commit_then_cancel(path, **kwargs):
        changed = update(path, **kwargs)
        calls.append(path)
        if len(calls) == 1:
            grid.cancel_bulk_flags()
        return changed

    monkeypatch.setattr(manager, "update_tags", commit_then_cancel)
    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000) as emitted:
        grid.start_bulk_tags("add", ("partial",), records=records)

    result = emitted.args[0]
    assert result["cancelled"] is True
    assert result["changed"] == (str(first),)
    assert result["failures"] == ()
    assert calls == [str(first)]
    assert manager.get_tags(first) == ("partial",)
    assert manager.get_tags(second) == ()


def test_noop_bulk_refreshes_one_details_snapshot(tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    path = state.paths[0]
    record = _record(state, path)
    assert manager.set_details(
        str(path), tags=("already present",),
        expected_identity=dict(record.file_identity))
    snapshots = []
    read_snapshot = manager.metadata_bindings

    def count_snapshot():
        snapshots.append(True)
        return read_snapshot()

    monkeypatch.setattr(manager, "metadata_bindings", count_snapshot)
    result = _bulk(qtbot, grid, "add", ("already present",), (record,))

    assert result["changed"] == ()
    assert result["unchanged"] == (str(path),)
    assert snapshots == [True]


def test_cancelled_before_first_tick_consumes_pending_details_debt_once(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid, manager = state.grid, state.manager
    record = _record(state, state.paths[0])
    snapshots = []
    applies = []
    read_snapshot = manager.metadata_bindings
    clear_sections = grid._clear_sections

    def count_snapshot():
        snapshots.append(True)
        return read_snapshot()

    def count_apply():
        applies.append(True)
        clear_sections()

    monkeypatch.setattr(manager, "metadata_bindings", count_snapshot)
    monkeypatch.setattr(grid, "_clear_sections", count_apply)
    grid.start_bulk_tags("add", ("not committed",), records=(record,))
    grid._bulk_timer.stop()
    grid.refresh_library_details()
    assert snapshots == [True]
    assert grid._details_rebuild_pending is True
    grid.cancel_bulk_flags()

    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000) as emitted:
        grid._bulk_timer.start(0)

    assert emitted.args[0]["cancelled"] is True
    assert emitted.args[0]["changed"] == ()
    assert snapshots == [True]
    assert len(applies) == 1
    assert manager.get_tags(record.path) == ()


def test_inactive_cancel_does_not_create_an_extra_grid_apply(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid = state.grid
    record = _record(state, state.paths[0])
    applies = []
    clear_sections = grid._clear_sections

    def count_apply():
        applies.append(True)
        clear_sections()

    monkeypatch.setattr(grid, "_clear_sections", count_apply)
    grid.start_bulk_tags("add", ("not committed",), records=(record,))
    grid._bulk_timer.stop()
    grid.cancel_bulk_flags()

    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000) as emitted:
        grid._bulk_timer.start(0)

    assert emitted.args[0]["cancelled"] is True
    assert emitted.args[0]["changed"] == ()
    assert applies == []


def test_main_details_route_deduplicates_active_and_parked_editor(
        editor_shell, tag_grid, monkeypatch):
    from main import MainWindow

    root = editor_shell
    path = tag_grid.paths[0]
    calls = {"snapshot": 0, "viewer": []}
    previous_grid = root.clip_grid
    previous_active = root._active_clip_viewer
    had_parked = hasattr(root, "_parked_editor_shell")
    previous_parked = getattr(root, "_parked_editor_shell", None)

    class Grid:
        def refresh_library_details(self, _path):
            calls["snapshot"] += 1

        def library_details_for(self, _path):
            return {"identity": {"size": 1}, "tags": (),
                    "description": "", "error": None}

    viewer = SimpleNamespace(
        clip_path=str(path),
        refresh_library_details=lambda current, *, details: calls["viewer"].append(
            (current, details)))
    try:
        root.clip_grid = Grid()
        root._active_clip_viewer = viewer
        root._parked_editor_shell = viewer

        MainWindow._on_library_details_changed(root, str(path), refresh_grid=True)
        assert calls["snapshot"] == 1
        assert len(calls["viewer"]) == 1

        MainWindow._on_library_details_changed(root, str(path), refresh_grid=False)
        assert calls["snapshot"] == 1
        assert len(calls["viewer"]) == 2
    finally:
        root.clip_grid = previous_grid
        root._active_clip_viewer = previous_active
        if had_parked:
            root._parked_editor_shell = previous_parked
        elif hasattr(root, "_parked_editor_shell"):
            del root._parked_editor_shell


@pytest.mark.parametrize("tag_grid", [120], indirect=True)
def test_details_snapshot_refresh_is_one_read_and_cached_card_menu_is_io_free(
        tag_grid, monkeypatch, qtbot):
    state = tag_grid
    grid, manager = state.grid, state.manager
    reads = []
    original_bindings = manager.metadata_bindings

    def count_snapshot():
        reads.append(True)
        return original_bindings()

    monkeypatch.setattr(manager, "metadata_bindings", count_snapshot)
    grid.refresh_library_details()
    assert reads == [True]
    qtbot.waitUntil(lambda: bool(grid._thumb_widgets), timeout=5000)

    card_path = next(iter(grid._thumb_widgets))
    card = grid._thumb_widgets[card_path]
    visible_key = clip_grid.canonical_media_path(card_path)
    visible_record = grid._records[visible_key]
    materialized_keys = {
        clip_grid.canonical_media_path(path) for path in grid._thumb_widgets}
    offscreen_key = next(
        path for path in grid._records if path not in materialized_keys)
    offscreen_record = grid._records[offscreen_key]
    menus = []

    def inspect_menu(menu, *_args, **_kwargs):
        menus.append(tuple(action.text() for action in menu.actions()))

    _answer_menu(monkeypatch, inspect_menu)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cached card tag lookup must not perform metadata or stat I/O")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "metadata_bindings", forbidden)
        patch.setattr(manager, "get", forbidden)
        patch.setattr(manager, "get_tags", forbidden)
        patch.setattr(clip_grid.os, "stat", forbidden)
        visible_details = grid.library_details_for(visible_record.path)
        offscreen_details = grid.library_details_for(offscreen_record.path)
        assert visible_details["identity"] == visible_record.file_identity
        assert offscreen_details["identity"] == offscreen_record.file_identity
        original_visible_tags = tuple(visible_details["tags"])
        visible_details["identity"]["size"] = -1
        visible_details["tags"] = (*original_visible_tags, "caller mutation")
        assert grid.library_details_for(visible_record.path)["identity"] == (
            visible_record.file_identity)
        assert tuple(grid.library_details_for(visible_record.path)["tags"]) == original_visible_tags
        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(3, 3), QPoint(12, 12))
        card.contextMenuEvent(event)
        assert len(menus) == 1
        assert any(label.startswith("Edit tags") for label in menus[0])


@pytest.mark.parametrize("dirty_field", ["tags", "description"])
def test_grid_details_update_only_clean_active_editor_field(
        editor_shell, tag_grid, monkeypatch, dirty_field):
    state, root = tag_grid, editor_shell
    path = state.paths[0]
    manager, grid = state.manager, state.grid
    identity = dict(manager.inspect_media(path).stat)
    assert manager.set_details(
        str(path), tags=("initial tag",), description="initial description",
        expected_identity=identity)
    root.clip_metadata_manager = manager
    viewer = _open_editor(root, path)
    captured_identity = dict(viewer._details_identity)
    player = viewer.player
    loaded_path = viewer.clip_path
    if dirty_field == "tags":
        viewer._tag_edit.setText("unsaved tag draft")
        viewer._tag_edit.setModified(True)
    else:
        viewer._desc_edit.setText("unsaved description draft")
        viewer._desc_edit.setModified(True)

    assert manager.set_details(
        str(path), tags=("latest committed tag",),
        description="latest committed description", expected_identity=identity)
    grid.refresh_library_details(str(path))
    details = grid.library_details_for(str(path))
    assert details is not None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("viewer refresh must consume supplied grid snapshot")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(manager, "get", forbidden)
            patch.setattr(manager, "get_tags", forbidden)
            patch.setattr(manager, "set_details", forbidden)
            viewer.refresh_library_details(str(path), details=details)

        assert viewer._details_identity == captured_identity
        assert viewer.clip_path == loaded_path
        assert viewer.player is player
        if dirty_field == "tags":
            assert viewer._tag_edit.text() == "unsaved tag draft"
            assert viewer._tag_edit.isModified()
            assert viewer._desc_edit.text() == "latest committed description"
            assert not viewer._desc_edit.isModified()
        else:
            assert viewer._tag_edit.text() == "latest committed tag"
            assert not viewer._tag_edit.isModified()
            assert viewer._desc_edit.text() == "unsaved description draft"
            assert viewer._desc_edit.isModified()
    finally:
        viewer._tag_edit.setModified(False)
        viewer._desc_edit.setModified(False)


def test_replacement_details_do_not_refresh_old_editor_or_player(
        editor_shell, tag_grid, monkeypatch):
    state, root = tag_grid, editor_shell
    path = state.paths[1]
    manager, grid = state.manager, state.grid
    original_identity = dict(manager.inspect_media(path).stat)
    assert manager.set_details(
        str(path), tags=("original tag",), description="original description",
        expected_identity=original_identity)
    root.clip_metadata_manager = manager
    viewer = _open_editor(root, path)
    captured_identity = dict(viewer._details_identity)
    original_player = viewer.player
    original_source = viewer._playback_path
    original_tag_text = viewer._tag_edit.text()
    original_description = viewer._desc_edit.text()

    grid.refresh_library_details(str(path))
    mismatched_details = grid.library_details_for(str(path))
    assert mismatched_details is not None
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(b"new same-path identity")
    replacement.replace(path)
    replacement_identity = dict(manager.inspect_media(path).stat)
    assert replacement_identity != captured_identity
    mismatched_details["identity"] = replacement_identity
    mismatched_details["tags"] = ["replacement-only tag"]
    mismatched_details["description"] = "replacement-only description"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("viewer refresh must not inspect replacement or use manager")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "get", forbidden)
        patch.setattr(manager, "get_tags", forbidden)
        patch.setattr(clip_grid.os, "stat", forbidden)
        viewer.refresh_library_details(str(path), details=mismatched_details)

    assert viewer._details_identity == captured_identity
    assert viewer.clip_path == str(path)
    assert viewer.player is original_player
    assert viewer._playback_path == original_source
    assert viewer._tag_edit.text() == original_tag_text
    assert viewer._desc_edit.text() == original_description


def test_tag_update_defers_one_apply_until_mutation_finishes_and_scan_is_unchanged(
        tag_grid, qtbot, monkeypatch):
    state = tag_grid
    grid = state.grid
    grid._scanning_started = True
    initial_generation = grid._scan_generation
    grid._begin_scan("foreground_refresh")
    qtbot.waitUntil(
        lambda: grid._scan_generation > initial_generation
        and grid._active_scan_worker is None,
        timeout=10000)

    committed, held = state.paths[:2]
    committed_record, held_record = _record(state, committed), _record(state, held)
    _select(state, committed)
    _select(state, held, toggle=True)
    assert committed_record in grid.selected_records and held_record in grid.selected_records
    assert grid._prepare_card_mutation(held_record.path) == ""
    actual_applies = []
    clear_sections = grid._clear_sections

    def count_actual_apply():
        actual_applies.append(True)
        clear_sections()

    monkeypatch.setattr(grid, "_clear_sections", count_actual_apply)
    with qtbot.waitSignal(grid.bulk_tags_finished, timeout=5000):
        grid.start_bulk_tags("add", ("Committed",), records=(committed_record,))

    assert state.manager.get_tags(str(committed)) == ("Committed",)
    assert actual_applies == []
    records_before_scan = grid._records
    generation_before = grid._scan_generation
    grid._finish_card_mutation(held_record.path)
    qtbot.waitUntil(
        lambda: grid._scan_generation > generation_before
        and grid._active_scan_worker is None,
        timeout=10000)

    assert grid._records is records_before_scan
    assert len(actual_applies) == 1
    assert committed_record in grid.selected_records
    assert held_record in grid.selected_records
