"""Offscreen Grid/Main rename integration with synthetic files and no app boot."""
from __future__ import annotations

import sqlite3
import threading
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Signal, Slot
from PySide6.QtGui import QImage

from core import export_queue
from core.clip_file_operations import ClipFileOperations
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import FileOperationState, MutationResult
from core.clip_readiness import ClipReadinessRegistry, ClipReadinessState
from core.upload_manager import UploadManager
from main import MainWindow
from ui import clip_grid


class _Controller(QObject):
    _ui_call = Signal(object)

    @Slot(object)
    def _deliver(self, callback):
        callback()


def _stored_path(database, clip_id):
    with sqlite3.connect(database) as connection:
        return connection.execute('SELECT path FROM entries WHERE clip_id=?', (clip_id,)).fetchone()[0]


@pytest.fixture
def rename_ui(qapp, qtbot, tmp_path, monkeypatch):
    owned = tmp_path / 'library'
    owned.mkdir()
    source, destination = owned / 'source.png', owned / 'renamed.png'
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0)
    assert image.save(str(source))
    database = tmp_path / 'metadata.sqlite3'
    manager = ClipMetadataManager(database_path=database)
    manager.migrate()
    binding = manager.register_media(manager.inspect_media(source))
    manager.set(str(source), tag='Screenshot')
    manager.set_editor_draft(str(source), {'saved_edit': [None, 1]})
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(owned), 'imported_clip_folders': [], 'hotkeys': {},
        'audio_capture_enabled': False,
    }.get(key, default))
    for name, value in {'_path_claims': {}, '_local_mutations': set(), '_mutation_tokens': {},
                        '_initializing': False}.items():
        monkeypatch.setattr(export_queue, name, value)
    grid = clip_grid.ClipGrid(settings, autostart=False)
    queued_thumbnails = []
    monkeypatch.setattr(grid._thread_pool, 'start', queued_thumbnails.append)
    # Screenshots enter through normal section materialization, whereas the
    # CLIP_SAVED upsert API deliberately accepts completed videos only.
    grid._add_section('TEST SCREENSHOTS', [str(source)], 0)
    # No decoder was launched; clear only the corresponding synthetic bookkeeping.
    grid._thumbnail_jobs_inflight.clear()
    grid._thumbnail_workers.clear()
    card = grid._thumb_widgets[str(source)]
    refreshes, warnings = [], []
    monkeypatch.setattr(grid, 'force_refresh', lambda: refreshes.append(True))
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    controller = _Controller()
    controller._ui_call.connect(controller._deliver)
    for name in (
            '_rename_settings', '_launch_file_worker', '_start_library_recovery',
            '_finish_library_recovery', '_configure_keyboard_capture_for_request',
            '_clip_mutation_reason', '_reserve_clip_paths',
            '_release_clip_paths', '_detach_clip_editors', '_request_clip_rename', '_on_clip_renamed',
            '_cancel_waiting_file_admissions'):
        setattr(controller, name, MethodType(getattr(MainWindow, name), controller))
    controller.settings_manager = settings
    controller.clip_metadata_manager = manager
    controller._clip_readiness = ClipReadinessRegistry()
    controller._file_worker_stop = threading.Event()
    controller._file_worker_lock = threading.Lock()
    controller._file_workers = set()
    controller._waiting_file_admissions = {}
    controller._file_operations_ready = True
    controller._metadata_startup_error = None
    controller._library_recovery_started = False
    controller._library_recovery_error = None
    controller._startup_game_detection = False
    controller._active_clip_viewer = None
    controller._active_editor_session = None
    controller._editor_sessions = []
    controller._pending_manual_record_path = None
    controller._manual_record_path = None
    controller.clip_grid = grid
    controller.upload_manager = UploadManager(settings, controller._clip_readiness)
    controller._file_operations = ClipFileOperations(manager, settings_provider=controller._rename_settings)
    controller.errors, controller.started_services = [], []
    controller.statuses = []
    controller._set_status = lambda *args: controller.statuses.append(args)
    controller.push_error = lambda *args, **kwargs: controller.errors.append((args, kwargs))
    controller._start_background_services = lambda: controller.started_services.append('background')
    controller.start_engine = lambda: controller.started_services.append('engine')
    controller._start_game_detection = lambda: controller.started_services.append('game detection')
    controller._keyboard_overlay_capture = SimpleNamespace(
        configure=lambda _: controller.started_services.append('overlay'))
    grid.set_rename_handler(controller._request_clip_rename)
    grid.clip_renamed.connect(controller._on_clip_renamed)
    state = SimpleNamespace(
        controller=controller, grid=grid, card=card, source=source, destination=destination,
        manager=manager, database=database, binding=binding, warnings=warnings,
        refreshes=refreshes, original_bytes=source.read_bytes(),
        metadata=manager.get_by_id(binding.clip_id))
    yield state
    controller._file_worker_stop.set()
    controller._cancel_waiting_file_admissions()
    with controller._file_worker_lock:
        threads = tuple(controller._file_workers)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    controller.upload_manager.stop()
    grid.shutdown()
    grid.deleteLater()
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.mark.parametrize('failure', ['none', 'history', 'execute-after-commit'])
def test_real_grid_main_worker_rename_emits_only_after_sql_commit(rename_ui, qtbot, monkeypatch, failure):
    ui = rename_ui
    controller = ui.controller
    reached, release = threading.Event(), threading.Event()
    probe_threads, observed = [], []

    def checkpoint(name, operation):
        if name == 'before_metadata_commit':
            probe_threads.append(QThread.currentThread())
            reached.set()
            assert release.wait(3)

    controller._file_operations._checkpoint = checkpoint
    if failure == 'history':
        def history_failure(old, new):
            assert _stored_path(ui.database, ui.binding.clip_id) == str(ui.destination)
            raise OSError('injected history persistence failure')
        monkeypatch.setattr(controller.upload_manager, 'rename_history_path', history_failure)
    elif failure == 'execute-after-commit':
        execute = controller._file_operations.execute

        def fail_after_commit(op_id):
            assert execute(op_id).succeeded
            raise OSError('injected return-path failure after durable commit')
        monkeypatch.setattr(controller._file_operations, 'execute', fail_after_commit)
    ui.grid.clip_renamed.connect(lambda old, new: observed.append(
        (old, new, _stored_path(ui.database, ui.binding.clip_id), QThread.currentThread())))
    try:
        ui.card._begin_mutation('rename', str(ui.destination))
        qtbot.waitUntil(reached.is_set, timeout=2500)
        assert ui.card._mutation_busy
        assert not controller._waiting_file_admissions
        assert observed == []
        assert _stored_path(ui.database, ui.binding.clip_id) == str(ui.source)
        assert not export_queue.begin_local_mutations([ui.source, ui.destination])
        assert not controller.upload_manager.begin_local_mutations([ui.source, ui.destination])
        assert not controller._clip_readiness.can_access(ui.source)
        assert probe_threads[0] != controller.thread()
        release.set()
        qtbot.waitUntil(lambda: len(observed) == 1, timeout=3000)
        assert observed[0] == (str(ui.source), str(ui.destination), str(ui.destination), controller.thread())
        assert not ui.card._mutation_busy
        assert ui.card.file_path == str(ui.destination)
        assert ui.destination.read_bytes() == ui.original_bytes
        reopened = ClipMetadataManager(database_path=ui.database)
        assert reopened.get_by_id(ui.binding.clip_id) == ui.metadata
        assert reopened.resolve_media(ui.destination).file_id == ui.binding.file_id
        assert not ui.warnings
        if failure != 'none':
            assert any(fields.get('level') == 'warning' for _, fields in controller.errors)
        assert export_queue.begin_local_mutations([ui.source, ui.destination])
        export_queue.end_local_mutations([ui.source, ui.destination])
        assert controller.upload_manager.begin_local_mutations([ui.source, ui.destination])
        controller.upload_manager.end_local_mutations([ui.source, ui.destination])
    finally:
        release.set()


def test_main_detaches_editor_and_waits_for_thumbnail_and_audio_readers_on_gui(
        rename_ui, qtbot, monkeypatch):
    from core import ffmpeg_playback

    ui = rename_ui
    cancelled, observed = threading.Event(), []
    ui.grid._thumbnail_workers[str(ui.source)] = SimpleNamespace(cancel_event=cancelled)
    ui.grid._thumbnail_jobs_inflight.add(str(ui.source))
    audio_ready = {'value': False}
    audio_threads = []

    def cleanup(path):
        audio_threads.append(QThread.currentThread())
        return audio_ready['value']

    monkeypatch.setattr(ffmpeg_playback, 'finish_audio_cleanup', cleanup)
    closed = []
    ui.controller._active_clip_viewer = SimpleNamespace(
        clip_path=str(ui.source), close=lambda: closed.append(QThread.currentThread()))
    ui.grid.clip_renamed.connect(lambda *args: observed.append(args))
    ui.card._begin_mutation('rename', str(ui.destination))
    assert cancelled.is_set()
    assert closed == [ui.controller.thread()]
    assert not ui.manager.pending_operations()
    assert ui.source.read_bytes() == ui.original_bytes
    assert observed == []
    ui.grid._thumbnail_jobs_inflight.discard(str(ui.source))
    ui.grid._thumbnail_workers.pop(str(ui.source))
    audio_ready['value'] = True
    qtbot.waitUntil(lambda: len(observed) == 1, timeout=3000)
    assert all(thread == ui.controller.thread() for thread in audio_threads)


@pytest.mark.parametrize('lifecycle', ['recycled', 'destroyed'])
def test_card_lifecycle_cannot_strand_main_reservations_or_apply_to_another_file(
        rename_ui, qtbot, lifecycle):
    ui = rename_ui
    reached, release = threading.Event(), threading.Event()
    observed = []

    def checkpoint(name, operation):
        if name == 'before_metadata_commit':
            reached.set()
            assert release.wait(3)

    ui.controller._file_operations._checkpoint = checkpoint
    ui.grid.clip_renamed.connect(lambda *args: observed.append(args))
    try:
        ui.card._begin_mutation('rename', str(ui.destination))
        qtbot.waitUntil(reached.is_set, timeout=2500)
        if lifecycle == 'recycled':
            other = ui.source.with_name('different.png')
            other.write_bytes(ui.original_bytes)
            ui.card.rebind(str(other), imported=False, upload_enabled=False, uploaded=False,
                           upload_info=None, ready=True, card_width=320)
        else:
            ui.grid._thumb_widgets.pop(str(ui.source))
            ui.grid.thumbnails.remove(ui.card)
            ui.card.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        release.set()
        qtbot.waitUntil(lambda: len(observed) == 1, timeout=3000)
        assert _stored_path(ui.database, ui.binding.clip_id) == str(ui.destination)
        if lifecycle == 'recycled':
            assert ui.card.file_path == str(other)
            assert not ui.card._mutation_busy
        assert export_queue.begin_local_mutations([ui.source, ui.destination])
        export_queue.end_local_mutations([ui.source, ui.destination])
        assert ui.controller.upload_manager.begin_local_mutations([ui.source, ui.destination])
        ui.controller.upload_manager.end_local_mutations([ui.source, ui.destination])
        assert not ui.warnings
    finally:
        release.set()


def test_synchronous_rejection_then_exception_completes_card_and_grid_only_once(rename_ui):
    ui = rename_ui
    observed = []

    def reject(old, new, allow_linked, completed):
        completed(MutationResult('', FileOperationState.ROLLED_BACK, old, new, 'rejected'))
        raise RuntimeError('handler raised after returning the rejection')

    ui.grid.set_rename_handler(reject)
    ui.grid.clip_renamed.connect(lambda *args: observed.append(args))
    ui.card._begin_mutation('rename', str(ui.destination))
    assert not ui.card._mutation_busy
    assert len(ui.warnings) == 1
    assert len(ui.refreshes) == 1
    assert observed == []
    assert not ui.grid._mutation_paths
    assert ui.source.read_bytes() == ui.original_bytes


@pytest.mark.parametrize('attribute', ['_manual_record_path', '_pending_manual_record_path'])
@pytest.mark.parametrize('target', ['source', 'destination'])
def test_main_blocks_both_paths_of_unfinished_manual_recording(rename_ui, attribute, target):
    ui = rename_ui
    setattr(ui.controller, attribute, getattr(ui, target))
    ui.card._begin_mutation('rename', str(ui.destination))
    assert not ui.card._mutation_busy
    assert len(ui.warnings) == 1
    assert ui.source.read_bytes() == ui.original_bytes
    assert not ui.manager.pending_operations()
    assert not ui.grid._mutation_paths


def test_grid_scanning_starts_once_only_after_successful_main_recovery(rename_ui, monkeypatch):
    ui = rename_ui
    controller = ui.controller
    scans = []
    monkeypatch.setattr(ui.grid, '_load_clips', lambda: scans.append(True))
    controller._file_operations_ready = False
    controller._clip_readiness.set_initializing(True)
    controller.upload_manager.set_initializing(True)
    export_queue.set_initializing(True)

    controller._finish_library_recovery('unknown journal format', ())
    assert not scans and not controller.started_services
    assert not controller._file_operations_ready
    assert controller._clip_readiness.state(ui.source) is ClipReadinessState.INITIALIZING
    assert not export_queue.begin_local_mutations([ui.source])
    assert not controller.upload_manager.begin_local_mutations([ui.source])
    controller._finish_library_recovery(None, (str(ui.source),))
    ui.grid.start_scanning()
    assert scans == [True]
    assert controller.started_services == ['background', 'overlay', 'engine', 'game detection']
    assert controller._file_operations_ready
    assert controller._clip_readiness.state(ui.source) is ClipReadinessState.RECOVERY_REQUIRED
    other = ui.source.with_name('other-complete.png')
    other.write_bytes(ui.original_bytes)
    assert not controller._clip_mutation_reason((str(other),))


def test_real_startup_worker_keeps_global_gates_closed_for_unknown_journal_kind(rename_ui, qtbot):
    ui = rename_ui
    controller = ui.controller
    operation = controller._file_operations.prepare_rename(
        ui.binding.clip_id, ui.destination, admission={
            'owned_root': str(ui.source.parent), 'imported_roots': [], 'allow_linked': False})
    with sqlite3.connect(ui.database) as connection:
        connection.execute('UPDATE file_operations SET kind=? WHERE op_id=?',
                           ('unknown_future_kind', operation.op_id))
    controller._file_operations_ready = False
    controller._clip_readiness.set_initializing(True)
    controller.upload_manager.set_initializing(True)
    export_queue.set_initializing(True)
    controller._start_library_recovery()
    qtbot.waitUntil(lambda: bool(controller.errors), timeout=3000)

    assert not controller._file_operations_ready
    assert not controller.started_services
    assert not ui.grid._scanning_started
    assert not export_queue.begin_local_mutations([ui.source])
    assert not controller.upload_manager.begin_local_mutations([ui.source])
    assert ui.source.read_bytes() == ui.original_bytes
    assert _stored_path(ui.database, ui.binding.clip_id) == str(ui.source)


def test_closed_startup_gate_blocks_filter_sort_and_resume_and_status_never_reads_engine(
        rename_ui, monkeypatch):
    ui = rename_ui
    scans = []
    monkeypatch.setattr(ui.grid._scan_thread_pool, 'start', scans.append)
    ui.grid._records.clear()
    ui.grid._on_filter_changed(1)
    ui.grid._on_sort_changed(1)
    ui.grid.set_background_paused(True)
    ui.grid.set_background_paused(False)

    assert scans == []
    assert not ui.grid.refresh_timer.isActive()
    assert not ui.grid._watcher.directories()
    ui.controller._file_operations_ready = False
    MainWindow._update_status(ui.controller)
    assert ui.controller.statuses[-1][0] == 'PREPARING LIBRARY'
    ui.controller._library_recovery_error = 'unsupported operation format'
    MainWindow._update_status(ui.controller)
    assert ui.controller.statuses[-1][0] == 'LIBRARY RECOVERY REQUIRED'


def test_shutdown_releases_waiting_admission_without_late_dialog_or_scan(
        rename_ui, qtbot, monkeypatch):
    from core import ffmpeg_playback

    ui = rename_ui
    controller = ui.controller
    monkeypatch.setattr(ffmpeg_playback, 'finish_audio_cleanup', lambda _: False)
    ui.card._begin_mutation('rename', str(ui.destination))
    assert controller._waiting_file_admissions
    assert not controller._file_workers
    assert not ui.manager.pending_operations()
    controller._file_worker_stop.set()

    controller._cancel_waiting_file_admissions()

    assert not controller._waiting_file_admissions
    assert export_queue.begin_local_mutations([ui.source, ui.destination])
    export_queue.end_local_mutations([ui.source, ui.destination])
    assert controller.upload_manager.begin_local_mutations([ui.source, ui.destination])
    controller.upload_manager.end_local_mutations([ui.source, ui.destination])
    assert controller._clip_readiness.can_access(ui.source)
    qtbot.wait(150)  # Deliver the already-scheduled reader retry after cancellation.
    assert not ui.warnings and not ui.refreshes
    assert not controller._file_workers
    assert ui.source.read_bytes() == ui.original_bytes
    assert not ui.destination.exists()


def _snapshot(trim_start):
    from ui.clip_viewer import EditorSnapshot

    return EditorSnapshot(trim_start, 1.0, ((0.0, 1.0, False),), 0, None, 1.0, ())


def _draft_viewer(ui, snapshot, closed, *, close_result=True):
    from ui.clip_viewer import ClipViewer

    viewer = SimpleNamespace(
        clip_path=str(ui.source), _mm=ui.manager, _draft_dirty=True,
        _discard_editor_draft=False, _last_persisted_snapshot=None,
        _capture_editor_state=lambda: snapshot,
        close=lambda: (closed.append(True), close_result)[1])
    viewer._flush_editor_draft = MethodType(ClipViewer._flush_editor_draft, viewer)
    viewer._flush_clip_details = MethodType(ClipViewer._flush_clip_details, viewer)
    return viewer


def _abort_metadata_updates(database):
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER reject_editor_save BEFORE UPDATE OF value ON entries "
                           "BEGIN SELECT RAISE(ABORT,'injected editor persistence failure'); END")


@pytest.mark.parametrize('failure', ['sqlite-error', 'false', 'exception', 'close-false', 'details-error'])
def test_active_editor_save_failure_prevents_close_and_rename_and_releases_claims(
        rename_ui, monkeypatch, failure):
    from PySide6.QtWidgets import QLineEdit
    from ui.clip_viewer import _editor_snapshot_to_draft
    from ui.editor_sessions import EditorSession

    ui = rename_ui
    snapshot = _snapshot(0.3)
    closed, removed = [], []
    viewer = _draft_viewer(ui, snapshot, closed, close_result=failure != 'close-false')
    session = EditorSession(str(ui.source), state={'snapshot': _snapshot(0.1)})
    ui.controller._active_clip_viewer = viewer
    ui.controller._active_editor_session = session
    ui.controller._editor_sessions = [session]
    ui.controller._remove_editor_session = removed.append
    if failure in {'sqlite-error', 'details-error'}:
        _abort_metadata_updates(ui.database)
        if failure == 'details-error':
            viewer._draft_dirty = False
            viewer._tag_edit = QLineEdit('Unsaved tag', ui.grid)
            viewer._desc_edit = QLineEdit('Unsaved description', ui.grid)
            viewer._tag_edit.setModified(True)
            viewer._desc_edit.setModified(True)
    elif failure == 'false':
        viewer._flush_editor_draft = lambda: False
    elif failure == 'exception':
        def fail():
            raise OSError('flush callback failed')
        viewer._flush_editor_draft = fail

    ui.card._begin_mutation('rename', str(ui.destination))

    assert ui.controller._active_clip_viewer is viewer
    assert ui.controller._editor_sessions == [session]
    assert removed == []
    assert closed == ([True] if failure == 'close-false' else [])
    assert not ui.card._mutation_busy
    assert len(ui.warnings) == 1
    assert not ui.manager.pending_operations()
    assert not ui.controller._file_workers and not ui.controller._waiting_file_admissions
    assert ui.source.read_bytes() == ui.original_bytes
    assert _stored_path(ui.database, ui.binding.clip_id) == str(ui.source)
    reopened = ClipMetadataManager(database_path=ui.database)
    if failure == 'close-false':
        assert reopened.get_editor_draft(str(ui.source)) == _editor_snapshot_to_draft(snapshot)
    else:
        assert reopened.get_by_id(ui.binding.clip_id) == ui.metadata
    if failure == 'sqlite-error':
        assert viewer._draft_dirty
    assert export_queue.begin_local_mutations([ui.source, ui.destination])
    export_queue.end_local_mutations([ui.source, ui.destination])
    assert ui.controller.upload_manager.begin_local_mutations([ui.source, ui.destination])
    ui.controller.upload_manager.end_local_mutations([ui.source, ui.destination])


@pytest.mark.parametrize('save', ['sqlite-error', 'false', 'success'])
def test_parked_session_snapshot_is_saved_before_removal_or_retained_after_failure(
        rename_ui, qtbot, monkeypatch, save):
    from ui.clip_viewer import _editor_snapshot_to_draft
    from ui.editor_sessions import EditorSession

    ui = rename_ui
    snapshot = _snapshot(0.4)
    session = EditorSession(str(ui.source), state={'snapshot': snapshot})
    ui.controller._editor_sessions = [session]
    removed = []

    def remove(current):
        removed.append(current)
        ui.controller._editor_sessions.remove(current)

    ui.controller._remove_editor_session = remove
    if save == 'sqlite-error':
        _abort_metadata_updates(ui.database)
    elif save == 'false':
        monkeypatch.setattr(ui.manager, 'set_editor_draft', lambda *args, **kwargs: False)
    ui.card._begin_mutation('rename', str(ui.destination))
    if save == 'success':
        qtbot.waitUntil(lambda: ui.card.file_path == str(ui.destination), timeout=3000)
        assert removed == [session]
        reopened = ClipMetadataManager(database_path=ui.database)
        assert reopened.get_editor_draft(str(ui.destination)) == _editor_snapshot_to_draft(snapshot)
        assert reopened.resolve_media(ui.destination).file_id == ui.binding.file_id
    else:
        assert removed == []
        assert ui.controller._editor_sessions == [session]
        assert len(ui.warnings) == 1
        assert not ui.manager.pending_operations()
        assert ui.source.read_bytes() == ui.original_bytes
        assert ui.manager.get_by_id(ui.binding.clip_id) == ui.metadata


def test_active_editor_snapshot_wins_over_older_parked_snapshot_on_rename(rename_ui, qtbot):
    from ui.clip_viewer import _editor_snapshot_to_draft
    from ui.editor_sessions import EditorSession

    ui = rename_ui
    live, parked = _snapshot(0.6), _snapshot(0.1)
    closed = []
    viewer = _draft_viewer(ui, live, closed)
    session = EditorSession(str(ui.source), state={'snapshot': parked})
    ui.controller._active_clip_viewer = viewer
    ui.controller._active_editor_session = session
    ui.controller._editor_sessions = [session]
    ui.controller._remove_editor_session = ui.controller._editor_sessions.remove

    ui.card._begin_mutation('rename', str(ui.destination))

    qtbot.waitUntil(lambda: ui.card.file_path == str(ui.destination), timeout=3000)
    reopened = ClipMetadataManager(database_path=ui.database)
    assert reopened.get_editor_draft(str(ui.destination)) == _editor_snapshot_to_draft(live)
    assert reopened.get_editor_draft(str(ui.destination)) != _editor_snapshot_to_draft(parked)
    assert not viewer._draft_dirty and closed == [True]
    assert ui.controller._active_clip_viewer is None
    assert ui.controller._editor_sessions == []
