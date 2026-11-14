"""Favorites and destructive-action boundaries on retained synthetic libraries."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.clip_file_operations import ClipFileOperations
from core.clip_metadata_manager import ClipMetadataManager
from core.clip_metadata_types import BindingState, FileRole
from core import library_ownership


def _open(directory):
    return ClipMetadataManager(database_path=directory / 'metadata.sqlite3')


@pytest.fixture
def library(tmp_path, monkeypatch):
    # Explicit injection must remain sufficient even if real-home access fails.
    monkeypatch.setattr(Path, 'home', lambda: pytest.fail('F05 must not consult a real profile'))
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original synthetic media')
    manager = _open(tmp_path)
    manager.migrate()
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(str(media), tag='Original tag', description='Original description')
    manager.set_editor_draft(str(media), {'edits': [1, None, True]})
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        value = manager.get_by_id(binding.clip_id)
        value['unknown_future'] = {'nested': [None, {'keep': True}]}
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps(value), binding.clip_id))
    manager.reload()
    return manager, media, binding


@pytest.mark.parametrize('actions,expected,auto_owned', [
    ([{'favorite': True}], {'favorite': True, 'protected': True}, False),
    ([{'favorite': True, 'protected': False}], {'favorite': True, 'protected': True}, False),
    ([{'favorite': True}, {'protected': False}, {'favorite': True}],
     {'favorite': True, 'protected': True}, False),
    ([{'favorite': True}, {'favorite': False}], {'favorite': False, 'protected': False}, False),
    ([{'protected': True}], {'favorite': True, 'protected': True}, False),
    ([{'favorite': True}, {'protected': False}], {'favorite': False, 'protected': False}, False),
    ([{'protected': True}, {'favorite': False}], {'favorite': False, 'protected': False}, False),
    ([{'favorite': True}, {'protected': True}, {'favorite': False}],
     {'favorite': False, 'protected': False}, False),
    ([{'favorite': False, 'protected': True}], {'favorite': False, 'protected': False}, False),
])

def test_favorite_and_legacy_alias_remain_unified_after_restart(library, actions, expected, auto_owned):
    manager, media, binding = library
    before = manager.get_by_id(binding.clip_id)
    assert manager.get_library_flags(media) == {'favorite': False, 'protected': False}
    for action in actions:
        result = manager.set_library_flags(media, **action)
    assert result == expected
    result['favorite'] = 'caller mutation'
    reopened = _open(media.parent)
    assert manager.get_library_flags(media) == reopened.get_library_flags(media) == expected
    assert reopened.resolve_media(media).clip_id == binding.clip_id
    stored = dict(expected)
    if auto_owned:
        stored['favorite_auto_protected'] = True
    assert reopened.get_by_id(binding.clip_id) == {**before, 'library_flags': stored}
    assert reopened.inspect_schema().version == 2


@pytest.mark.parametrize('value', [0, 1, 'true', [], {}])
def test_flag_inputs_are_strict_and_rejected_without_writes(library, value):
    manager, media, binding = library
    before = manager.get_by_id(binding.clip_id)
    with pytest.raises(ValueError):
        manager.set_library_flags(media, favorite=value)
    assert _open(media.parent).get_by_id(binding.clip_id) == before


def test_unfavorite_releases_only_its_own_automatic_protection_after_restart(library):
    manager, media, binding = library
    before = manager.get_by_id(binding.clip_id)
    manager.set_library_flags(media, favorite=True)
    reopened = _open(media.parent)
    assert reopened.set_library_flags(media, favorite=False) == {'favorite': False, 'protected': False}
    assert _open(media.parent).get_library_flags(media) == {'favorite': False, 'protected': False}
    assert reopened.get_by_id(binding.clip_id) == {
        **before, 'library_flags': {'favorite': False, 'protected': False}}
    with reopened.delete_guard(media):
        pass  # The real primitive's admission is open again without deleting the fixture.


@pytest.mark.parametrize('manual_takeover', [False, True])
def test_stale_unfavorite_uses_current_sql_protection_ownership(library, manual_takeover):
    stale, media, binding = library
    fresh = _open(media.parent)
    before = fresh.get_by_id(binding.clip_id)
    fresh.set_library_flags(media, favorite=True)
    if manual_takeover:
        fresh.set_library_flags(media, protected=True)
    result = stale.set_library_flags(media, favorite=False)
    assert result == {'favorite': False, 'protected': False}
    assert stale.get_by_id(binding.clip_id) == {
        **before, 'library_flags': {'favorite': False, 'protected': False}}
    assert _open(media.parent).get_library_flags(media) == result


@pytest.mark.parametrize('write', ['tag', 'draft'])
def test_stale_metadata_write_cannot_resurrect_auto_marker_after_explicit_protection(library, write):
    stale, media, binding = library
    stale.set_library_flags(media, favorite=True)
    fresh = _open(media.parent)
    fresh.set_library_flags(media, protected=True)
    if write == 'tag':
        stale.set(str(media), tag='New tag after manual protection')
    else:
        stale.set_editor_draft(str(media), {'manual protection': 'retained'})
    reopened = _open(media.parent)
    assert 'favorite_auto_protected' not in reopened.get_by_id(binding.clip_id)['library_flags']
    assert reopened.set_library_flags(media, favorite=False) == {'favorite': False, 'protected': False}


@pytest.mark.parametrize('marker', ['absent', None, False, 1, 'true', {}, []])
def test_explicit_unfavorite_releases_legacy_protection_regardless_of_auto_marker(library, marker):
    manager, media, binding = library
    before = manager.get_by_id(binding.clip_id)
    flags = {'favorite': True, 'protected': True, 'future': {'keep': [1]}}
    if marker != 'absent':
        flags['favorite_auto_protected'] = marker
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps({**before, 'library_flags': flags}), binding.clip_id))
    manager.reload()
    assert manager.set_library_flags(media, favorite=False) == {'favorite': False, 'protected': False}
    reopened = _open(media.parent)
    assert reopened.get_by_id(binding.clip_id)['library_flags']['future'] == {'keep': [1]}
    with reopened.delete_guard(media):
        pass  # Explicitly removing the favorite releases legacy protection too.
    assert reopened.set_library_flags(media, protected=False) == {'favorite': False, 'protected': False}
    assert 'favorite_auto_protected' not in reopened.get_by_id(binding.clip_id)['library_flags']
    with reopened.delete_guard(media):
        pass


@pytest.mark.parametrize('legacy_flags', [
    {'favorite': False, 'protected': True},
    {'favorite': True, 'protected': False},
    {'favorite': True, 'protected': True},
])
def test_legacy_protected_clip_is_favorite_until_explicitly_removed(library, legacy_flags):
    manager, media, binding = library
    before = manager.get_by_id(binding.clip_id)
    flags = {**legacy_flags, 'favorite_auto_protected': True}
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps({**before, 'library_flags': flags}), binding.clip_id))
    manager.reload()
    assert manager.get_library_flags(media) == {'favorite': True, 'protected': True}
    assert manager.flagged_paths('favorite') == (str(media),)
    assert manager.get_by_id(binding.clip_id)['library_flags'] == flags
    with pytest.raises(OSError):
        with manager.delete_guard(media):
            pytest.fail('legacy protection must survive until the favorite is removed')
    assert manager.set_library_flags(media, favorite=False) == {'favorite': False, 'protected': False}


def test_stale_flag_setter_merges_latest_sql_metadata_and_other_flag(library):
    stale, media, binding = library
    current = _open(media.parent)
    current.set_library_flags(media, protected=True)
    current.set(str(media), tag='New tag', description='New description')
    current.set_editor_draft(str(media), {'new edits': [9]})
    before = current.get_by_id(binding.clip_id)
    stale.set_library_flags(media, favorite=True)
    expected = {**before, 'library_flags': {'favorite': True, 'protected': True}}
    assert stale.get_by_id(binding.clip_id) == expected
    assert _open(media.parent).get_by_id(binding.clip_id) == expected


def test_alias_updates_same_identity_and_preserves_unknown_nested_flags(library, monkeypatch):
    manager, media, binding = library
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        value = manager.get_by_id(binding.clip_id)
        value['library_flags'] = {'favorite': False, 'protected': False, 'future': {'keep': [1]}}
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps(value), binding.clip_id))
    manager.reload()
    monkeypatch.chdir(media.parent)
    manager.set_library_flags('./clip.mp4', favorite=True)
    assert manager.resolve_media('./clip.mp4').clip_id == binding.clip_id
    assert manager.get_by_id(binding.clip_id)['library_flags'] == {
        'favorite': True, 'protected': True,
        'future': {'keep': [1]}}
    assert manager.flagged_paths('favorite') == (str(media),)
    with pytest.raises(ValueError):
        manager.flagged_paths('uploaded')


def test_favorite_png_rename_preserves_flags_ids_and_draft_after_restart(library):
    manager, media, binding = library
    # The file operation supports regular images as well as videos; no decoder is needed.
    image = media.with_name('screenshot.png')
    image.write_bytes(b'synthetic image file')
    image_binding = manager.register_media(manager.inspect_media(image))
    manager.set_editor_draft(str(image), {'crop': [0, 0, 2, 2]})
    manager.set_library_flags(image, favorite=True)
    metadata = manager.get_by_id(image_binding.clip_id)
    destination = image.with_name('renamed.png')
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(image_binding.clip_id, destination, admission={
        'owned_root': str(media.parent), 'imported_roots': [], 'allow_linked': False})
    assert service.execute(operation.op_id).succeeded
    reopened = _open(media.parent)
    assert reopened.flagged_paths('favorite') == (str(destination),)
    assert reopened.resolve_media(destination).file_id == image_binding.file_id
    assert reopened.get_by_id(image_binding.clip_id) == metadata
    assert reopened.get_editor_draft(str(destination)) == {'crop': [0, 0, 2, 2]}
    assert reopened.get_by_id(binding.clip_id)['tag'] == 'Original tag'
    assert reopened.set_library_flags(destination, favorite=False) == {
        'favorite': False, 'protected': False}


@pytest.mark.parametrize('write', ['tag', 'description', 'draft', 'clear', 'prune'])
def test_stale_ordinary_metadata_writes_preserve_new_sql_flags(library, monkeypatch, write):
    import core.clip_metadata_manager as module

    stale, media, binding = library
    fresh = _open(media.parent)
    fresh.set_library_flags(media, favorite=True)
    if write == 'tag':
        stale.set(str(media), tag='Edited tag')
    elif write == 'description':
        stale.set(str(media), description='Edited description')
    elif write == 'draft':
        assert stale.set_editor_draft(str(media), {'new': True})
    elif write == 'clear':
        stale.clear_editor_draft(str(media))
    else:
        monkeypatch.setattr(module, '_EDITOR_DRAFT_LIMIT', 1)
        other = media.with_name('newest.mp4')
        other.write_bytes(b'newest synthetic clip')
        stale.register_media(stale.inspect_media(other))
        assert stale.set_editor_draft(str(other), {'newest': True})
    reopened = _open(media.parent)
    assert reopened.get_library_flags(media) == {'favorite': True, 'protected': True}
    assert reopened.get_by_id(binding.clip_id)['unknown_future'] == {'nested': [None, {'keep': True}]}
    if write == 'clear':
        assert reopened.get_editor_draft(str(media)) is None
    elif write == 'prune':
        # Current SQLite drafts remain durable when the legacy limit changes.
        assert reopened.get_editor_draft(str(media)) == {'edits': [1, None, True]}
    assert 'favorite_auto_protected' not in reopened.get_by_id(binding.clip_id)['library_flags']
    assert reopened.set_library_flags(media, favorite=False) == {'favorite': False, 'protected': False}


@pytest.mark.parametrize('failure', ['abort', 'rollback', 'commit'])
@pytest.mark.parametrize('direction', ['on', 'off'])
def test_flag_transaction_failure_never_publishes_ram_or_partial_db(library, failure, direction):
    manager, media, binding = library
    if direction == 'off':
        manager.set_library_flags(media, favorite=True)
    before = copy.deepcopy(manager.get_by_id(binding.clip_id))
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        if failure == 'commit':
            connection.executescript('CREATE TABLE parent (id INTEGER PRIMARY KEY); '
                'CREATE TABLE child (id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED); '
                'CREATE TRIGGER fail_flags AFTER UPDATE ON entries BEGIN '
                'INSERT INTO child VALUES (42); END;')
        else:
            connection.execute('CREATE TRIGGER fail_flags BEFORE UPDATE ON entries BEGIN '
                               f"SELECT RAISE({failure.upper()}, 'synthetic refusal'); END")
    with pytest.raises(OSError):
        manager.set_library_flags(media, favorite=direction == 'on')
    assert manager.get_by_id(binding.clip_id) == before
    assert _open(media.parent).get_by_id(binding.clip_id) == before
    assert media.read_bytes() == b'original synthetic media'


def test_flagged_paths_excludes_missing_and_replaced_but_keeps_identity_history(library):
    manager, media, old = library
    manager.set_library_flags(media, favorite=True)
    assert manager.flagged_paths('favorite') == (str(media),)
    retained = media.with_name('retained-original.mp4')
    media.rename(retained)
    assert manager.flagged_paths('favorite') == ()
    media.write_bytes(b'a different synthetic replacement')
    assert manager.get_library_flags(media) == {'favorite': False, 'protected': False}
    assert manager.flagged_paths('favorite') == ()
    manager.set_library_flags(media, protected=True)
    new = manager.resolve_media(media)
    reopened = _open(media.parent)
    assert new.clip_id != old.clip_id and new.file_id != old.file_id
    assert reopened.get_by_id(old.clip_id)['library_flags'] == {
        'favorite': True, 'protected': True}
    assert any(file.binding_state == BindingState.DETACHED for file in reopened.files_for_clip(old.clip_id))
    assert reopened.get_by_id(new.clip_id) == {'library_flags': {'favorite': True, 'protected': True}}
    assert retained.read_bytes() == b'original synthetic media'


def test_legacy_adoption_and_flags_are_atomic_without_rewriting_draft_identity(tmp_path):
    media = tmp_path / 'legacy.png'
    media.write_bytes(b'synthetic screenshot')
    legacy = {'tag': 'Legacy', 'unknown': [1, None], 'editor_draft': {
        'version': 1, 'file': {'size': media.stat().st_size, 'mtime_ns': media.stat().st_mtime_ns},
        'updated_at': 1, 'state': {'crop': [0, 0, 1, 1]}}}
    (tmp_path / 'metadata.json').write_text(json.dumps({str(media): legacy}), encoding='utf-8')
    manager = _open(tmp_path)
    with pytest.raises(RuntimeError):
        manager.set_library_flags(media, favorite=True)
    assert not manager._database.exists()
    manager.migrate()
    before = manager.resolve_media(media)
    manager.set_library_flags(media, favorite=True)
    after = _open(tmp_path)
    assert after.resolve_media(media).clip_id == before.clip_id
    assert after.get_by_id(before.clip_id) == {
        **legacy, 'library_flags': {'favorite': True, 'protected': True}}
    assert after.get_editor_draft(str(media)) == legacy['editor_draft']['state']


def test_guard_uses_fresh_sql_and_holds_writer_lock_through_delete_boundary(library):
    stale, media, _binding = library
    current = _open(media.parent)
    current.set_library_flags(media, protected=True)
    with pytest.raises(OSError):
        with stale.delete_guard(media):
            pytest.fail('protected unlink boundary must never be entered')
    current.set_library_flags(media, protected=False)
    with stale.delete_guard(media) as identity:
        assert identity == stale.inspect_media(media).stat
        with closing(sqlite3.connect(stale._database, timeout=0)) as contender, contender:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                contender.execute('BEGIN IMMEDIATE')
    assert media.exists()


def test_delete_guard_protects_real_hardlink_alias(library):
    manager, media, _binding = library
    alias = media.with_name('hardlink-alias.mp4')
    try:
        os.link(media, alias)
    except OSError as error:
        pytest.skip(f'Hardlink unavailable on this test volume: {error}')
    assert os.stat(media).st_ino == os.stat(alias).st_ino
    manager.set_library_flags(media, protected=True)
    with pytest.raises(OSError):
        with manager.delete_guard(alias):
            pytest.fail('a hardlink to protected media must not reach unlink')
    assert alias.read_bytes() == media.read_bytes()


def test_migrated_protected_legacy_without_saved_identity_also_blocks_hardlink(tmp_path):
    media, alias = tmp_path / 'legacy.mp4', tmp_path / 'legacy-hardlink.mp4'
    media.write_bytes(b'protected legacy media')
    try:
        os.link(media, alias)
    except OSError as error:
        pytest.skip(f'Hardlink unavailable on this test volume: {error}')
    assert (media.stat().st_dev, media.stat().st_ino) == (alias.stat().st_dev, alias.stat().st_ino)
    metadata = {'tag': 'Legacy favorite', 'library_flags': {'favorite': True, 'protected': True}}
    (tmp_path / 'metadata.json').write_text(json.dumps({str(media): metadata}), encoding='utf-8')
    manager = _open(tmp_path)
    manager.migrate()
    binding = manager.resolve_media(media)
    assert binding.state == BindingState.LEGACY_UNVERIFIED
    assert manager.files_for_clip(binding.clip_id)[0].identity is None
    with pytest.raises(OSError):
        with manager.delete_guard(media):
            pytest.fail('legacy protected primary must not reach unlink')
    with pytest.raises(OSError):
        with manager.delete_guard(alias):
            pass  # A read-only probe: even the buggy guard never deletes data here.
    assert manager.get_by_id(binding.clip_id) == metadata
    assert alias.read_bytes() == media.read_bytes() == b'protected legacy media'


def test_guard_blocks_associated_derived_file_of_protected_clip(library):
    manager, media, binding = library
    derived = media.with_name('derived.mp4')
    derived.write_bytes(b'synthetic derived copy')
    manager.associate_file(binding.clip_id, manager.inspect_media(derived), role=FileRole.DERIVED)
    manager.set_library_flags(media, protected=True)
    with pytest.raises(OSError):
        with manager.delete_guard(derived):
            pytest.fail('protected associated file must not reach unlink')
    assert derived.read_bytes() == b'synthetic derived copy'


def test_guard_blocks_registered_audio_sidecar_of_protected_clip(library):
    from uuid import uuid4
    from core.audio_manifest import AudioSourceManifestEntry, build_manifest, write_manifest_atomic

    manager, media, binding = library
    source = AudioSourceManifestEntry(str(uuid4()), 1, 'system', 'System Mix',
        'system-default', None, 0, 10_000_000, 48_000, 2, 'float32')
    sidecar = write_manifest_atomic(build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[source]), media)
    manager.associate_file(binding.clip_id, manager.inspect_media(sidecar), role=FileRole.SIDECAR)
    manager.set_library_flags(media, protected=True)
    original = sidecar.read_bytes()
    with pytest.raises(OSError):
        with manager.delete_guard(sidecar):
            pytest.fail('protected audio manifest must not reach unlink')
    assert sidecar.read_bytes() == original


def test_linked_ancestor_is_rejected_at_identity_and_delete_boundaries(library):
    manager, media, _binding = library
    alias = media.parent / 'linked-folder'
    try:
        os.symlink(media.parent, alias, target_is_directory=True)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            pytest.skip(f'Windows symlink privilege unavailable: {error}')
        raise
    aliased_media = alias / media.name
    with pytest.raises(OSError):
        library_ownership.media_file_identity(aliased_media)
    with pytest.raises(OSError):
        library_ownership.delete_media(aliased_media, metadata_manager=manager)
    assert media.read_bytes() == b'original synthetic media'


@pytest.mark.parametrize('broken', ['missing', 'corrupt', 'future', 'invalid-flags'])
def test_delete_guard_fails_closed_when_current_database_is_untrustworthy(library, broken):
    manager, media, binding = library
    if broken == 'missing':
        manager._database.rename(media.parent / 'retained-metadata.sqlite3')
    elif broken == 'corrupt':
        manager._database.rename(media.parent / 'retained-metadata.sqlite3')
        manager._database.write_bytes(b'not sqlite')
    else:
        with closing(sqlite3.connect(manager._database)) as connection, connection:
            if broken == 'future':
                connection.execute('PRAGMA user_version=99')
            else:
                connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                    (json.dumps({'library_flags': {'protected': 'false'}}), binding.clip_id))
    with pytest.raises((OSError, ValueError, RuntimeError)):
        with manager.delete_guard(media):
            pytest.fail('untrusted database must not reach unlink')
    assert media.read_bytes() == b'original synthetic media'


def test_delete_guard_rejects_changed_identity_and_pending_journal(library):
    manager, media, binding = library
    before = manager.inspect_media(media).stat
    media.rename(media.with_name('retained-before-replacement.mp4'))
    media.write_bytes(b'replacement file')
    with pytest.raises(OSError):
        with manager.delete_guard(media, expected_identity=before):
            pytest.fail('replacement must not reach unlink')
    manager.register_media(manager.inspect_media(media))
    current = manager.resolve_media(media)
    service = ClipFileOperations(manager)
    operation = service.prepare_rename(current.clip_id, media.with_name('destination.mp4'), admission={
        'owned_root': str(media.parent), 'imported_roots': [], 'allow_linked': False})
    with pytest.raises(OSError):
        with _open(media.parent).delete_guard(media):
            pytest.fail('reserved journal source must not reach unlink')
    assert manager.get_operation(operation.op_id).state.value == 'PREPARED'
    assert manager.get_by_id(binding.clip_id)['tag'] == 'Original tag'


def test_owned_delete_requires_manager_and_checks_protection_at_actual_primitive(library):
    manager, media, _binding = library
    result = library_ownership.generic_delete_owned_media(media, media.parent, [])
    assert not result.deleted and media.exists()
    stale = _open(media.parent)
    manager.set_library_flags(media, protected=True)
    result = library_ownership.generic_delete_owned_media(
        media, media.parent, [], metadata_manager=stale)
    assert not result.deleted and media.exists()
    manager.set_library_flags(media, protected=False)
    identity = manager.inspect_media(media).stat
    assert library_ownership.delete_media(
        media, metadata_manager=stale, expected_identity=identity) is not False
    assert not media.exists()


def test_imported_original_remains_owned_by_user_even_when_unprotected(library):
    manager, media, _binding = library
    manager.set_library_flags(media, protected=False)
    result = library_ownership.generic_delete_owned_media(
        media, media.parent, [media.parent], metadata_manager=manager)
    assert not result.deleted
    assert result.ownership == library_ownership.MediaOwnership.LINKED_IMPORTED
    assert media.read_bytes() == b'original synthetic media'


@pytest.mark.parametrize('name', ['desktop_clip_from_test.mp4.partial', '.clip.partial.token.mp4'])
def test_incomplete_capture_and_export_staging_files_are_not_favoritable(library, name):
    manager, media, _binding = library
    partial = media.with_name(name)
    partial.write_bytes(b'incomplete synthetic output')
    with pytest.raises(ValueError):
        manager.set_library_flags(partial, favorite=True)
    assert manager.resolve_media(partial) is None
    assert manager.flagged_paths('favorite') == ()
    assert partial.read_bytes() == b'incomplete synthetic output'


def test_flag_reads_do_not_reopen_sqlite_for_each_card(library, monkeypatch):
    manager, media, _binding = library
    manager.set_library_flags(media, favorite=True)
    with monkeypatch.context() as readers:
        readers.setattr(sqlite3, 'connect', lambda *args, **kwargs: pytest.fail(
            'card flag reads must use the validated snapshot'))
        assert manager.get_library_flags(media) == {'favorite': True, 'protected': True}
        assert manager.flagged_paths('favorite') == (str(media),)
        assert manager.flagged_paths('protected') == (str(media),)


def test_conflicting_legacy_bindings_do_not_report_unprotected_default(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'synthetic conflicted clip')
    (tmp_path / 'sub').mkdir()
    alias = str(tmp_path / 'sub' / '..' / media.name)
    records = {str(media): {'tag': 'first'}, alias: {
        'tag': 'other', 'library_flags': {'protected': True}}}
    (tmp_path / 'metadata.json').write_text(json.dumps(records), encoding='utf-8')
    manager = _open(tmp_path)
    manager.migrate()
    assert manager.resolve_media(media).state == BindingState.CONFLICT
    with pytest.raises((ValueError, OSError)):
        manager.get_library_flags(media)
    assert manager.get(str(media)) == {'tag': '', 'description': ''}
    with pytest.raises((ValueError, OSError)):
        manager.set_library_flags(media, protected=False)
    assert manager.flagged_paths('favorite') == ()
