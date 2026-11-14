"""Real host upload decisions with synthetic plugin IPC and retained media."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from core import export_queue, upload_manager
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_readiness import ClipReadinessRegistry


@pytest.fixture
def upload_library(qapp, tmp_path, monkeypatch):
    for name, filename in (('_SETTINGS_FILE', 'upload-settings.json'),
                           ('_HISTORY_FILE', 'upload-history.json'),
                           ('_LEGACY_HISTORY_FILE', 'legacy-history.json')):
        monkeypatch.setattr(upload_manager, name, tmp_path / filename)
    for name, value in {'_path_claims': {}, '_local_mutations': set(), '_mutation_tokens': {},
                        '_initializing': False}.items():
        monkeypatch.setattr(export_queue, name, value)
    owned = tmp_path / 'library'
    owned.mkdir()
    media = owned / 'upload.mp4'
    media.write_bytes(b'synthetic original upload')
    metadata = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    metadata.migrate()
    metadata.register_media(metadata.inspect_media(media))
    values = {'clips_directory': str(owned), 'imported_clip_folders': []}
    settings = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    readiness = ClipReadinessRegistry()
    manager = upload_manager.UploadManager(settings, readiness, metadata_manager=metadata)
    manager.set('upload_auto_delete', True)
    monkeypatch.setattr(manager, 'is_enabled', lambda: True)
    yield SimpleNamespace(manager=manager, metadata=metadata, media=media,
                          values=values, readiness=readiness, directory=tmp_path)
    manager.stop()


@pytest.mark.parametrize('during_upload', [
    'none', 'protect', 'favorite', 'replacement', 'database-failure', 'imported',
    'export', 'recovery', 'missing-manager',
])
def test_host_auto_delete_rechecks_current_protection_identity_ownership_and_admission(
        upload_library, monkeypatch, qtbot, during_upload, record_property):
    from test_library_trash_integration import make_trash_host, stop_trash_host

    state = upload_library
    requests = []
    manager, media = state.manager, state.media
    state.metadata.migrate(target_version=3)
    stale = ClipMetadataManager(database_path=state.metadata._database)
    manager._metadata_manager = stale
    grid = QWidget()
    qtbot.addWidget(grid)
    grid._release_thumbnail_reader = lambda path: True
    grid.remove_trashed_paths = lambda paths: None
    host = make_trash_host(manager._core_settings, state.metadata, grid, manager, readiness=state.readiness)
    manager.automatic_trash_requested.connect(host._on_automatic_trash_requested)

    def plugin(action, payload, **kwargs):
        requests.append((action, payload))
        if during_upload in ('protect', 'favorite'):
            state.metadata.set_library_flags(media, **{
                'protected' if during_upload == 'protect' else 'favorite': True})
        elif during_upload == 'replacement':
            media.rename(media.with_name('retained-uploaded-original.mp4'))
            media.write_bytes(b'new file under same upload name')
        elif during_upload == 'database-failure':
            state.metadata._database.rename(state.directory / 'retained-metadata.sqlite3')
            state.metadata._database.write_bytes(b'corrupt synthetic database')
        elif during_upload == 'imported':
            state.values['imported_clip_folders'] = [str(media.parent)]
        elif during_upload == 'export':
            assert export_queue._claim_paths((str(media),))
        elif during_upload == 'recovery':
            state.readiness.set_recovery_paths([str(media)])
        elif during_upload == 'missing-manager':
            manager._metadata_manager = None
            host.clip_metadata_manager = None
        return {'ok': True, 'message': 'Provider confirmed upload'}

    monkeypatch.setattr(manager, '_invoke_plugin', plugin)
    candidates, completed = [], []
    try:
        success, message = manager._do_single_upload(str(media), cleanup_candidates=candidates)
        assert len(candidates) == 1
        path, identity = candidates[0]
        host._request_clip_trash(path, identity, completed.append, automatic=True)
        qtbot.waitUntil(lambda: len(completed) == 1)
    finally:
        stop_trash_host(host)
    assert success is True and message.startswith('Provider confirmed upload')
    assert len(requests) == 1
    assert requests[0][0] == 'upload'
    assert requests[0][1]['config']['upload_auto_delete'] is False
    record_property('trash_result_error', completed[0].error)
    if during_upload == 'none':
        assert completed[0].succeeded, completed[0].error
        assert not media.exists()
        assert 'trash' in message.lower() and completed[0].succeeded
        item, = host._file_operations.list_trash()
        assert Path(item.stored_path).read_bytes() == b'synthetic original upload'
    else:
        assert media.exists()
        assert not completed[0].succeeded and completed[0].error
        expected_denial = {
            'protect': 'This clip is a favorite',
            'favorite': 'This clip is a favorite',
            'replacement': 'selected file identity changed',
            'database-failure': 'file is not a database',
            'imported': 'only owned clips can enter Trash',
            'export': 'queued for export or already reserved',
            'recovery': 'not ready for a file operation',
            'missing-manager': "'NoneType' object has no attribute 'resolve_media'",
        }
        assert expected_denial[during_upload] in completed[0].error
        if during_upload == 'missing-manager':
            assert host.clip_metadata_manager is None
        assert 'trash' in message.lower() or 'kept' in message.lower()
        if during_upload == 'replacement':
            assert media.read_bytes() == b'new file under same upload name'
            assert media.with_name('retained-uploaded-original.mp4').read_bytes() == b'synthetic original upload'
        else:
            assert media.read_bytes() == b'synthetic original upload'
    if during_upload != 'export':
        assert export_queue.begin_local_mutations([str(media)])
        export_queue.end_local_mutations([str(media)])


@pytest.mark.parametrize('response', [
    {'ok': 'false'}, {'ok': 1}, {'ok': None}, {}, {'ok': False},
])
def test_only_literal_true_provider_success_can_delete(upload_library, monkeypatch, response):
    state = upload_library
    calls = []
    monkeypatch.setattr(state.manager, '_invoke_plugin',
                        lambda *args, **kwargs: calls.append(args) or response)
    success, _message = state.manager._do_single_upload(str(state.media))
    assert success is False
    assert state.media.read_bytes() == b'synthetic original upload'
    assert len(calls) == 1


@pytest.mark.parametrize('option', [False, 'true', 1, None])
def test_auto_delete_requires_literal_boolean_opt_in(upload_library, monkeypatch, option):
    state = upload_library
    state.manager.set('upload_auto_delete', option)
    monkeypatch.setattr(state.manager, '_invoke_plugin',
                        lambda *args, **kwargs: {'ok': True, 'message': 'Uploaded'})
    assert state.manager._do_single_upload(str(state.media))[0] is True
    assert state.media.read_bytes() == b'synthetic original upload'


def test_actual_serialized_plugin_upload_request_disables_plugin_deletion(upload_library, monkeypatch):
    state = upload_library
    received = []
    monkeypatch.setattr(state.manager, '_activation', lambda _spec: {
        'activation_id': 'synthetic-activation', 'executable': 'never-launched.exe'})

    def fake_run(command, **options):
        received.append((command, json.loads(options['input'])))
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "message": "Uploaded"}\n', stderr='')

    monkeypatch.setattr(upload_manager.subprocess, 'run', fake_run)
    state.metadata.set_library_flags(state.media, protected=True)
    assert state.manager._do_single_upload(str(state.media))[0] is True
    assert len(received) == 1
    request = received[0][1]
    assert request['action'] == 'upload'
    assert request['path'] == str(state.media)
    assert request['config']['upload_auto_delete'] is False
    assert state.media.exists()


def test_compressed_upload_retains_copy_and_protected_original_without_trash_sink(
        upload_library, monkeypatch):
    state = upload_library
    original = state.media
    state.metadata.set_library_flags(original, favorite=True)
    uploaded_paths = []

    def compression(source, destination, preset, **kwargs):
        assert source == str(original)
        destination.write_bytes(b'synthetic compressed upload')
        return destination, SimpleNamespace(summary='synthetic compression')

    def plugin(action, payload, **kwargs):
        uploaded_paths.append(payload['path'])
        assert payload['config']['upload_auto_delete'] is False
        return {'ok': True, 'message': 'Uploaded compressed copy'}

    monkeypatch.setattr(upload_manager, 'compress_media', compression)
    monkeypatch.setattr(state.manager, '_invoke_plugin', plugin)
    success, message = state.manager._compress_then_upload(str(original))
    assert success is True and 'original preserved' in message.lower()
    assert len(uploaded_paths) == 1 and uploaded_paths[0] != str(original)
    assert original.read_bytes() == b'synthetic original upload'
    from pathlib import Path
    assert Path(uploaded_paths[0]).read_bytes() == b'synthetic compressed upload'
    assert 'kept' in message.lower()
    with closing(sqlite3.connect(state.metadata._database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
