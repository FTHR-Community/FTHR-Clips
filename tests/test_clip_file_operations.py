"""Durable local rename contracts on retained synthetic library copies."""
from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from core.audio_manifest import (
    AudioSourceManifestEntry, build_manifest, manifest_path_for,
    read_manifest_for_media, write_manifest_atomic,
)
from core.clip_file_operations import ClipFileOperations
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import FileRole


class SimulatedCrash(BaseException):
    """Bypass ordinary error recovery to model interrupted process execution."""


def _manager(directory):
    return ClipMetadataManager(database_path=directory / 'metadata.sqlite3')


def _audio_manifest(media):
    from uuid import uuid4

    source = AudioSourceManifestEntry(
        str(uuid4()), 1, 'system', 'System Mix', 'system-default', None,
        0, 10_000_000, 48_000, 2, 'float32')
    return write_manifest_atomic(build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[source]), media)


def _seed_rename_library(tmp_path, *, associate=True):
    owned = tmp_path / 'library'
    owned.mkdir()
    source = owned / 'source.mp4'
    source.write_bytes(b'synthetic media bytes: retained exactly')
    sidecar = _audio_manifest(source)
    manager = _manager(tmp_path)
    manager.migrate()
    binding = manager.register_media(manager.inspect_media(source, hash_content=True))
    associated = (manager.associate_file(binding.clip_id, manager.inspect_media(sidecar),
                                         role=FileRole.SIDECAR) if associate else None)
    manager.set(str(source), tag='Retained tag', description='Retained description')
    manager.set_editor_draft(str(source), {'future_edit': {'keep': [1, None, True]}})
    admission = {'owned_root': str(owned), 'imported_roots': (), 'allow_linked': False}
    return {
        'directory': tmp_path, 'owned': owned, 'source': source, 'sidecar': sidecar,
        'destination': owned / 'renamed.mp4', 'manager': manager, 'binding': binding,
        'sidecar_id': associated.file_id if associated else None, 'admission': admission,
        'media_bytes': source.read_bytes(), 'manifest_bytes': sidecar.read_bytes(),
        'metadata': copy.deepcopy(manager.get_by_id(binding.clip_id)),
    }


@pytest.fixture
def rename_library(tmp_path):
    return _seed_rename_library(tmp_path)


def _fresh_service(library, **kwargs):
    manager = _manager(library['directory'])
    return manager, ClipFileOperations(manager, **kwargs)


def _assert_original_manifest_preserved(library, operation):
    locations = (library['sidecar'], Path(operation.plan['backup']))
    assert sum(path.is_file() and not path.is_symlink()
               and path.read_bytes() == library['manifest_bytes'] for path in locations) == 1


def _assert_committed(library, manager):
    source, destination = library['source'], library['destination']
    assert not source.exists()
    assert destination.read_bytes() == library['media_bytes']
    assert read_manifest_for_media(destination) is not None
    binding = manager.resolve_media(destination)
    assert (binding.clip_id, binding.file_id) == (
        library['binding'].clip_id, library['binding'].file_id)
    assert manager.get_by_id(binding.clip_id) == library['metadata']
    assert {file.file_id for file in manager.files_for_clip(binding.clip_id)} == {
        library['binding'].file_id, library['sidecar_id']}
    assert manager.get_editor_draft(str(destination)) == library['metadata']['editor_draft']['state']


def test_successful_media_and_manifest_rename_preserves_identity_and_full_metadata(rename_library):
    library = rename_library
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    manager, recovered = _fresh_service(library)
    assert recovered.recover_pending() == ()
    _assert_committed(library, manager)


@pytest.mark.parametrize('checkpoint', [
    'after_prepared', 'after_applying', 'after_sidecar_backup', 'after_media_rename',
    'after_manifest_rename', 'before_metadata_commit', 'after_metadata_commit',
    'before_cleanup', 'after_cleanup',
])
def test_interrupted_rename_recovers_twice_without_losing_media_manifest_or_ids(
        rename_library, checkpoint):
    library = rename_library
    interrupted = []

    def crash(name, operation):
        if name == checkpoint:
            interrupted.append(operation.op_id)
            raise SimulatedCrash(name)

    service = ClipFileOperations(library['manager'], checkpoint=crash)
    with pytest.raises(SimulatedCrash):
        operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                           admission=library['admission'])
        service.execute(operation.op_id)
    assert len(interrupted) == 1
    manager, recovered = _fresh_service(library)
    recovered.recover_pending()
    reopened, recovered_again = _fresh_service(library)
    recovered_again.recover_pending()
    assert not recovered_again.blocked_paths()
    if checkpoint in {'after_metadata_commit', 'before_cleanup', 'after_cleanup'}:
        _assert_committed(library, reopened)
        assert reopened.get_operation(interrupted[0]).state == 'COMMITTED'
    else:
        assert library['source'].read_bytes() == library['media_bytes']
        assert library['sidecar'].read_bytes() == library['manifest_bytes']
        assert not library['destination'].exists()
        assert not manifest_path_for(library['destination']).exists()
        assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
        observed = reopened.resolve_media(library['source'])
        assert (observed.clip_id, observed.file_id) == (
            library['binding'].clip_id, library['binding'].file_id)
        assert reopened.get_operation(interrupted[0]).state == 'ROLLED_BACK'


def test_concurrent_draft_save_after_prepared_is_rejected_without_metadata_loss(rename_library):
    library = rename_library
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    concurrent = _manager(library['directory'])
    with pytest.raises(OSError):
        concurrent.set(str(library['source']), tag='Must not publish')
    with pytest.raises(OSError):
        concurrent.set_editor_draft(str(library['source']), {'new_edit': [1, 2, 3]})
    assert concurrent.get_by_id(library['binding'].clip_id) == library['metadata']
    assert _manager(library['directory']).get_by_id(library['binding'].clip_id) == library['metadata']

    assert service.execute(operation.op_id).succeeded

    reopened, _ = _fresh_service(library)
    _assert_committed(library, reopened)


def test_regular_screenshot_rename_without_manifest_is_supported(tmp_path, qapp):
    from PySide6.QtGui import QImage

    source, destination = tmp_path / 'screen.png', tmp_path / 'renamed.png'
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0)
    assert image.save(str(source))
    original = source.read_bytes()
    manager = _manager(tmp_path)
    manager.migrate()
    binding = manager.register_media(manager.inspect_media(source))
    manager.set(str(source), tag='Screenshot')
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(binding.clip_id, destination, admission={
        'owned_root': str(tmp_path), 'imported_roots': (), 'allow_linked': False})

    assert service.execute(operation.op_id).succeeded

    reopened = _manager(tmp_path)
    assert destination.read_bytes() == original
    assert not source.exists()
    assert not manifest_path_for(destination).exists()
    assert reopened.resolve_media(destination).clip_id == binding.clip_id
    assert reopened.get(str(destination))['tag'] == 'Screenshot'


@pytest.mark.parametrize('target', ['media', 'manifest'])
@pytest.mark.parametrize('collision', ['regular', 'dangling-symlink'])
def test_destination_created_after_prepare_is_not_overwritten_or_undone(
        rename_library, target, collision):
    library = rename_library
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    occupied = (library['destination'] if target == 'media'
                else manifest_path_for(library['destination']))
    if collision == 'regular':
        occupied.write_bytes(b'foreign file created after plan')
    else:
        try:
            occupied.symlink_to(library['owned'] / 'absent-foreign-target')
        except OSError as error:
            pytest.skip(f'creating symlinks is unavailable: {error}')
    result = service.execute(operation.op_id)

    assert not result.succeeded
    assert result.status == 'RECOVERY_REQUIRED'
    for _ in range(2):
        _, recovered = _fresh_service(library)
        recovered.recover_pending()
    assert sum(path.is_file() and not path.is_symlink()
               and path.read_bytes() == library['media_bytes']
               for path in (library['source'], library['destination'])) == 1
    _assert_original_manifest_preserved(library, operation)
    if collision == 'regular':
        assert occupied.read_bytes() == b'foreign file created after plan'
    else:
        assert occupied.is_symlink() and not occupied.exists()
    assert recovered.blocked_paths()

    assert library['manager'].get_by_id(library['binding'].clip_id) == library['metadata']


def test_current_owned_root_is_rechecked_after_prepare(rename_library):
    library = rename_library
    settings = dict(library['admission'])
    service = ClipFileOperations(library['manager'], settings_provider=lambda: settings)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    other = library['directory'] / 'different-library'
    other.mkdir()
    settings['owned_root'] = str(other)

    result = service.execute(operation.op_id)

    assert result.status == 'RECOVERY_REQUIRED'
    assert library['source'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert not library['destination'].exists()


def test_imported_clip_requires_explicit_linked_confirmation(rename_library):
    library = rename_library
    owned = library['directory'] / 'actual-owned-library'
    owned.mkdir()
    admission = {'owned_root': str(owned), 'imported_roots': [str(library['owned'])],
                 'allow_linked': False}
    service = ClipFileOperations(library['manager'])
    with pytest.raises((OSError, ValueError)):
        service.prepare_rename(library['binding'].clip_id, library['destination'],
                               admission=admission)
    assert not library['manager'].pending_operations()
    admission['allow_linked'] = True
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=admission)
    assert service.execute(operation.op_id).succeeded
    reopened, _ = _fresh_service(library)
    _assert_committed(library, reopened)


@pytest.mark.parametrize('when', ['before-prepare', 'after-prepare'])
def test_missing_media_never_discards_metadata_or_foreign_offline_copy(rename_library, when):
    library = rename_library
    service = ClipFileOperations(library['manager'])
    if when == 'after-prepare':
        operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                           admission=library['admission'])
    retained = library['owned'] / 'offline-original.mp4'
    library['source'].rename(retained)
    if when == 'before-prepare':
        with pytest.raises((OSError, ValueError)):
            service.prepare_rename(library['binding'].clip_id, library['destination'],
                                   admission=library['admission'])
    else:
        assert service.execute(operation.op_id).status == 'RECOVERY_REQUIRED'
    reopened, recovered = _fresh_service(library)
    recovered.recover_pending()
    assert retained.read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
    assert not library['destination'].exists()


def test_foreign_replacement_at_rollback_source_is_never_overwritten(rename_library):
    library = rename_library

    def crash(name, operation):
        if name == 'after_media_rename':
            raise SimulatedCrash(name)

    service = ClipFileOperations(library['manager'], checkpoint=crash)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    with pytest.raises(SimulatedCrash):
        service.execute(operation.op_id)
    library['source'].write_bytes(b'foreign replacement at original filename')
    for _ in range(2):
        manager, recovered = _fresh_service(library)
        recovered.recover_pending()
    assert library['source'].read_bytes() == b'foreign replacement at original filename'
    assert library['destination'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert manager.get_operation(operation.op_id).state == 'RECOVERY_REQUIRED'
    assert recovered.blocked_paths()


def test_case_only_rename_keeps_primary_and_sidecar_file_ids(rename_library):
    library = rename_library
    library['destination'] = library['source'].with_name('SOURCE.mp4')
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    assert service.execute(operation.op_id).succeeded
    manager, _ = _fresh_service(library)
    assert library['destination'].read_bytes() == library['media_bytes']
    assert read_manifest_for_media(library['destination']) is not None
    binding = manager.resolve_media(library['destination'])
    assert (binding.clip_id, binding.file_id) == (
        library['binding'].clip_id, library['binding'].file_id)
    assert manager.get_by_id(binding.clip_id) == library['metadata']
    assert {file.file_id for file in manager.files_for_clip(binding.clip_id)} == {
        library['binding'].file_id, library['sidecar_id']}


def test_destination_cannot_escape_source_directory(rename_library):
    library = rename_library
    outside = library['directory'] / 'outside'
    outside.mkdir()
    service = ClipFileOperations(library['manager'])
    with pytest.raises((OSError, ValueError)):
        service.prepare_rename(library['binding'].clip_id, Path(outside / 'moved.mp4'),
                               admission=library['admission'])
    assert library['source'].read_bytes() == library['media_bytes']
    assert not library['manager'].pending_operations()


def test_changed_source_identity_after_prepare_is_not_renamed(rename_library):
    library = rename_library
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    retained = library['owned'] / 'retained-original.mp4'
    library['source'].rename(retained)
    library['source'].write_bytes(b'new unrelated media at same path')

    assert service.execute(operation.op_id).status == 'RECOVERY_REQUIRED'

    assert library['source'].read_bytes() == b'new unrelated media at same path'
    assert retained.read_bytes() == library['media_bytes']
    _assert_original_manifest_preserved(library, operation)
    assert not library['destination'].exists()
    reopened, _ = _fresh_service(library)
    assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
    assert reopened.get(str(library['source'])) == {'tag': '', 'description': ''}


def test_destination_symlink_parent_is_rejected_before_prepare(rename_library):
    library = rename_library
    alias = library['directory'] / 'linked-library'
    try:
        alias.symlink_to(library['owned'], target_is_directory=True)
    except OSError as error:
        pytest.skip(f'creating directory symlinks is unavailable: {error}')
    service = ClipFileOperations(library['manager'])
    with pytest.raises((OSError, ValueError)):
        service.prepare_rename(library['binding'].clip_id, alias / 'renamed.mp4',
                               admission=library['admission'])
    assert library['source'].read_bytes() == library['media_bytes']
    assert not library['manager'].pending_operations()


@pytest.mark.parametrize('allow_linked', [False, True])
def test_unadmitted_external_original_is_not_owned_by_registering_it(rename_library, allow_linked):
    library = rename_library
    actual_owned = library['directory'] / 'actual-library'
    actual_owned.mkdir()
    service = ClipFileOperations(library['manager'])
    with pytest.raises((OSError, ValueError)):
        service.prepare_rename(library['binding'].clip_id, library['destination'], admission={
            'owned_root': str(actual_owned), 'imported_roots': (), 'allow_linked': allow_linked})
    assert library['source'].read_bytes() == library['media_bytes']
    assert not library['manager'].pending_operations()


def test_durable_commit_with_ram_publication_failure_is_never_rolled_back(
        rename_library, monkeypatch):
    library = rename_library
    manager = library['manager']
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    durable_states = []

    def fail_publication(file, metadata):
        durable_states.append(manager.get_operation(operation.op_id).state)
        raise RuntimeError('injected RAM publication failure after SQLite commit')

    monkeypatch.setattr(manager, '_publish_file', fail_publication)

    assert service.execute(operation.op_id).succeeded

    assert durable_states == ['COMMITTED']
    for _ in range(2):
        reopened, recovered = _fresh_service(library)
        recovered.recover_pending()
        _assert_committed(library, reopened)
    assert not recovered.blocked_paths()


@pytest.mark.parametrize('failure', ['sqlite-abort', 'deferred-commit'])
def test_sqlite_rename_failure_restores_original_bytes_and_keeps_metadata_atomic(
        rename_library, monkeypatch, failure):
    import core.clip_metadata_manager as module

    library = rename_library
    manager = library['manager']
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    connect = sqlite3.connect
    if failure == 'sqlite-abort':
        with connect(library['directory'] / 'metadata.sqlite3') as connection:
            connection.execute("CREATE TRIGGER reject_rename BEFORE UPDATE OF path ON entries "
                               "BEGIN SELECT RAISE(ABORT,'injected rename abort'); END")
    else:
        class DeferredFailure(sqlite3.Connection):
            def __exit__(self, *args):
                if (args[0] is None and self.in_transaction
                        and self.execute("SELECT 1 FROM file_operations WHERE state='COMMITTED'").fetchone()):
                    self.execute('CREATE TEMP TABLE qa_rename_parent (id INTEGER PRIMARY KEY)')
                    self.execute('CREATE TEMP TABLE qa_rename_child ('
                                 'id INTEGER REFERENCES qa_rename_parent(id) DEFERRABLE INITIALLY DEFERRED)')
                    self.execute('INSERT INTO qa_rename_child VALUES (123)')
                return super().__exit__(*args)

        def deferred_connect(*args, **kwargs):
            kwargs['factory'] = DeferredFailure
            return connect(*args, **kwargs)

        monkeypatch.setattr(module.sqlite3, 'connect', deferred_connect)

    assert service.execute(operation.op_id).status == 'ROLLED_BACK'

    reopened, recovered = _fresh_service(library)
    recovered.recover_pending()
    assert library['source'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert not library['destination'].exists()
    assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
    binding = reopened.resolve_media(library['source'])
    assert (binding.clip_id, binding.file_id) == (
        library['binding'].clip_id, library['binding'].file_id)
    assert reopened.get_operation(operation.op_id).state == 'ROLLED_BACK'


@pytest.mark.parametrize('role', [FileRole.SIDECAR, FileRole.DERIVED])
def test_stale_registry_is_rejected_before_preparing_then_reload_preserves_sidecar_id(
        tmp_path, role):
    library = _seed_rename_library(tmp_path, associate=False)
    writer = library['manager']
    stale = _manager(tmp_path)
    associated_path = library['sidecar']
    if role == FileRole.DERIVED:
        associated_path = library['owned'] / 'derived.mp4'
        associated_path.write_bytes(b'unknown associated derived file')
    linked = writer.associate_file(library['binding'].clip_id, writer.inspect_media(associated_path),
                                    role=role)
    service = ClipFileOperations(stale)

    with pytest.raises(OSError):
        service.prepare_rename(library['binding'].clip_id, library['destination'],
                               admission=library['admission'])

    assert library['source'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert not writer.pending_operations()
    stale.reload()
    if role == FileRole.DERIVED:
        with pytest.raises(ValueError):
            service.prepare_rename(library['binding'].clip_id, library['destination'],
                                   admission=library['admission'])
        assert any(file.file_id == linked.file_id
                   for file in writer.files_for_clip(library['binding'].clip_id))
    else:
        library['sidecar_id'] = linked.file_id
        operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                           admission=library['admission'])
        assert service.execute(operation.op_id).succeeded
        reopened, _ = _fresh_service(library)
        _assert_committed(library, reopened)


def test_changed_registry_snapshot_before_commit_never_creates_duplicate_sidecar_ids(
        rename_library):
    from uuid import uuid4

    library = rename_library
    manager = library['manager']
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    changed_id = str(uuid4())
    # Simulate an external committed writer that bypassed manager reservations.
    with sqlite3.connect(library['directory'] / 'metadata.sqlite3') as connection:
        connection.execute('UPDATE clip_files SET file_id=? WHERE file_id=?',
                           (changed_id, library['sidecar_id']))

    assert not service.execute(operation.op_id).succeeded

    reopened, recovered = _fresh_service(library)
    recovered.recover_pending()
    files = reopened.files_for_clip(library['binding'].clip_id)
    assert {file.file_id for file in files} == {library['binding'].file_id, changed_id}
    assert library['source'].read_bytes() == library['media_bytes']
    assert library['sidecar'].read_bytes() == library['manifest_bytes']
    assert not library['destination'].exists()


@pytest.mark.parametrize('oversized', [False, True])
def test_manifest_input_reads_are_bounded_before_json_parsing(
        rename_library, monkeypatch, oversized):
    import builtins

    library = rename_library
    sidecar = library['sidecar']
    limit = 1024 * 1024
    reads = []
    path_open, builtin_open = Path.open, builtins.open

    class BoundedInput:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= limit + 1, 'manifest input was read without a bounded size'
            if oversized and size == limit + 1:
                # Model an oversized input without creating a large disk file.
                return b' ' * (limit + 1)
            return self.stream.read(size)

    def wrap(file, stream, mode):
        if (isinstance(file, (str, Path)) and Path(file) == sidecar
                and not any(flag in str(mode) for flag in 'wxa+')):
            return BoundedInput(stream)
        return stream

    def guarded_path_open(path, *args, **kwargs):
        return wrap(path, path_open(path, *args, **kwargs),
                    kwargs.get('mode', args[0] if args else 'r'))

    def guarded_builtin_open(file, *args, **kwargs):
        return wrap(file, builtin_open(file, *args, **kwargs),
                    kwargs.get('mode', args[0] if args else 'r'))

    monkeypatch.setattr(Path, 'open', guarded_path_open)
    monkeypatch.setattr(builtins, 'open', guarded_builtin_open)
    service = ClipFileOperations(library['manager'])
    if oversized:
        with pytest.raises(ValueError):
            service.prepare_rename(library['binding'].clip_id, library['destination'],
                                   admission=library['admission'])
        assert library['source'].read_bytes() == library['media_bytes']
        assert not library['manager'].pending_operations()
    else:
        operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                           admission=library['admission'])
        assert service.execute(operation.op_id).succeeded
    assert reads and max(reads) <= limit + 1


def test_manifest_patch_uses_the_exact_captured_and_validated_snapshot(rename_library, monkeypatch):
    import core.clip_file_operations as module
    from uuid import uuid4

    library = rename_library
    original = read_manifest_for_media(library['source'])
    other_snapshot = copy.deepcopy(original)
    other_snapshot['transaction_id'] = str(uuid4())
    other_snapshot['sources'][0]['display_name'] = 'Other snapshot'
    # A second path lookup may observe another valid document. The operation
    # must validate/patch its captured bytes rather than obtaining a new snapshot.
    monkeypatch.setattr(module, 'read_manifest_for_media',
                        lambda _: copy.deepcopy(other_snapshot), raising=False)
    service = ClipFileOperations(library['manager'])
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    assert service.execute(operation.op_id).succeeded

    actual = read_manifest_for_media(library['destination'])
    assert actual == {**original, 'media_file': library['destination'].name}


def test_unknown_operation_kind_never_executes_or_recovers_as_a_rename(rename_library):
    library = rename_library
    manager = library['manager']
    files_before = manager.files_for_clip(library['binding'].clip_id)
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(library['binding'].clip_id, library['destination'],
                                       admission=library['admission'])
    with sqlite3.connect(library['directory'] / 'metadata.sqlite3') as connection:
        connection.execute('UPDATE file_operations SET kind=? WHERE op_id=?',
                           ('unknown_future_kind', operation.op_id))

    stored_operation = manager.get_operation(operation.op_id)
    with pytest.raises(OSError):
        service.execute(operation.op_id)
    assert manager.get_operation(operation.op_id) == stored_operation
    for _ in range(2):
        reopened, recovered = _fresh_service(library)
        with pytest.raises(OSError):
            recovered.recover_pending()
        assert reopened.get_operation(operation.op_id) == stored_operation
        assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
        assert reopened.files_for_clip(library['binding'].clip_id) == files_before
        assert library['source'].read_bytes() == library['media_bytes']
        assert library['sidecar'].read_bytes() == library['manifest_bytes']
        assert not library['destination'].exists()
    stage = Path(operation.plan['stage'])
    stage_bytes = stage.read_bytes()
    for terminal_state in ('COMMITTED', 'ROLLED_BACK'):
        # Future journal formats may have terminal records awaiting cleanup;
        # an unknown kind still grants no permission to touch even private files.
        with sqlite3.connect(library['directory'] / 'metadata.sqlite3') as connection:
            connection.execute('UPDATE file_operations SET state=?,cleanup_done=0 WHERE op_id=?',
                               (terminal_state, operation.op_id))
        reopened, recovered = _fresh_service(library)
        stored_operation = reopened.get_operation(operation.op_id)
        with pytest.raises(OSError):
            recovered.execute(operation.op_id)
        for _ in range(2):
            with pytest.raises(OSError):
                recovered.recover_pending()
        assert reopened.get_operation(operation.op_id) == stored_operation
        assert stage.read_bytes() == stage_bytes
        assert library['source'].read_bytes() == library['media_bytes']
        assert library['sidecar'].read_bytes() == library['manifest_bytes']
        assert reopened.get_by_id(library['binding'].clip_id) == library['metadata']
        assert reopened.files_for_clip(library['binding'].clip_id) == files_before
