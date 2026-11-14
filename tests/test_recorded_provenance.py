"""Capture display provenance survives changing the configured library roots."""
import json
import struct

import pytest

from core.capture_highlights import HIGHLIGHT_UUID
from core.clip_metadata_manager import ClipMetadataManager
from core.library_index import scan_library
from core.library_ownership import generic_delete_owned_media, MediaOwnership


@pytest.mark.parametrize('migrated', [False, True])
def test_recorded_origin_persists_and_rejects_replacement(tmp_path, migrated):
    media = tmp_path / 'old-library' / 'renamed.mp4'
    media.parent.mkdir()
    media.write_bytes(b'recorded media')
    db = tmp_path / 'metadata.sqlite3'
    manager = ClipMetadataManager(database_path=db)
    if migrated:
        manager.migrate(target_version=6)
    identity = manager._file_identity(media)
    manager.mark_recorded(str(media), expected_identity=identity, group='My Game')
    manager.set(str(media), description='Keep my description')
    reloaded = ClipMetadataManager(database_path=db)
    result = scan_library(tmp_path / 'new-library', [media.parent],
                          recorded_media=reloaded.recorded_media_snapshot())
    record, = result.records
    assert record.recorded and not record.imported
    assert record.recorded_group == 'My Game'
    assert not scan_library(tmp_path / 'new-library', [media.parent], filter_='imported',
                            recorded_media=reloaded.recorded_media_snapshot()).records
    assert reloaded.is_recorded(str(media))
    media.write_bytes(b'unrelated replacement original')
    assert not reloaded.is_recorded(str(media))
    assert scan_library(tmp_path / 'new-library', [media.parent],
                        recorded_media=reloaded.recorded_media_snapshot()).records[0].imported
    with pytest.raises(OSError):
        reloaded.mark_recorded(str(media), expected_identity=identity)


def test_old_generated_names_remain_recorded_after_storage_changes(tmp_path):
    old = tmp_path / 'old'
    old.mkdir()
    (old / 'My Game_clip_from_08Nov2026_09-12-30.mp4').write_bytes(b'old recording')
    (old / 'desktop_clip_from_08Nov2026_09-12-31.mp4').write_bytes(b'old desktop')
    (old / 'external.mp4').write_bytes(b'original external')
    result = scan_library(tmp_path / 'new', [old])
    records = {record.recorded_group: record for record in result.records if record.recorded}
    assert set(records) == {'My Game', 'Desktop'}
    assert all(not record.imported for record in records.values())
    assert {record.path for record in scan_library(tmp_path / 'new', [old], filter_='imported').records} == {
        str(old / 'external.mp4')}
    # Display provenance must never permit deleting a linked original.
    result = generic_delete_owned_media(records['My Game'].path, tmp_path / 'new', [old])
    assert not result.deleted and result.ownership == MediaOwnership.LINKED_IMPORTED


def test_native_capture_uuid_recovers_renamed_legacy_clip_without_registry(tmp_path):
    def box(kind, payload):
        return struct.pack('>I4s', 8 + len(payload), kind) + payload
    old = tmp_path / 'old' / 'Game'
    old.mkdir(parents=True)
    media = old / 'renamed.mp4'
    media.write_bytes(box(b'ftyp', b'isom\x00\x00\x02\x00mp42') + box(
        b'uuid', HIGHLIGHT_UUID + json.dumps({'version': 1, 'markers': []}).encode()))
    record, = scan_library(tmp_path / 'new', [old]).records
    assert record.recorded and not record.imported
    assert record.recorded_group == 'Game'


def test_generic_metadata_does_not_claim_external_media(tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    media = external / 'original.mp4'
    media.write_bytes(b'external media')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.set(str(media), tag='favorite')
    assert not manager.is_recorded(str(media))
    record, = scan_library(tmp_path / 'new', [external],
                           recorded_media=manager.recorded_media_snapshot()).records
    assert record.imported and not record.recorded


def test_legacy_recorded_metadata_survives_migration_and_capture_rename(tmp_path):
    media = tmp_path / 'capture.mp4'
    media.write_bytes(b'recorded clip')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.mark_recorded(str(media), expected_identity=manager._file_identity(media), group='Original Game')
    manager.migrate(target_version=6)
    assert manager.is_recorded(str(media))
    # Explicit registration verifies the legacy binding before a file operation.
    manager.register_media(manager.inspect_media(media), adopt_legacy=True)
    renamed = tmp_path / 'custom title.mp4'
    media.rename(renamed)
    manager.rename_path(str(media), str(renamed))
    record, = scan_library(tmp_path / 'new', [tmp_path],
                           recorded_media=manager.recorded_media_snapshot()).records
    assert record.recorded and not record.imported
    assert record.recorded_group == 'Original Game'
