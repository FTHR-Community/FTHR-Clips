"""Contract tests for album metadata, membership, and schema-4 migration.

All media and catalogs in this module live under pytest's temporary directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.clip_metadata_manager import ClipMetadataManager


def _manager(tmp_path: Path) -> ClipMetadataManager:
    return ClipMetadataManager(database_path=tmp_path / "metadata.sqlite3")


def _schema4_manager(tmp_path: Path) -> ClipMetadataManager:
    manager = _manager(tmp_path)
    manager.migrate(target_version=4)
    return manager


def _clip(manager: ClipMetadataManager, tmp_path: Path, name: str = "clip.mp4"):
    path = tmp_path / name
    path.write_bytes(("synthetic:" + name).encode())
    binding = manager.register_media(manager.inspect_media(path))
    return path, binding, dict(manager.inspect_media(path).stat)


def test_default_migration_stays_at_schema_two(tmp_path):
    manager = _manager(tmp_path)
    assert manager.migrate().to_version == 2


def test_empty_schema_zero_album_reads_do_not_create_metadata_files(tmp_path):
    manager = _manager(tmp_path)

    assert manager.list_albums() == ()
    assert manager.album_bindings("missing") == {}
    assert not (tmp_path / "metadata.sqlite3").exists()
    assert not (tmp_path / "metadata.json").exists()


@pytest.mark.parametrize("source_version", [2, 3])
def test_schema2_or3_upgrade_preserves_existing_metadata_and_backup(tmp_path, source_version):
    manager = _manager(tmp_path)
    assert manager.migrate(target_version=source_version).to_version == source_version
    path, binding, _ = _clip(manager, tmp_path)
    manager.set(str(path), tag="keep", description="opaque survives")
    manager.set_library_flags(path, favorite=True, protected=False)
    manager.set_editor_draft(str(path), {"segments": [[1, 5, False]], "future": [None, 7]})
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)).fetchone()[0])
        value["future_metadata"] = {"nested": ["retain", None, 23]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
    manager.reload()  # External SQL edits become visible only after refreshing the RAM snapshot.
    before = manager.get_by_id(binding.clip_id)

    result = manager.migrate(target_version=4)

    assert (result.from_version, result.to_version, result.changed) == (source_version, 4, True)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == source_version
        assert backup.execute("SELECT clip_id,path,value FROM entries").fetchone() == (
            binding.clip_id, str(path), json.dumps(value))
    assert manager.inspect_schema().version == 4
    assert manager.get_by_id(binding.clip_id) == before
    assert manager.migrate(target_version=4).changed is False
    with pytest.raises(ValueError, match="downgrade"):
        manager.migrate()


def test_schema3_to4_backup_includes_committed_wal_metadata(tmp_path):
    manager = _manager(tmp_path)
    manager.migrate(target_version=3)
    path, binding, _ = _clip(manager, tmp_path)
    db = sqlite3.connect(tmp_path / "metadata.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)).fetchone()[0])
        value["committed_wal_value"] = {"keep": ["v3", 17]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
        db.commit()
        assert Path(str(tmp_path / "metadata.sqlite3") + "-wal").stat().st_size > 0
    finally:
        db.close()

    manager.reload()
    result = manager.migrate(target_version=4)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        saved = json.loads(backup.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)).fetchone()[0])
        assert saved["committed_wal_value"] == {"keep": ["v3", 17]}


def test_album_crud_normalizes_names_and_rejects_invalid_or_duplicate_names(tmp_path):
    manager = _schema4_manager(tmp_path)

    album = manager.create_album("  Cafe\u0301   Clips  ")

    assert album.name == "Caf\u00e9 Clips"
    assert album.album_id and album.created_utc
    assert manager.list_albums() == (album,)
    with pytest.raises(FrozenInstanceError):
        album.name = "mutable"
    renamed = manager.rename_album(album.album_id, "  Final   Cuts ")
    assert renamed.album_id == album.album_id
    assert renamed.name == "Final Cuts"
    assert renamed.created_utc == album.created_utc
    for invalid in ("", " \t ", "bad\tname", "bad\nname", "x" * 81):
        with pytest.raises((ValueError, TypeError)):
            manager.create_album(invalid)
    second = manager.create_album("Work")
    with pytest.raises((ValueError, TypeError)):
        manager.rename_album(second.album_id, " final   cuts ")
    assert manager.list_albums() == (renamed, second)
    assert manager.delete_album(renamed.album_id) is None
    assert manager.list_albums() == (second,)


def test_album_membership_is_many_to_many_idempotent_and_returns_identity_evidence(tmp_path):
    manager = _schema4_manager(tmp_path)
    first = manager.create_album("First")
    second = manager.create_album("Second")
    path, binding, identity = _clip(manager, tmp_path)

    assert manager.add_album_member(first.album_id, path, expected_identity=identity) is True
    assert manager.add_album_member(first.album_id, path, expected_identity=identity) is False
    assert manager.add_album_member(second.album_id, path, expected_identity=identity) is True
    first_evidence = manager.album_bindings(first.album_id)
    second_evidence = manager.album_bindings(second.album_id)
    assert len(first_evidence) == len(second_evidence) == 1
    assert next(iter(first_evidence.values())) == identity
    assert next(iter(second_evidence.values())) == identity
    assert manager.remove_album_member(first.album_id, path, expected_identity=identity) is True
    assert manager.remove_album_member(first.album_id, path, expected_identity=identity) is False
    assert manager.album_bindings(first.album_id) == {}
    assert len(manager.album_bindings(second.album_id)) == 1
    assert manager.resolve_media(path).clip_id == binding.clip_id


def test_adding_hash_registered_primary_preserves_verified_binding_and_metadata(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Verified")
    path = tmp_path / "verified.mp4"
    path.write_bytes(b"synthetic media with verified digest")
    evidence = manager.inspect_media(path, hash_content=True)
    binding = manager.register_media(evidence)
    manager.set(str(path), tag="keep tag", description="keep description")
    before_files = manager.files_for_clip(binding.clip_id)
    before_metadata = manager.get_by_id(binding.clip_id)
    assert before_files[0].binding_state.value == "CONTENT_VERIFIED"
    assert before_files[0].content_sha256 == evidence.content_sha256

    assert manager.add_album_member(album.album_id, path,
                                    expected_identity=dict(evidence.stat)) is True

    after_files = manager.files_for_clip(binding.clip_id)
    assert after_files == before_files
    assert after_files[0].binding_state.value == "CONTENT_VERIFIED"
    assert after_files[0].content_sha256 == evidence.content_sha256
    assert manager.get_by_id(binding.clip_id) == before_metadata


@pytest.mark.parametrize("mutation", ["missing", "extra", "boolean", "float", "negative_inode"])
def test_album_add_rejects_malformed_file_identity_evidence(tmp_path, mutation):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Evidence schema")
    path, _, identity = _clip(manager, tmp_path)
    invalid = dict(identity)
    if mutation == "missing":
        invalid.pop("mtime_ns")
    elif mutation == "extra":
        invalid["future"] = 0
    elif mutation == "boolean":
        invalid["size"] = True
    elif mutation == "float":
        invalid["size"] = float(identity["size"])
    else:
        invalid["st_ino"] = -1

    with pytest.raises(ValueError, match="identity"):
        manager.add_album_member(album.album_id, path, expected_identity=invalid)
    assert manager.album_bindings(album.album_id) == {}


def test_media_change_at_final_recheck_rolls_back_membership_transaction(tmp_path, monkeypatch):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Race guard")
    path = tmp_path / "racing.mp4"
    path.write_bytes(b"stable at initial check")
    original_inspect = manager.inspect_media
    inspect_calls = 0

    def change_before_final_recheck(candidate, *, hash_content=False):
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 2:
            path.write_bytes(b"changed after membership SQL insert")
        return original_inspect(candidate, hash_content=hash_content)

    monkeypatch.setattr(manager, "inspect_media", change_before_final_recheck)
    with pytest.raises(ValueError, match="changed since evidence"):
        manager.add_album_member(album.album_id, path)
    assert inspect_calls == 2
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM entries WHERE path=?",
                          (str(path),)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM clip_files WHERE path=?",
                          (str(path),)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM album_members").fetchone()[0] == 0


def test_stale_identity_and_unfinished_operation_cannot_change_album_membership(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Evidence")
    path, binding, identity = _clip(manager, tmp_path)
    path.unlink()
    path.write_bytes(b"replacement at same path")
    # A stale identity is not a path-tracking/rename operation.
    with pytest.raises((OSError, ValueError)):
        manager.add_album_member(album.album_id, path, expected_identity=identity)
    assert manager.album_bindings(album.album_id) == {}
    assert manager.resolve_media(path).clip_id == binding.clip_id


def test_open_file_operation_blocks_album_membership_writes(tmp_path):
    from core.clip_file_operations import ClipFileOperations

    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Journal guard")
    owned = tmp_path / "library"
    owned.mkdir()
    media = owned / "one.mp4"
    media.write_bytes(b"synthetic journal guard media")
    binding = manager.register_media(manager.inspect_media(media))
    identity = dict(manager.inspect_media(media).stat)
    admission = {"owned_root": str(owned), "imported_roots": (), "allow_linked": False}
    operations = ClipFileOperations(manager)
    pending = operations.prepare_rename(
        binding.clip_id, owned / "renamed.mp4", admission=admission)

    with pytest.raises((OSError, ValueError)):
        manager.add_album_member(album.album_id, media, expected_identity=identity)
    with pytest.raises((OSError, ValueError)):
        manager.remove_album_member(album.album_id, media, expected_identity=identity)
    assert manager.album_bindings(album.album_id) == {}
    assert manager.get_operation(pending.op_id).state.value == "PREPARED"


def test_prepared_schema_three_journal_survives_upgrade_and_recovers(tmp_path):
    from core.clip_file_operations import ClipFileOperations

    manager = _manager(tmp_path)
    manager.migrate(target_version=3)
    owned = tmp_path / "library"
    owned.mkdir()
    media = owned / "one.mp4"
    media.write_bytes(b"synthetic prepared journal")
    binding = manager.register_media(manager.inspect_media(media))
    manager.set(str(media), tag="preserve across recovery")
    before_metadata = manager.get_by_id(binding.clip_id)
    before_files = manager.files_for_clip(binding.clip_id)
    admission = {"owned_root": str(owned), "imported_roots": (), "allow_linked": False}
    operations = ClipFileOperations(manager)
    pending = operations.prepare_rename(binding.clip_id, owned / "renamed.mp4",
                                        admission=admission)

    manager.migrate(target_version=4)
    manager.reload()
    results = operations.recover_pending()

    assert len(results) == 1
    assert not results[0].succeeded
    assert media.read_bytes() == b"synthetic prepared journal"
    assert not (owned / "renamed.mp4").exists()
    assert manager.get_operation(pending.op_id).state.value == "ROLLED_BACK"
    assert manager.resolve_media(media).clip_id == binding.clip_id
    assert manager.get_by_id(binding.clip_id) == before_metadata
    assert manager.files_for_clip(binding.clip_id) == before_files


def test_album_membership_cannot_target_an_associated_sidecar(tmp_path):
    from core.clip_metadata_types import FileRole

    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Primary only")
    media, binding, _identity = _clip(manager, tmp_path)
    sidecar_path = tmp_path / "derived.mp4"
    sidecar_path.write_bytes(b"synthetic derived audio")
    sidecar_evidence = manager.inspect_media(sidecar_path)
    manager.associate_file(binding.clip_id, sidecar_evidence, role=FileRole.DERIVED)
    with pytest.raises((OSError, ValueError)):
        manager.add_album_member(album.album_id, sidecar_path,
                                 expected_identity=dict(sidecar_evidence.stat))
    assert manager.album_bindings(album.album_id) == {}


def test_album_membership_rejects_unfinished_media_candidate(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Finished only")
    partial = tmp_path / "capture.mp4.partial"
    partial.write_bytes(b"not a completed clip")

    with pytest.raises((OSError, ValueError)):
        manager.add_album_member(album.album_id, partial)
    assert manager.album_bindings(album.album_id) == {}
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM entries WHERE path=?",
                          (str(partial),)).fetchone()[0] == 0


def test_album_bindings_is_defensive_ram_snapshot_without_filesystem_or_sql_io(tmp_path, monkeypatch):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Snapshot")
    path, _, identity = _clip(manager, tmp_path)
    manager.add_album_member(album.album_id, path, expected_identity=identity)
    manager.reload()
    expected = manager.album_bindings(album.album_id)
    assert len(expected) == 1
    snapshot_identity = next(iter(expected.values()))
    assert set(snapshot_identity) == {"st_dev", "st_ino", "size", "mtime_ns"}
    assert all(type(value) is int for value in snapshot_identity.values())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("album_bindings must use the confirmed in-memory snapshot")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    snapshot = manager.album_bindings(album.album_id)
    assert snapshot == expected
    key = next(iter(snapshot))
    snapshot[key]["size"] += 1
    assert manager.album_bindings(album.album_id) == expected


@pytest.mark.parametrize("conflict_kind", ["same_path_key", "duplicate_primary"])
def test_album_bindings_hides_ambiguous_primary_snapshot(tmp_path, conflict_kind):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Ambiguous")
    first_path, first_binding, first_identity = _clip(manager, tmp_path, "first.mp4")
    manager.add_album_member(album.album_id, first_path, expected_identity=first_identity)
    second = None
    if conflict_kind == "same_path_key":
        second_path, second_binding, second_identity = _clip(manager, tmp_path, "second.mp4")
        manager.add_album_member(album.album_id, second_path, expected_identity=second_identity)
        second = second_binding

    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        first_file = db.execute(
            "SELECT file_id,clip_id,role,path,path_key,binding_state,identity_json,"
            "content_sha256,provenance FROM clip_files WHERE clip_id=? AND role='PRIMARY'",
            (first_binding.clip_id,)).fetchone()
        if conflict_kind == "duplicate_primary":
            duplicate = ("duplicate-primary-row", *first_file[1:])
            db.execute("INSERT INTO clip_files VALUES (?,?,?,?,?,?,?,?,?)", duplicate)
        else:
            db.execute("UPDATE clip_files SET path_key=? WHERE clip_id=? AND role='PRIMARY'",
                       (first_file[4], second.clip_id))
    manager.reload()
    assert manager.album_bindings(album.album_id) == {}


def test_stale_identity_refuses_membership_removal(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Stale remove")
    path, binding, old_identity = _clip(manager, tmp_path)
    assert manager.add_album_member(album.album_id, path, expected_identity=old_identity)
    path.unlink()
    path.write_bytes(b"replacement at same path")

    with pytest.raises((OSError, ValueError)):
        manager.remove_album_member(album.album_id, path, expected_identity=old_identity)
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM album_members WHERE album_id=? AND clip_id=?",
                          (album.album_id, binding.clip_id)).fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_stale_manager_reconciles_replacement_committed_by_another_manager(
        tmp_path, operation):
    manager_a = _schema4_manager(tmp_path)
    album = manager_a.create_album("Cross-manager replacement")
    path, old_binding, old_identity = _clip(manager_a, tmp_path)
    assert manager_a.add_album_member(album.album_id, path,
                                      expected_identity=old_identity)
    manager_b = _manager(tmp_path)  # Cache the old primary before the replacement.
    path.unlink()
    path.write_bytes(b"replacement committed by manager B")
    new_evidence = manager_b.inspect_media(path)
    new_binding = manager_b.register_media(new_evidence)
    assert new_binding.clip_id != old_binding.clip_id

    if operation == "add":
        assert manager_a.add_album_member(
            album.album_id, path, expected_identity=dict(new_evidence.stat)) is True
        with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
            members = {row[0] for row in db.execute(
                "SELECT clip_id FROM album_members WHERE album_id=?", (album.album_id,))}
        assert members == {old_binding.clip_id, new_binding.clip_id}
        bindings = manager_a.album_bindings(album.album_id)
        from core.library_index import canonical_media_path
        assert bindings == {canonical_media_path(path): dict(new_evidence.stat)}
    else:
        # The album has only the stale historical ID; removing this new occupant
        # must not resurrect that old primary in manager A's cached read model.
        assert manager_a.remove_album_member(
            album.album_id, path, expected_identity=dict(new_evidence.stat)) is False
        with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
            members = {row[0] for row in db.execute(
                "SELECT clip_id FROM album_members WHERE album_id=?", (album.album_id,))}
        assert members == {old_binding.clip_id}
        assert manager_a.album_bindings(album.album_id) == {}
    assert manager_a.resolve_media(path).clip_id == new_binding.clip_id
    old_files = manager_a.files_for_clip(old_binding.clip_id)
    assert all(file.binding_state.value == "DETACHED" for file in old_files)


def test_inconsistent_entry_and_primary_paths_reject_replacement_without_mutation(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Mismatched paths")
    path, binding, _old_identity = _clip(manager, tmp_path)
    assert manager.add_album_member(album.album_id, path)
    path.unlink()
    path.write_bytes(b"new occupant after entry path corruption")
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        db.execute("UPDATE entries SET path=? WHERE clip_id=?",
                   (str(tmp_path / "other.mp4"), binding.clip_id))
        before_entries = db.execute(
            "SELECT clip_id,path,value FROM entries ORDER BY clip_id").fetchall()
        before_files = db.execute(
            "SELECT file_id,clip_id,role,path,path_key,binding_state,identity_json "
            "FROM clip_files ORDER BY file_id").fetchall()
        before_members = db.execute(
            "SELECT album_id,clip_id FROM album_members ORDER BY album_id,clip_id").fetchall()
    current_identity = dict(manager.inspect_media(path).stat)

    with pytest.raises((OSError, ValueError)):
        manager.add_album_member(album.album_id, path, expected_identity=current_identity)

    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT clip_id,path,value FROM entries ORDER BY clip_id").fetchall() == before_entries
        assert db.execute(
            "SELECT file_id,clip_id,role,path,path_key,binding_state,identity_json "
            "FROM clip_files ORDER BY file_id").fetchall() == before_files
        assert db.execute(
            "SELECT album_id,clip_id FROM album_members ORDER BY album_id,clip_id").fetchall() == before_members


def test_stale_manager_remove_publishes_replacement_clip_metadata_and_flags(tmp_path):
    manager_a = _schema4_manager(tmp_path)
    album = manager_a.create_album("Replacement metadata")
    path, old_binding, old_identity = _clip(manager_a, tmp_path)
    assert manager_a.add_album_member(album.album_id, path,
                                      expected_identity=old_identity)
    manager_b = _manager(tmp_path)
    path.unlink()
    path.write_bytes(b"new clip with nonempty metadata")
    evidence = manager_b.inspect_media(path, hash_content=True)
    new_binding = manager_b.register_media(evidence)
    assert new_binding.clip_id != old_binding.clip_id
    manager_b.set(str(path), tag="replacement tag", description="preserved replacement")
    manager_b.set_library_flags(path, favorite=True, protected=True)
    manager_b.set_editor_draft(str(path), {"opaque": {"revision": 9, "items": [2, None]}})
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (new_binding.clip_id,)).fetchone()[0])
        value["future_opaque"] = {"keep": ["from manager B", 31]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), new_binding.clip_id))
    manager_b.reload()
    assert manager_b.add_album_member(
        album.album_id, path, expected_identity=dict(evidence.stat))

    assert manager_a.remove_album_member(
        album.album_id, path, expected_identity=dict(evidence.stat)) is True

    assert manager_a.get(str(path)) == {
        "tag": "replacement tag", "description": "preserved replacement"}
    assert manager_a.get_by_id(new_binding.clip_id)["future_opaque"] == {
        "keep": ["from manager B", 31]}
    assert manager_a.get_editor_draft(str(path)) == {
        "opaque": {"revision": 9, "items": [2, None]}}
    assert manager_a.get_library_flags(path) == {"favorite": True, "protected": True}
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM album_members WHERE album_id=? AND clip_id=?",
                          (album.album_id, new_binding.clip_id)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM album_members WHERE album_id=? AND clip_id=?",
                          (album.album_id, old_binding.clip_id)).fetchone()[0] == 1


def test_membership_follows_stable_clip_id_through_rename_trash_restore_then_purge(tmp_path):
    from core.clip_file_operations import ClipFileOperations

    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Lifecycle")
    owned = tmp_path / "library"
    owned.mkdir()
    media = owned / "one.mp4"
    media.write_bytes(b"synthetic lifecycle clip")
    binding = manager.register_media(manager.inspect_media(media))
    identity = dict(manager.inspect_media(media).stat)
    manager.add_album_member(album.album_id, media, expected_identity=identity)
    manager.set(str(media), tag="preserve")
    manager.set_editor_draft(str(media), {"opaque": [None, 3]})
    admission = {"owned_root": str(owned), "imported_roots": (), "allow_linked": False}

    operations = ClipFileOperations(manager)
    renamed = owned / "renamed.mp4"
    rename = operations.prepare_rename(binding.clip_id, renamed, admission=admission)
    assert operations.execute(rename.op_id).succeeded
    assert manager.resolve_media(renamed).clip_id == binding.clip_id
    assert len(manager.album_bindings(album.album_id)) == 1

    operation = operations.prepare_trash(
        binding.clip_id, admission=admission,
        expected_identity=dict(manager.inspect_media(renamed).stat))
    assert operations.execute(operation.op_id).succeeded
    assert manager.album_bindings(album.album_id) == {}
    restore_item = next(item for item in operations.list_trash() if item.clip_id == binding.clip_id)
    restore = operations.prepare_restore(restore_item.trash_id, admission=admission)
    assert operations.execute(restore.op_id).succeeded
    assert len(manager.album_bindings(album.album_id)) == 1
    assert manager.get(restore_item.original_path)["tag"] == "preserve"
    assert manager.get_editor_draft(restore_item.original_path) == {"opaque": [None, 3]}

    purge_trash = operations.prepare_trash(
        binding.clip_id, admission=admission,
        expected_identity=dict(manager.inspect_media(renamed).stat))
    assert operations.execute(purge_trash.op_id).succeeded
    trashed = next(item for item in operations.list_trash() if item.clip_id == binding.clip_id)
    purge = operations.prepare_purge(trashed.trash_id, admission=admission)
    assert operations.execute(purge.op_id).succeeded
    assert manager.album_bindings(album.album_id) == {}
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM album_members WHERE clip_id=?",
                          (binding.clip_id,)).fetchone()[0] == 0
    assert manager.get_by_id(binding.clip_id) is not None  # metadata remains a tombstone


def test_replacement_at_same_path_does_not_inherit_membership(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("Original")
    path, old_binding, identity = _clip(manager, tmp_path)
    manager.add_album_member(album.album_id, path, expected_identity=identity)
    path.unlink()
    path.write_bytes(b"new occupant")
    new_binding = manager.register_media(manager.inspect_media(path))

    assert new_binding.clip_id != old_binding.clip_id
    assert manager.album_bindings(album.album_id) == {}


def test_removing_unregistered_path_never_registers_or_creates_metadata(tmp_path):
    manager = _schema4_manager(tmp_path)
    album = manager.create_album("No implicit registration")
    path = tmp_path / "unregistered.mp4"
    path.write_bytes(b"standalone synthetic file")

    assert manager.remove_album_member(album.album_id, path) is False
    assert manager.get(str(path)) == {"tag": "", "description": ""}
    assert manager.resolve_media(path) is None
    assert manager.album_bindings(album.album_id) == {}
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM entries WHERE path=?",
                          (str(path),)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM clip_files WHERE path=?",
                          (str(path),)).fetchone()[0] == 0


def test_unknown_and_future_catalog_schemas_fail_closed_without_mutating_database(tmp_path):
    _schema4_manager(tmp_path)
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        db.execute("PRAGMA user_version=99")
    reopened = _manager(tmp_path)
    from core.clip_metadata_types import UnsupportedSchemaVersion
    with pytest.raises(UnsupportedSchemaVersion):
        reopened.list_albums()
    with pytest.raises(UnsupportedSchemaVersion):
        reopened.create_album("Must not write")
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 0


def test_schema_four_ddl_failure_rolls_back_schema_three(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.migrate(target_version=3)
    path, binding, _ = _clip(manager, tmp_path)
    manager.set(str(path), tag="before failure")
    before = manager.get_by_id(binding.clip_id)

    def fail_schema_creation(_connection):
        raise sqlite3.OperationalError("injected schema-4 DDL failure")

    monkeypatch.setattr(manager, "_create_album_schema", fail_schema_creation)
    with pytest.raises(OSError, match="injected schema-4 DDL failure"):
        manager.migrate(target_version=4)
    with sqlite3.connect(tmp_path / "metadata.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "albums" not in tables
        assert "album_members" not in tables
    assert manager.get_by_id(binding.clip_id) == before


def test_incompatible_existing_albums_table_aborts_schema_three_upgrade(tmp_path):
    manager = _manager(tmp_path)
    manager.migrate(target_version=3)
    database = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE albums(name TEXT NOT NULL)")
        db.execute("INSERT INTO albums(name) VALUES ('foreign catalog row')")
        incompatible_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='albums'").fetchone()[0]
        incompatible_rows = db.execute("SELECT name FROM albums").fetchall()

    with pytest.raises(OSError):
        manager.migrate(target_version=4)

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='albums'").fetchone()[0] == incompatible_sql
        assert db.execute("SELECT name FROM albums").fetchall() == incompatible_rows
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='album_members'").fetchone() is None

    backups = list(tmp_path.glob("metadata.sqlite3.pre-v4-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='albums'").fetchone()[0] == incompatible_sql
        assert backup.execute("SELECT name FROM albums").fetchall() == incompatible_rows


def test_concurrent_managers_observe_committed_catalog_and_membership(tmp_path):
    first_manager = _schema4_manager(tmp_path)
    second_manager = _manager(tmp_path)
    second_manager.migrate(target_version=4)
    album = first_manager.create_album("Shared")
    path, _, identity = _clip(first_manager, tmp_path)

    second_manager.reload()
    assert second_manager.list_albums() == (album,)
    assert second_manager.add_album_member(album.album_id, path, expected_identity=identity)
    first_manager.reload()
    assert len(first_manager.album_bindings(album.album_id)) == 1


def test_stale_manager_writes_check_fresh_sql_without_resurrection(tmp_path):
    first = _schema4_manager(tmp_path)
    second = _manager(tmp_path)
    second.migrate(target_version=4)
    album = first.create_album("Current")
    second.reload()
    path, _, identity = _clip(first, tmp_path)
    assert first.add_album_member(album.album_id, path, expected_identity=identity)

    # The RAM view is intentionally stale; a mutating operation must still use SQL truth.
    assert second.remove_album_member(album.album_id, path, expected_identity=identity)
    first.reload()
    assert first.album_bindings(album.album_id) == {}
    assert second.create_album("New from stale manager").name == "New from stale manager"
    first.reload()
    assert {item.name for item in first.list_albums()} == {"Current", "New from stale manager"}
