"""Observable favorite/protection behavior on real isolated Qt library cards."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QImage

from core.clip_metadata_manager import ClipMetadataManager
from ui import clip_grid
from ui import clip_viewer


def _flag_database_snapshot(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple((table, tuple(connection.execute(f'SELECT * FROM {table} ORDER BY 1')))
                     for table in ('entries', 'clip_files', 'file_operations'))


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'size', 'mtime_ns'])
def test_stale_selection_evidence_cannot_change_registered_clip_flags(tmp_path, monkeypatch, field):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Explicit test DB must not read a real profile'))
    media = tmp_path / 'selected.mp4'
    media.write_bytes(b'synthetic selected file')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    binding = manager.register_media(manager.inspect_media(media))
    manager.set_library_flags(media, protected=True)
    before = _flag_database_snapshot(manager._database)
    stale = dict(manager.inspect_media(media).stat)
    stale[field] += 1
    with pytest.raises(OSError):
        manager.set_library_flags(media, favorite=True, protected=False, expected_identity=stale)
    assert _flag_database_snapshot(manager._database) == before
    reopened = ClipMetadataManager(database_path=manager._database)
    assert reopened.get_library_flags(media) == {'favorite': True, 'protected': True}
    assert reopened.get_by_id(binding.clip_id) == manager.get_by_id(binding.clip_id)


@pytest.mark.parametrize('field,value', [('st_ino', 0), ('st_dev', -1), ('size', -1)])
def test_invalid_selection_evidence_cannot_remove_protection_or_write_sql(
        tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Explicit test DB must not read a real profile'))
    media = tmp_path / 'protected.mp4'
    media.write_bytes(b'protected original')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    manager.set_library_flags(media, protected=True)
    before = _flag_database_snapshot(manager._database)
    invalid = dict(manager.inspect_media(media).stat)
    invalid[field] = value
    with pytest.raises(ValueError):
        manager.set_library_flags(media, protected=False, expected_identity=invalid)
    assert _flag_database_snapshot(manager._database) == before
    assert manager.get_library_flags(media) == {'favorite': True, 'protected': True}


def test_zero_device_with_positive_inode_is_valid_selection_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Explicit test DB must not read a real profile'))
    media = tmp_path / 'zero-device.mp4'
    media.write_bytes(b'actual synthetic file')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    stat, lstat = os.stat, os.lstat

    def zero_device(call):
        def observe(path, *args, **kwargs):
            info = call(path, *args, **kwargs)
            if isinstance(path, (str, bytes, os.PathLike)) and Path(path) == media:
                values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                values['st_dev'] = 0
                return SimpleNamespace(**values)
            return info
        return observe

    # Model an otherwise valid filesystem that reports device zero. Retain the
    # real file's positive inode, type, bytes, size and timestamps consistently.
    with monkeypatch.context() as guard:
        guard.setattr(os, 'stat', zero_device(stat))
        guard.setattr(os, 'lstat', zero_device(lstat))
        evidence = dict(manager.inspect_media(media).stat)
        assert evidence['st_dev'] == 0 and evidence['st_ino'] > 0
        assert manager.set_library_flags(media, favorite=True, expected_identity=evidence) == {
            'favorite': True, 'protected': True}
        assert manager.get_library_flags(media) == {'favorite': True, 'protected': True}


@pytest.mark.parametrize('registration', ['unregistered', 'legacy', 'registered'])
def test_same_path_replacement_is_not_registered_or_adopted_by_stale_selection(
        tmp_path, monkeypatch, registration):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Explicit test DB must not read a real profile'))
    media = tmp_path / 'selected.mp4'
    media.write_bytes(b'original bytes')
    database = tmp_path / 'metadata.sqlite3'
    if registration == 'legacy':
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE entries (path TEXT PRIMARY KEY, value TEXT NOT NULL)')
            connection.execute('INSERT INTO entries VALUES (?,?)',
                               (str(media), '{"tag":"Legacy must survive","unknown":{"keep":true}}'))
    manager = ClipMetadataManager(database_path=database)
    manager.migrate()
    if registration == 'registered':
        manager.register_media(manager.inspect_media(media))
        manager.set_library_flags(media, favorite=True)
    expected = dict(manager.inspect_media(media).stat)
    before = _flag_database_snapshot(database)
    retained_original = tmp_path / 'retained-original.mp4'
    media.rename(retained_original)
    media.write_bytes(b'replaced bytes')  # Same size and restored mtime: stat inode must still distinguish it.
    info = retained_original.stat()
    os.utime(media, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert media.stat().st_ino != expected['st_ino']
    with pytest.raises(OSError):
        manager.set_library_flags(media, favorite=True, expected_identity=expected)
    assert _flag_database_snapshot(database) == before
    reopened = ClipMetadataManager(database_path=database)
    assert _flag_database_snapshot(reopened._database) == before
    assert retained_original.read_bytes() == b'original bytes'
    assert media.read_bytes() == b'replaced bytes'


@pytest.mark.parametrize('manual_protection', [False, True])
def test_current_selection_evidence_keeps_favorite_auto_and_manual_protection_rules(
        tmp_path, monkeypatch, manual_protection):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Explicit test DB must not read a real profile'))
    media = tmp_path / 'selected.mp4'
    media.write_bytes(b'synthetic current selection')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    identity = dict(manager.inspect_media(media).stat)
    if manual_protection:
        manager.set_library_flags(media, protected=True, expected_identity=identity)
    assert manager.set_library_flags(media, favorite=True, expected_identity=identity) == {
        'favorite': True, 'protected': True}
    assert manager.set_library_flags(media, favorite=False, expected_identity=identity) == {
        'favorite': False, 'protected': False}
    reopened = ClipMetadataManager(database_path=manager._database)
    assert reopened.get_library_flags(media) == {'favorite': False, 'protected': False}


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

    # PySide's static exec descriptor is cached on instances; replacing the
    # module constructor gives this test an ordinary deterministic override.
    monkeypatch.setattr(clip_grid, 'QMenu', Menu)


@pytest.fixture
def favorite_library(qapp, qtbot, tmp_path, monkeypatch):
    owned = tmp_path / 'owned'
    imported = tmp_path / 'imported'
    owned.mkdir()
    imported.mkdir()
    paths = [owned / 'first.mp4', owned / 'second.mp4', imported / 'linked.png']
    for path in paths[:2]:
        path.write_bytes(b'synthetic card media')
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0)
    assert image.save(str(paths[2]))
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    for path in paths:
        manager.register_media(manager.inspect_media(path))
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(owned), 'imported_clip_folders': [str(imported)],
    }.get(key, default), set=lambda *args: None, save_settings=lambda: None)
    grid = clip_grid.ClipGrid(settings, metadata_manager=manager, autostart=False)
    qtbot.addWidget(grid)
    monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
    grid._imported_files = {str(paths[2])}
    grid._records = grid._records_from_paths({str(path) for path in paths})
    warnings, questions = [], []
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'information',
                        lambda *args, **kwargs: warnings.append((args, kwargs)))
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question',
                        lambda *args, **kwargs: questions.append((args, kwargs)) or True)
    yield SimpleNamespace(grid=grid, manager=manager, paths=paths,
                          warnings=warnings, questions=questions)
    grid.shutdown()


@pytest.mark.parametrize('index', [0, 2])
def test_actual_star_button_persists_favorite_and_protection_for_video_and_imported_png(
        favorite_library, qtbot, index):
    state = favorite_library
    path = state.paths[index]
    card = state.grid._create_thumbnail(str(path), 240, ready_override=True)
    qtbot.waitUntil(card.favorite_btn.isEnabled)
    observed = []
    state.grid.library_flags_changed.connect(observed.append)
    card.favorite_btn.click()
    qtbot.waitUntil(lambda: card.favorite_btn.isChecked())
    assert state.manager.get_library_flags(path) == {'favorite': True, 'protected': True}
    assert not card.delete_btn.isEnabled()
    assert not hasattr(card, 'protection_label')
    assert card.favorite_btn.accessibleName() == 'Remove favorite'
    assert observed == [str(path)]
    card.favorite_btn.click()
    qtbot.waitUntil(lambda: not card.favorite_btn.isChecked())
    assert state.manager.get_library_flags(path) == {'favorite': False, 'protected': False}
    assert card.delete_btn.isEnabled()
    assert path.exists() and not state.warnings


def test_star_overlay_is_inside_thumbnail_and_uses_no_body_layout_slot(favorite_library, qtbot):
    state = favorite_library
    card = state.grid._create_thumbnail(str(state.paths[0]), 300, ready_override=True)
    card.show()
    assert card.favorite_btn.parentWidget() is card._thumb_frame
    assert card._body_frame.layout().indexOf(card.favorite_btn) == -1
    rect = card.favorite_btn.geometry()
    assert card._thumb_frame.rect().contains(rect)
    assert rect.top() <= 16 and card._thumb_frame.width() - rect.right() <= 16


def test_overlay_clicks_do_not_open_clip_and_keep_status_badges_separate(favorite_library, qtbot):
    state = favorite_library
    path = state.paths[0]
    state.grid._imported_files.add(str(path))
    card = state.grid._create_thumbnail(str(path), 300, ready_override=True)
    opened, clicked = [], []
    card.opened.connect(lambda *args: opened.append(args))
    card.clicked.connect(clicked.append)
    card.show()
    qtbot.mouseClick(card.favorite_btn, Qt.MouseButton.LeftButton)
    assert card.favorite_btn.isChecked()
    assert opened == [] and clicked == []
    qtbot.mouseDClick(card.favorite_btn, Qt.MouseButton.LeftButton)
    assert opened == [] and clicked == []
    card.set_uploaded(True)
    card.set_ready(False)
    for badge in (card._imported_badge, card._upload_badge, card._finalizing_badge):
        assert not card.favorite_btn.geometry().intersects(badge.geometry())
    card.set_ready(True)
    qtbot.mouseClick(card._thumb_frame, Qt.MouseButton.LeftButton, pos=QPoint(100, 70))
    assert len(opened) == 1 and clicked == [str(path)]


def test_legacy_card_protection_is_cleared_by_removing_favorite(favorite_library):
    state = favorite_library
    path = state.paths[0]
    state.manager.set_library_flags(path, protected=True)
    card = state.grid._create_thumbnail(str(path), 240, ready_override=True)
    assert card.favorite_btn.isChecked()
    assert not card.delete_btn.isEnabled()
    card.favorite_btn.click()
    assert state.manager.get_library_flags(path) == {'favorite': False, 'protected': False}
    assert card.delete_btn.isEnabled()
    assert not card.favorite_btn.isChecked()




def test_viewer_star_on_off_reenables_delete_for_automatically_protected_clip(favorite_viewer):
    state, viewer = favorite_viewer
    viewer.favorite_btn.click()
    assert not viewer.delete_btn.isEnabled()
    viewer.favorite_btn.click()
    assert state.manager.get_library_flags(state.paths[0]) == {'favorite': False, 'protected': False}
    assert not viewer.favorite_btn.isChecked() and viewer.delete_btn.isEnabled()
    assert not hasattr(viewer, 'protection_label')


def test_viewer_legacy_protection_is_a_removable_favorite(favorite_viewer):
    state, viewer = favorite_viewer
    state.manager.set_library_flags(state.paths[0], protected=True)
    viewer.refresh_library_flags()
    assert viewer.favorite_btn.isChecked()
    viewer.favorite_btn.click()
    assert state.manager.get_library_flags(state.paths[0]) == {'favorite': False, 'protected': False}
    assert viewer.delete_btn.isEnabled()
    assert not hasattr(viewer, 'protect_btn')




def test_context_menu_exposes_only_favorite_and_removal_allows_delete(
        favorite_library, qtbot, monkeypatch):
    state = favorite_library
    path = state.paths[0]
    state.manager.set_library_flags(path, favorite=True)
    card = state.grid._create_thumbnail(str(path), 240, ready_override=True)
    qtbot.waitUntil(card.favorite_btn.isChecked)
    def choose_remove(menu, position):
        labels = [action.text() for action in menu.actions()]
        assert 'Protect clip' not in labels and 'Unprotect clip' not in labels
        return next(action for action in menu.actions() if action.text() == 'Remove favorite')
    _answer_menu(monkeypatch, choose_remove)
    card.contextMenuEvent(SimpleNamespace(globalPos=lambda: QPoint(0, 0)))
    assert state.manager.get_library_flags(path) == {'favorite': False, 'protected': False}
    assert not card.favorite_btn.isChecked() and card.delete_btn.isEnabled()
    assert path.exists()




def test_ready_upload_and_theme_updates_never_reenable_protected_delete(favorite_library, qtbot):
    state = favorite_library
    path = state.paths[0]
    state.manager.set_library_flags(path, favorite=True)
    card = state.grid._create_thumbnail(str(path), 240, ready_override=True)
    qtbot.waitUntil(card.favorite_btn.isChecked)
    for update in (
            lambda: card.set_ready(False), lambda: card.set_ready(True),
            lambda: card.set_upload_info({'url': 'https://example.invalid/uploaded'}),
            lambda: card.set_uploaded(True), lambda: card.set_upload_status('Uploading'),
            lambda: card.set_upload_status(''), card.refresh_theme):
        update()
        assert not card.delete_btn.isEnabled()
        assert card.favorite_btn.isChecked()


def test_real_card_recycling_resets_flags_in_both_directions(favorite_library, qtbot):
    state = favorite_library
    protected, plain = state.paths[:2]
    state.manager.set_library_flags(protected, favorite=True)
    card = state.grid._create_thumbnail(str(protected), 240, ready_override=True)
    qtbot.waitUntil(card.favorite_btn.isChecked)
    state.grid._release_virtual_card(str(protected))
    reused = state.grid._create_thumbnail(str(plain), 240, ready_override=True)
    assert reused is card
    qtbot.waitUntil(lambda: reused.favorite_btn.isEnabled() and reused.delete_btn.isEnabled())
    assert not reused.favorite_btn.isChecked()
    assert not hasattr(reused, 'protection_label')
    reused.resize_card(420)
    assert reused.favorite_btn.parentWidget() is reused._thumb_frame
    assert reused._thumb_frame.rect().contains(reused.favorite_btn.geometry())
    assert reused._thumb_frame.width() - reused.favorite_btn.geometry().right() <= 16
    state.grid._release_virtual_card(str(plain))
    back = state.grid._create_thumbnail(str(protected), 240, ready_override=True)
    assert back is card
    qtbot.waitUntil(back.favorite_btn.isChecked)
    assert not back.delete_btn.isEnabled()
    assert back.file_path == str(protected)
    assert back._thumb_frame.width() == 300
    assert back._thumb_frame.width() - back.favorite_btn.geometry().right() <= 16


def test_favorites_filter_preserves_hidden_records_and_later_flag_changes(favorite_library):
    state = favorite_library
    first, plain, imported = state.paths
    state.manager.set_library_flags(first, favorite=True)
    state.manager.set_library_flags(imported, favorite=True)
    state.grid.filter_combo.setCurrentText('Favorites')
    assert {record.path for record in state.grid._filtered_records()} == {str(first), str(imported)}
    assert len(state.grid._records) == 3
    state.manager.set_library_flags(plain, favorite=True)
    state.manager.set_library_flags(first, favorite=False)
    state.grid.refresh_library_flags(str(first))
    assert {record.path for record in state.grid._filtered_records()} == {str(plain), str(imported)}
    state.grid.filter_combo.setCurrentText('All clips')
    assert {record.path for record in state.grid._filtered_records()} == {str(path) for path in state.paths}
    assert state.manager.get_library_flags(first) == {'favorite': False, 'protected': False}
    assert state.manager.get_library_flags(imported) == {'favorite': True, 'protected': True}


def test_late_protection_blocks_dedicated_linked_delete_after_confirmation(
        favorite_library, monkeypatch):
    state = favorite_library
    path = state.paths[2]
    card = state.grid._create_thumbnail(str(path), 240, ready_override=True)

    def confirm_then_protect(*args, **kwargs):
        fresh = ClipMetadataManager(database_path=state.manager._database)
        fresh.set_library_flags(path, protected=True)
        return True

    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'question', confirm_then_protect)
    deleted = []
    card.deleted.connect(deleted.append)
    card._confirm_delete(allow_linked=True)
    assert path.exists()
    assert deleted == [] and not card._mutation_busy
    assert state.warnings


def test_open_context_menu_cannot_toggle_new_clip_after_card_recycling(
        favorite_library, monkeypatch):
    state = favorite_library
    first, second = state.paths[:2]
    card = state.grid._create_thumbnail(str(first), 240, ready_override=True)

    def recycle_during_menu(menu, position):
        selected = next(action for action in menu.actions() if action.text() == 'Add favorite')
        state.grid._release_virtual_card(str(first))
        assert state.grid._create_thumbnail(str(second), 240, ready_override=True) is card
        return selected

    _answer_menu(monkeypatch, recycle_during_menu)
    card.contextMenuEvent(SimpleNamespace(globalPos=lambda: QPoint(0, 0)))
    assert state.manager.get_library_flags(first) == {'favorite': False, 'protected': False}
    assert state.manager.get_library_flags(second) == {'favorite': False, 'protected': False}


def test_unavailable_protection_disables_card_actions_across_ready_updates(
        favorite_library, monkeypatch):
    state = favorite_library
    card = state.grid._create_thumbnail(str(state.paths[0]), 240, ready_override=True)

    def unavailable(path):
        raise OSError('synthetic metadata unavailable')

    monkeypatch.setattr(state.manager, 'get_library_flags', unavailable)
    card.refresh_library_flags()
    assert not card.favorite_btn.isEnabled() and not card.delete_btn.isEnabled()
    assert 'unavailable' in card.delete_btn.toolTip().lower()
    card.set_ready(False)
    card.set_ready(True)
    card.refresh_theme()
    assert not card.favorite_btn.isEnabled() and not card.delete_btn.isEnabled()


@pytest.fixture
def favorite_viewer(favorite_library, qtbot, monkeypatch):
    from core import export_queue
    from core.clip_readiness import ClipReadinessRegistry
    from core.upload_manager import UploadManager
    from test_library_trash_integration import make_trash_host, stop_trash_host

    state = favorite_library
    for name, value in {'_path_claims': {}, '_local_mutations': set(), '_mutation_tokens': {},
                        '_initializing': False}.items():
        monkeypatch.setattr(export_queue, name, value)
    state.manager.migrate(target_version=3)
    uploader = UploadManager(state.grid._sm, ClipReadinessRegistry(), metadata_manager=state.manager)
    state.grid._sm._upload_manager_ref = uploader
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *args: (10, 1920, 1080, 30, 1_000_000, 1_000_000))
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', lambda _path, cancelled=None: ())
    monkeypatch.setattr(clip_viewer.FthrMessageDialog, 'warning',
                        lambda *args, **kwargs: state.warnings.append((args, kwargs)))
    monkeypatch.setattr(clip_viewer.FthrMessageDialog, 'information',
                        lambda *args, **kwargs: state.warnings.append((args, kwargs)))
    monkeypatch.setattr(clip_viewer.FthrMessageDialog, 'question',
                        lambda *args, **kwargs: state.questions.append((args, kwargs)) or True)
    viewer = clip_viewer.ClipViewer(str(state.paths[0]), None,
                                  settings_manager=state.grid._sm, metadata_manager=state.manager)
    qtbot.addWidget(viewer)
    host = make_trash_host(state.grid._sm, state.manager, state.grid, uploader)
    host._active_clip_viewer = viewer
    state.trash_completions = []

    def request(path, identity, completed, *, editor_page=None):
        def done(result):
            completed(result)
            state.trash_completions.append(result)
        host._request_clip_trash(path, identity, done, editor_page=editor_page)

    viewer._trash_handler = request
    state.wait_trash = lambda: qtbot.waitUntil(lambda: len(state.trash_completions) == 1)
    yield state, viewer
    stop_trash_host(host)
    # An uncertain operation deliberately locks its page. Retire this test's
    # synthetic widget only after all assertions and its owned workers finish.
    viewer._trash_pending_token = None
    viewer.close()
    uploader.stop()


def test_viewer_favorite_is_the_only_delete_protection_control(favorite_viewer):
    state, viewer = favorite_viewer
    observed = []
    viewer.library_flags_changed.connect(observed.append)
    viewer.favorite_btn.click()
    assert state.manager.get_library_flags(state.paths[0]) == {'favorite': True, 'protected': True}
    assert not viewer.delete_btn.isEnabled()
    assert not hasattr(viewer, 'protect_btn')
    viewer.favorite_btn.click()
    assert state.manager.get_library_flags(state.paths[0]) == {'favorite': False, 'protected': False}
    assert not viewer.favorite_btn.isChecked() and viewer.delete_btn.isEnabled()
    assert observed == [str(state.paths[0]), str(state.paths[0])]
    assert not state.warnings




def test_stale_viewer_cannot_teardown_or_delete_after_another_manager_protects(
        favorite_viewer, monkeypatch):
    state, viewer = favorite_viewer
    fresh = ClipMetadataManager(database_path=state.manager._database)
    fresh.set_library_flags(state.paths[0], protected=True)
    teardowns, deleted = [], []
    handler = viewer._trash_handler

    def observed_request(*args, **kwargs):
        deleted.append(args)
        return handler(*args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(viewer, '_teardown_player', lambda **kwargs: teardowns.append(True))
        boundary.setattr(viewer, '_trash_handler', observed_request)
        viewer._delete_clip()
    assert state.paths[0].exists()
    assert teardowns == [] and deleted == []
    assert state.warnings and not state.questions


def test_failed_viewer_flag_write_reports_failure_without_fake_checked_state(
        favorite_viewer, monkeypatch):
    state, viewer = favorite_viewer
    observed = []
    viewer.library_flags_changed.connect(observed.append)

    def save_failure(*args, **kwargs):
        raise OSError('synthetic write refusal')

    monkeypatch.setattr(state.manager, 'set_library_flags', save_failure)
    viewer.favorite_btn.click()
    assert not viewer.favorite_btn.isChecked()
    assert state.manager.get_library_flags(state.paths[0]) == {'favorite': False, 'protected': False}
    assert state.warnings and observed == []


def test_real_main_grid_signal_rebuilds_favorites_once_and_viewer_updates_still_refresh(
        qapp, qtbot, tmp_path, monkeypatch):
    from pathlib import Path
    import main
    from test_pyside_ui_smoke import _CaptureBridgeStub, _CaptureCardStub

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(main, 'CaptureCardClient', _CaptureCardStub)
    monkeypatch.setattr(main, 'CaptureBridge', _CaptureBridgeStub)
    for name in ('_setup_hotkeys', '_start_mic_recorder', 'start_engine', 'stop_engine',
                 '_start_library_recovery', '_start_background_services'):
        monkeypatch.setattr(main.MainWindow, name, lambda _self: None)
    window = main.MainWindow()
    qtbot.addWidget(window)
    grid, manager = window.clip_grid, window.clip_metadata_manager
    viewer = None
    try:
        manager.migrate()
        # Recovery itself is covered separately; advance its explicit admission
        # state here without starting scan, capture, maintenance or services.
        window._file_operations_ready = True
        window._clip_readiness.set_initializing(False)
        from core import export_queue
        export_queue.set_initializing(False)
        window.upload_manager.set_initializing(False)
        media = tmp_path / 'signal-path.mp4'
        media.write_bytes(b'synthetic signal-path media')
        manager.register_media(manager.inspect_media(media))
        manager.set_library_flags(media, favorite=True)
        monkeypatch.setattr(grid, '_start_thumbnail_worker', lambda *args: None)
        grid._records = grid._records_from_paths({str(media)})
        grid.filter_combo.setCurrentText('Favorites')
        grid._apply_current_index()
        card = grid._thumb_widgets[str(media)]
        assert card.favorite_btn.isEnabled()
        applied = []
        apply_index = grid._apply_current_index

        def count_rebuild():
            applied.append(True)
            return apply_index()

        monkeypatch.setattr(grid, '_apply_current_index', count_rebuild)
        card.favorite_btn.click()  # Actual constructor-installed Card -> Grid -> Main wiring.
        assert len(applied) == 1
        assert grid._filtered_records() == []
        applied.clear()
        monkeypatch.setattr(clip_viewer, 'discover_playback_sources',
                            lambda _path, cancelled=None: ())
        viewer = clip_viewer.ClipViewer(str(media), None, settings_manager=window.settings_manager,
                                       metadata_manager=manager)
        qtbot.addWidget(viewer)
        viewer.library_flags_changed.connect(window._on_library_flags_changed)
        viewer.favorite_btn.click()
        assert len(applied) == 1
        assert [record.path for record in grid._filtered_records()] == [str(media)]
    finally:
        if viewer is not None:
            viewer.close()
        with monkeypatch.context() as shutdown:
            shutdown.setattr(main.QApplication, 'instance', staticmethod(lambda: None))
            window._perform_full_shutdown()
        window._shutdown_requested = True
        window.close()


def test_reused_editor_shell_refreshes_favorite_and_protection_for_each_clip(
        favorite_library, qtbot, monkeypatch):
    from ui.clip_editor_window import ClipEditorWindow

    state = favorite_library
    protected, plain = state.paths[:2]
    state.manager.set_library_flags(protected, favorite=True)
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *args: (10, 1920, 1080, 30, 1_000_000, 1_000_000))
    for name in ('_set_media_source', '_prepare_playback_source',
                 '_discover_audio_sources_async', '_load_timeline_thumbnails_if_idle'):
        monkeypatch.setattr(clip_viewer.ClipViewer, name, lambda *args, **kwargs: None)
    shell = ClipEditorWindow(str(protected), None, settings_manager=state.grid._sm,
                             metadata_manager=state.manager)
    qtbot.addWidget(shell)
    page = shell.clip_page
    try:
        assert page.favorite_btn.isChecked() and not page.delete_btn.isEnabled()
        for destination, expected_favorite in ((plain, False), (protected, True), (plain, False)):
            assert shell.replace_clip(str(destination), autoplay=False)
            assert shell.clip_page is page
            assert page.clip_path == str(destination)
            assert page.favorite_btn.isChecked() == expected_favorite
            assert page.favorite_btn.text() == ('Remove favorite' if expected_favorite else 'Add favorite')
            assert not hasattr(page, 'protect_btn')
            assert page.delete_btn.isEnabled() == (not expected_favorite)
            assert not hasattr(page, 'protection_label')
        page.favorite_btn.click()
        assert state.manager.get_library_flags(plain) == {'favorite': True, 'protected': True}
        assert state.manager.get_library_flags(protected) == {'favorite': True, 'protected': True}
    finally:
        shell.close()


@pytest.mark.parametrize('failure', ['sqlite-abort', 'false', 'exception', 'dirty', 'details-modified'])
def test_delete_flush_failure_keeps_real_editor_alive_and_unsaved_edits(
        favorite_viewer, monkeypatch, failure):
    from contextlib import closing
    import sqlite3
    from core import export_queue, ffmpeg_playback

    state, viewer = favorite_viewer
    media = state.paths[0]
    viewer._effects['contrast'] = 42
    viewer._draft_dirty = True
    viewer._last_persisted_snapshot = None
    viewer._draft_save_timer.stop()
    before = viewer._capture_editor_state()
    original_metadata = state.manager.get_by_id(state.manager.resolve_media(media).clip_id)
    if failure in ('sqlite-abort', 'details-modified'):
        with closing(sqlite3.connect(state.manager._database)) as connection, connection:
            connection.execute("CREATE TRIGGER refuse_draft BEFORE UPDATE OF value ON entries "
                "WHEN instr(NEW.value,'editor_draft') > 0 "
                "BEGIN SELECT RAISE(ABORT,'synthetic editor save failure'); END")
        if failure == 'details-modified':
            viewer._tag_edit.setText('Unsaved tag')
            viewer._tag_edit.setModified(True)
    teardowns = []
    original_teardown = viewer._teardown_player

    def teardown(**kwargs):
        teardowns.append(True)
        return original_teardown(**kwargs)

    def cleanup(path):
        if viewer._teardown_complete:
            fresh = ClipMetadataManager(database_path=state.manager._database)
            fresh.set_library_flags(media, protected=True)
        return True

    with monkeypatch.context() as boundary:
        if failure == 'false':
            boundary.setattr(viewer, '_flush_editor_draft', lambda: False)
        elif failure == 'dirty':
            boundary.setattr(viewer, '_flush_editor_draft', lambda: None)
        elif failure == 'exception':
            def refuse():
                raise OSError('synthetic flush failure')
            boundary.setattr(viewer, '_flush_editor_draft', refuse)
        if failure == 'details-modified':
            # Simulate a details saver returning without acknowledging its dirty input.
            boundary.setattr(viewer, '_flush_clip_details', lambda: None)
        boundary.setattr(viewer, '_teardown_player', teardown)
        boundary.setattr(ffmpeg_playback, 'finish_audio_cleanup', cleanup)
        viewer._delete_clip()
        state.wait_trash()
    assert media.read_bytes() == b'synthetic card media'
    assert teardowns == []
    assert not viewer._closing and not viewer._teardown_complete
    assert viewer._capture_editor_state() == before
    assert viewer._draft_dirty or failure == 'details-modified'
    assert state.warnings
    if failure != 'details-modified':
        reopened = ClipMetadataManager(database_path=state.manager._database)
        metadata = reopened.get_by_id(reopened.resolve_media(media).clip_id)
        metadata.pop('library_flags', None)  # The deliberately late external protection is independent.
        assert metadata == original_metadata
    assert export_queue.begin_local_mutations([str(media)])
    export_queue.end_local_mutations([str(media)])


def test_late_delete_refusal_restores_same_file_editor_snapshot_and_navigation_state(
        favorite_viewer, monkeypatch):
    from core import export_queue, ffmpeg_playback

    state, viewer = favorite_viewer
    media = state.paths[0]
    undo = viewer._capture_editor_state()
    viewer._effects['contrast'] = 42
    viewer._undo_stack = [undo]
    viewer._pending_seek_ms = 1000
    viewer._draft_dirty = True
    viewer._draft_save_timer.stop()
    snapshot = viewer._capture_editor_state()
    protected = []

    def cleanup(path):
        if viewer._teardown_complete and not protected:
            fresh = ClipMetadataManager(database_path=state.manager._database)
            fresh.set_library_flags(media, protected=True)
            protected.append(True)
        return True

    monkeypatch.setattr(ffmpeg_playback, 'finish_audio_cleanup', cleanup)
    viewer._delete_clip()
    state.wait_trash()
    assert protected == [True]
    assert media.read_bytes() == b'synthetic card media'
    assert not viewer._closing and not viewer._teardown_complete
    assert viewer._capture_editor_state() == snapshot
    assert viewer._undo_stack == [undo]
    assert viewer._last_stable_position_ms == 1000
    assert not viewer.delete_btn.isEnabled()
    assert state.warnings
    reopened = ClipMetadataManager(database_path=state.manager._database)
    assert reopened.get_editor_draft(str(media)) == clip_viewer._editor_snapshot_to_draft(snapshot)
    assert reopened.get_library_flags(media)['protected'] is True
    assert export_queue.begin_local_mutations([str(media)])
    export_queue.end_local_mutations([str(media)])


@pytest.mark.parametrize('failure', ['replacement', 'reload-failure'])
def test_late_delete_refusal_keeps_snapshot_without_loading_replacement_or_losing_reload_failure(
        favorite_viewer, monkeypatch, failure):
    from core import ffmpeg_playback

    state, viewer = favorite_viewer
    media = state.paths[0]
    viewer._effects['contrast'] = 42
    viewer._draft_dirty = True
    viewer._draft_save_timer.stop()
    snapshot = viewer._capture_editor_state()
    changed, loads = [], []

    def cleanup(path):
        if viewer._teardown_complete and not changed:
            changed.append(True)
            if failure == 'replacement':
                media.rename(media.with_name('retained-before-delete-replacement.mp4'))
                media.write_bytes(b'new replacement must not be loaded or deleted')
            else:
                fresh = ClipMetadataManager(database_path=state.manager._database)
                fresh.set_library_flags(media, protected=True)
        return True

    def reload_failure(*args, **kwargs):
        loads.append(args)
        raise OSError('synthetic renderer reload failure')

    with monkeypatch.context() as boundary:
        boundary.setattr(ffmpeg_playback, 'finish_audio_cleanup', cleanup)
        boundary.setattr(viewer, 'load_clip', reload_failure)
        viewer._delete_clip()
        state.wait_trash()
    assert changed == [True] and state.warnings
    assert viewer._delete_session_snapshot['snapshot'] == snapshot
    if failure == 'replacement':
        assert loads == []
        assert media.read_bytes() == b'new replacement must not be loaded or deleted'
        assert media.with_name('retained-before-delete-replacement.mp4').read_bytes() == b'synthetic card media'
    else:
        assert len(loads) == 1
        assert media.read_bytes() == b'synthetic card media'


@pytest.mark.parametrize('state', ['protected', 'favorite', 'missing-manager', 'database-failure', 'unprotected'])
def test_recording_failure_path_obeys_current_library_protection_without_capture(
        favorite_library, state):
    from main import MainWindow

    library = favorite_library
    media = library.paths[0]
    stale = ClipMetadataManager(database_path=library.manager._database)
    if state in ('protected', 'favorite'):
        library.manager.set_library_flags(media, **{state: True})
    elif state == 'database-failure':
        library.manager._database.rename(media.parent.parent / 'retained-recording-metadata.sqlite3')
        library.manager._database.write_bytes(b'corrupt synthetic metadata')
    stopped, errors, restored = [], [], []
    host = SimpleNamespace(
        _manual_record_path=media, _manual_record_timer=SimpleNamespace(stop=lambda: stopped.append(True)),
        _desired_capture_request=None,
        clip_metadata_manager=None if state == 'missing-manager' else stale,
        _set_status=lambda *args: None,
        push_error=lambda *args, **kwargs: errors.append((args, kwargs)),
        _open_recordings_folder=lambda: None,
        _restore_clip_capture_profile=lambda: restored.append(True))
    host._clear_manual_stop_request = lambda: MainWindow._clear_manual_stop_request(host)
    host._capture_resources_busy = (
        lambda **kwargs: MainWindow._capture_resources_busy(host, **kwargs))
    host._apply_latest_desired_request_if_idle = (
        lambda: MainWindow._apply_latest_desired_request_if_idle(host))
    MainWindow._fail_manual_recording(host, 'synthetic writer failure')
    assert stopped == [True] and restored == [True]
    assert host._desired_capture_request is None
    assert host._manual_record_state == 'idle' and host._manual_record_path is None
    assert len(errors) == 1
    if state == 'unprotected':
        assert not media.exists()
    else:
        assert media.read_bytes() == b'synthetic card media'
        assert 'kept' in errors[0][0][1].lower()
