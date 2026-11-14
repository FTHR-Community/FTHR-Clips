"""Schema-6 metadata snapshot and restore-receipt contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from core.clip_metadata_manager import ClipMetadataManager, UnsupportedSchemaVersion


def _manager(tmp_path: Path, version=6):
    manager = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    manager.migrate(target_version=version)
    return manager


def _media(manager, root, name="clip.mp4"):
    path = root / name
    path.write_bytes(b"restore metadata media")
    evidence = manager.inspect_media(path)
    return path, manager.register_media(evidence), dict(evidence.stat)


def test_schema5_to6_preserves_catalog_and_adds_receipts(tmp_path):
    manager = _manager(tmp_path, version=5)
    path, binding, identity = _media(manager, tmp_path)
    manager.set(path, tag="catalog", description="kept")
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute(
            "UPDATE entries SET value=? WHERE clip_id=?",
            ('{"tag":"catalog","description":"kept","unknown":{"kept":true}}',
             binding.clip_id),
        )
    album = manager.create_album("Saved clips")
    manager.add_album_member(album.album_id, path, expected_identity=identity)
    manager.create_saved_search(
        "Catalog search",
        {"version": 1, "text": "catalog", "tags_all": [],
         "album_id": album.album_id, "kind": "clips",
         "favorites_only": False, "sort": "newest"},
    )
    result = manager.migrate(target_version=6)

    assert result.to_version == 6
    assert manager.get_by_id(binding.clip_id)["tag"] == "catalog"
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute(
            "SELECT COUNT(*) FROM restore_receipts").fetchone() == (0,)
    snapshot = manager.export_backup_snapshot()
    assert snapshot["schema_version"] == 6
    assert snapshot["entries"][0]["value"]["unknown"] == {"kept": True}
    assert snapshot["albums"] and snapshot["memberships"]
    assert snapshot["searches"][0]["criteria"]["album_id"] == album.album_id


def test_schema5_wal_trash_rows_survive_additive_schema6_migration(tmp_path):
    manager = _manager(tmp_path, version=5)
    path, binding, identity = _media(manager, tmp_path)
    trash_id = str(uuid4())
    stored = tmp_path / ".fthr-trash" / trash_id / path.name
    stored.parent.mkdir(parents=True)
    stored.write_bytes(path.read_bytes())
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE entries SET path=NULL WHERE clip_id=?", (binding.clip_id,))
        connection.execute(
            "UPDATE clip_files SET path=?,path_key=?,binding_state='TRASHED' WHERE clip_id=?",
            (str(stored), manager._path_key(stored), binding.clip_id),
        )
        connection.execute(
            "INSERT INTO trash_items VALUES (?,?,?,?,?,?,?,NULL)",
            (trash_id, binding.clip_id, "TRASHED", "2026-10-21T00:00:00+00:00",
             str(path), str(stored), "[]"),
        )
        connection.commit()
    manager.migrate(target_version=6)
    item = manager.get_trash_item(trash_id)
    assert item is not None and item.status.value == "TRASHED"
    snapshot = manager.export_backup_snapshot()
    assert snapshot["trash"][0]["members"] == []
    assert snapshot["files"][0]["binding_state"] == "TRASHED"


def test_schema6_snapshot_rejects_unfinished_journal_and_purging(tmp_path):
    manager = _manager(tmp_path)
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute(
            "INSERT INTO file_operations VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid4()), 1, "rename", str(uuid4()), "PREPARED", "{}", None, 0),
        )
        connection.commit()
    with pytest.raises(OSError, match="unfinished"):
        manager.export_backup_snapshot()


def test_restore_receipt_commit_replay_and_foreign_digest(tmp_path):
    manager = _manager(tmp_path)
    destination = tmp_path / "restored"
    destination.mkdir()
    path = destination / "new.mp4"
    path.write_bytes(b"new restored media")
    identity = dict(manager.inspect_media(path).stat)
    clip_id, file_id = str(uuid4()), str(uuid4())
    records = {
        "entries": [{"clip_id": clip_id, "path": str(path),
                     "value": {"tag": "new", "unknown": [1, 2, 3]}}],
        "files": [{"file_id": file_id, "clip_id": clip_id, "role": "PRIMARY",
                   "path": str(path), "path_key": manager._path_key(path),
                   "binding_state": "STAT_BOUND", "identity": identity,
                   "content_sha256": None, "provenance": "backup restore"}],
        "trash": [], "albums": [], "memberships": [], "searches": [],
    }
    restore_uuid = str(uuid4())
    first = manager.commit_backup_restore(
        restore_uuid, "a" * 64, destination, records, owned_root=tmp_path)
    assert first["already_imported"] is False
    receipt = manager.get_restore_receipt(restore_uuid)
    assert receipt["manifest_sha256"] == "a" * 64
    replay = manager.commit_backup_restore(
        restore_uuid, "a" * 64, destination, records, owned_root=tmp_path)
    assert replay["already_imported"] is True
    with pytest.raises(ValueError, match="different content"):
        manager.commit_backup_restore(
            restore_uuid, "b" * 64, destination, records, owned_root=tmp_path)
    assert manager.get_by_id(clip_id)["unknown"] == [1, 2, 3]


def test_restore_receipt_transaction_rolls_back_on_insert_failure(tmp_path):
    manager = _manager(tmp_path)
    destination = tmp_path / "restored"
    destination.mkdir()
    path = destination / "new.mp4"
    path.write_bytes(b"new restored media")
    identity = dict(manager.inspect_media(path).stat)
    clip_id, file_id = str(uuid4()), str(uuid4())
    records = {
        "entries": [{"clip_id": clip_id, "path": str(path), "value": {}}],
        "files": [{"file_id": file_id, "clip_id": clip_id, "role": "PRIMARY",
                   "path": str(path), "path_key": manager._path_key(path),
                   "binding_state": "STAT_BOUND", "identity": identity,
                   "content_sha256": None, "provenance": "backup restore"}],
        "trash": [], "albums": [], "memberships": [], "searches": [],
    }
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute(
            "CREATE TRIGGER reject_restore BEFORE INSERT ON entries "
            "BEGIN SELECT RAISE(ABORT, 'restore insert failure'); END")
        connection.commit()
    with pytest.raises(OSError, match="restore insert failure"):
        manager.commit_backup_restore(
            str(uuid4()), "c" * 64, destination, records, owned_root=tmp_path)
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM restore_receipts").fetchone() == (0,)


def test_schema6_missing_receipt_table_fails_closed(tmp_path):
    manager = _manager(tmp_path)
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute("DROP TABLE restore_receipts")
        connection.commit()
    with pytest.raises(OSError, match="restore receipt"):
        manager.export_backup_snapshot()


def test_schema6_ddl_failure_rolls_back_version_and_receipt_table(tmp_path):
    manager = _manager(tmp_path, version=5)

    def fail_schema(_connection):
        raise sqlite3.OperationalError("injected receipt DDL failure")

    manager._create_restore_receipt_schema = fail_schema
    with pytest.raises(OSError, match="receipt DDL failure"):
        manager.migrate(target_version=6)
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='restore_receipts'").fetchone() is None


def test_schema7_is_future_and_never_treated_as_empty(tmp_path):
    _manager(tmp_path)
    with sqlite3.connect(tmp_path / "library.sqlite3") as connection:
        connection.execute("PRAGMA user_version=7")
        connection.commit()
    future = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    with pytest.raises(UnsupportedSchemaVersion):
        future.migrate(target_version=6)
