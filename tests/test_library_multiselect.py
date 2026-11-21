"""Real Qt selection gestures and bounded actions on retained test libraries."""
from __future__ import annotations

import os
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QScrollArea

from core.clip_file_operations import ClipFileOperations
from core.clip_metadata_manager import ClipMetadataManager
from ui import clip_grid


@pytest.fixture
def selection_library(request, tmp_path, monkeypatch, qtbot):
    count = getattr(request, 'param', 8)
    owned, imported = tmp_path / 'owned', tmp_path / 'imported'
    owned.mkdir()
    imported.mkdir()
    for index in range(count - 1):
        media = owned / f'clip-{index:04}.mp4'
        media.write_bytes(b'synthetic selection media')
        os.utime(media, ns=(1_700_000_000_000_000_000 + index * 1_000_000_000,) * 2)
    screenshot = imported / 'screenshot.png'
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    assert image.save(str(screenshot))
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    monkeypatch.setattr(clip_grid.ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(owned), 'imported_clip_folders': [str(imported)],
    }.get(key, default))
    grid = clip_grid.ClipGrid(settings, metadata_manager=manager, autostart=False)
    grid.set_upload_enabled_checker(lambda: True)
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    scroll.resize(1350, 850)
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.show()
    grid.start_scanning()
    qtbot.waitUntil(lambda: len(grid._records) == count and grid._active_scan_worker is None,
                    timeout=10000)
    grid.refresh_timer.stop()
    grid._debounce_timer.stop()
    grid._watcher.blockSignals(True)
    qtbot.waitUntil(lambda: bool(grid._thumb_widgets))
    warnings = []
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    yield SimpleNamespace(grid=grid, scroll=scroll, manager=manager, owned=owned,
                          imported=imported, warnings=warnings)
    grid.shutdown()


def _visible_card(state, *, image=False):
    cards = [card for card in state.grid._thumb_widgets.values() if card.is_video != image]
    return next(card for card in cards if card.isVisible())


@pytest.mark.parametrize('selection_library', [40], indirect=True)
def test_bulk_plugin_receives_selected_videos_including_scrolled_out_rows(
        selection_library, qtbot):
    grid = selection_library.grid
    selection_library.scroll.resize(800, 280)
    qtbot.waitUntil(lambda: len(grid._thumb_widgets) < len(grid._records))
    sent = []
    grid._bulk_clip_actions_provider = lambda paths: [('Send to Phone…', lambda: sent.append(paths))]

    grid.select_all_results()
    menu = grid.bulk_actions_btn.menu()
    menu.aboutToShow.emit()
    action = next(action for action in menu.actions() if action.text() == 'Send to Phone…')
    action.trigger()
    assert sent == [tuple(record.path for record in grid.selected_records if record.kind == 'video')]
    assert len(sent[0]) == len(grid.selected_records) - 1

    grid.clear_selection()
    action.trigger()
    assert len(sent) == 1
    menu.aboutToShow.emit()
    assert 'Send to Phone…' not in [action.text() for action in menu.actions()]


@pytest.mark.parametrize('target_name', ['_thumb_frame', 'thumb_label'])
@pytest.mark.parametrize('exit_action', ['done', 'escape'])
def test_real_thumbnail_clicks_require_explicit_selection_mode(
        selection_library, qtbot, target_name, exit_action):
    grid = selection_library.grid
    card = _visible_card(selection_library)
    target = getattr(card, target_name)
    opened = []
    card.opened.connect(lambda *args: opened.append(args))

    qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
    assert len(opened) == 1
    assert grid.selected_records == ()

    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    assert grid.select_mode_btn.isChecked()
    assert grid.selection_toolbar.isVisible()
    qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
    assert len(opened) == 1
    assert grid.selected_records == (grid._record_for_path(card.file_path),)

    qtbot.mouseClick(grid.clear_selection_btn, Qt.MouseButton.LeftButton)
    assert grid.selected_records == ()
    assert grid.select_mode_btn.isChecked()
    assert grid.selection_toolbar.isVisible()

    qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
    assert len(opened) == 1
    assert grid.selected_records == (grid._record_for_path(card.file_path),)

    if exit_action == 'done':
        qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    else:
        grid.setFocus()
        qtbot.keyClick(grid, Qt.Key.Key_Escape)
    assert grid.selected_records == ()
    assert not grid.select_mode_btn.isChecked()
    assert not grid.selection_toolbar.isVisible()
    assert not card.selection_checkbox.isVisible()
    assert not card.property('selected')

    qtbot.mouseClick(target, Qt.MouseButton.LeftButton)
    assert len(opened) == 2


@pytest.mark.parametrize('image', [False, True])
def test_plain_and_shift_clicks_open_outside_mode_and_control_selects_inside(
        selection_library, qtbot, image):
    state = selection_library
    card = _visible_card(state, image=image)
    opened, clicked = [], []
    card.opened.connect(lambda *args: opened.append(args))
    card.clicked.connect(clicked.append)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ShiftModifier)
    assert state.grid.selected_records == ()
    assert clicked == [card.file_path, card.file_path]
    assert len(opened) == (2 if not image else 0)
    qtbot.mouseClick(state.grid.select_mode_btn, Qt.MouseButton.LeftButton)
    assert state.grid.select_mode_btn.text() == 'Done selecting'
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    assert [record.path for record in state.grid.selected_records] == [card.file_path]
    assert len(opened) == (2 if not image else 0)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ShiftModifier)
    assert [record.path for record in state.grid.selected_records] == [card.file_path]
    assert len(opened) == (2 if not image else 0)
    qtbot.mouseClick(state.grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton)
    assert clicked == [card.file_path, card.file_path, card.file_path]
    assert len(opened) == (3 if not image else 0)


def test_mode_checkbox_and_escape_use_model_count_without_opening(selection_library, qtbot):
    grid = selection_library.grid
    card = _visible_card(selection_library)
    opened = []
    card.opened.connect(lambda *args: opened.append(args))
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    assert grid.select_mode_btn.text() == 'Done selecting'
    assert card.selection_checkbox.isVisible()
    qtbot.mouseClick(card.selection_checkbox, Qt.MouseButton.LeftButton)
    assert card.selection_checkbox.isChecked() and len(grid.selected_records) == 1
    assert grid.selection_count_label.text() == '1 selected'
    assert not opened
    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key.Key_Escape)
    assert grid.selected_records == ()
    assert not grid.select_mode_btn.isChecked()
    assert not card.selection_checkbox.isVisible()
    assert grid.select_mode_btn.text() == 'Select'


def test_shift_and_control_shift_clicks_use_the_visible_sorted_range(selection_library, qtbot):
    grid = selection_library.grid
    ordered = grid._filtered_records()
    opened = []
    grid.clip_opened.connect(lambda *args: opened.append(args))
    grid.screenshot_clicked.connect(lambda *args: opened.append(args))
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(grid._thumb_widgets[ordered[1].path].thumb_label,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
    qtbot.mouseClick(grid._thumb_widgets[ordered[4].path].thumb_label,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    assert grid.selected_records == tuple(ordered[1:5])
    qtbot.mouseClick(grid._thumb_widgets[ordered[0].path].thumb_label,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
    qtbot.mouseClick(grid._thumb_widgets[ordered[2].path].thumb_label,
                     Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert grid.selected_records == tuple(ordered[:5])
    assert not opened


def test_committed_real_rename_transfers_selection_and_keeps_metadata_identity(
        selection_library, qtbot):
    state = selection_library
    grid = state.grid
    card = _visible_card(state)
    old_path = card.file_path
    binding = state.manager.register_media(state.manager.inspect_media(old_path))
    state.manager.set_editor_draft(old_path, {'trim_start': 1, 'trim_end': 3})
    original_metadata = state.manager.get_by_id(binding.clip_id)
    service = ClipFileOperations(state.manager)
    committed = []

    def rename(old, new, allow_linked, completed):
        operation = service.prepare_rename(binding.clip_id, new, admission={
            'owned_root': str(state.owned), 'imported_roots': [str(state.imported)],
            'allow_linked': allow_linked})
        result = service.execute(operation.op_id)
        committed.append(result)
        completed(result)

    grid.set_rename_handler(rename)
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    qtbot.mouseDClick(card.title_label, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.title_edit.hasFocus())
    qtbot.keyClick(card.title_edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(card.title_edit, 'renamed-selection')
    qtbot.keyClick(card.title_edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(committed) and not card._mutation_busy)
    assert committed[0].succeeded
    new_path = str(state.owned / 'renamed-selection.mp4')
    assert [record.path for record in grid.selected_records] == [new_path]
    assert state.manager.resolve_media(new_path).clip_id == binding.clip_id
    assert state.manager.get_by_id(binding.clip_id) == original_metadata
    assert card.file_path == new_path and card.selection_checkbox.isChecked()


def test_entering_finalization_keeps_visible_selection_checkbox_clear_of_status(selection_library, qtbot):
    grid = selection_library.grid
    card = _visible_card(selection_library)
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    assert card.selection_checkbox.isVisible()
    card.set_ready(False)
    assert card._finalizing_badge is not None and card._finalizing_badge.isVisible()
    assert not card.selection_checkbox.geometry().intersects(card._finalizing_badge.geometry())
    assert not card.selection_checkbox.geometry().intersects(card.favorite_btn.geometry())
    for width in (300, 420, 300):
        card.resize_card(width, force=True)
        assert not card.selection_checkbox.geometry().intersects(card._finalizing_badge.geometry())
        assert not card.selection_checkbox.geometry().intersects(card.favorite_btn.geometry())
    card.set_ready(True)
    assert card.selection_checkbox.isVisible()


@pytest.mark.parametrize('selection_library', [1000], indirect=True)
def test_control_a_selects_all_filtered_results_without_materializing_all_cards(
        selection_library, qtbot):
    grid = selection_library.grid
    grid.filter_combo.setCurrentIndex(1)  # Videos, excluding the imported screenshot.
    qtbot.waitUntil(lambda: all(record.kind == 'video' for record in grid._filtered_records()))
    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert len(grid.selected_records) == 999
    assert grid.selection_count_label.text() == '999 selected'
    assert grid.select_mode_btn.isChecked()
    assert grid.select_mode_btn.text() == 'Done selecting'
    assert len(grid._thumb_widgets) <= 96
    assert all(record.kind == 'video' for record in grid.selected_records)
    first = grid.selected_records[0]
    qtbot.waitUntil(lambda: first.path in grid._thumb_widgets, timeout=5000)
    grid.clear_selection()
    card = grid._thumb_widgets[first.path]
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    bar = selection_library.scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: first.path not in grid._thumb_widgets, timeout=5000)
    assert all(not card.selection_checkbox.isChecked() and not card.property('selected')
               for card in grid._thumb_widgets.values())
    assert grid.selected_records == (first,)
    assert grid.selection_count_label.text() == '1 selected'
    bar.setValue(0)
    qtbot.waitUntil(lambda: first.path in grid._thumb_widgets, timeout=5000)
    assert grid._thumb_widgets[first.path].selection_checkbox.isChecked()
    grid.filter_combo.setCurrentIndex(2)
    assert grid.selected_records == ()
    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert len(grid.selected_records) == 1 and grid.selected_records[0].kind == 'image'
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    assert grid.selected_records == () and not grid.select_mode_btn.isChecked()


def test_rename_text_keeps_control_a_and_escape_in_the_editor(selection_library, qtbot):
    grid = selection_library.grid
    card = _visible_card(selection_library)
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(card.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    before = grid.selected_records
    qtbot.mouseDClick(card.title_label, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: card.title_edit.hasFocus())
    qtbot.keyClick(card.title_edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert card.title_edit.selectedText() == card.title_edit.text()
    assert grid.selected_records == before
    qtbot.keyClick(card.title_edit, Qt.Key.Key_Escape)
    assert not card._editing_name
    assert grid.selected_records == before


def test_star_share_and_delete_buttons_do_not_toggle_selection(selection_library, qtbot, monkeypatch):
    state = selection_library
    grid = state.grid
    card = _visible_card(state)
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(card.favorite_btn, Qt.MouseButton.LeftButton)
    assert grid.selected_records == ()
    assert card.favorite_btn.isChecked() and not card.delete_btn.isEnabled()
    qtbot.mouseClick(card.favorite_btn, Qt.MouseButton.LeftButton)
    uploads = []
    card.upload_requested.connect(uploads.append)
    qtbot.mouseClick(card.share_btn, Qt.MouseButton.LeftButton)
    assert uploads == [card.file_path] and grid.selected_records == ()
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: False)
    qtbot.mouseClick(card.delete_btn, Qt.MouseButton.LeftButton)
    assert grid.selected_records == ()


def test_recycling_and_sort_refresh_do_not_inherit_another_files_marker(selection_library, qtbot):
    grid = selection_library.grid
    selected = _visible_card(selection_library)
    old_path = selected.file_path
    qtbot.mouseClick(grid.select_mode_btn, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(selected.thumb_label, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    other = next(record for record in grid._records.values()
                 if record.kind == 'video' and record.path != old_path)
    grid._release_virtual_card(old_path)
    grid._release_virtual_card(other.path)
    recycled = grid._create_thumbnail(other.path, 300, ready_override=True)
    assert recycled is selected
    assert not recycled.selection_checkbox.isChecked()
    assert not recycled.property('selected')
    assert [record.path for record in grid.selected_records] == [old_path]
    grid.sort_combo.setCurrentIndex(1)
    assert [record.path for record in grid.selected_records] == [old_path]
    grid._apply_current_index()  # Same published index, as on refresh.
    assert [record.path for record in grid.selected_records] == [old_path]


@pytest.mark.parametrize('selection_library', [1000], indirect=True)
def test_materializing_and_refreshing_cards_does_not_walk_the_whole_index_per_card(
        selection_library, qtbot):
    grid = selection_library.grid
    grid.select_all_results()
    # Selection publication is independent from budgeted card materialization.
    # Wait for the real settled viewport state before measuring indexed work.
    qtbot.waitUntil(
        lambda: (grid._virtual_initialized
                 and not grid._virtual_update_timer.isActive()
                 and not grid._resize_timer.isActive()
                 and len(grid._thumb_widgets) >= 8),
        timeout=5000)
    visits = {'records': 0, 'selection_order': 0}

    class CountedRecords(dict):
        def values(self):
            for value in super().values():
                visits['records'] += 1
                yield value

    class CountedOrder(tuple):
        def __iter__(self):
            for record in super().__iter__():
                visits['selection_order'] += 1
                yield record

    # Publish has completed. Count actual traversal work in the two indexed
    # collections, allowing bounded whole-index maintenance but not one pass
    # for every visible or recycled card.
    original_records, original_order = grid._records, grid._selection._ordered
    grid._records = CountedRecords(original_records)
    grid._selection._ordered = CountedOrder(original_order)
    try:
        grid._update_selection_ui()
        paths = list(grid._thumb_widgets)[:12]
        assert len(paths) >= 8
        for path in paths:
            grid._release_virtual_card(path)
        for path in paths:
            card = grid._create_thumbnail(path, 300, ready_override=True)
            assert card.selection_checkbox.isChecked()
        assert visits['records'] <= len(original_records) * 2, visits
        assert visits['selection_order'] <= len(original_order) * 2, visits
        assert grid.selection_count_label.text() == '1000 selected'
    finally:
        grid._records = original_records
        grid._selection._ordered = original_order


def test_export_signal_contains_all_selected_snapshot_records_not_only_visible_cards(
        selection_library, qtbot):
    grid = selection_library.grid
    grid.set_bulk_export_enabled(True)
    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    snapshots = []
    grid.bulk_export_requested.connect(snapshots.append)
    qtbot.mouseClick(grid.bulk_export_btn, Qt.MouseButton.LeftButton)
    assert snapshots == [grid.selected_records]
    assert isinstance(snapshots[0], tuple)
    assert all(record.file_identity is not None for record in snapshots[0])


def test_bulk_flags_reports_mixed_ready_busy_stale_and_failed_items(selection_library, qtbot, monkeypatch):
    state = selection_library
    grid = state.grid
    ordered = grid._filtered_records()
    not_ready, busy, stale, write_failure = ordered[:4]
    grid.set_readiness_checker(lambda path: path != not_ready.path)
    grid._mutation_paths.add(busy.path)
    # Keep the old selected stat while replacing the indexed file's bytes.
    from pathlib import Path
    stale_path = Path(stale.path)
    stale_path.rename(stale_path.with_name('retained-original-' + stale_path.name))
    stale_path.write_bytes(b'replacement selection media')
    original = state.manager.set_library_flags

    def fail_one(path, **kwargs):
        if path == write_failure.path:
            raise OSError('synthetic per-item write failure')
        return original(path, **kwargs)

    monkeypatch.setattr(state.manager, 'set_library_flags', fail_one)
    grid.select_all_results()
    completed = []
    grid.bulk_flags_finished.connect(completed.append)
    grid.start_bulk_flags(favorite=True)
    assert not grid.filter_combo.isEnabled() and not grid.select_all_btn.isEnabled()
    qtbot.waitUntil(lambda: bool(completed))
    result = completed[0]
    assert set(result['successes']) == {record.path for record in ordered[4:]}
    assert {path for path, error in result['failures']} == {
        not_ready.path, busy.path, stale.path, write_failure.path}
    assert all(error for path, error in result['failures'])
    assert result['cancelled'] is False
    assert grid.filter_combo.isEnabled() and grid.select_all_btn.isEnabled()
    for path in result['successes']:
        assert state.manager.get_library_flags(path) == {'favorite': True, 'protected': True}
    grid._mutation_paths.clear()


def test_batch_cancel_between_items_stops_further_writes_and_releases_controls(
        selection_library, qtbot, monkeypatch):
    grid, manager = selection_library.grid, selection_library.manager
    original = manager.set_library_flags
    calls = []

    def cancel_after_first(path, **kwargs):
        result = original(path, **kwargs)
        calls.append(path)
        grid.cancel_bulk_flags()
        return result

    monkeypatch.setattr(manager, 'set_library_flags', cancel_after_first)
    grid.select_all_results()
    completed = []
    grid.bulk_flags_finished.connect(completed.append)
    grid.start_bulk_flags(favorite=True)
    qtbot.waitUntil(lambda: bool(completed))
    assert len(calls) == 1
    assert completed[0] == {'successes': tuple(calls), 'failures': (), 'cancelled': True}
    assert grid._bulk_job is None and grid.select_all_btn.isEnabled()


def test_unfavorite_batch_releases_protection_and_rebuilds_favorites_once(
        selection_library, qtbot, monkeypatch):
    state = selection_library
    grid = state.grid
    records = grid._filtered_records()
    for record in records:
        state.manager.set_library_flags(record.path, favorite=True)
    state.manager.set_library_flags(records[0].path, protected=True)
    grid.filter_combo.setCurrentIndex(4)
    grid.select_all_results()
    rebuilds = []
    original = grid._apply_current_index

    def counted():
        rebuilds.append(True)
        return original()

    monkeypatch.setattr(grid, '_apply_current_index', counted)
    completed = []
    grid.bulk_flags_finished.connect(completed.append)
    grid.start_bulk_flags(favorite=False)
    qtbot.waitUntil(lambda: bool(completed))
    assert len(completed[0]['successes']) == len(records)
    assert not completed[0]['failures']
    assert rebuilds == [True]
    assert not grid._filtered_records() and not grid.selected_records
    for record in records:
        assert state.manager.get_library_flags(record.path) == {'favorite': False, 'protected': False}


def test_legacy_record_without_file_evidence_is_reported_not_written(selection_library, qtbot):
    grid = selection_library.grid
    record = grid._filtered_records()[0]
    legacy = replace(record, st_dev=None, st_ino=None)
    grid._records[record.identity] = legacy
    grid._selection.select_all((legacy,))
    grid._update_selection_ui()
    completed = []
    grid.bulk_flags_finished.connect(completed.append)
    grid.start_bulk_flags(favorite=True)
    qtbot.waitUntil(lambda: bool(completed))
    assert not completed[0]['successes']
    assert [path for path, error in completed[0]['failures']] == [record.path]
    assert selection_library.manager.resolve_media(record.path) is None


@pytest.mark.parametrize('answer', ['accept', 'cancel', 'shutdown', 'start-failure'])
def test_main_folder_dialog_freezes_selection_and_never_changes_viewer_state(
        selection_library, monkeypatch, answer):
    import copy
    import main
    from core import export_queue
    from ui import export_jobs

    state = selection_library
    records = list(state.grid._filtered_records())
    frozen = tuple(records)
    stopped = threading.Event()
    viewer_state = {'trim': [1, 3], 'segments': [(1, 3)], 'position': 2, 'undo': ['retained']}
    before = copy.deepcopy(viewer_state)
    queued, opened, errors = [], [], []
    host = SimpleNamespace(
        _file_worker_stop=stopped, _shutdown_complete=False,
        _clip_readiness=SimpleNamespace(can_access=lambda path: path != frozen[0].path),
        clip_metadata_manager=state.manager, viewer_state=viewer_state,
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)))

    def choose(*args, **kwargs):
        records.clear()  # Modal event processing changes the live selection.
        if answer == 'shutdown':
            stopped.set()
        return '' if answer == 'cancel' else str(state.owned)

    def submit(snapshot, directory, **kwargs):
        if answer == 'start-failure':
            raise OSError('synthetic original admission failure')
        queued.append((snapshot, directory, kwargs))

    monkeypatch.setattr(main.QFileDialog, 'getExistingDirectory', choose)
    monkeypatch.setattr(export_queue, 'submit_originals', submit)
    monkeypatch.setattr(export_jobs, 'show_export_jobs', lambda: opened.append(True))
    main.MainWindow._export_library_originals(host, records)
    assert viewer_state == before
    if answer == 'accept':
        assert len(queued) == 1 and queued[0][0] == frozen
        assert queued[0][2]['metadata_manager'] is state.manager
        assert queued[0][2]['readiness'] == {
            record.path: record.path != frozen[0].path for record in frozen}
        assert opened == [True] and not errors
    elif answer == 'start-failure':
        assert len(errors) == 1 and not queued and not opened
    else:
        assert not queued and not opened and not errors


def test_export_details_are_scrollable_read_only_plain_text(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QPlainTextEdit
    from core import export_queue
    from ui import export_jobs

    text = '<b>literal error, never HTML</b>\n' + '\n'.join(f'Item {index}: failed' for index in range(500))
    task = export_queue.ExportTask(str(tmp_path / 'source.mp4'), str(tmp_path), 'originals',
                                   detail=text, done=True)
    monkeypatch.setattr(export_jobs, 'tasks', lambda: (task,))
    jobs = export_jobs.ExportJobsDialog()
    qtbot.addWidget(jobs)
    jobs.show()
    jobs.refresh()
    captures = []

    def inspect(dialog):
        qtbot.addWidget(dialog)
        dialog.show()
        editor = dialog.findChild(QPlainTextEdit)
        assert editor is not None
        qtbot.waitUntil(lambda: editor.verticalScrollBar().maximum() > 0)
        captures.append((editor.toPlainText(), editor.isReadOnly()))
        dialog.reject()
        return 0

    monkeypatch.setattr(export_jobs.FthrDialog, 'exec', inspect)
    qtbot.mouseClick(jobs.details_button, Qt.MouseButton.LeftButton)
    assert captures == [(text, True)]
