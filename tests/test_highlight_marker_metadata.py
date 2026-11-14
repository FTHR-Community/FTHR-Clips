from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from core.clip_metadata_manager import ClipMetadataManager
from core.highlight_markers import (
    HighlightMarkerError,
    UnsupportedHighlightMarkersError,
)


def _open(directory):
    manager = ClipMetadataManager(database_path=directory / 'metadata.sqlite3')
    manager.migrate()
    return manager


def _setup(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'synthetic clip')
    manager = _open(tmp_path)
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(media, tag='keep', description='description')
    return manager, media, binding


def _marker(position, label=''):
    return {'marker_id': str(uuid4()), 'position_100ns': position, 'label': label}


def test_marker_write_uses_owned_merge_and_survives_reopen(tmp_path):
    manager, media, binding = _setup(tmp_path)
    marker = _marker(12_345, 'one')

    assert manager.set_highlight_markers(
        media, [marker], expected_identity=manager.inspect_media(media).stat,
        expected_clip_id=binding.clip_id) == [marker]
    assert manager.get_highlight_markers(media) == [marker]
    metadata = manager.get_by_id(binding.clip_id)
    assert metadata['tag'] == 'keep'
    assert metadata['description'] == 'description'

    reopened = _open(tmp_path)
    reopened.register_media(reopened.inspect_media(media))
    assert reopened.get_highlight_markers(media) == [marker]


def test_operation_owned_in_place_size_change_keeps_clip_owner(tmp_path):
    manager, media, binding = _setup(tmp_path)
    media.write_bytes(b'synthetic clip plus native marker box')
    current = manager.inspect_media(media)
    marker = _marker(22)
    assert manager.set_highlight_markers(
        media, [marker], expected_identity=current.stat,
        expected_clip_id=binding.clip_id) == [marker]
    assert manager.resolve_media(media).clip_id == binding.clip_id
    assert manager.get(media)['tag'] == 'keep'


def test_stale_identity_and_clip_id_refuse_without_partial_write(tmp_path):
    manager, media, binding = _setup(tmp_path)
    identity = manager.inspect_media(media).stat
    with pytest.raises(OSError):
        manager.set_highlight_markers(
            media, [_marker(0)], expected_identity=identity,
            expected_clip_id=None)
    with pytest.raises(OSError):
        manager.set_highlight_markers(
            media, [_marker(1)], expected_identity=dict(identity, size=999),
            expected_clip_id=binding.clip_id)
    with pytest.raises(OSError):
        manager.set_highlight_markers(
            media, [_marker(2)], expected_identity=identity,
            expected_clip_id='wrong-clip')
    assert manager.get_highlight_markers(media) == []


def test_unsupported_existing_payload_is_preserved_and_write_is_refused(tmp_path):
    manager, media, binding = _setup(tmp_path)
    before = manager.get_by_id(binding.clip_id)
    unsupported = {'version': 99, 'markers': []}
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        value = {**before, 'library_highlights': unsupported}
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps(value), binding.clip_id))
    manager.reload()
    with pytest.raises(UnsupportedHighlightMarkersError):
        manager.set_highlight_markers(media, [_marker(1)])
    assert manager.get_by_id(binding.clip_id)['library_highlights'] == unsupported


@pytest.mark.parametrize('stored', ['{"version":1,"markers":[]}', None, False, {}])
def test_present_non_mapping_or_unsupported_payload_is_never_replaced(tmp_path, stored):
    manager, media, binding = _setup(tmp_path)
    before = manager.get_by_id(binding.clip_id)
    with closing(sqlite3.connect(manager._database)) as connection, connection:
        value = {**before, 'library_highlights': stored}
        connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                           (json.dumps(value), binding.clip_id))
    manager.reload()
    with pytest.raises((HighlightMarkerError, UnsupportedHighlightMarkersError)):
        manager.set_highlight_markers(media, [_marker(1)])
    assert manager.get_by_id(binding.clip_id)['library_highlights'] == stored


def test_stale_viewer_cannot_write_after_fresh_sql_replacement(tmp_path):
    stale, media, old_binding = _setup(tmp_path)
    media.write_bytes(b'replacement clip')
    fresh = _open(tmp_path)
    new_binding = fresh.register_media(fresh.inspect_media(media))
    assert new_binding.clip_id != old_binding.clip_id

    # The stale manager still has A's cached identity.  The default API carries
    # no fresh authority and must refuse before merging B's SQL row.
    with pytest.raises(OSError):
        stale.set_highlight_markers(media, [_marker(3)])
    assert fresh.get_highlight_markers(media) == []

    # A caller that actually captured B's fresh identity and ClipID is allowed
    # to write the current owner deliberately.
    current = fresh.inspect_media(media)
    marker = _marker(4)
    assert fresh.set_highlight_markers(
        media, [marker], expected_identity=current.stat,
        expected_clip_id=new_binding.clip_id) == [marker]
    assert fresh.get_highlight_markers(media) == [marker]

    # A second default call must refuse as well.  The first refusal cannot
    # retire stale RAM evidence and thereby turn this into an implicit adopt.
    with pytest.raises(OSError):
        stale.set_highlight_markers(media, [_marker(5)])
    assert fresh.get_highlight_markers(media) == [marker]


def test_legacy_fullsave_preserves_highlights_owned_by_fresh_sql(tmp_path):
    manager, media, binding = _setup(tmp_path)
    marker = _marker(5)
    current = manager.inspect_media(media)
    manager.set_highlight_markers(
        media, [marker], expected_identity=current.stat,
        expected_clip_id=binding.clip_id)
    manager._commit_changes(
        {str(media): {'tag': 'legacy writer'}}, owned_fields=None)
    assert manager.get_highlight_markers(media) == [marker]


def test_fresh_snapshot_rejects_stale_open_identity_after_path_replacement(tmp_path):
    stale, media, old_binding = _setup(tmp_path)
    old_identity = stale.inspect_media(media).stat
    media.write_bytes(b'replacement generation B')
    fresh = _open(tmp_path)
    fresh_binding = fresh.register_media(fresh.inspect_media(media))
    assert fresh_binding.clip_id != old_binding.clip_id

    with pytest.raises(OSError):
        stale.get_highlight_state_snapshot(
            media, expected_identity=old_identity)

    current = fresh.inspect_media(media).stat
    assert fresh.get_highlight_state_snapshot(
        media, expected_identity=current) == {}


def test_unregistered_empty_marker_payload_is_registered(tmp_path):
    media = tmp_path / 'new.mp4'
    media.write_bytes(b'new clip')
    manager = _open(tmp_path)
    current = manager.inspect_media(media).stat

    assert manager.set_highlight_markers(
        media, (), expected_identity=current, expected_clip_id=None) == []
    assert manager.has_highlight_markers(media, fresh=True,
                                         expected_identity=current)
    assert manager.get_highlight_markers(media) == []


def test_marker_append_clears_stale_content_digest(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'hashed clip')
    manager = _open(tmp_path)
    binding = manager.register_media(manager.inspect_media(media, hash_content=True))
    media.write_bytes(b'hashed clip plus native marker bytes')
    current = manager.inspect_media(media).stat

    manager.set_highlight_markers(
        media, [_marker(8)], expected_identity=current,
        expected_clip_id=binding.clip_id)

    with closing(sqlite3.connect(manager._database)) as connection:
        row = connection.execute(
            'SELECT content_sha256 FROM clip_files WHERE file_id=?',
            (binding.file_id,)).fetchone()
    assert row == (None,)
