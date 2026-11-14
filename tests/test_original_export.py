"""Byte-exact original bundles and late evidence checks on isolated files."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core import original_export
from core.audio_manifest import (
    AudioSourceManifestEntry, build_manifest,
    read_manifest_for_media, write_manifest_atomic,
)
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import FileRole
from core.library_index import LibraryRecord, canonical_media_path


def _record(path):
    info = path.stat()
    return LibraryRecord(str(path), canonical_media_path(path), (), False,
                         'image' if path.suffix == '.png' else 'video',
                         info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)


def _replace_retaining_original(path):
    info = path.stat()
    retained = path.with_name('retained-' + path.name)
    path.rename(retained)
    path.write_bytes(b'x' * info.st_size)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert path.stat().st_ino != info.st_ino
    return retained


@pytest.fixture
def originals(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('Export tests must not read a real profile'))
    paths = []
    for directory, payload in [('first', b'first original bytes'), ('second', b'second original bytes')]:
        folder = tmp_path / directory
        folder.mkdir()
        media = folder / 'same.mp4'
        media.write_bytes(payload)
        paths.append(media)
    output = tmp_path / 'exports'
    output.mkdir()
    source = AudioSourceManifestEntry(str(uuid4()), 1, 'system', 'System Mix',
                                      'system-default', None, 0, 10_000_000, 48_000, 2, 'float32')
    sidecar = write_manifest_atomic(build_manifest(
        media_path=paths[0], transaction_id=str(uuid4()), sources=[source]), paths[0])
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    bindings = [manager.register_media(manager.inspect_media(path)) for path in paths]
    manager.associate_file(bindings[0].clip_id, manager.inspect_media(sidecar), role=FileRole.SIDECAR)
    manager.set(str(paths[0]), tag='Untouched', description='Original metadata')
    manager.set_editor_draft(str(paths[0]), {'trim_start': 2, 'trim_end': 4})
    manager.set_library_flags(paths[0], favorite=True)
    return SimpleNamespace(paths=tuple(paths), records=tuple(map(_record, paths)),
                           output=output, manager=manager, bindings=bindings, sidecar=sidecar)


def _export(library, **kwargs):
    return original_export.export_originals(
        library.records, library.output,
        member_snapshot=library.manager.export_members_snapshot, **kwargs)


def test_same_named_originals_and_manifest_are_byte_exact_without_changing_sources_or_metadata(originals):
    library = originals
    sources = {path: path.read_bytes() for path in (*library.paths, library.sidecar)}
    metadata = [library.manager.get_by_id(binding.clip_id) for binding in library.bindings]
    result = _export(library)
    assert result.successes == tuple(map(str, library.paths))
    assert result.failures == () and not result.cancelled
    final = Path(result.output)
    assert final.is_dir() and final.parent == library.output
    copies = list(final.rglob('same.mp4'))
    assert len(copies) == 2 and copies[0].parent != copies[1].parent
    assert {path.read_bytes() for path in copies} == {sources[path] for path in library.paths}
    manifests = list(final.rglob(library.sidecar.name))
    assert len(manifests) == 1 and manifests[0].read_bytes() == sources[library.sidecar]
    assert read_manifest_for_media(manifests[0].parent / library.paths[0].name) is not None
    assert all(path.read_bytes() == payload for path, payload in sources.items())
    reopened = ClipMetadataManager(database_path=library.manager._database)
    assert [reopened.get_by_id(binding.clip_id) for binding in library.bindings] == metadata


@pytest.mark.parametrize('when', [
    'before', 'while-copying', 'before-commit', 'during-validation', 'after-commit',
])
def test_cancel_respects_the_atomic_publication_boundary(originals, monkeypatch, when):
    library = originals
    cancel = threading.Event()
    kwargs = {'cancel': cancel}
    if when == 'before':
        cancel.set()
    elif when == 'while-copying':
        real_open = open

        class CancelReader:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def read(self, size):
                block = self.handle.read(min(size, 4))
                cancel.set()
                return block

        def tracked_open(path, mode, *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return CancelReader(handle) if mode == 'rb' and Path(path) == library.paths[0] else handle

        monkeypatch.setattr(original_export, 'open', tracked_open, raising=False)
    elif when == 'before-commit':
        kwargs['progress'] = lambda percent: cancel.set() if percent >= 95 else None
    elif when == 'during-validation':
        validate = original_export._validate_stage

        def cancel_after_validation(*args, **kwargs):
            validate(*args, **kwargs)
            cancel.set()

        monkeypatch.setattr(original_export, '_validate_stage', cancel_after_validation)
    else:
        publish = original_export.rename_no_replace

        def cancel_after_commit(stage, final):
            publish(stage, final)
            cancel.set()

        monkeypatch.setattr(original_export, 'rename_no_replace', cancel_after_commit)
    result = _export(library, **kwargs)
    assert cancel.is_set()
    if when == 'after-commit':
        assert result.successes == tuple(map(str, library.paths))
        assert not result.cancelled and Path(result.output).is_dir()
    else:
        assert result.cancelled and result.output is None and not result.successes
        assert not list(library.output.glob('FTHR-originals-*'))
    assert library.paths[0].read_bytes() == b'first original bytes'
    assert library.paths[1].read_bytes() == b'second original bytes'


@pytest.mark.parametrize('when', ['prepared', 'while-copying', 'late-validation'])
def test_one_replaced_original_is_named_as_failed_while_other_original_is_published(
        originals, monkeypatch, when):
    library = originals
    prepared, failures = original_export.prepare_originals(
        library.records, member_snapshot=library.manager.export_members_snapshot)
    assert not failures
    kwargs = {'prepared': prepared}
    if when == 'prepared':
        _replace_retaining_original(library.paths[0])
    elif when == 'while-copying':
        copy = original_export._copy_primary

        def replace_after_copy(item, destination, cancel, owned):
            digest = copy(item, destination, cancel, owned)
            if item.record.path == str(library.paths[0]):
                _replace_retaining_original(library.paths[0])
            return digest

        monkeypatch.setattr(original_export, '_copy_primary', replace_after_copy)
    else:
        kwargs['progress'] = lambda percent: (
            _replace_retaining_original(library.paths[0]) if percent >= 95 else None)
    result = _export(library, **kwargs)
    assert result.successes == (str(library.paths[1]),)
    assert len(result.failures) == 1 and result.failures[0][0] == str(library.paths[0])
    assert result.failures[0][1]
    assert [path.read_bytes() for path in Path(result.output).rglob('same.mp4')] == [b'second original bytes']


@pytest.mark.parametrize('late', [False, True])
def test_existing_foreign_final_directory_is_never_overwritten(originals, monkeypatch, late):
    token = 'deterministic-collision'
    monkeypatch.setattr(original_export, 'uuid4', lambda: SimpleNamespace(hex=token))
    foreign = originals.output / f'FTHR-originals-{token}'

    def create_foreign():
        foreign.mkdir()
        (foreign / 'owner.txt').write_bytes(b'foreign output')

    if late:
        publish = original_export.rename_no_replace

        def collide(stage, final):
            create_foreign()
            return publish(stage, final)

        monkeypatch.setattr(original_export, 'rename_no_replace', collide)
    else:
        create_foreign()
    with pytest.raises(FileExistsError):
        _export(originals)
    assert (foreign / 'owner.txt').read_bytes() == b'foreign output'
    assert sorted(path.name for path in foreign.iterdir()) == ['owner.txt']
    assert all(path.exists() for path in originals.paths)


def test_fresh_sql_member_snapshot_rejects_derived_file_added_by_another_manager(originals):
    library = originals
    prepared, failures = original_export.prepare_originals(
        library.records, member_snapshot=library.manager.export_members_snapshot)
    assert not failures
    other = ClipMetadataManager(database_path=library.manager._database)
    derived = library.paths[0].parent / 'unknown-derived.wav'
    derived.write_bytes(b'additional registered bytes')
    other.associate_file(library.bindings[0].clip_id, other.inspect_media(derived), role=FileRole.DERIVED)
    fresh = library.manager.export_members_snapshot(library.paths[0])
    assert any(member.role == FileRole.DERIVED for member in fresh)
    result = _export(library, prepared=prepared)
    assert result.successes == (str(library.paths[1]),)
    assert [path for path, error in result.failures] == [str(library.paths[0])]
    assert not list(Path(result.output).rglob(derived.name))


def test_provenance_only_sql_annotation_does_not_invalidate_the_original_snapshot(originals):
    library = originals
    prepared, failures = original_export.prepare_originals(
        library.records, member_snapshot=library.manager.export_members_snapshot)
    assert not failures
    with closing(sqlite3.connect(library.manager._database)) as connection, connection:
        connection.execute('UPDATE clip_files SET provenance=?', ('fresh audit annotation',))
    result = _export(library, prepared=prepared)
    assert result.successes == tuple(map(str, library.paths)) and result.failures == ()


@pytest.mark.parametrize('problem', ['changed', 'foreign', 'oversized', 'missing-evidence'])
def test_untrusted_manifest_or_missing_selected_evidence_is_a_named_item_failure(
        originals, problem):
    library = originals
    if problem == 'missing-evidence':
        from dataclasses import replace
        library.records = (replace(library.records[0], st_ino=None), library.records[1])
    elif problem == 'oversized':
        library.sidecar.write_bytes(b'x' * (1024 * 1024 + 1))
    elif problem == 'foreign':
        manifest = json.loads(library.sidecar.read_bytes())
        manifest['media_sha256'] = hashlib.sha256(library.paths[1].read_bytes()).hexdigest()
        library.sidecar.write_text(json.dumps(manifest), encoding='utf-8')
    else:
        library.sidecar.write_bytes(b'invalid replaced manifest')
    result = _export(library)
    assert result.successes == (str(library.paths[1]),)
    assert [path for path, error in result.failures] == [str(library.paths[0])]
    assert not result.cancelled


def test_export_member_snapshot_fails_closed_for_missing_or_future_database(originals):
    database = originals.manager._database
    database.rename(database.with_name('retained-metadata.sqlite3'))
    with pytest.raises(OSError):
        originals.manager.export_members_snapshot(originals.paths[0])
    assert not database.exists()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('PRAGMA user_version=99')
    with pytest.raises(OSError):
        originals.manager.export_members_snapshot(originals.paths[0])


def test_unknown_output_inode_is_rejected_before_creating_or_cleaning_stage(originals, monkeypatch):
    real_lstat = os.lstat

    def unknown_inode(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) == originals.output:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=0,
                                   st_file_attributes=getattr(info, 'st_file_attributes', 0))
        return info

    with monkeypatch.context() as guard:
        guard.setattr(os, 'lstat', unknown_inode)
        with pytest.raises(OSError):
            _export(originals)
    assert list(originals.output.iterdir()) == []
