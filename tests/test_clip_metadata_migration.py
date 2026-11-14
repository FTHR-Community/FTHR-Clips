"""Migration and verified media association using isolated library copies."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import BindingState, FileRole, UnsupportedSchemaVersion


def _manager(directory):
    return ClipMetadataManager(database_path=directory / 'metadata.sqlite3',
                               legacy_path=directory / 'metadata.json')


def _seed_sqlite(database, records, *, wal=False):
    connection = sqlite3.connect(database)
    if wal:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('CREATE TABLE entries (path TEXT PRIMARY KEY, value TEXT NOT NULL)')
    connection.executemany('INSERT INTO entries VALUES (?,?)',
                           [(path, json.dumps(value)) for path, value in records.items()])
    connection.commit()
    if wal:
        return connection
    connection.close()
    return None


def _v0_snapshot(database):
    connection = sqlite3.connect(database)
    try:
        return (
            connection.execute('PRAGMA user_version').fetchone()[0],
            connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                               'ORDER BY name').fetchall(),
            connection.execute('SELECT path,value FROM entries ORDER BY path').fetchall(),
        )
    finally:
        connection.close()


def _migrated_rows(database):
    connection = sqlite3.connect(database)
    try:
        return {path: (clip_id, json.loads(value)) for clip_id, path, value in
                connection.execute('SELECT clip_id,path,value FROM entries')}
    finally:
        connection.close()


def _new_library(directory):
    manager = _manager(directory)
    manager.migrate(target_version=1)
    return manager


@pytest.mark.parametrize('source', ['json', 'sqlite'])
def test_explicit_migration_preserves_all_entries_and_ids_without_media_scan(
        tmp_path, monkeypatch, source):
    media_root = tmp_path / 'unavailable-media'
    records = {str(media_root / f'clip-{index}.mp4'): {
        'tag': f'Tag {index}', 'description': 'Grüße 🎬',
        'unknown': {'list': [None, True, {'future': index}]},
        'editor_draft': {'version': 1, 'updated_at': index,
                         'file': {'size': 1, 'mtime_ns': 1},
                         'state': {'unknown_edit': {'value': index}}},
    } for index in range(260)}
    database, legacy = tmp_path / 'metadata.sqlite3', tmp_path / 'metadata.json'
    if source == 'json':
        legacy.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        original_json = legacy.read_bytes()
    else:
        _seed_sqlite(database, records)
        original_sql = _v0_snapshot(database)
    real_stat = os.stat

    def no_media_scan(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and str(path).casefold().startswith(
                str(media_root).casefold()):
            pytest.fail('migration must not inspect unavailable media')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', no_media_scan)
    manager = _manager(tmp_path)
    assert manager.inspect_schema().version == 0
    assert manager.get(next(iter(records)))['tag'] == 'Tag 0'
    assert not database.exists() if source == 'json' else _v0_snapshot(database) == original_sql
    result = manager.migrate(target_version=1)
    monkeypatch.setattr(os, 'stat', real_stat)
    assert result.changed and result.to_version == 1
    assert result.migrated_entries == len(records)
    assert result.backup_path is not None
    if source == 'json':
        assert result.backup_path.read_bytes() == original_json
        assert legacy.read_bytes() == original_json
    else:
        assert _v0_snapshot(result.backup_path) == original_sql
    rows = _migrated_rows(database)
    assert {path: value for path, (_identity, value) in rows.items()} == records
    identities = {path: identity for path, (identity, _value) in rows.items()}
    assert len(set(identities.values())) == len(records)
    assert all(isinstance(identity, str) and identity for identity in identities.values())
    for path in (next(iter(records)), list(records)[-1]):
        binding = manager.resolve_media(path)
        assert binding.state == BindingState.LEGACY_UNVERIFIED
        assert binding.clip_id == identities[path]
        assert manager.get_by_id(binding.clip_id) == records[path]
    reopened = _manager(tmp_path)
    assert reopened.migrate(target_version=1).changed is False
    assert _migrated_rows(database) == rows
    copied = reopened.get_by_id(identities[next(iter(records))])
    copied['unknown']['list'].append('not persisted')
    assert reopened.get_by_id(identities[next(iter(records))]) == records[next(iter(records))]


def test_migration_backup_contains_committed_wal_rows(tmp_path):
    records = {str(tmp_path / 'wal.mp4'): {'tag': 'Committed in WAL'}}
    database = tmp_path / 'metadata.sqlite3'
    writer = _seed_sqlite(database, records, wal=True)
    try:
        assert Path(str(database) + '-wal').stat().st_size > 0
        result = _manager(tmp_path).migrate(target_version=1)
        assert result.backup_path is not None
        backup = sqlite3.connect(result.backup_path)
        try:
            assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            values = {path: json.loads(value) for path, value in
                      backup.execute('SELECT path,value FROM entries')}
            assert values == records
        finally:
            backup.close()
    finally:
        writer.close()


@pytest.mark.parametrize('provided', [
    'both', 'database-only', 'legacy-only', 'blank-database', 'blank-legacy',
    'same-canonical-path',
])
def test_injected_paths_never_read_home_and_empty_library_requires_explicit_migration(
        tmp_path, monkeypatch, provided):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail(
        'injected storage must never consult the real profile'))
    database = tmp_path / 'clip_metadata.sqlite3'
    legacy = tmp_path / 'clip_metadata.json'
    options = {'database_path': database, 'legacy_path': legacy}
    if provided == 'database-only':
        options.pop('legacy_path')
    elif provided == 'legacy-only':
        options.pop('database_path')
    elif provided == 'blank-database':
        options = {'database_path': ''}
    elif provided == 'blank-legacy':
        options = {'legacy_path': ''}
    elif provided == 'same-canonical-path':
        options['legacy_path'] = tmp_path / 'sub' / '..' / database.name
    if provided in {'blank-database', 'blank-legacy', 'same-canonical-path'}:
        with pytest.raises(ValueError):
            ClipMetadataManager(**options)
        assert not database.exists() and not legacy.exists()
        return
    manager = ClipMetadataManager(**options)
    assert manager.inspect_schema().kind == 'empty'
    assert manager.get(str(tmp_path / 'absent.mp4')) == {'tag': '', 'description': ''}
    assert not database.exists()
    with pytest.raises(RuntimeError):
        manager.resolve_media(str(tmp_path / 'absent.mp4'))
    manager.migrate(target_version=1)
    assert manager.inspect_schema().version == 1
    assert manager.inspect_schema().writable
    assert _migrated_rows(database) == {}


@pytest.mark.parametrize('source', ['json', 'sqlite'])
def test_corrupt_source_is_not_replaced_by_empty_or_older_metadata(tmp_path, source):
    database, legacy = tmp_path / 'metadata.sqlite3', tmp_path / 'metadata.json'
    legacy.write_text('{"old.mp4":{"tag":"Older backup"}}', encoding='utf-8')
    damaged = legacy if source == 'json' else database
    damaged.write_bytes(b'not valid metadata')
    original = damaged.read_bytes()
    with pytest.raises((OSError, ValueError)):
        _manager(tmp_path).migrate(target_version=1)
    assert damaged.read_bytes() == original


def test_future_schema_is_read_only_and_cannot_be_downgraded(tmp_path):
    database = tmp_path / 'metadata.sqlite3'
    media = tmp_path / 'future.mp4'
    media.write_bytes(b'future media')
    _seed_sqlite(database, {str(media): {
        'tag': 'Keep future data', 'editor_draft': {'version': 1, 'state': {}}}})
    connection = sqlite3.connect(database)
    connection.execute('PRAGMA user_version=99')
    connection.commit()
    connection.close()
    original = database.read_bytes()
    manager = _manager(tmp_path)
    assert manager.inspect_schema().kind == 'future'
    assert not manager.inspect_schema().writable
    evidence = manager.inspect_media(str(media))
    operations = (
        manager.migrate,
        lambda: manager.set(str(media), tag='Do not write'),
        lambda: manager.set_editor_draft(str(media), {'value': 'Do not write'}),
        lambda: manager.clear_editor_draft(str(media)),
        lambda: manager.rename_path(str(media), str(tmp_path / 'new.mp4')),
        lambda: manager.register_media(evidence),
        lambda: manager.associate_file('future-id', evidence, role=FileRole.DERIVED),
    )
    for operation in operations:
        with pytest.raises(UnsupportedSchemaVersion):
            operation()
    assert database.read_bytes() == original


@pytest.mark.parametrize('failure', ['backup', 'locked', 'commit'])
def test_migration_failure_retains_v0_and_retries_without_partial_ids(
        tmp_path, monkeypatch, failure):
    import core.clip_metadata_manager as module

    database = tmp_path / 'metadata.sqlite3'
    records = {str(tmp_path / 'clip.mp4'): {'tag': 'Keep', 'unknown': [1, 2]}}
    _seed_sqlite(database, records)
    snapshot = _v0_snapshot(database)
    manager = _manager(tmp_path)
    connect = sqlite3.connect

    class BackupFailure(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise OSError('injected backup write failure')

    class DeferredCommitFailure(sqlite3.Connection):
        def poison_commit(self):
            if not self.in_transaction or getattr(self, '_poisoned', False):
                return
            self._poisoned = True
            self.execute('CREATE TEMP TABLE qa_parent (id INTEGER PRIMARY KEY)')
            self.execute('CREATE TEMP TABLE qa_child (id INTEGER REFERENCES qa_parent(id) '
                         'DEFERRABLE INITIALLY DEFERRED)')
            self.execute('INSERT INTO qa_child VALUES (123)')

        def commit(self):
            self.poison_commit()
            return super().commit()

        def __exit__(self, *args):
            if args[0] is None:
                self.poison_commit()
            return super().__exit__(*args)

    def failing_connect(*args, **kwargs):
        kwargs['timeout'] = 0.01
        if failure == 'backup':
            kwargs['factory'] = BackupFailure
        elif failure == 'commit':
            kwargs['factory'] = DeferredCommitFailure
        connection = connect(*args, **kwargs)
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    blocker = connect(database) if failure == 'locked' else None
    if blocker is not None:
        blocker.execute('BEGIN IMMEDIATE')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.sqlite3, 'connect', failing_connect)
            with pytest.raises((OSError, sqlite3.Error)):
                manager.migrate(target_version=1)
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    assert _v0_snapshot(database) == snapshot
    assert manager.get(next(iter(records)))['tag'] == 'Keep'
    assert manager.inspect_schema().version == 0
    manager.migrate(target_version=1)
    rows = _migrated_rows(database)
    assert len(rows) == 1
    assert next(iter(rows.values()))[1] == next(iter(records.values()))
    assert _manager(tmp_path).migrate(target_version=1).changed is False


def test_canonical_legacy_collision_never_chooses_or_merges_an_entry(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    alias_dir = tmp_path / 'sub'
    alias_dir.mkdir()
    alias = alias_dir / '..' / media.name
    records = {str(media): {'tag': 'First'}, str(alias): {'tag': 'Second'}}
    _seed_sqlite(tmp_path / 'metadata.sqlite3', records)
    manager = _manager(tmp_path)
    manager.migrate(target_version=1)
    rows = _migrated_rows(tmp_path / 'metadata.sqlite3')
    assert len(rows) == 2
    binding = manager.resolve_media(str(media))
    assert binding.state == BindingState.CONFLICT
    assert binding.clip_id is None and binding.file_id is None
    with pytest.raises((ValueError, FileExistsError)):
        manager.register_media(manager.inspect_media(str(media)), adopt_legacy=True)
    assert {manager.get_by_id(identity)['tag'] for identity, _value in rows.values()} == {
        'First', 'Second'}


def test_legacy_adoption_is_explicit_and_does_not_rewrite_draft_identity(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    manager = _manager(tmp_path)
    manager.set(str(media), tag='Legacy')
    manager.set_editor_draft(str(media), {'segments': [[0, 1, False]]})
    manager.migrate(target_version=1)
    before = manager.resolve_media(str(media))
    metadata = manager.get_by_id(before.clip_id)
    evidence = manager.inspect_media(str(media))
    unadopted = manager.register_media(evidence)
    assert unadopted.clip_id == before.clip_id
    assert unadopted.state == BindingState.LEGACY_UNVERIFIED
    assert unadopted.file_id is None
    adopted = manager.register_media(evidence, adopt_legacy=True)
    assert adopted.clip_id == before.clip_id and adopted.file_id
    assert adopted.state == BindingState.STAT_BOUND
    assert manager.get_by_id(adopted.clip_id) == metadata
    assert len(manager.files_for_clip(adopted.clip_id)) == 1


def test_verified_path_replacement_detaches_old_metadata_instead_of_inheriting_it(tmp_path):
    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'first')
    old = manager.register_media(manager.inspect_media(str(media), hash_content=True))
    manager.set(str(media), tag='Original', description='Keep old details')
    manager.set_editor_draft(str(media), {'unknown': {'keep': True}})
    metadata = manager.get_by_id(old.clip_id)
    stamp = media.stat()
    media.rename(tmp_path / 'original-retained.mp4')
    replacement = tmp_path / 'replacement.mp4'
    replacement.write_bytes(b'other')
    replacement.rename(media)
    os.utime(media, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert manager.get(str(media)) == {'tag': '', 'description': ''}
    current = manager.register_media(manager.inspect_media(str(media), hash_content=True))
    assert current.clip_id != old.clip_id
    assert manager.get_by_id(current.clip_id) == {}
    assert manager.get_by_id(old.clip_id) == metadata
    assert manager.files_for_clip(old.clip_id)[0].binding_state == BindingState.DETACHED
    rows = _migrated_rows(tmp_path / 'metadata.sqlite3')
    assert rows[None][0] == old.clip_id


def test_content_verification_distinguishes_in_place_change_hidden_by_stat(tmp_path):
    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'first')
    evidence = manager.inspect_media(str(media), hash_content=True)
    old = manager.register_media(evidence)
    original = media.stat()
    media.write_bytes(b'other')
    os.utime(media, ns=(original.st_atime_ns, original.st_mtime_ns))
    stat_only = manager.inspect_media(str(media))
    assert dict(stat_only.stat) == dict(evidence.stat)
    assert stat_only.content_sha256 is None
    assert manager.resolve_media(str(media)).clip_id == old.clip_id
    changed = manager.inspect_media(str(media), hash_content=True)
    assert changed.content_sha256 != evidence.content_sha256
    assert manager.register_media(changed).clip_id != old.clip_id


def test_missing_and_renamed_media_keep_ids_and_drafts_and_reject_collision(tmp_path):
    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    bound = manager.register_media(manager.inspect_media(str(media)))
    manager.set_editor_draft(str(media), {'value': 'draft'})
    retained = tmp_path / 'retained.mp4'
    media.rename(retained)
    assert manager.resolve_media(str(media)).state == BindingState.MISSING
    assert manager.get_by_id(bound.clip_id)['editor_draft']['state'] == {'value': 'draft'}
    target = tmp_path / 'renamed.mp4'
    retained.rename(target)
    manager.rename_path(str(media), str(target))
    renamed = manager.resolve_media(str(target))
    assert (renamed.clip_id, renamed.file_id) == (bound.clip_id, bound.file_id)
    assert manager.get_editor_draft(str(target)) == {'value': 'draft'}
    if os.path.normcase('A') == os.path.normcase('a'):
        manager.rename_path(str(target), str(target).upper())
        assert manager.resolve_media(str(target)).clip_id == bound.clip_id
    manager.clear_editor_draft(str(target))
    assert manager.resolve_media(str(target)).clip_id == bound.clip_id
    other = tmp_path / 'other.mp4'
    other.write_bytes(b'other')
    other_binding = manager.register_media(manager.inspect_media(str(other)))
    with pytest.raises(FileExistsError):
        manager.rename_path(str(target), str(other))
    assert manager.resolve_media(str(other)).clip_id == other_binding.clip_id
    assert manager.resolve_media(str(target)).clip_id == bound.clip_id


@pytest.mark.parametrize('unsafe', ['', b'clip.mp4', 'bad\0name.mp4', 'https://example.test/a.mp4'])
def test_media_inspection_rejects_unsafe_path_forms(tmp_path, unsafe):
    manager = _new_library(tmp_path)
    with pytest.raises((TypeError, ValueError, OSError)):
        manager.inspect_media(unsafe)
    with pytest.raises((ValueError, OSError)):
        manager.inspect_media(str(tmp_path))


def _audio_sidecar(media):
    from core.audio_manifest import (
        AudioSourceManifestEntry, build_manifest, write_manifest_atomic,
    )
    source = AudioSourceManifestEntry(
        source_uuid=str(uuid4()), stream_index=1, source_type='system',
        display_name='System', persistent_identity='default-system', icon_reference=None,
        first_active_100ns=0, last_active_100ns=10, sample_rate=48000, channels=2,
        sample_format='fltp')
    return write_manifest_atomic(build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[source]), media)


def test_sidecar_association_is_explicit_verified_and_idempotent(tmp_path, monkeypatch):
    from core.library_index import canonical_media_path

    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    sidecar = _audio_sidecar(media)
    original = sidecar.read_bytes()
    primary = manager.register_media(manager.inspect_media(str(media)))
    monkeypatch.chdir(tmp_path)
    alias = manager.register_media(manager.inspect_media('./clip.mp4'))
    assert (alias.clip_id, alias.file_id) == (primary.clip_id, primary.file_id)
    assert manager.files_for_clip(primary.clip_id)[0].path_key == canonical_media_path(media)
    assert [file.role for file in manager.files_for_clip(primary.clip_id)] == [FileRole.PRIMARY]
    evidence = manager.inspect_media(str(sidecar), hash_content=True)
    linked = manager.associate_file(primary.clip_id, evidence)
    again = manager.associate_file(primary.clip_id, evidence)
    assert linked.file_id == again.file_id
    assert linked.role == FileRole.SIDECAR
    assert linked.path_key == canonical_media_path(sidecar)
    assert linked.clip_id == primary.clip_id
    assert sidecar.read_bytes() == original
    assert len(manager.files_for_clip(primary.clip_id)) == 2
    with pytest.raises(ValueError):
        manager.associate_file(primary.clip_id, evidence, role=FileRole.PRIMARY)


@pytest.mark.parametrize('invalid', [
    'foreign', 'stale', 'corrupt', 'missing', 'unbound', 'primary-race', 'sidecar-race',
])
def test_invalid_sidecar_cannot_be_associated_or_change_metadata(tmp_path, monkeypatch, invalid):
    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    sidecar = _audio_sidecar(media)
    if invalid == 'unbound':
        manager.set(str(media), tag='Unverified path metadata')
        primary = manager.resolve_media(str(media))
    else:
        primary = manager.register_media(manager.inspect_media(str(media)))
    if invalid == 'foreign':
        foreign = tmp_path / 'foreign.mp4'
        foreign.write_bytes(b'foreign media')
        sidecar = _audio_sidecar(foreign)
    elif invalid == 'stale':
        document = json.loads(sidecar.read_text(encoding='utf-8'))
        document['media_sha256'] = '0' * 64
        sidecar.write_text(json.dumps(document), encoding='utf-8')
    elif invalid == 'corrupt':
        sidecar.write_text('not JSON', encoding='utf-8')
    evidence = manager.inspect_media(str(sidecar), hash_content=True)
    if invalid == 'missing':
        sidecar.rename(tmp_path / 'sidecar-retained.json')
    elif invalid in {'primary-race', 'sidecar-race'}:
        from core import audio_manifest

        read_manifest = audio_manifest.read_manifest_for_media

        def replace_after_validation(path):
            verified = read_manifest(path)
            victim = media if invalid == 'primary-race' else sidecar
            original = victim.read_bytes()
            victim.rename(victim.with_name('race-original-' + victim.name))
            victim.write_bytes(original)
            return verified

        monkeypatch.setattr(audio_manifest, 'read_manifest_for_media', replace_after_validation)
    before = manager.files_for_clip(primary.clip_id)
    metadata = copy.deepcopy(manager.get_by_id(primary.clip_id))
    database_before = (tmp_path / 'metadata.sqlite3').read_bytes()
    with pytest.raises(ValueError):
        manager.associate_file(primary.clip_id, evidence)
    assert manager.files_for_clip(primary.clip_id) == before
    assert manager.get_by_id(primary.clip_id) == metadata
    assert (tmp_path / 'metadata.sqlite3').read_bytes() == database_before


def test_explicit_derived_file_reassociation_preserves_history_without_ownership(tmp_path):
    manager = _new_library(tmp_path)
    media = tmp_path / 'external-original.mp4'
    media.write_bytes(b'external source')
    primary = manager.register_media(manager.inspect_media(str(media)))
    derived = tmp_path / 'derived.mp4'
    derived.write_bytes(b'first derived')
    first = manager.associate_file(primary.clip_id, manager.inspect_media(str(derived)),
                                   role=FileRole.DERIVED)
    assert manager.associate_file(primary.clip_id, manager.inspect_media(str(derived)),
                                  role=FileRole.DERIVED).file_id == first.file_id
    derived.rename(tmp_path / 'derived-retained.mp4')
    derived.write_bytes(b'new derived')
    current = manager.associate_file(primary.clip_id, manager.inspect_media(str(derived)),
                                     role=FileRole.DERIVED)
    assert current.file_id != first.file_id
    files = manager.files_for_clip(primary.clip_id)
    assert next(file for file in files if file.file_id == first.file_id).binding_state == (
        BindingState.DETACHED)
    assert media.read_bytes() == b'external source'


def _identity_rows(database):
    connection = sqlite3.connect(database)
    try:
        return (
            connection.execute('SELECT * FROM entries ORDER BY clip_id').fetchall(),
            connection.execute('SELECT * FROM clip_files ORDER BY file_id').fetchall(),
        )
    finally:
        connection.close()


@pytest.mark.parametrize('replacement_autosave', [False, True])
def test_stale_manager_keeps_drafts_and_never_reassigns_old_details(
        tmp_path, monkeypatch, replacement_autosave):
    manager_a = _new_library(tmp_path)
    clock = {'now': 100.0}
    monkeypatch.setattr('core.clip_metadata_manager.time.time', lambda: clock['now'])
    media = tmp_path / 'original.mp4'
    media.write_bytes(b'old media')
    original = manager_a.register_media(manager_a.inspect_media(str(media)))
    manager_a.set(str(media), tag='Original tag', description='Original description')
    manager_a.set_editor_draft(str(media), {'value': 'original draft'})
    manager_b = _manager(tmp_path) if replacement_autosave else None
    media.rename(tmp_path / 'original-retained.mp4')
    media.write_bytes(b'replacement media')
    clock['now'] = 200.0
    if manager_b is not None:
        assert manager_b.set_editor_draft(str(media), {'value': 'replacement draft'})
        replacement = manager_b.resolve_media(str(media))
        assert replacement.clip_id != original.clip_id
        replacement_metadata = manager_b.get_by_id(replacement.clip_id)
        assert manager_b.get(str(media)) == {'tag': '', 'description': ''}
        assert replacement_metadata['editor_draft']['state'] == {
            'value': 'replacement draft'}
    else:
        assert manager_a.resolve_media(str(media)).state == BindingState.DETACHED
        assert manager_a.get(str(media)) == {'tag': '', 'description': ''}
    other = tmp_path / 'newest.mp4'
    other.write_bytes(b'newest media')
    newest = manager_a.register_media(manager_a.inspect_media(str(other)))
    clock['now'] = 300.0

    assert manager_a.set_editor_draft(str(other), {'value': 'newest draft'})

    reopened = _manager(tmp_path)
    assert reopened.get(str(media)) == {'tag': '', 'description': ''}
    if replacement_autosave:
        assert reopened.resolve_media(str(media)).clip_id == replacement.clip_id
        assert reopened.resolve_media(str(media)).file_id == replacement.file_id
        actual = reopened.get_by_id(replacement.clip_id)
        assert {k: v for k, v in actual.items() if k != 'editor_draft'} == {
            k: v for k, v in replacement_metadata.items() if k != 'editor_draft'}
        assert actual['editor_draft']['state'] == {'value': 'replacement draft'}
        assert reopened.files_for_clip(original.clip_id)[0].binding_state == BindingState.DETACHED
    else:
        observed = reopened.resolve_media(str(media))
        assert observed.state == BindingState.DETACHED
        assert (observed.clip_id, observed.file_id) == (original.clip_id, original.file_id)
        assert reopened.files_for_clip(original.clip_id)[0].binding_state == BindingState.STAT_BOUND
    assert reopened.get_by_id(original.clip_id)['tag'] == 'Original tag'
    assert reopened.get_by_id(original.clip_id)['description'] == 'Original description'
    assert reopened.get_by_id(original.clip_id)['editor_draft']['state'] == {
        'value': 'original draft'}
    assert reopened.get_editor_draft(str(other)) == {'value': 'newest draft'}
    assert reopened.get_by_id(newest.clip_id)['editor_draft']['state'] == {
        'value': 'newest draft'}
    assert len(_migrated_rows(tmp_path / 'metadata.sqlite3')) == (3 if replacement_autosave else 2)
    assert reopened.resolve_media(str(other)).clip_id == newest.clip_id


@pytest.mark.parametrize('alias_kind', ['relative', 'dot-dot'])
@pytest.mark.parametrize('insertion_order', ['AB', 'BA'])
@pytest.mark.parametrize('clock_order', ['forward', 'equal', 'backward'])
def test_alias_draft_counts_clip_once_and_keeps_successful_save(
        tmp_path, monkeypatch, alias_kind, insertion_order, clock_order):
    manager = _new_library(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'sub').mkdir()
    clock = {'now': 0.0}
    monkeypatch.setattr('core.clip_metadata_manager.time.time', lambda: clock['now'])
    media = {}
    bindings = {}
    for label in insertion_order:
        path = tmp_path / f'{label}.mp4'
        path.write_bytes(label.encode())
        media[label] = path
        bindings[label] = manager.register_media(manager.inspect_media(str(path)))
        clock['now'] = {'A': 100.0, 'B': 200.0}[label]
        manager.set_editor_draft(str(path), {'value': label})
    alias = './B.mp4' if alias_kind == 'relative' else str(tmp_path / 'sub' / '..' / 'B.mp4')
    clock['now'] = {'forward': 300.0, 'equal': 200.0, 'backward': 50.0}[clock_order]

    assert manager.set_editor_draft(alias, {'value': 'B edited'}) is True

    assert manager.get_editor_draft(str(media['A'])) == {'value': 'A'}
    assert manager.get_editor_draft(alias) == {'value': 'B edited'}
    reopened = _manager(tmp_path)
    assert reopened.get_editor_draft(str(media['A'])) == {'value': 'A'}
    assert reopened.get_editor_draft(str(media['B'])) == {'value': 'B edited'}
    for label in ('A', 'B'):
        current = reopened.resolve_media(str(media[label]))
        assert (current.clip_id, current.file_id) == (
            bindings[label].clip_id, bindings[label].file_id)
    assert len(_migrated_rows(tmp_path / 'metadata.sqlite3')) == 2


@pytest.mark.parametrize('role', [FileRole.DERIVED, FileRole.SIDECAR])
@pytest.mark.parametrize('operation', ['register', 'set'])
@pytest.mark.parametrize('actor_cache', ['fresh', 'stale'])
def test_associated_file_cannot_be_registered_as_another_primary(
        tmp_path, role, operation, actor_cache):
    owner = _new_library(tmp_path)
    media = tmp_path / 'owner.mp4'
    media.write_bytes(b'owner media')
    primary = owner.register_media(owner.inspect_media(str(media)))
    owner.set(str(media), tag='Owner metadata')
    actor = _manager(tmp_path) if actor_cache == 'stale' else None
    if role == FileRole.SIDECAR:
        associated = _audio_sidecar(media)
    else:
        associated = tmp_path / 'derived.mp4'
        associated.write_bytes(b'derived media')
    linked = owner.associate_file(primary.clip_id, owner.inspect_media(str(associated)), role=role)
    if actor is None:
        actor = _manager(tmp_path)
    database = tmp_path / 'metadata.sqlite3'
    before = _identity_rows(database)
    original_bytes = associated.read_bytes()

    with pytest.raises(FileExistsError):
        if operation == 'register':
            actor.register_media(actor.inspect_media(str(associated)))
        else:
            actor.set(str(associated), tag='Must not create another primary')

    assert _identity_rows(database) == before
    reopened = _manager(tmp_path)
    assert reopened.resolve_media(str(associated)) is None
    assert reopened.get_by_id(primary.clip_id)['tag'] == 'Owner metadata'
    files = reopened.files_for_clip(primary.clip_id)
    assert {file.file_id for file in files} == {primary.file_id, linked.file_id}
    assert next(file for file in files if file.file_id == linked.file_id).role == role
    assert associated.read_bytes() == original_bytes


@pytest.mark.parametrize('cached_association', [False, True])
def test_associate_checks_current_database_ownership_before_create_or_idempotent_return(
        tmp_path, cached_association):
    writer = _new_library(tmp_path)
    owner_path = tmp_path / 'owner.mp4'
    owner_path.write_bytes(b'owner media')
    owner = writer.register_media(writer.inspect_media(str(owner_path)))
    child = tmp_path / 'derived.mp4'
    child.write_bytes(b'derived media')
    evidence = writer.inspect_media(str(child))
    if cached_association:
        original = writer.associate_file(owner.clip_id, evidence, role=FileRole.DERIVED)
        foreign_path = tmp_path / 'foreign.mp4'
        foreign_path.write_bytes(b'foreign media')
        foreign = writer.register_media(writer.inspect_media(str(foreign_path)))
        stale = _manager(tmp_path)
        # Simulate an independent committed writer changing association ownership.
        # The old idempotent RAM shortcut must not grant the previous owner access.
        with sqlite3.connect(tmp_path / 'metadata.sqlite3') as connection:
            connection.execute('UPDATE clip_files SET clip_id=? WHERE file_id=?',
                               (foreign.clip_id, original.file_id))
    else:
        stale = _manager(tmp_path)
        foreign = writer.register_media(evidence)
    before = _identity_rows(tmp_path / 'metadata.sqlite3')

    with pytest.raises(FileExistsError):
        stale.associate_file(owner.clip_id, evidence, role=FileRole.DERIVED)

    assert _identity_rows(tmp_path / 'metadata.sqlite3') == before
    reopened = _manager(tmp_path)
    assert all(file.path != str(child) for file in reopened.files_for_clip(owner.clip_id))
    assert any(file.path == str(child) for file in reopened.files_for_clip(foreign.clip_id))
    assert child.read_bytes() == b'derived media'


def test_cached_reads_do_not_open_database_but_file_replacement_and_future_writes_stay_guarded(
        tmp_path, monkeypatch):
    import core.clip_metadata_manager as module

    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original media')
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(str(media), tag='Original tag', description='Original description')
    manager.set_editor_draft(str(media), {'unknown_edit': [1, None]})
    metadata = copy.deepcopy(manager.get_by_id(binding.clip_id))
    files = manager.files_for_clip(binding.clip_id)
    (tmp_path / 'sub').mkdir()
    alias = str(tmp_path / 'sub' / '..' / media.name)

    def no_database_reads(*args, **kwargs):
        pytest.fail('cached UI reads must not open a SQLite connection')

    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, 'connect', no_database_reads)
        for _ in range(5):
            assert manager.get(alias) == {'tag': 'Original tag', 'description': 'Original description'}
            assert manager.resolve_media(alias).clip_id == binding.clip_id
            assert manager.get_by_id(binding.clip_id) == metadata
            assert manager.files_for_clip(binding.clip_id) == files
            assert manager.get(str(tmp_path / 'unknown.mp4')) == {'tag': '', 'description': ''}
        media.rename(tmp_path / 'retained-original.mp4')
        media.write_bytes(b'replacement media')
        assert manager.get(alias) == {'tag': '', 'description': ''}
        assert manager.resolve_media(alias).state == BindingState.DETACHED
        assert manager.get_by_id(binding.clip_id) == metadata

    # Explicit inspection and all writes remain authoritative even after caching.
    before_future_write = _identity_rows(tmp_path / 'metadata.sqlite3')
    with sqlite3.connect(tmp_path / 'metadata.sqlite3') as connection:
        connection.execute('PRAGMA user_version=99')
    assert manager.inspect_schema().version == 99
    assert not manager.inspect_schema().writable
    with pytest.raises(UnsupportedSchemaVersion):
        manager.set(str(media), tag='Must not publish through an old cache')
    assert _identity_rows(tmp_path / 'metadata.sqlite3') == before_future_write


def test_journal_upgrade_backs_up_schema1_and_preserves_ids_metadata_and_file_history(tmp_path):
    manager = _new_library(tmp_path)
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original media')
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(str(media), tag='Keep', description='Unknown metadata survives migration')
    manager.set_editor_draft(str(media), {'future_edit': {'keep': [None, 2]}})
    derived = tmp_path / 'derived.mp4'
    derived.write_bytes(b'derived')
    manager.associate_file(binding.clip_id, manager.inspect_media(derived), role=FileRole.DERIVED)
    before = _identity_rows(tmp_path / 'metadata.sqlite3')
    metadata = copy.deepcopy(manager.get_by_id(binding.clip_id))
    files = manager.files_for_clip(binding.clip_id)

    result = manager.migrate()

    assert result.changed and (result.from_version, result.to_version) == (1, 2)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 1
    assert _identity_rows(result.backup_path) == before
    assert _identity_rows(tmp_path / 'metadata.sqlite3') == before
    reopened = _manager(tmp_path)
    assert reopened.inspect_schema().version == 2
    assert reopened.get_by_id(binding.clip_id) == metadata
    assert reopened.files_for_clip(binding.clip_id) == files
    assert not reopened.pending_operations()
    assert not reopened.migrate().changed
    with pytest.raises(ValueError):
        reopened.migrate(target_version=1)
