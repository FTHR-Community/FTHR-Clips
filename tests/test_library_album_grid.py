"""User-facing album filtering checks using synthetic media and an isolated DB."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QFontDatabase

from core.clip_metadata_manager import ClipMetadataManager
from ui import clip_grid


@pytest.fixture
def album_grid(qapp, qtbot, tmp_path, monkeypatch):
    # Match the application's startup font registration so offscreen Qt
    # captures render the bundled project font rather than empty glyph boxes.
    font_dir = Path(__file__).resolve().parents[1] / "FTHR_UI" / "assets" / "fonts"
    if font_dir.is_dir() and not any(
            "Oswald" in family for family in QFontDatabase.families()):
        for font_path in sorted(font_dir.glob("Oswald-*.ttf")):
            QFontDatabase.addApplicationFont(str(font_path))

    owned = tmp_path / "owned"
    owned.mkdir()
    paths = [owned / "first.mp4", owned / "second.mp4", owned / "third.mp4"]
    for path in paths:
        path.write_bytes(("synthetic:" + path.name).encode())

    manager = ClipMetadataManager(database_path=tmp_path / "metadata.sqlite3")
    manager.migrate(target_version=4)
    for path in paths:
        manager.register_media(manager.inspect_media(path))
    album = manager.create_album("Selected")
    for path in paths[:2]:
        manager.add_album_member(
            album.album_id, path, expected_identity=dict(manager.inspect_media(path).stat))

    settings = SimpleNamespace(
        get=lambda key, default=None: {
            "clips_directory": str(owned), "imported_clip_folders": [],
        }.get(key, default),
        set=lambda *args: None,
        save_settings=lambda: None,
    )
    grid = clip_grid.ClipGrid(settings, metadata_manager=manager, autostart=False)
    qtbot.addWidget(grid)
    monkeypatch.setattr(grid, "_start_thumbnail_worker", lambda *args: None)
    grid._records = grid._records_from_paths({str(path) for path in paths})
    grid.show()
    qtbot.waitUntil(lambda: grid.isVisible())
    yield SimpleNamespace(grid=grid, manager=manager, album=album, paths=paths)
    grid.shutdown()


def _select_album(grid, album_id):
    combo = grid.album_combo
    index = combo.findData(album_id)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_album_selector_filters_by_scanned_identity_without_sql_or_filesystem_io(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    _select_album(grid, state.album.album_id)
    expected = {str(path) for path in state.paths[:2]}

    def forbidden(*args, **kwargs):
        raise AssertionError("album filtering must use the in-memory snapshot")

    # Keep the no-I/O contract assertion scoped to the filter call.  Qt and
    # pytest may process events after the test body, and must retain real stat.
    with monkeypatch.context() as patch:
        patch.setattr(state.manager, "album_bindings", forbidden)
        patch.setattr(clip_grid.os, "stat", forbidden)
        assert {record.path for record in grid._filtered_records()} == expected


def test_album_filter_does_not_inherit_membership_for_replacement_at_same_path(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    replaced = state.paths[0]
    original_record = grid._records[clip_grid.canonical_media_path(replaced)]
    replaced.unlink()
    replaced.write_bytes(b"replacement synthetic content")
    # Model a completed library scan: its record carries the new file identity.
    grid._records = grid._records_from_paths({str(path) for path in state.paths})
    assert grid._records[clip_grid.canonical_media_path(replaced)].file_identity != original_record.file_identity
    _select_album(grid, state.album.album_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("album filtering must not stat media or query SQLite per card")

    with monkeypatch.context() as patch:
        patch.setattr(state.manager, "album_bindings", forbidden)
        patch.setattr(clip_grid.os, "stat", forbidden)
        visible = {record.path for record in grid._filtered_records()}
    assert str(state.paths[1]) in visible
    assert str(replaced) not in visible


def test_all_albums_selector_restores_unfiltered_library(album_grid):
    state = album_grid
    grid = state.grid
    _select_album(grid, state.album.album_id)
    assert len(grid._filtered_records()) == 2
    all_index = grid.album_combo.findData(None)
    assert all_index >= 0
    grid.album_combo.setCurrentIndex(all_index)
    assert {record.path for record in grid._filtered_records()} == {
        str(path) for path in state.paths}


def test_album_catalog_uses_stable_ids_and_all_albums_has_no_id(album_grid):
    state = album_grid
    grid = state.grid

    assert grid.album_combo.itemData(0) is None
    assert grid.album_combo.itemText(0) == "All albums"
    index = grid.album_combo.findData(state.album.album_id)
    assert index > 0
    assert grid.album_combo.itemText(index) == state.album.name
    _select_album(grid, state.album.album_id)
    assert grid._album_id == state.album.album_id


def test_album_manage_actions_create_rename_delete_without_touching_media(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    original_bytes = {path: path.read_bytes() for path in state.paths}
    actions = grid._album_manage_menu.actions()
    assert [action.text() for action in actions] == ["New", "Rename", "Delete"]

    monkeypatch.setattr(clip_grid.FthrInputDialog, "get_text",
                        lambda *args, **kwargs: ("Created from menu", True))
    actions[0].trigger()
    created = next(album for album in state.manager.list_albums()
                   if album.name == "Created from menu")
    assert grid._album_id == created.album_id

    monkeypatch.setattr(clip_grid.FthrInputDialog, "get_text",
                        lambda *args, **kwargs: ("Renamed from menu", True))
    actions[1].trigger()
    assert next(album for album in state.manager.list_albums()
                if album.album_id == created.album_id).name == "Renamed from menu"

    monkeypatch.setattr(clip_grid.FthrMessageDialog, "question",
                        lambda *args, **kwargs: True)
    actions[2].trigger()
    assert all(album.album_id != created.album_id
               for album in state.manager.list_albums())
    assert {path: path.read_bytes() for path in state.paths} == original_bytes


def test_delete_confirmation_cannot_switch_target_to_another_album(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    first = state.album
    second = state.manager.create_album("Keep this one")
    grid._refresh_album_catalog(selected_id=first.album_id)
    _select_album(grid, first.album_id)

    def switch_during_confirmation(*args, **kwargs):
        _select_album(grid, second.album_id)
        return True

    monkeypatch.setattr(clip_grid.FthrMessageDialog, "question",
                        switch_during_confirmation)
    grid._album_manage_menu.actions()[2].trigger()
    remaining = {album.album_id for album in state.manager.list_albums()}
    assert remaining == {first.album_id, second.album_id}


def test_rename_prompt_cannot_switch_target_to_another_album(album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    first = state.album
    second = state.manager.create_album("Other album")
    grid._refresh_album_catalog(selected_id=first.album_id)
    _select_album(grid, first.album_id)

    def switch_during_prompt(*args, **kwargs):
        _select_album(grid, second.album_id)
        return "Wrong target", True

    monkeypatch.setattr(clip_grid.FthrInputDialog, "get_text", switch_during_prompt)
    grid._album_manage_menu.actions()[1].trigger()
    names = {album.album_id: album.name for album in state.manager.list_albums()}
    assert names[first.album_id] == first.name
    assert names[second.album_id] == second.name


def test_bulk_assignment_uses_captured_records_reports_stale_evidence_and_rebuilds_once(
        album_grid, qtbot, monkeypatch):
    state = album_grid
    grid = state.grid
    target = state.manager.create_album("Bulk target")
    grid._refresh_album_catalog()
    _select_album(grid, target.album_id)
    captured = tuple(grid._records.values())
    # Simulate one selected item becoming a replacement after selection.
    stale_path = state.paths[2]
    stale_path.unlink()
    stale_path.write_bytes(b"new synthetic identity")

    rebuilds = []
    apply_index = grid._apply_current_index

    def count_rebuild():
        rebuilds.append(True)
        apply_index()

    monkeypatch.setattr(grid, "_apply_current_index", count_rebuild)
    with qtbot.waitSignal(grid.bulk_albums_finished, timeout=5000) as emitted:
        grid.start_bulk_album_membership(
            target.album_id, records=captured)

    result = emitted.args[0]
    assert result["album_id"] == target.album_id
    assert result["remove"] is False
    assert {str(path) for path in result["changed"]} == {
        str(state.paths[0]), str(state.paths[1])}
    assert [path for path, _reason in result["failures"]] == [str(stale_path)]
    assert result["cancelled"] is False
    assert len(rebuilds) == 1
    assert set(state.manager.album_bindings(target.album_id)) == {
        clip_grid.canonical_media_path(state.paths[0]),
        clip_grid.canonical_media_path(state.paths[1])}


def test_start_scanning_refreshes_album_catalog_after_schema_migration(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    late = state.manager.create_album("Migrated catalog")
    monkeypatch.setattr(grid, "_watch_subdir", lambda _path: None)
    monkeypatch.setattr(grid, "_load_clips", lambda: None)

    grid.start_scanning()

    assert grid.album_combo.findData(late.album_id) >= 0
    assert grid.album_combo.itemText(grid.album_combo.findData(late.album_id)) == late.name


def test_index_refresh_reads_new_ram_membership_snapshot(album_grid):
    state = album_grid
    grid = state.grid
    _select_album(grid, state.album.album_id)
    third = state.paths[2]
    assert str(third) not in {record.path for record in grid._filtered_records()}

    identity = dict(state.manager.inspect_media(third).stat)
    assert state.manager.add_album_member(
        state.album.album_id, third, expected_identity=identity) is True
    # Re-applying the current index is the same boundary used by scan/refresh.
    grid._apply_current_index()
    assert {record.path for record in grid._filtered_records()} == {
        str(path) for path in state.paths}


def test_bulk_album_job_can_be_cancelled_before_processing(album_grid, qtbot):
    state = album_grid
    grid = state.grid
    album = state.manager.create_album("Cancel target")
    grid._refresh_album_catalog()
    captured = tuple(grid._records.values())

    with qtbot.waitSignal(grid.bulk_albums_finished, timeout=5000) as emitted:
        grid.start_bulk_album_membership(album.album_id, records=captured)
        grid.cancel_bulk_flags()

    result = emitted.args[0]
    assert result["cancelled"] is True
    assert result["changed"] == ()
    assert result["failures"] == ()
    assert state.manager.album_bindings(album.album_id) == {}


def test_album_remove_debt_rebuilds_after_unrelated_mutation_and_unchanged_scan(
        album_grid, qtbot):
    state = album_grid
    grid = state.grid
    # Start from records produced by the real scanner, so the later control
    # scan has an unchanged result and exercises next_records == _records.
    grid._scanning_started = True
    initial_generation = grid._scan_generation
    grid._begin_scan("foreground_refresh")
    qtbot.waitUntil(
        lambda: (grid._scan_generation > initial_generation
                 and grid._active_scan_worker is None),
        timeout=10000)

    first_path, second_path = state.paths[:2]
    _select_album(grid, state.album.album_id)
    first_record = grid._records[clip_grid.canonical_media_path(first_path)]
    second_record = grid._records[clip_grid.canonical_media_path(second_path)]
    grid._select_card(first_record.path, clip_grid.Qt.KeyboardModifier.NoModifier,
                      checkbox=True)
    assert first_record in grid.selected_records

    # Hold a separate card mutation open while the album removal commits.
    assert grid._prepare_card_mutation(second_record.path) == ""
    with qtbot.waitSignal(grid.bulk_albums_finished, timeout=5000):
        grid.start_bulk_album_membership(
            state.album.album_id, remove=True, records=(first_record,))

    assert str(first_path) in {
        path for files in grid._section_files.values() for path in files}
    assert state.manager.album_bindings(state.album.album_id)

    # Let the production scanner and its real completion callback run.  The
    # fixture's three synthetic MP4 files remain unchanged, so the scan must
    # follow the next_records == _records path.
    records_before_scan = grid._records
    generation_before = grid._scan_generation
    grid._finish_card_mutation(second_record.path)
    qtbot.waitUntil(
        lambda: (grid._scan_generation > generation_before
                 and grid._active_scan_worker is None),
        timeout=10000)

    assert grid._records is records_before_scan
    section_paths = {path for files in grid._section_files.values() for path in files}
    assert str(first_path) not in section_paths
    assert str(second_path) in section_paths
    assert first_record not in grid.selected_records
    assert second_record in grid._filtered_records()


def test_saved_clip_upsert_keeps_existing_album_filter_membership(album_grid):
    state = album_grid
    grid = state.grid
    _select_album(grid, state.album.album_id)

    grid.upsert_saved_clip(str(state.paths[0]), ready=True)

    visible = {record.path for record in grid._filtered_records()}
    assert str(state.paths[0]) in visible
    assert str(state.paths[1]) in visible
    assert str(state.paths[2]) not in visible


@pytest.mark.parametrize(
    "name",
    ("<b>" + ("Long album label " * 4) + "</b>", "W" * 80),
    ids=("literal-html", "wide-W-80"),
)
def test_long_literal_html_album_label_stays_bounded_in_toolbar(
        album_grid, qapp, name):
    state = album_grid
    grid = state.grid
    album = state.manager.create_album(name)
    grid._refresh_album_catalog()
    _select_album(grid, album.album_id)
    index = grid.album_combo.findData(album.album_id)
    assert grid.album_combo.itemText(index) == name
    assert grid.album_combo.itemData(index, clip_grid.Qt.ItemDataRole.ToolTipRole) == name
    assert grid._all_count_label.accessibleName().startswith(name)
    assert grid._all_count_label.toolTip() == name
    assert grid._all_count_label.textFormat() == clip_grid.Qt.TextFormat.PlainText
    assert grid.album_combo.width() <= grid.album_combo.maximumWidth()
    assert grid.album_combo.isHidden()
    assert not grid.album_combo.isVisible()
    assert grid.album_manage_btn.isHidden()
    assert not grid.album_manage_btn.isVisible()

    capture_dir = os.environ.get("F07_ALBUM_CAPTURE_DIR")
    if capture_dir:
        Path(capture_dir).mkdir(parents=True, exist_ok=True)
    for width in (1100, 1440):
        grid.resize(width, 720)
        grid.ensurePolished()
        layout = grid.layout
        if layout is not None:
            layout.activate()
        qapp.processEvents()
        assert grid.width() == width
        pixmap = grid.grab()
        assert pixmap.width() == width
        if capture_dir:
            tag = "wide-W-80" if name == "W" * 80 else "literal-html"
            output = Path(capture_dir) / f"album-toolbar-{tag}-{width}.png"
            assert pixmap.save(str(output))


def test_new_album_dialog_acceptance_after_shutdown_does_not_write(
        album_grid, monkeypatch):
    state = album_grid
    grid = state.grid
    before = state.manager.list_albums()

    def accept_then_shutdown(*args, **kwargs):
        grid._shutdown_started = True
        return "Must not be created", True

    monkeypatch.setattr(clip_grid.FthrInputDialog, "get_text",
                        accept_then_shutdown)
    grid._album_manage_menu.actions()[0].trigger()
    assert state.manager.list_albums() == before
