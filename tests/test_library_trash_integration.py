"""Host admission is required before ordinary library removal."""
from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from core.clip_metadata_manager import ClipMetadataManager
from core.clip_file_operations import ClipFileOperations
from core.clip_readiness import ClipReadinessRegistry, ClipReadinessState
from main import MainWindow
from test_library_rename_integration import _Controller, rename_ui as rename_ui
from ui import clip_grid


def make_trash_host(settings, manager, grid, uploader, *, readiness=None):
    """Bind real Main admission/worker methods without booting an application."""
    host = _Controller()
    host._ui_call.connect(host._deliver)
    for name in ('_rename_settings', '_launch_file_worker', '_reserve_clip_paths', '_release_clip_paths',
                 '_cancel_waiting_file_admissions', '_request_clip_trash', '_request_trash_restore',
                 '_request_trash_purge', '_request_library_trash_operation', '_suspend_trash_editor',
                 '_finish_trash_editor', '_on_automatic_trash_requested'):
        setattr(host, name, MethodType(getattr(MainWindow, name), host))
    host.settings_manager, host.clip_metadata_manager, host.clip_grid = settings, manager, grid
    host.upload_manager = uploader
    host._clip_readiness = readiness or ClipReadinessRegistry()
    host._file_worker_stop = threading.Event()
    host._file_worker_lock = threading.Lock()
    host._file_workers = set()
    host._waiting_file_admissions = {}
    host._file_operations_ready = True
    host._shutdown_complete = False
    host._active_clip_viewer = host._parked_editor_shell = None
    host._active_editor_session = None
    host._editor_sessions = []
    host._manual_record_path = host._pending_manual_record_path = None
    host._file_operations = ClipFileOperations(manager, settings_provider=host._rename_settings)
    host.errors = []
    host.push_error = lambda *args, **kwargs: host.errors.append((args, kwargs))
    host._remove_editor_session = host._editor_sessions.remove
    return host


def stop_trash_host(host):
    host._file_worker_stop.set()
    host._cancel_waiting_file_admissions()
    with host._file_worker_lock:
        threads = tuple(host._file_workers)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    host.deleteLater()


@pytest.fixture
def trash_ui(rename_ui, monkeypatch):
    from core import ffmpeg_playback

    ui = rename_ui
    ui.manager.migrate(target_version=3)
    for name in ('_request_clip_trash', '_request_trash_restore', '_request_trash_purge',
                 '_request_library_trash_operation', '_suspend_trash_editor', '_finish_trash_editor'):
        setattr(ui.controller, name, MethodType(getattr(MainWindow, name), ui.controller))
    ui.grid._metadata_manager = ui.manager
    ui.card._metadata_manager = ui.manager
    ui.card.refresh_library_flags()
    ui.grid.set_trash_handler(ui.controller._request_clip_trash)
    monkeypatch.setattr(ffmpeg_playback, 'finish_audio_cleanup', lambda path: True)
    return ui


def _request_and_wait(ui, qtbot, *, automatic=False):
    completed = []
    evidence = dict(ui.manager.inspect_media(ui.source).stat)
    ui.controller._request_clip_trash(str(ui.source), evidence, completed.append, automatic=automatic)
    qtbot.waitUntil(lambda: len(completed) == 1)
    return completed[0]


def _session_page(ui, qtbot, monkeypatch, *, teardown_failure=False):
    """Real QWidget and actual delete callback; decoder boundaries stay synthetic."""
    from PySide6.QtCore import QTimer
    from ui.clip_viewer import ClipViewer, EditorSnapshot, _editor_snapshot_to_draft

    page = QWidget()
    qtbot.addWidget(page)
    page.clip_path, page.sm, page._mm = str(ui.source), ui.controller.settings_manager, ui.manager
    page._library_generation = object()
    page._trash_pending_token = None
    page._trash_handler = ui.controller._request_clip_trash
    page._linked_import = False
    page._upload_enabled = False
    page._upload_coordinator = ui.controller.upload_manager
    page._closing = page._teardown_complete = False
    page._draft_save_timer = QTimer(page)
    page._draft_save_timer.setSingleShot(True)
    snapshot = EditorSnapshot(.25, .75, ((.25, .75, False),), 0, None, 1.0, ())
    state = {'snapshot': snapshot, 'undo': [snapshot], 'position_ms': 1200}
    restored, loads, warnings = [], [], []

    def capture():
        assert ui.manager.set_editor_draft(page.clip_path, _editor_snapshot_to_draft(snapshot))
        return state

    def teardown(**kwargs):
        page._closing = page._teardown_complete = True
        if teardown_failure:
            raise OSError('injected teardown failure after full snapshot')

    def load(path, **kwargs):
        assert kwargs['_trash_restore_token'] is page._trash_pending_token
        loads.append((path, page._library_generation))
        page._library_generation = object()
        page._closing = page._teardown_complete = False
        page._trash_handler = kwargs['trash_handler']

    page._capture_delete_session = capture
    page._teardown_player = teardown
    page.load_clip = load
    page.restore_session_state = restored.append
    page.refresh_library_flags = lambda: None
    page._delete_clip = MethodType(ClipViewer._delete_clip, page)
    page._restore_delete_session = MethodType(ClipViewer._restore_delete_session, page)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    ui.controller._active_clip_viewer = page
    return page, state, restored, loads, warnings


@pytest.mark.parametrize('imported', [False, True])
def test_card_without_trash_host_keeps_original_metadata_and_draft(qtbot, tmp_path, monkeypatch, imported):
    owned = tmp_path / 'library'
    owned.mkdir()
    source = owned / 'screenshot.png'
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0)
    assert image.save(str(source))
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate(target_version=3)
    binding = manager.register_media(manager.inspect_media(source))
    manager.set(str(source), tag='Keep screenshot', description='No irreversible fallback')
    assert manager.set_editor_draft(str(source), {'saved_edit': [None, 1]})
    before = source.read_bytes()
    metadata = copy.deepcopy(manager.get_by_id(binding.clip_id))
    warnings = []
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    card = clip_grid.ClipThumbnail(str(source), clips_root=str(owned), imported=imported,
                                   metadata_manager=manager)
    qtbot.addWidget(card)

    card._begin_mutation('delete')

    assert not card._mutation_busy
    assert source.read_bytes() == before
    reopened = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    assert reopened.get_by_id(binding.clip_id) == metadata
    assert reopened.resolve_media(source).file_id == binding.file_id
    assert not reopened.pending_operations()


@pytest.mark.parametrize('ancestor_import', [False, True])
def test_real_card_host_trash_publishes_on_gui_only_after_sql_commit(trash_ui, qtbot, ancestor_import):
    ui = trash_ui
    if ancestor_import:
        previous_get = ui.controller.settings_manager.get
        ui.controller.settings_manager.get = lambda key, default=None: (
            [str(ui.source.parent.parent)] if key == 'imported_clip_folders'
            else previous_get(key, default))
    reached, proceed = threading.Event(), threading.Event()
    completions = []

    def checkpoint(name, operation):
        if name == 'before_metadata_commit':
            reached.set()
            assert proceed.wait(3), 'test must release the deliberate commit boundary'

    def host(path, evidence, completed):
        def done(result):
            with sqlite3.connect(ui.database) as connection:
                current_path = connection.execute(
                    'SELECT path FROM entries WHERE clip_id=?', (ui.binding.clip_id,)).fetchone()[0]
            completions.append((result, current_path, QThread.currentThread()))
            completed(result)
        ui.controller._request_clip_trash(path, evidence, done)

    ui.controller._file_operations._checkpoint = checkpoint
    ui.grid.set_trash_handler(host)
    try:
        ui.card._begin_mutation('delete')
        qtbot.waitUntil(reached.is_set)
        assert completions == []
        with sqlite3.connect(ui.database) as connection:
            assert connection.execute('SELECT path FROM entries WHERE clip_id=?',
                                      (ui.binding.clip_id,)).fetchone()[0] == str(ui.source)
    finally:
        proceed.set()
    qtbot.waitUntil(lambda: len(completions) == 1)
    result, current_path, delivered_on = completions[0]
    assert result.succeeded and current_path is None
    assert delivered_on == ui.controller.thread()
    assert not ui.source.exists()
    item, = ui.controller._file_operations.list_trash()
    assert Path(item.stored_path).read_bytes() == ui.original_bytes
    assert ui.manager.get_by_id(ui.binding.clip_id) == ui.metadata
    assert ui.controller.upload_manager.begin_local_mutations([str(ui.source)])
    ui.controller.upload_manager.end_local_mutations([str(ui.source)])


@pytest.mark.parametrize('blocked', ['protected', 'finalizing', 'closed-recovery-gate', 'manual-recording'])
def test_real_host_refusal_keeps_original_ids_metadata_and_claims(trash_ui, qtbot, blocked):
    from core import export_queue

    ui = trash_ui
    if blocked == 'protected':
        ui.manager.set_library_flags(ui.source, protected=True)
    elif blocked == 'finalizing':
        ui.controller._clip_readiness.engine_committed(ui.source, needs_finalization=True)
    elif blocked == 'manual-recording':
        ui.controller._pending_manual_record_path = ui.source
    else:
        # Closed recovery admission is the visible shutdown/startup boundary;
        # do not set the worker-stop flag, which intentionally suppresses callbacks.
        ui.controller._file_operations_ready = False
    before = copy.deepcopy(ui.manager.get_by_id(ui.binding.clip_id))
    result = _request_and_wait(ui, qtbot)
    assert not result.succeeded and result.error
    assert ui.source.read_bytes() == ui.original_bytes
    assert ui.manager.get_by_id(ui.binding.clip_id) == before
    assert not ui.manager.pending_operations()
    assert export_queue.begin_local_mutations([str(ui.source)])
    export_queue.end_local_mutations([str(ui.source)])


@pytest.mark.parametrize('fail_cleanup', [False, True])
def test_purge_releases_readiness_only_after_durable_cleanup(trash_ui, qtbot, fail_cleanup):
    ui = trash_ui
    readiness = ui.controller._clip_readiness
    readiness.engine_committed(ui.source, needs_finalization=False)
    assert _request_and_wait(ui, qtbot).succeeded
    item, = ui.controller._file_operations.list_trash()
    assert readiness.is_tracked(item.stored_path)
    if fail_cleanup:
        def checkpoint(name, _operation):
            if name == 'before_purge_member':
                raise OSError('purge cleanup unavailable')
        ui.controller._file_operations._checkpoint = checkpoint
    completed = []
    ui.controller._request_trash_purge(
        item.trash_id, lambda *result: completed.append(result), detailed=True)
    qtbot.waitUntil(lambda: bool(completed))
    result, _item, cleanup_done = completed[0]
    assert result.succeeded
    assert cleanup_done is not fail_cleanup
    assert readiness.is_tracked(item.stored_path) is fail_cleanup


def test_restore_resets_stale_failed_readiness_on_a_free_destination(trash_ui, qtbot):
    ui = trash_ui
    assert _request_and_wait(ui, qtbot).succeeded
    item, = ui.controller._file_operations.list_trash()
    destination = ui.source.with_name('restored.png')
    readiness = ui.controller._clip_readiness
    readiness.finalization_failed(destination, 'old unrelated finalization failed', base_clip_usable=False)
    assert readiness.state(destination) == ClipReadinessState.FINALIZATION_FAILED
    completed = []
    ui.controller._request_trash_restore(item.trash_id, lambda *result: completed.append(result),
                                          destination=str(destination))
    qtbot.waitUntil(lambda: len(completed) == 1)
    result, fresh, cleanup_done = completed[0]
    assert result.succeeded and fresh.status == 'RESTORED' and cleanup_done
    assert destination.read_bytes() == ui.original_bytes
    assert readiness.can_access(destination)
    assert ui.manager.resolve_media(destination).clip_id == ui.binding.clip_id
    assert ui.manager.get_by_id(ui.binding.clip_id) == ui.metadata


def test_editor_teardown_failure_restores_snapshot_and_keeps_file_and_session(trash_ui, qtbot, monkeypatch):
    ui = trash_ui
    page, state, restored, loads, warnings = _session_page(ui, qtbot, monkeypatch, teardown_failure=True)
    completed = []
    ui.controller._request_clip_trash(str(ui.source), dict(ui.manager.inspect_media(ui.source).stat),
                                      completed.append, editor_page=page)
    qtbot.waitUntil(lambda: len(completed) == 1)
    assert not completed[0].succeeded
    assert ui.source.read_bytes() == ui.original_bytes
    assert restored == [state] and len(loads) == 1
    assert page.isEnabled() and not page._closing and not page._teardown_complete
    assert ui.controller._active_clip_viewer is page
    assert page._trash_pending_token is None
    assert ui.manager.get_editor_draft(str(ui.source))['trim_start'] == .25


def test_rollback_rebind_keeps_host_and_reports_failure_after_generation_changes(trash_ui, qtbot, monkeypatch):
    ui = trash_ui
    page, state, restored, loads, warnings = _session_page(ui, qtbot, monkeypatch)
    generation = page._library_generation
    with sqlite3.connect(ui.database) as connection:
        connection.execute("CREATE TRIGGER refuse_trash BEFORE UPDATE OF path ON entries "
                           "BEGIN SELECT RAISE(ABORT,'injected trash commit abort'); END")
    page._delete_clip()
    qtbot.waitUntil(lambda: bool(warnings))
    assert ui.source.read_bytes() == ui.original_bytes
    assert restored == [state] and len(loads) == 1
    assert page._library_generation is not generation
    assert page._trash_handler == ui.controller._request_clip_trash
    assert page._trash_pending_token is None and page.isEnabled()
    assert warnings[-1][0][1] == 'Move to Trash incomplete'


def test_explicit_host_trash_adopts_only_the_selected_legacy_file_and_keeps_metadata(trash_ui, qtbot):
    ui = trash_ui
    legacy = ui.database.with_name('legacy-host.json')
    legacy.write_text(json.dumps({str(ui.source): {
        'tag': 'Legacy tag', 'description': 'Keep the deliberate adoption metadata',
        'unknown_payload': {'values': [None, True, 7]},
    }}), encoding='utf-8')
    manager = ClipMetadataManager(database_path=ui.database.with_name('legacy-host.sqlite3'),
                                  legacy_path=legacy)
    manager.migrate(target_version=3)
    binding = manager.resolve_media(ui.source)
    assert binding.state == 'LEGACY_UNVERIFIED'
    metadata = copy.deepcopy(manager.get_by_id(binding.clip_id))
    ui.controller.clip_metadata_manager = manager
    ui.controller._file_operations = ClipFileOperations(
        manager, settings_provider=ui.controller._rename_settings)
    completed = []

    ui.controller._request_clip_trash(str(ui.source), dict(manager.inspect_media(ui.source).stat), completed.append)

    qtbot.waitUntil(lambda: len(completed) == 1)
    assert completed[0].succeeded
    item, = ui.controller._file_operations.list_trash()
    assert item.clip_id == binding.clip_id
    assert Path(item.stored_path).read_bytes() == ui.original_bytes
    assert manager.get_by_id(binding.clip_id) == metadata
    assert not ui.source.exists()


@pytest.mark.parametrize('editor', ['active', 'parked'])
def test_automatic_trash_never_closes_an_editor_or_discards_its_unsaved_session(trash_ui, qtbot, editor):
    ui = trash_ui
    closed = []
    shell = SimpleNamespace(clip_path=str(ui.source), close=lambda: closed.append(True))
    setattr(ui.controller, '_active_clip_viewer' if editor == 'active' else '_parked_editor_shell', shell)
    result = _request_and_wait(ui, qtbot, automatic=True)
    assert not result.succeeded and 'editor' in result.error.lower()
    assert closed == []
    assert ui.source.read_bytes() == ui.original_bytes
    assert ui.manager.get_by_id(ui.binding.clip_id) == ui.metadata
    assert not ui.manager.pending_operations()


@pytest.mark.parametrize('cancel_after_first', [False, True])
def test_real_bulk_trash_handles_mixed_protection_and_cancel_with_one_final_rebuild(
        trash_ui, qtbot, monkeypatch, cancel_after_first):
    ui = trash_ui
    sources = [ui.source]
    for name in ('second.png', 'third.png'):
        path = ui.source.with_name(name)
        path.write_bytes(ui.original_bytes)
        ui.manager.register_media(ui.manager.inspect_media(path))
        sources.append(path)
    grid = ui.grid
    # The parent fixture deliberately collects rather than starts QRunnables.
    # New cards must stay at that decoder boundary without phantom readers.
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    grid._replace_records(grid._records_from_paths({str(path) for path in sources}))
    grid._apply_current_index()
    grid.select_all_results()
    selected = grid.selected_records
    assert len(selected) == 3 and all(record.file_identity is not None for record in selected)
    protected = Path(selected[1].path)
    ui.manager.set_library_flags(protected, protected=True)
    protected_binding = ui.manager.resolve_media(protected)
    protected_metadata = copy.deepcopy(ui.manager.get_by_id(protected_binding.clip_id))
    rebuilds, completed = [], []
    apply = grid._apply_current_index

    def counted_apply():
        rebuilds.append(True)
        return apply()

    def dispatch(path, identity, done):
        def finished(result):
            if cancel_after_first:
                grid.cancel_bulk_flags()
            done(result)
        ui.controller._request_clip_trash(path, identity, finished)

    monkeypatch.setattr(grid, '_apply_current_index', counted_apply)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    grid.set_trash_handler(dispatch)
    grid.bulk_trash_finished.connect(completed.append)

    grid.start_bulk_trash()

    qtbot.waitUntil(lambda: len(completed) == 1)
    result = completed[0]
    assert result['cancelled'] is cancel_after_first
    expected_successes = ({selected[0].path} if cancel_after_first else
                          {record.path for record in selected if record.path != str(protected)})
    assert set(result['successes']) == expected_successes
    assert len(result['failures']) == (0 if cancel_after_first else 1)
    if not cancel_after_first:
        assert result['failures'][0][0] == str(protected)
    assert rebuilds == [True]
    assert protected.read_bytes() == ui.original_bytes
    assert ui.manager.get_by_id(protected_binding.clip_id) == protected_metadata
    for path in sources:
        if str(path) not in expected_successes:
            assert path.read_bytes() == ui.original_bytes
    assert {item.original_path for item in ui.controller._file_operations.list_trash()} == expected_successes


def test_worker_publishes_automatic_trash_only_after_releasing_its_upload_claim(trash_ui, qtbot, monkeypatch):
    from core.ffmpeg_tools import get_ffmpeg_exe

    ui = trash_ui
    uploader = ui.controller.upload_manager
    source = ui.source.with_name('completed-upload.mp4')
    subprocess.run([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'color=c=blue:s=16x16:r=5', '-frames:v', '1',
                    '-c:v', 'mpeg4', '-pix_fmt', 'yuv420p', str(source)],
                   check=True, capture_output=True, timeout=20)
    binding = ui.manager.register_media(ui.manager.inspect_media(source))
    uploader.set('upload_auto_delete', True)
    monkeypatch.setattr(uploader, 'is_enabled', lambda: True)
    payloads, observed = [], []

    def plugin(action, payload, **kwargs):
        payloads.append((action, payload))
        return {'ok': True, 'message': 'Synthetic provider confirmed upload'}

    def candidate(path, identity):
        acquired = uploader._in_flight_lock.acquire(blocking=False)
        assert acquired, 'automatic follow-up must not run under the upload admission lock'
        try:
            assert path not in uploader._in_flight
        finally:
            uploader._in_flight_lock.release()
        observed.append((path, identity))

    monkeypatch.setattr(uploader, '_invoke_plugin', plugin)
    uploader.automatic_trash_requested.connect(candidate)
    ready = threading.Event()
    ready.set()
    with uploader._in_flight_lock:
        uploader._in_flight.add(str(source))
    uploader._queue.put(('upload', str(source), ready))
    worker = threading.Thread(target=uploader._worker_loop, name='isolated-test-upload-worker')
    uploader._worker_thread = worker
    worker.start()
    try:
        qtbot.waitUntil(lambda: len(observed) == 1)
    finally:
        uploader.stop()
    assert not worker.is_alive()
    assert observed == [(str(source), dict(ui.manager.inspect_media(source).stat))]
    assert len(payloads) == 1 and payloads[0][1]['config']['upload_auto_delete'] is False
    assert source.is_file() and ui.manager.resolve_media(source).clip_id == binding.clip_id


def _publish_scan_result(worker, records):
    """Deliver one previously captured discovery snapshot through its real signal."""
    from core.library_index import LibraryScanResult, ScanStats

    worker.result = LibraryScanResult(tuple(records), tuple(records), frozenset(), (), ScanStats())
    worker.signals.finished.emit({record.path for record in records},
                                 [(record.mtime, record.path) for record in records], set(), [])


@pytest.mark.parametrize('publication', ['index', 'saved-upsert'])
def test_successful_old_bulk_identity_preserves_a_newly_indexed_replacement_and_selection(
        trash_ui, qtbot, monkeypatch, publication):
    ui, completed, replacement, workers = trash_ui, [], [], []
    grid = ui.grid
    source = ui.source.with_name('reused-name.mp4')
    source.write_bytes(b'old synthetic media selected for recoverable removal')
    binding = ui.manager.register_media(ui.manager.inspect_media(source))
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    grid._replace_records(grid._records_from_paths({str(source)}))
    grid._apply_current_index()
    grid.select_all_results()
    frozen, = grid.selected_records
    old_evidence = dict(frozen.file_identity)
    if publication == 'saved-upsert':
        monkeypatch.setattr(grid._scan_thread_pool, 'start', workers.append)
        grid._scanning_started = True
        grid._begin_scan('background_refresh')

    def dispatch(path, evidence, done):
        assert evidence == old_evidence

        def after_commit(result):
            assert result.succeeded and not source.exists()
            source.write_bytes(b'a newly recorded replacement at the exact same library path')
            new_binding = ui.manager.register_media(ui.manager.inspect_media(source))
            assert new_binding.clip_id != binding.clip_id
            records = grid._records_from_paths({str(source)})
            current, = records.values()
            assert current.file_identity != old_evidence
            # A separate discovery/save publication has already made this new
            # file current while the old bulk completion is still queued.
            grid._replace_records(records)
            grid._known_files = {record.path for record in records.values()}
            grid._selection.select_all((current,))
            if publication == 'saved-upsert':
                grid.upsert_saved_clip(str(source), ready=True)
            replacement.append((current, new_binding))
            done(result)
        ui.controller._request_clip_trash(path, evidence, after_commit)

    grid.set_trash_handler(dispatch)
    grid.bulk_trash_finished.connect(completed.append)

    grid.start_bulk_trash()

    qtbot.waitUntil(lambda: len(completed) == 1)
    current, new_binding = replacement[0]
    assert completed[0]['successes'] == (str(source),)
    assert completed[0]['failures'] == () and not completed[0]['cancelled']
    assert grid._records[current.identity] == current
    assert grid.selected_records == (current,)
    assert str(source) in grid._known_files
    if publication == 'saved-upsert':
        assert grid._scan_upserts[current.identity] == current
        _publish_scan_result(workers[0], (frozen,))
        assert grid._records[current.identity] == current
    assert source.read_bytes() == b'a newly recorded replacement at the exact same library path'
    assert ui.manager.resolve_media(source).clip_id == new_binding.clip_id
    old_item, = ui.controller._file_operations.list_trash()
    assert old_item.clip_id == binding.clip_id
    assert Path(old_item.stored_path).read_bytes() == b'old synthetic media selected for recoverable removal'


def test_scan_captured_before_trash_cannot_republish_the_removed_record(trash_ui, qtbot, monkeypatch):
    ui, workers = trash_ui, []
    grid = ui.grid
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    monkeypatch.setattr(grid._scan_thread_pool, 'start', workers.append)
    grid._replace_records(grid._records_from_paths({str(ui.source)}))
    grid._apply_current_index()
    old_records = tuple(grid._records.values())
    grid._scanning_started = True
    grid._begin_scan('background_refresh')
    old_worker, = workers
    completed = []

    grid._dispatch_trash(str(ui.source), dict(ui.manager.inspect_media(ui.source).stat), completed.append)

    qtbot.waitUntil(lambda: len(completed) == 1)
    assert completed[0].succeeded and not ui.source.exists()
    assert not grid._records
    _publish_scan_result(old_worker, old_records)
    qtbot.wait(50)
    assert not grid._records and str(ui.source) not in grid._known_files
    assert grid.selected_records == ()
    item, = ui.controller._file_operations.list_trash()
    assert Path(item.stored_path).read_bytes() == ui.original_bytes


def test_successful_trash_reconciles_a_record_with_stale_stat_identity(
        trash_ui, qtbot, monkeypatch):
    """A fresh commit identity still clears a card built from an older stat."""
    ui = trash_ui
    grid = ui.grid
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    grid._replace_records(grid._records_from_paths({str(ui.source)}))
    grid._apply_current_index()
    old_record, = tuple(grid._records.values())

    ui.source.write_bytes(ui.original_bytes + b'fresh stat')
    fresh_identity = dict(ui.manager.inspect_media(ui.source).stat)
    ui.source.unlink()
    grid.remove_trashed_paths(((str(ui.source), fresh_identity),))

    assert old_record.file_identity != fresh_identity
    assert not grid._records
    assert str(ui.source) not in grid._records_by_path


def test_two_successful_trash_commits_consume_rebuild_debt_once(
        trash_ui, qtbot, monkeypatch):
    """A queued A/B commit pair performs one final index rebuild."""
    ui = trash_ui
    grid = ui.grid
    second = ui.source.with_name('second-rebuild-debt.png')
    second.write_bytes(ui.original_bytes)
    ui.manager.register_media(ui.manager.inspect_media(second))
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    grid._replace_records(grid._records_from_paths({str(ui.source), str(second)}))
    grid._apply_current_index()
    records = {record.path: record for record in grid._records.values()}
    first_identity = dict(records[str(ui.source)].file_identity)
    second_identity = dict(records[str(second)].file_identity)
    rebuilds = []
    monkeypatch.setattr(grid, '_apply_current_index', lambda: rebuilds.append(True))

    # B is still admitted while A's successful commit is published. A leaves
    # debt behind; releasing B before its own publish reproduces the race.
    grid._mutation_paths.add(str(second))
    grid.remove_trashed_paths(((str(ui.source), first_identity),))
    assert rebuilds == []
    assert grid._trash_rebuild_pending
    grid._mutation_paths.remove(str(second))
    grid.remove_trashed_paths(((str(second), second_identity),))

    assert rebuilds == [True]
    assert not grid._records
    assert not grid._trash_rebuild_pending


@pytest.mark.parametrize('mode', ['single-success', 'bulk-success', 'single-refusal',
                                  'bulk-refusal', 'bulk-cancel'])
def test_deferred_mutation_and_bulk_scan_requests_coalesce_after_refusal_or_cancel(
        trash_ui, qtbot, monkeypatch, mode):
    from core.clip_metadata_types import FileOperationState, MutationResult

    ui, callbacks, completed, workers = trash_ui, [], [], []
    grid = ui.grid
    monkeypatch.setattr(grid, 'force_refresh', MethodType(clip_grid.ClipGrid.force_refresh, grid))
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    monkeypatch.setattr(grid._scan_thread_pool, 'start', workers.append)
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    grid._replace_records(grid._records_from_paths({str(ui.source)}))
    grid._apply_current_index()
    records = tuple(grid._records.values())
    grid._scanning_started = True
    grid.set_trash_handler(lambda path, evidence, done: callbacks.append(done))
    if mode.startswith('single-'):
        grid._dispatch_trash(str(ui.source), dict(records[0].file_identity), completed.append)
    else:
        grid.select_all_results()
        grid.bulk_trash_finished.connect(completed.append)
        grid.start_bulk_trash()
        qtbot.waitUntil(lambda: len(callbacks) == 1)
    for _ in range(3):
        grid.force_refresh()
    assert workers == []
    if mode == 'bulk-cancel':
        grid.cancel_bulk_flags()

    succeeded = mode.endswith('-success')
    if succeeded:
        service = ui.controller._file_operations
        operation = service.prepare_trash(ui.binding.clip_id,
                                           admission=ui.controller._rename_settings())
        outcome = service.execute(operation.op_id)
        assert outcome.succeeded
    else:
        outcome = MutationResult('', FileOperationState.ROLLED_BACK, str(ui.source),
                                  str(ui.source), 'The source was safely retained')
    callbacks[0](outcome)
    if mode.startswith('bulk-'):
        # The real public watcher/refresh route can now see an idle item
        # admission while the bulk job still owns its final queued callback.
        grid.force_refresh()

    qtbot.waitUntil(lambda: len(completed) == 1 and len(workers) == 1)
    if mode.startswith('bulk-'):
        assert completed[0]['successes'] == ((str(ui.source),) if succeeded else ())
        if not succeeded:
            assert completed[0]['failures'][0][0] == str(ui.source)
        assert completed[0]['cancelled'] is (mode == 'bulk-cancel')
    else:
        assert completed[0].succeeded is succeeded
    _publish_scan_result(workers[0], () if succeeded else records)
    qtbot.wait(50)
    assert len(workers) == 1
    assert tuple(grid._records.values()) == (() if succeeded else records)
    if succeeded:
        assert not ui.source.exists()
        item, = ui.controller._file_operations.list_trash()
        assert Path(item.stored_path).read_bytes() == ui.original_bytes
    else:
        assert ui.source.read_bytes() == ui.original_bytes


@pytest.mark.parametrize('lifecycle', ['active', 'paused', 'shutdown'])
def test_active_scan_completion_drains_deferred_trash_refresh_once(
        trash_ui, qtbot, monkeypatch, lifecycle):
    ui, workers, completed = trash_ui, [], []
    grid = ui.grid
    monkeypatch.setattr(grid, 'force_refresh', MethodType(clip_grid.ClipGrid.force_refresh, grid))
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    monkeypatch.setattr(grid._scan_thread_pool, 'start', workers.append)
    grid._replace_records(grid._records_from_paths({str(ui.source)}))
    grid._known_files = {str(ui.source)}
    grid._apply_current_index()
    old_records = tuple(grid._records.values())
    grid._scanning_started = True
    grid._begin_scan('foreground_refresh')
    transition_worker, = workers
    assert grid._active_scan_worker is transition_worker

    grid._dispatch_trash(str(ui.source), dict(ui.manager.inspect_media(ui.source).stat), completed.append)
    for _ in range(3):
        grid.force_refresh()
    qtbot.waitUntil(lambda: len(completed) == 1)
    assert completed[0].succeeded and not ui.source.exists()
    # Deliver the pending single-shot while the genuine transition is still
    # running. Its old directory snapshot must not consume the newer request.
    qtbot.wait(20)
    assert workers == [transition_worker]
    newly_saved = ui.source.with_name('saved-during-transition.png')
    newly_saved.write_bytes(ui.original_bytes)
    ui.manager.register_media(ui.manager.inspect_media(newly_saved))
    if lifecycle == 'paused':
        grid.set_background_paused(True)
    elif lifecycle == 'shutdown':
        grid.shutdown()

    _publish_scan_result(transition_worker, old_records)

    if lifecycle in ('paused', 'shutdown'):
        qtbot.wait(50)
        assert workers == [transition_worker]
        if lifecycle == 'shutdown':
            assert newly_saved.read_bytes() == ui.original_bytes
            return
        grid.set_background_paused(False)
    qtbot.waitUntil(lambda: len(workers) == 2)
    # The second runnable performs a real scan of only this retained test
    # library, including the file absent from the transition's stale snapshot.
    workers[1].run()
    qtbot.waitUntil(lambda: any(record.path == str(newly_saved) for record in grid._records.values()))
    qtbot.wait(50)
    assert len(workers) == 2
    assert {record.path for record in grid._records.values()} == {str(newly_saved)}
    assert str(ui.source) not in grid._known_files
    assert grid.selected_records == ()
    item, = ui.controller._file_operations.list_trash()
    assert Path(item.stored_path).read_bytes() == ui.original_bytes
