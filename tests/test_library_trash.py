"""Recoverable removal on isolated, retained synthetic library copies."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from core.audio_manifest import (
    AudioSourceManifestEntry, build_manifest, manifest_path_for,
    read_manifest_for_media, write_manifest_atomic,
)
from core.clip_file_operations import ClipFileOperations
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import FileRole


class InterruptedProcess(BaseException):
    """Leave durable state for a new service rather than normal error recovery."""


def _manager(directory):
    return ClipMetadataManager(database_path=directory / 'metadata.sqlite3')


def _rows(directory):
    with sqlite3.connect(directory / 'metadata.sqlite3') as connection:
        return connection.execute(
            'SELECT clip_id,path,value FROM entries ORDER BY clip_id').fetchall()


def _seed(directory, *, manifest=True, suffix='.mp4'):
    owned = directory / 'library'
    owned.mkdir()
    source = owned / ('original' + suffix)
    source.write_bytes(b'retained synthetic primary media')
    manager = _manager(directory)
    manager.migrate(target_version=3)
    binding = manager.register_media(manager.inspect_media(source, hash_content=True))
    sidecar = None
    if manifest:
        audio = AudioSourceManifestEntry(
            str(uuid4()), 1, 'system', 'Game Audio', 'system-default', None,
            0, 10_000_000, 48_000, 2, 'float32')
        sidecar = write_manifest_atomic(build_manifest(
            media_path=source, transaction_id=str(uuid4()), sources=[audio]), source)
        manager.associate_file(binding.clip_id, manager.inspect_media(sidecar),
                               role=FileRole.SIDECAR)
    manager.set(str(source), tag='Keep tag', description='Keep description')
    # Trash fixtures must be ordinary clips; favorites are never deletable.
    manager.set_library_flags(source, favorite=False)
    manager.set_editor_draft(str(source), {'segments': [[0, 1, False]], 'opaque': [None, True]})
    # Unknown JSON fields have no separate public setter and must survive untouched.
    with sqlite3.connect(directory / 'metadata.sqlite3') as connection:
        value = json.loads(connection.execute(
            'SELECT value FROM entries WHERE clip_id=?', (binding.clip_id,)).fetchone()[0])
        value['unknown_future_metadata'] = {'nested': ['retain', None, 17]}
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps(value), binding.clip_id))
    manager.reload()
    return {
        'directory': directory, 'owned': owned, 'source': source, 'sidecar': sidecar,
        'manager': manager, 'binding': binding,
        'evidence': dict(manager.inspect_media(source).stat),
        'admission': {'owned_root': str(owned), 'imported_roots': (), 'allow_linked': False},
        'metadata': copy.deepcopy(manager.get_by_id(binding.clip_id)),
        'file_ids': {file.file_id for file in manager.files_for_clip(binding.clip_id)},
        'media_bytes': source.read_bytes(),
        'manifest_bytes': sidecar.read_bytes() if sidecar else None,
    }


@pytest.fixture
def library(tmp_path):
    return _seed(tmp_path)


def _fresh(library, **kwargs):
    manager = _manager(library['directory'])
    return manager, ClipFileOperations(manager, **kwargs)


def _trash(library, service=None):
    service = service or ClipFileOperations(library['manager'])
    operation = service.prepare_trash(
        library['binding'].clip_id, admission=library['admission'],
        expected_identity=library['evidence'])
    assert service.execute(operation.op_id).succeeded
    items = [item for item in service.list_trash()
             if item.clip_id == library['binding'].clip_id and item.status == 'TRASHED']
    assert len(items) == 1
    return items[0]


def _assert_live(library, manager, path):
    path = Path(path)
    assert path.read_bytes() == library['media_bytes']
    binding = manager.resolve_media(path)
    assert (binding.clip_id, binding.file_id) == (
        library['binding'].clip_id, library['binding'].file_id)
    assert manager.get_by_id(binding.clip_id) == library['metadata']
    assert {file.file_id for file in manager.files_for_clip(binding.clip_id)} == library['file_ids']
    assert manager.get_editor_draft(str(path)) == library['metadata']['editor_draft']['state']
    if library['sidecar']:
        manifest = read_manifest_for_media(path)
        assert manifest is not None
        assert manifest['sources'][0]['display_name'] == 'Game Audio'


def test_schema3_upgrade_is_backed_up_and_default_migration_remains_schema2(tmp_path):
    manager = _manager(tmp_path)
    assert manager.migrate().to_version == 2
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original')
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(str(media), tag='Keep')
    manager.set_editor_draft(str(media), {'unknown_edit': [None, 1]})
    before = _rows(tmp_path)

    result = manager.migrate(target_version=3)

    assert (result.from_version, result.to_version, result.changed) == (2, 3, True)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 2
        assert backup.execute('SELECT clip_id,path,value FROM entries ORDER BY clip_id').fetchall() == before
    assert _rows(tmp_path) == before
    reopened = _manager(tmp_path)
    assert reopened.inspect_schema().version == 3
    assert reopened.get_by_id(binding.clip_id) == manager.get_by_id(binding.clip_id)
    assert not reopened.migrate(target_version=3).changed
    assert ClipFileOperations(reopened).list_trash() == ()


@pytest.mark.parametrize('manifest,suffix', [(True, '.mp4'), (False, '.mp4'), (False, '.png')])
def test_trash_and_restore_preserve_bytes_ids_drafts_flags_and_unknown_metadata(
        tmp_path, manifest, suffix):
    library = _seed(tmp_path, manifest=manifest, suffix=suffix)
    item = _trash(library)
    assert item.original_path == str(library['source'])
    assert not library['source'].exists()
    stored = Path(item.stored_path)
    assert stored.is_relative_to(library['owned'] / '.fthr-trash')
    assert stored.read_bytes() == library['media_bytes']
    manager, service = _fresh(library)
    row = next(row for row in _rows(tmp_path) if row[0] == item.clip_id)
    assert row[1] is None
    assert manager.get_by_id(item.clip_id) == library['metadata']
    if manifest:
        assert not library['sidecar'].exists()
        assert read_manifest_for_media(stored) is not None

    operation = service.prepare_restore(item.trash_id, admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    reopened, recovered = _fresh(library)
    assert recovered.recover_pending() == ()
    _assert_live(library, reopened, library['source'])
    assert recovered.list_trash() == ()
    assert recovered.get_trash_item(item.trash_id).status == 'RESTORED'


def test_restore_chooses_noncolliding_name_without_inheriting_replacement_metadata(library):
    item = _trash(library)
    library['source'].write_bytes(b'new unrelated bytes')
    manager, service = _fresh(library)
    replacement = manager.register_media(manager.inspect_media(library['source']))
    manager.set(str(library['source']), tag='Replacement')
    assert replacement.clip_id != item.clip_id
    assert manager.get_by_id(item.clip_id) == library['metadata']
    operation = service.prepare_restore(item.trash_id, admission=library['admission'])
    result = service.execute(operation.op_id)
    assert result.succeeded
    restored = Path(result.new_path)
    assert restored.name == 'original (restored 1).mp4'
    assert library['source'].read_bytes() == b'new unrelated bytes'
    reopened, _ = _fresh(library)
    assert reopened.get(str(library['source']))['tag'] == 'Replacement'
    _assert_live(library, reopened, restored)


def test_restore_destination_created_after_prepare_is_never_overwritten(library):
    item = _trash(library)
    manager, service = _fresh(library)
    operation = service.prepare_restore(item.trash_id, admission=library['admission'])
    library['source'].write_bytes(b'foreign late destination')
    assert not service.execute(operation.op_id).succeeded
    assert library['source'].read_bytes() == b'foreign late destination'
    assert Path(item.stored_path).read_bytes() == library['media_bytes']
    assert _manager(library['directory']).get_by_id(item.clip_id) == library['metadata']


@pytest.mark.parametrize('protection', ['fresh-primary', 'protected-hardlink'])
def test_trash_rechecks_current_protection_including_real_hardlinks(library, protection):
    stale = _manager(library['directory'])
    writer = library['manager']
    path = library['source']
    if protection == 'protected-hardlink':
        path = library['owned'] / 'protected-alias.mp4'
        os.link(library['source'], path)
        writer.register_media(writer.inspect_media(path))
    writer.set_library_flags(path, protected=True)
    before = _rows(library['directory'])
    with pytest.raises((OSError, ValueError)):
        ClipFileOperations(stale).prepare_trash(
            library['binding'].clip_id, admission=library['admission'],
            expected_identity=library['evidence'])
    assert library['source'].read_bytes() == library['media_bytes']
    assert _rows(library['directory']) == before


@pytest.mark.parametrize('invalid', ['replacement', 'missing', 'foreign', 'derived', 'bad-manifest'])
def test_invalid_trash_inputs_never_remove_media_or_change_metadata(library, invalid):
    manager = library['manager']
    if invalid == 'replacement':
        library['source'].rename(library['owned'] / 'retained-original.mp4')
        library['source'].write_bytes(b'replacement')
    elif invalid == 'missing':
        library['source'].rename(library['owned'] / 'retained-offline.mp4')
    elif invalid == 'foreign':
        library['admission']['owned_root'] = str(library['directory'] / 'another-library')
        Path(library['admission']['owned_root']).mkdir()
        library['admission']['imported_roots'] = (str(library['owned']),)
        library['admission']['allow_linked'] = True
    elif invalid == 'derived':
        derived = library['owned'] / 'derived.mp4'
        derived.write_bytes(b'not authorized for destructive mutation')
        manager.associate_file(library['binding'].clip_id, manager.inspect_media(derived),
                               role=FileRole.DERIVED)
    else:
        library['sidecar'].write_bytes(b'{not valid json')
    before = _rows(library['directory'])
    existing = {path: path.read_bytes() for path in library['owned'].iterdir() if path.is_file()}
    with pytest.raises((OSError, ValueError)):
        ClipFileOperations(manager).prepare_trash(
            library['binding'].clip_id, admission=library['admission'],
            expected_identity=library['evidence'])
    assert _rows(library['directory']) == before
    assert all(path.read_bytes() == body for path, body in existing.items())


def test_trash_draft_is_not_evicted_by_more_than_250_live_drafts(library):
    item = _trash(library)
    manager, _ = _fresh(library)
    for index in range(251):
        other = library['owned'] / f'other-{index}.mp4'
        other.write_bytes(bytes([index]))
        manager.register_media(manager.inspect_media(other))
        assert manager.set_editor_draft(str(other), {'new_live_edit': index})
    reopened, service = _fresh(library)
    assert reopened.get_by_id(item.clip_id) == library['metadata']
    operation = service.prepare_restore(item.trash_id, admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    _assert_live(library, _manager(library['directory']), library['source'])


@pytest.mark.parametrize('action', ['details', 'draft', 'clear-draft', 'flags', 'export', 'rename'])
def test_stale_manager_cannot_reactivate_or_overwrite_a_trashed_clip(library, action):
    stale = _manager(library['directory'])
    item = _trash(library)
    before = _rows(library['directory'])
    calls = {
        'details': lambda: stale.set(str(library['source']), tag='Must not replace trash metadata'),
        'draft': lambda: stale.set_editor_draft(str(library['source']), {'must_not_publish': True}),
        'clear-draft': lambda: stale.clear_editor_draft(str(library['source'])),
        'flags': lambda: stale.set_library_flags(library['source'], favorite=False),
        'export': lambda: stale.export_members_snapshot(library['source']),
        'rename': lambda: ClipFileOperations(stale).prepare_rename(
            item.clip_id, library['owned'] / 'not-renamed.mp4', admission=library['admission']),
    }
    if action == 'draft':
        assert calls[action]() is False
    elif action == 'clear-draft':
        assert calls[action]() is None
    elif action == 'export':
        assert calls[action]() == ()
    else:
        with pytest.raises((OSError, ValueError)):
            calls[action]()
    assert _rows(library['directory']) == before
    assert _manager(library['directory']).get_by_id(item.clip_id) == library['metadata']
    assert Path(item.stored_path).read_bytes() == library['media_bytes']
    assert not library['source'].exists()


def test_path_reads_and_observed_stored_file_do_not_promote_trash_binding(library):
    stale = _manager(library['directory'])
    item = _trash(library)
    try:
        details = stale.get(str(library['source']))
    except OSError:
        details = None
    assert details in (None, {}, {'tag': '', 'description': ''})
    with pytest.raises((OSError, ValueError)):
        stale.get_library_flags(library['source'])
    manager, _ = _fresh(library)
    files = manager.files_for_clip(item.clip_id)
    primary = next(file for file in files if file.role == FileRole.PRIMARY)
    assert primary.binding_state == 'TRASHED'
    observed = manager.resolve_media(item.stored_path)
    assert observed is None or observed.state == 'TRASHED'
    assert next(file for file in manager.files_for_clip(item.clip_id)
                if file.file_id == primary.file_id).binding_state == 'TRASHED'
    assert manager.get_by_id(item.clip_id) == library['metadata']


@pytest.mark.parametrize('actor', ['fresh', 'stale'])
def test_new_file_at_old_path_gets_new_id_and_no_trash_metadata(library, actor):
    stale = _manager(library['directory'])
    item = _trash(library)
    library['source'].write_bytes(b'brand new primary')
    manager = stale if actor == 'stale' else _manager(library['directory'])
    if actor == 'stale':
        before = _rows(library['directory'])
        with pytest.raises(OSError):
            manager.register_media(manager.inspect_media(library['source']))
        assert _rows(library['directory']) == before
        assert Path(item.stored_path).read_bytes() == library['media_bytes']
        manager.reload()
    new = manager.register_media(manager.inspect_media(library['source']))
    assert new.clip_id != item.clip_id
    assert new.file_id != library['binding'].file_id
    assert manager.get_by_id(new.clip_id) == {}
    assert manager.get_library_flags(library['source']) == {'favorite': False, 'protected': False}
    assert manager.get_by_id(item.clip_id) == library['metadata']
    reopened, _ = _fresh(library)
    assert reopened.resolve_media(library['source']).clip_id == new.clip_id
    assert Path(item.stored_path).read_bytes() == library['media_bytes']


@pytest.mark.parametrize('kind', ['trash', 'restore'])
def test_metadata_abort_rolls_back_file_moves_and_keeps_full_metadata(library, kind):
    item = _trash(library) if kind == 'restore' else None
    manager, service = _fresh(library)
    operation = (service.prepare_trash(library['binding'].clip_id, admission=library['admission'])
                 if kind == 'trash' else
                 service.prepare_restore(item.trash_id, admission=library['admission']))
    before = _rows(library['directory'])
    with sqlite3.connect(library['directory'] / 'metadata.sqlite3') as connection:
        connection.execute("CREATE TRIGGER reject_path_update BEFORE UPDATE OF path ON entries "
                           "BEGIN SELECT RAISE(ABORT,'injected trash metadata failure'); END")
    assert service.execute(operation.op_id).status == 'ROLLED_BACK'
    assert _rows(library['directory']) == before
    reopened, _ = _fresh(library)
    assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
    if kind == 'trash':
        _assert_live(library, reopened, library['source'])
        assert library['sidecar'].read_bytes() == library['manifest_bytes']
    else:
        assert not library['source'].exists()
        assert Path(item.stored_path).read_bytes() == library['media_bytes']
        assert read_manifest_for_media(item.stored_path) is not None


@pytest.mark.parametrize('kind,point,occurrence', [
    (kind, point, occurrence)
    for kind in ('trash', 'restore')
    for point, occurrence in [
        ('after_prepared', 1), ('after_applying', 1),
        ('before_member_move', 1), ('before_member_move', 2),
        ('after_member_move', 1), ('after_member_move', 2),
        ('before_metadata_commit', 1), ('after_metadata_commit', 1),
        ('before_cleanup', 1), ('after_cleanup', 1),
    ]
] + [('restore', 'before_member_move', 3), ('restore', 'after_member_move', 3)])
def test_interrupted_move_recovers_twice_preserving_primary_manifest_and_metadata(
        library, kind, point, occurrence):
    item = _trash(library) if kind == 'restore' else None
    hits = []

    def crash(name, operation):
        if name == point:
            hits.append(operation.op_id)
            if len(hits) == occurrence:
                raise InterruptedProcess(name)

    manager, service = _fresh(library, checkpoint=crash)
    with pytest.raises(InterruptedProcess):
        if kind == 'trash':
            operation = service.prepare_trash(library['binding'].clip_id,
                                               admission=library['admission'])
        else:
            operation = service.prepare_restore(item.trash_id, admission=library['admission'])
        service.execute(operation.op_id)
    assert len(hits) == occurrence
    for _ in range(2):
        manager, recovered = _fresh(library)
        recovered.recover_pending()
    assert not recovered.blocked_paths()
    assert manager.get_by_id(library['binding'].clip_id) == library['metadata']
    committed = point in {'after_metadata_commit', 'before_cleanup', 'after_cleanup'}
    live = (kind == 'restore') == committed
    if live:
        _assert_live(library, manager, library['source'])
    else:
        assert not library['source'].exists()
        trashed = next(item for item in recovered.list_trash()
                       if item.clip_id == library['binding'].clip_id and item.status == 'TRASHED')
        assert Path(trashed.stored_path).read_bytes() == library['media_bytes']
        assert read_manifest_for_media(trashed.stored_path) is not None


@pytest.mark.parametrize('point,occurrence', [
    ('after_purge_decision', 1), ('before_purge_member', 1), ('before_purge_member', 2),
    ('after_purge_member', 1), ('after_purge_member', 2),
])
def test_purge_decision_is_durable_before_unlink_and_recovery_only_moves_forward(
        library, point, occurrence):
    item = _trash(library)
    hit = []

    def crash(name, operation):
        if name == point:
            observer, service = _fresh(library)
            assert next(row for row in service.list_trash()
                        if row.trash_id == item.trash_id).status == 'PURGING'
            assert observer.get_operation(operation.op_id).state == 'COMMITTED'
            hit.append(name)
            if len(hit) == occurrence:
                raise InterruptedProcess(name)

    manager, service = _fresh(library, checkpoint=crash)
    operation = service.prepare_purge(item.trash_id, admission=library['admission'])
    with pytest.raises(InterruptedProcess):
        service.execute(operation.op_id)
    assert hit == [point] * occurrence
    observer, pending = _fresh(library)
    with pytest.raises((OSError, ValueError)):
        pending.prepare_restore(item.trash_id, admission=library['admission'])
    with pytest.raises((OSError, ValueError)):
        observer.set_library_flags(library['source'], protected=False)
    for _ in range(2):
        observer, recovered = _fresh(library)
        recovered.recover_pending()
    assert recovered.list_trash() == ()
    assert recovered.get_trash_item(item.trash_id).status == 'PURGED'
    assert observer.get_operation(operation.op_id).cleanup_done
    assert not Path(item.stored_path).exists()
    assert not manifest_path_for(item.stored_path).exists()
    assert not library['source'].exists()


def test_foreign_file_at_purge_member_path_is_kept_and_not_reported_purged(library):
    item = _trash(library)
    stored = Path(item.stored_path)
    stored.rename(stored.with_name('retained-original.mp4'))
    stored.write_bytes(b'foreign replacement inside trash')
    manager, service = _fresh(library)
    try:
        operation = service.prepare_purge(item.trash_id, admission=library['admission'])
    except (OSError, ValueError):
        pass
    else:
        service.execute(operation.op_id)
    assert stored.read_bytes() == b'foreign replacement inside trash'
    assert manager.get_by_id(item.clip_id) == library['metadata']
    assert next(row for row in service.list_trash()
                if row.trash_id == item.trash_id).status != 'PURGED'


def _manifest_hardlink(library, *, registered):
    """Equal basenames/media make one real manifest valid for both primaries."""
    folder = library['owned'] / 'other-camera'
    folder.mkdir()
    media = folder / library['source'].name
    media.write_bytes(library['media_bytes'])
    manifest = manifest_path_for(media)
    os.link(library['sidecar'], manifest)
    manager = library['manager']
    binding = manager.register_media(manager.inspect_media(media))
    if registered:
        manager.associate_file(binding.clip_id, manager.inspect_media(manifest),
                               role=FileRole.SIDECAR)
    assert read_manifest_for_media(media) is not None
    assert os.path.samefile(library['sidecar'], manifest)
    return media, manifest, binding


@pytest.mark.parametrize('registered', [False, True])
def test_protected_manifest_hardlink_owner_blocks_trash_even_without_sidecar_registration(
        library, registered):
    other, manifest, binding = _manifest_hardlink(library, registered=registered)
    library['manager'].set_library_flags(other, protected=True)
    before = _rows(library['directory'])
    with pytest.raises(OSError):
        ClipFileOperations(library['manager']).prepare_trash(
            library['binding'].clip_id, admission=library['admission'])
    assert _rows(library['directory']) == before
    assert library['source'].read_bytes() == other.read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == manifest.read_bytes() == library['manifest_bytes']
    assert _manager(library['directory']).resolve_media(other).clip_id == binding.clip_id


@pytest.mark.parametrize('phase', ['prepared-trash', 'before-purge', 'prepared-purge'])
@pytest.mark.parametrize('registered', [False, True])
def test_late_manifest_alias_protection_prevents_trash_or_purge_without_loss(
        library, phase, registered):
    other, manifest, _ = _manifest_hardlink(library, registered=registered)
    manager, service = _fresh(library)
    if phase == 'prepared-trash':
        operation = service.prepare_trash(library['binding'].clip_id,
                                           admission=library['admission'])
        item = None
    else:
        item = _trash(library, service)
        operation = (service.prepare_purge(item.trash_id, admission=library['admission'])
                     if phase == 'prepared-purge' else None)
    writer = _manager(library['directory'])
    writer.set_library_flags(other, protected=True)
    before = _rows(library['directory'])
    if operation is None:
        with pytest.raises(OSError):
            service.prepare_purge(item.trash_id, admission=library['admission'])
    else:
        assert not service.execute(operation.op_id).succeeded
        service.recover_pending()
    assert _rows(library['directory']) == before
    assert other.read_bytes() == library['media_bytes']
    assert manifest.read_bytes() == library['manifest_bytes']
    original_location = library['source'] if item is None else Path(item.stored_path)
    assert original_location.read_bytes() == library['media_bytes']
    assert manifest_path_for(original_location).read_bytes() == library['manifest_bytes']


def test_manifest_protected_after_purge_commit_is_retained_with_pending_metadata(library):
    other, manifest, _ = _manifest_hardlink(library, registered=False)
    item = _trash(library)
    hits = []

    def protect_second_member(name, operation):
        if name == 'before_purge_member':
            hits.append(name)
            if len(hits) == 2:
                assert _manager(library['directory']).get_operation(operation.op_id).state == 'COMMITTED'
                _manager(library['directory']).set_library_flags(other, protected=True)

    manager, service = _fresh(library, checkpoint=protect_second_member)
    operation = service.prepare_purge(item.trash_id, admission=library['admission'])
    assert service.execute(operation.op_id).succeeded  # Durable decision, not completed cleanup.
    assert len(hits) == 2
    for _ in range(2):
        manager, service = _fresh(library)
        service.recover_pending()
    durable = manager.get_operation(operation.op_id)
    assert durable.state == 'COMMITTED' and not durable.cleanup_done and durable.error
    assert service.get_trash_item(item.trash_id).status == 'PURGING'
    assert manager.get_by_id(item.clip_id) == library['metadata']
    assert not Path(item.stored_path).exists()
    assert manifest_path_for(item.stored_path).read_bytes() == library['manifest_bytes']
    assert manifest.read_bytes() == library['manifest_bytes']
    assert other.read_bytes() == library['media_bytes']


@pytest.mark.parametrize('action', ['register', 'details', 'draft'])
def test_stale_original_path_writer_never_detaches_a_restored_binding(library, action):
    stale = _manager(library['directory'])
    item = _trash(library)
    manager, service = _fresh(library)
    destination = library['owned'] / 'restored-elsewhere.mp4'
    operation = service.prepare_restore(item.trash_id, destination=destination,
                                         admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    library['source'].write_bytes(b'new unrelated original path')
    before = _rows(library['directory'])
    restored_files = _manager(library['directory']).files_for_clip(item.clip_id)
    calls = {
        'register': lambda: stale.register_media(stale.inspect_media(library['source'])),
        'details': lambda: stale.set(str(library['source']), tag='New file details'),
        'draft': lambda: stale.set_editor_draft(str(library['source']), {'new_file_edit': True}),
    }
    with pytest.raises(OSError):
        calls[action]()
    assert _rows(library['directory']) == before
    reopened = _manager(library['directory'])
    assert reopened.files_for_clip(item.clip_id) == restored_files
    _assert_live(library, reopened, destination)
    stale.reload()
    new = stale.register_media(stale.inspect_media(library['source']))
    assert new.clip_id != item.clip_id
    assert new.file_id != library['binding'].file_id
    assert stale.get_by_id(new.clip_id) == {}
    assert _manager(library['directory']).files_for_clip(item.clip_id) == restored_files
    _assert_live(library, _manager(library['directory']), destination)


def test_core_trash_never_silently_adopts_a_legacy_binding(tmp_path):
    owned = tmp_path / 'library'
    owned.mkdir()
    source = owned / 'legacy.mp4'
    source.write_bytes(b'legacy original bytes')
    (tmp_path / 'metadata.json').write_text(json.dumps({str(source): {
        'tag': 'Legacy tag', 'description': 'Preserve until deliberate host adoption'}}),
        encoding='utf-8')
    manager = _manager(tmp_path)
    manager.migrate(target_version=3)
    binding = manager.resolve_media(source)
    assert binding.state == 'LEGACY_UNVERIFIED'
    before = _rows(tmp_path)
    with pytest.raises(OSError):
        ClipFileOperations(manager).prepare_trash(binding.clip_id, admission={
            'owned_root': str(owned), 'imported_roots': (), 'allow_linked': False})
    assert _rows(tmp_path) == before
    assert source.read_bytes() == b'legacy original bytes'
    assert _manager(tmp_path).resolve_media(source).state == 'LEGACY_UNVERIFIED'


def test_cross_volume_parent_evidence_is_rejected_instead_of_copying(library, monkeypatch):
    original = ClipFileOperations._parent

    def different_volume(path):
        result = original(path)
        if '.fthr-trash' in Path(path).parts:
            result = {**result, 'st_dev': result['st_dev'] + 1}
        return result

    monkeypatch.setattr(ClipFileOperations, '_parent', staticmethod(different_volume))
    before = _rows(library['directory'])
    with pytest.raises(OSError, match='Cross-volume'):
        ClipFileOperations(library['manager']).prepare_trash(
            library['binding'].clip_id, admission=library['admission'])
    assert _rows(library['directory']) == before
    assert library['source'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']


def test_foreign_replacement_after_purge_decision_keeps_metadata_and_stays_pending(library):
    item = _trash(library)
    stored = Path(item.stored_path)
    retained = stored.parent.parent / 'retained-before-purge-replacement.mp4'
    hits = []

    def replace_after_commit(name, operation):
        if name == 'after_purge_decision':
            hits.append(operation.op_id)
            stored.rename(retained)
            stored.write_bytes(b'foreign bytes after durable purge decision')

    manager, service = _fresh(library, checkpoint=replace_after_commit)
    operation = service.prepare_purge(item.trash_id, admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    assert hits == [operation.op_id]
    for _ in range(2):
        manager, service = _fresh(library)
        service.recover_pending()
    durable = manager.get_operation(operation.op_id)
    assert durable.state == 'COMMITTED' and durable.error and not durable.cleanup_done
    assert service.get_trash_item(item.trash_id).status == 'PURGING'
    assert manager.get_by_id(item.clip_id) == library['metadata']
    assert retained.read_bytes() == library['media_bytes']
    assert stored.read_bytes() == b'foreign bytes after durable purge decision'
    assert manifest_path_for(stored).read_bytes() == library['manifest_bytes']


@pytest.mark.parametrize('write', ['details', 'draft', 'flags'])
def test_active_writes_do_not_resolve_paths_of_unrelated_trash_members(library, monkeypatch, write):
    import core.clip_metadata_manager as module

    first = _trash(library)
    manager, service = _fresh(library)
    second = library['owned'] / 'second.mp4'
    second.write_bytes(b'second archived primary')
    binding = manager.register_media(manager.inspect_media(second))
    operation = service.prepare_trash(binding.clip_id, admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    active = library['owned'] / 'active.mp4'
    active.write_bytes(b'active unrelated primary')
    active_binding = manager.register_media(manager.inspect_media(active))
    trash_items = service.list_trash()
    assert len(trash_items) == 2
    unrelated = {os.path.normcase(os.path.abspath(file.path))
                 for item in trash_items for file in manager.files_for_clip(item.clip_id)}
    observed = []
    original = module.canonical_media_path

    def counted(path):
        text = os.path.normcase(os.path.abspath(os.fspath(path)))
        observed.append(text)
        assert text not in unrelated, 'ordinary writes must not resolve every archived member path'
        return original(path)

    with monkeypatch.context() as boundary:
        boundary.setattr(module, 'canonical_media_path', counted)
        if write == 'details':
            manager.set(str(active), tag='Active edit')
        elif write == 'draft':
            assert manager.set_editor_draft(str(active), {'active_edit': True})
        else:
            manager.set_library_flags(active, favorite=True, protected=False)
    assert observed  # The boundary observes real path work, rather than an unused stub.
    assert _manager(library['directory']).resolve_media(active).clip_id == active_binding.clip_id
    before = _rows(library['directory'])
    with pytest.raises(OSError):
        manager.set(first.stored_path, tag='Must still reject the actual inactive target')
    assert _rows(library['directory']) == before
    assert Path(first.stored_path).read_bytes() == library['media_bytes']


def test_targeted_recovery_does_not_process_another_prepared_operation(library):
    manager = library['manager']
    second = library['owned'] / 'second.mp4'
    second.write_bytes(b'second operation must remain untouched')
    binding = manager.register_media(manager.inspect_media(second))
    manager.set(str(second), tag='Independent metadata')
    interrupted = []

    def crash(name, operation):
        if name == 'after_member_move':
            interrupted.append(operation.op_id)
            raise InterruptedProcess(name)

    service = ClipFileOperations(manager, checkpoint=crash)
    first = service.prepare_trash(library['binding'].clip_id, admission=library['admission'])
    other = service.prepare_trash(binding.clip_id, admission=library['admission'])
    with pytest.raises(InterruptedProcess):
        service.execute(first.op_id)
    assert interrupted == [first.op_id]
    fresh, recovery = _fresh(library)
    other_before = fresh.get_operation(other.op_id)
    other_metadata = copy.deepcopy(fresh.get_by_id(binding.clip_id))
    recovery.recover_operation(first.op_id)
    final = _manager(library['directory'])
    assert final.get_operation(first.op_id).state == 'ROLLED_BACK'
    assert final.get_operation(other.op_id) == other_before
    assert final.get_operation(other.op_id).state == 'PREPARED'
    assert final.get_by_id(binding.clip_id) == other_metadata
    assert final.resolve_media(second).clip_id == binding.clip_id
    assert second.read_bytes() == b'second operation must remain untouched'
    _assert_live(library, final, library['source'])


def test_late_alias_protection_allows_safe_rollback_of_an_already_moved_primary(library):
    alias = library['owned'] / 'retained-alias.mp4'
    os.link(library['source'], alias)
    manager = library['manager']
    alias_binding = manager.register_media(manager.inspect_media(alias))
    original_alias_identity = dict(manager.inspect_media(alias).stat)
    moved = []

    def protect_after_primary_move(name, operation):
        if name == 'after_member_move' and not moved:
            moved.append(operation.op_id)
            assert not library['source'].exists()
            assert library['sidecar'].read_bytes() == library['manifest_bytes']
            _manager(library['directory']).set_library_flags(alias, protected=True)

    service = ClipFileOperations(manager, checkpoint=protect_after_primary_move)
    operation = service.prepare_trash(
        library['binding'].clip_id, admission=library['admission'],
        expected_identity=library['evidence'])

    result = service.execute(operation.op_id)

    assert moved == [operation.op_id]
    assert result.status == 'ROLLED_BACK'
    fresh, recovery = _fresh(library)
    assert fresh.get_operation(operation.op_id).state == 'ROLLED_BACK'
    _assert_live(library, fresh, library['source'])
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert alias.read_bytes() == library['media_bytes']
    assert dict(fresh.inspect_media(alias).stat) == original_alias_identity
    assert fresh.resolve_media(alias).clip_id == alias_binding.clip_id
    assert fresh.get_library_flags(alias) == {'favorite': True, 'protected': True}
    assert recovery.list_trash() == ()


@pytest.mark.parametrize('winerror', [32, 33])
@pytest.mark.parametrize('change', ['protect', 'replace'])
def test_sharing_retry_rechecks_fresh_protection_and_source_identity_outside_sql_guard(
        library, monkeypatch, winerror, change):
    import time
    from types import SimpleNamespace
    import core.clip_trash as module

    attempts, pauses = [], []
    retained = library['source'].with_name('retained-before-retry.mp4')
    alias = library['source'].with_name('alias-protected-during-retry.mp4')
    os.link(library['source'], alias)
    library['manager'].register_media(library['manager'].inspect_media(alias))
    native_move = module.rename_no_replace

    def locked_once(source, destination):
        attempts.append((source, destination))
        if len(attempts) == 1:
            error = OSError('Injected Windows sharing lock')
            error.winerror = winerror
            raise error
        return native_move(source, destination)

    def pause(seconds):
        pauses.append(seconds)
        assert len(pauses) == 1
        if change == 'protect':
            # A distinct SQLite writer must be able to commit during the pause.
            _manager(library['directory']).set_library_flags(alias, protected=True)
        else:
            library['source'].rename(retained)
            library['source'].write_bytes(b'foreign replacement while sharing lock was pending')

    monkeypatch.setattr(module, 'rename_no_replace', locked_once)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=pause))
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_trash(
        library['binding'].clip_id, admission=library['admission'],
        expected_identity=library['evidence'])

    result = service.execute(operation.op_id)

    assert not result.succeeded
    assert len(attempts) == len(pauses) == 1
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    fresh = _manager(library['directory'])
    expected_metadata = copy.deepcopy(library['metadata'])
    if change == 'protect':
        assert result.status == 'ROLLED_BACK'
        assert library['source'].read_bytes() == library['media_bytes']
        assert fresh.resolve_media(library['source']).file_id == library['binding'].file_id
        assert fresh.get_library_flags(alias)['protected'] is True
    else:
        assert retained.read_bytes() == library['media_bytes']
        assert library['source'].read_bytes() == b'foreign replacement while sharing lock was pending'
    assert fresh.get_by_id(library['binding'].clip_id) == expected_metadata
