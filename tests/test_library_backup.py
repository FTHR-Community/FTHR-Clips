"""Pure contract tests for the declarative library backup service."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from core.library_backup import (
    BackupOptions,
    BackupProgress,
    LibraryBackupService,
    VerifiedBackup,
)
from core.clip_files import PENDING_RESTORE_MARKER, is_pending_restore_directory
from core.library_index import scan_library


class _Manager:
    def __init__(self, root, media):
        self._database = root / "library.sqlite3"
        self._cover_root = root / "library.sqlite3.covers"
        self.media = media
        self.clip_id = str(uuid4())
        self.file_id = str(uuid4())
        info = os.stat(media, follow_symlinks=False)
        self._snapshot = {
            "schema_version": 6,
            "entries": [{"clip_id": self.clip_id, "path": str(media),
                          "value": {"tag": "kept", "unknown": {"x": True}}}],
            "files": [{
                "file_id": self.file_id, "clip_id": self.clip_id, "role": "PRIMARY",
                "path": str(media), "path_key": os.path.normcase(os.path.abspath(media)),
                "binding_state": "CONTENT_VERIFIED",
                "identity": {"st_dev": info.st_dev, "st_ino": info.st_ino,
                              "size": info.st_size, "mtime_ns": info.st_mtime_ns},
                "content_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "provenance": "test",
            }],
            "trash": [], "albums": [], "memberships": [], "searches": [],
        }

    @property
    def backup_cover_root(self):
        return self._cover_root

    def export_backup_snapshot(self):
        return json.loads(json.dumps(self._snapshot))

    def cover_asset_path_for_read(self, _clip_id, _revision):
        return self._cover_root / "missing" / "missing.png"

    def get_restore_receipt(self, _restore_uuid):
        return None

    def reload(self):
        return None

    def commit_backup_restore(self, restore_uuid, manifest_sha256, destination_root,
                              records, **_kwargs):
        return {"already_imported": False, "restore_uuid": restore_uuid,
                "manifest_sha256": manifest_sha256, "destination_root": destination_root,
                "clip_ids": [row["clip_id"] for row in records["entries"]],
                "file_ids": [row["file_id"] for row in records["files"]]}

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _service(tmp_path, *, reserve=None, imported_roots=()):
    root = tmp_path / "library"
    root.mkdir()
    media = root / "clip.mp4"
    media.write_bytes(b"backup test media")
    return LibraryBackupService(_Manager(root, media), root,
                                imported_roots=imported_roots, reserve=reserve), root


def test_create_and_verify_are_complete_and_hash_addressed(tmp_path):
    service, root = _service(tmp_path)
    result = service.create_backup(tmp_path / "package")
    verified = service.verify_backup(result.package_path)

    assert result.completed is True
    assert (Path(result.package_path) / "complete.json").is_file()
    assert verified.manifest["objects"]
    assert all(row["relative_path"].startswith("objects/")
               for row in verified.manifest["objects"])
    assert verified.manifest["source_root"]["identity"]["st_ino"] == os.stat(
        root, follow_symlinks=False).st_ino


def test_manifest_rejects_duplicate_json_keys_and_nonfinite_values():
    with pytest.raises(ValueError, match="duplicate"):
        LibraryBackupService._strict_json(b'{"a":1,"a":2}')
    with pytest.raises(ValueError, match="non-finite"):
        LibraryBackupService._validate_json(float("inf"))


@pytest.mark.parametrize("version", [True, 1.0, 2])
def test_owned_manifest_format_version_requires_exact_integer(version):
    with pytest.raises(ValueError, match="version"):
        LibraryBackupService._validate_manifest({"format_version": version})


@pytest.mark.parametrize("excluded", [None, "bad", 1, [{"path": "x"}],
                                       [{"path": "x", "reason": 1}]])
def test_manifest_rejects_malformed_exclusions(excluded):
    with pytest.raises(ValueError, match="excluded|exclusion"):
        LibraryBackupService._validate_manifest({
            "format_version": 1, "backup_uuid": str(uuid4()),
            "created_utc": "2026-01-01T00:00:00+00:00",
            "source_root": {"identity": {"st_dev": 1, "st_ino": 1,
                                             "size": 0, "mtime_ns": 0}},
            "options": {"include_imported": False, "include_trash": True},
            "entries": [], "files": [], "trash": [],
            "albums": [], "memberships": [], "searches": [], "covers": [],
            "assets": [], "objects": [], "excluded": excluded,
        })


def test_restore_preflight_manifest_digest_mismatch_writes_nothing(tmp_path, monkeypatch):
    service, root = _service(tmp_path)
    package = tmp_path / "package"
    destination = root / "restored"
    verified = VerifiedBackup(str(package), "a" * 64, {})
    monkeypatch.setattr(service, "verify_backup", lambda *_args, **_kwargs: verified)

    with pytest.raises(OSError, match="manifest changed"):
        service.restore_backup(
            package, destination, expected_manifest_sha256="b" * 64)
    assert not destination.exists()
    assert not any(root.glob(".fthr-restore-*"))


def test_cancel_keeps_a_recognizable_checkpoint(tmp_path):
    service, _root = _service(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        service.create_backup(tmp_path / "cancelled", cancel=cancel)
    checkpoint = tmp_path / "cancelled" / ".fthr-backup-state.json"
    assert checkpoint.is_file()
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert state["kind"] == "backup"
    assert state["phase"] == "preparing"


def test_reservation_failure_aborts_without_completion(tmp_path):
    calls = []
    service, _root = _service(tmp_path, reserve=lambda paths: calls.append(tuple(paths)) or False)
    with pytest.raises(OSError, match="busy"):
        service.create_backup(tmp_path / "busy")
    assert calls
    assert not (tmp_path / "busy" / "complete.json").exists()


def test_progress_is_frozen_and_reports_copy_phase():
    progress = BackupProgress("copy", 1, 2, 4, "object")
    assert progress.phase == "copy"
    with pytest.raises((AttributeError, TypeError)):
        progress.phase = "verify"
    assert BackupOptions() == BackupOptions(False, True)


def test_pending_restore_root_is_hidden_until_its_marker_is_removed(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    media = root / "pending.mp4"
    media.write_bytes(b"pending")
    (root / PENDING_RESTORE_MARKER).write_text(
        json.dumps({"format_version": 1, "restore_uuid": str(uuid4()),
                    "manifest_sha256": "0" * 64}), encoding="utf-8")

    assert scan_library(root).records == ()
    (root / PENDING_RESTORE_MARKER).unlink()
    assert [record.path for record in scan_library(root).records] == [str(media)]


def test_pending_restore_marker_permission_is_fail_closed(tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError("marker access denied")

    monkeypatch.setattr("core.clip_files.os.lstat", denied)
    assert is_pending_restore_directory(tmp_path) is True


def test_source_map_rejects_technical_media_path(tmp_path):
    service, _root = _service(tmp_path)
    with pytest.raises(OSError, match="technical"):
        service._source_evidence_class(
            tmp_path / ".fthr-restore-deadbeef" / "clip.mp4", {"covers": []}, False)


def test_resume_rejects_foreign_checkpoint_before_package_access(tmp_path):
    service, root = _service(tmp_path)
    restore_uuid = str(uuid4())
    foreign = tmp_path / f".fthr-restore-{restore_uuid}.json"
    foreign.write_text(json.dumps({
        "format_version": 1, "kind": "restore", "restore_uuid": restore_uuid,
        "checkpoint_path": str(foreign), "stage_root": str(root / "stage"),
        "destination_root": str(root / "restored"), "phase": "copying",
        "manifest_sha256": "0" * 64,
    }), encoding="utf-8")
    with pytest.raises(OSError, match="owned UUID checkpoint"):
        service.resume_restore(foreign)


def test_restore_stage_scratch_is_outside_published_payload(tmp_path):
    service, root = _service(tmp_path)
    package = tmp_path / "package"
    (package / "objects").mkdir(parents=True)
    payload = b"scratch boundary"
    digest = hashlib.sha256(payload).hexdigest()
    source = package / "objects" / digest
    source.write_bytes(payload)
    stage = root / "restore-stage"
    stage.mkdir()
    published_payload = stage / "payload"
    published_payload.mkdir()
    target = published_payload / "media" / "clip.mp4"

    service._stage_object(package, digest, target, published_payload, None)

    assert (stage / ".scratch").is_dir()
    assert not (published_payload / ".scratch").exists()
    assert target.read_bytes() == payload


def test_restore_staged_copy_preserves_source_mtime_for_fresh_identity(tmp_path):
    service, root = _service(tmp_path)
    payload = root / "restore-stage" / "payload" / "media"
    payload.mkdir(parents=True)
    old_path = payload / "old.mp4"
    new_path = payload / "new.mp4"
    old_path.write_bytes(b"older restored media")
    new_path.write_bytes(b"newer restored media")
    historical = ((old_path, 1_700_000_001_234_567_800),
                  (new_path, 1_800_000_001_234_567_800))
    observed = {}
    for path, source_mtime in historical:
        os.utime(path, ns=(source_mtime - 1, source_mtime - 1))
        source_identity = service._identity(path)
        source_identity["mtime_ns"] = source_mtime
        expected_identity = service._exclusive_identity(path)
        observed[path] = service._preserve_staged_mtime(
            path, source_identity, expected_identity)

    assert observed[old_path]["mtime_ns"] < observed[new_path]["mtime_ns"]
    records = {record.path: record for record in scan_library(
        root, sort_="oldest").records}
    assert records[str(old_path)].mtime_ns < records[str(new_path)].mtime_ns
    for path, identity in observed.items():
        assert records[str(path)].file_identity == identity


def test_restore_staged_mtime_accepts_filesystem_timestamp_quantization(tmp_path,
                                                                          monkeypatch):
    service, root = _service(tmp_path)
    staged = root / "quantized.mp4"
    staged.write_bytes(b"quantized restore")
    expected = service._exclusive_identity(staged)
    source_identity = dict(expected)
    source_identity["mtime_ns"] = 1_700_000_001_234_567_851
    original_set_mtime = service._set_mtime_nofollow

    def quantized_set_mtime(path, timestamp, expected_identity):
        return original_set_mtime(path, (timestamp // 100) * 100, expected_identity)

    monkeypatch.setattr(service, "_set_mtime_nofollow", quantized_set_mtime)
    observed = service._preserve_staged_mtime(staged, source_identity, expected)

    assert observed["mtime_ns"] == os.stat(staged, follow_symlinks=False).st_mtime_ns
    assert observed["mtime_ns"] % 100 == 0


def test_copy_object_keeps_replaced_temp_leaf_instead_of_deleting_foreign_bytes(
        tmp_path, monkeypatch):
    service, root = _service(tmp_path)
    source = root / "source.mp4"
    source.write_bytes(b"owned source")
    destination = tmp_path / "package" / "objects" / "object.bin"
    captured = {}

    def replace_with_foreign(temp, _destination):
        captured["path"] = Path(temp)
        captured["path"].unlink()
        captured["path"].write_bytes(b"foreign replacement")
        raise OSError("simulated publication interruption")

    monkeypatch.setattr("core.library_backup.rename_no_replace", replace_with_foreign)
    with pytest.raises(OSError, match="publication interruption"):
        service._copy_object(source, destination, service._identity(source), None,
                             expected_digest=hashlib.sha256(source.read_bytes()).hexdigest())

    assert captured["path"].read_bytes() == b"foreign replacement"
    assert not destination.exists()


def test_cold_restore_hardlink_stage_is_rejected_without_touching_alias(tmp_path):
    service, root = _service(tmp_path)
    package = tmp_path / "package"
    (package / "objects").mkdir(parents=True)
    payload = b"cold stage bytes"
    digest = hashlib.sha256(payload).hexdigest()
    (package / "objects" / digest).write_bytes(payload)
    external = tmp_path / "external.mp4"
    external.write_bytes(payload)
    staged = root / "stage" / "media" / "clip.mp4"
    staged.parent.mkdir(parents=True)
    try:
        os.link(external, staged)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable on this filesystem")
    before = service._identity(external)

    with pytest.raises(OSError, match="hardlink"):
        service._stage_object(package, digest, staged, root / "stage", None)

    assert external.read_bytes() == payload
    assert service._identity(external) == before


def test_replaced_stage_leaf_is_rejected_before_timestamp_update(tmp_path):
    service, root = _service(tmp_path)
    staged = root / "replace.mp4"
    staged.write_bytes(b"original staged")
    expected = service._exclusive_identity(staged)
    source_identity = dict(expected)
    source_identity["mtime_ns"] += 1000
    replacement = root / "replacement.mp4"
    replacement.write_bytes(b"original staged")
    os.replace(replacement, staged)
    replacement_identity = service._identity(staged)

    with pytest.raises(OSError, match="identity changed"):
        service._preserve_staged_mtime(staged, source_identity, expected)

    assert service._identity(staged) == replacement_identity


def test_commit_exception_with_matching_receipt_is_durable(tmp_path, monkeypatch):
    service, root = _service(tmp_path)
    destination = root / "restored"
    restore_uuid = str(uuid4())
    digest = "a" * 64
    receipt = {"restore_uuid": restore_uuid, "manifest_sha256": digest,
               "destination_root": str(destination)}

    def commit(*_args, **_kwargs):
        raise OSError("connection closed after commit")

    monkeypatch.setattr(service.manager, "commit_backup_restore", commit)
    monkeypatch.setattr(service.manager, "get_restore_receipt", lambda _uuid: receipt)
    reloaded = []
    monkeypatch.setattr(service.manager, "reload", lambda: reloaded.append(True))

    committed, note = service._commit_restore_records(
        restore_uuid, digest, destination, {}, None)

    assert committed["already_imported"] is True
    assert reloaded == [True]
    assert "committed" in note


def test_stable_root_identity_ignores_directory_mtime_changes():
    assert LibraryBackupService._same_stable_identity(
        {"st_dev": 4, "st_ino": 9}, {"st_dev": 4, "st_ino": 9})


def test_publish_stage_rejects_replaced_destination_parent_before_move(tmp_path):
    service, root = _service(tmp_path)
    stage_root = root / "restore-stage"
    stage_payload = stage_root / "payload"
    stage_payload.mkdir(parents=True)
    (stage_payload / "media.mp4").write_bytes(b"staged")
    destination_parent = root / "destination-parent"
    destination_parent.mkdir()
    expected_destination_parent = service._stable_directory_identity(destination_parent)
    moved_parent = root / "destination-parent-original"
    destination_parent.rename(moved_parent)
    destination_parent.mkdir()
    destination = destination_parent / "restored"

    with pytest.raises(OSError, match="parent|identity"):
        service._publish_stage(
            stage_payload, destination,
            expected_payload_identity=service._stable_directory_identity(stage_payload),
            expected_parent_identity=service._stable_directory_identity(stage_payload.parent),
            expected_destination_parent_identity=expected_destination_parent)

    assert stage_payload.is_dir()
    assert moved_parent.is_dir()
    assert not destination.exists()


def test_unregistered_walk_excludes_private_and_cover_namespaces(tmp_path):
    service, root = _service(tmp_path)
    loose = root / "loose.mp4"
    loose.write_bytes(b"loose")
    private = root / ".fthr"
    private.mkdir()
    (private / "migration.mp4").write_bytes(b"private")
    cover_root = service.manager.backup_cover_root
    cover_root.mkdir()
    (cover_root / "clip.png").write_bytes(b"cover")

    rows = service._walk_unregistered(
        [service.manager.media], BackupOptions(), None)

    assert [Path(row["source_path"]) for row in rows] == [loose]


def test_unregistered_imported_media_and_sidecar_keep_imported_source_kind(tmp_path):
    imported = tmp_path / "imported"
    imported.mkdir()
    media = imported / "foreign.mp4"
    media.write_bytes(b"imported media")
    sidecar = media.with_name(media.name + ".fthr-audio.json")
    sidecar.write_text(json.dumps({
        "manifest_version": 1,
        "transaction_id": str(uuid4()),
        "media_file": media.name,
        "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        "sources": [{
            "source_uuid": str(uuid4()), "stream_index": 1,
            "source_type": "application", "display_name": "Imported App",
            "persistent_identity": "imported-source", "icon_reference": None,
            "first_active_100ns": 0, "last_active_100ns": 1,
            "sample_rate": 48000, "channels": 2, "sample_format": "float32",
        }],
    }), encoding="utf-8")
    service, _root = _service(tmp_path, imported_roots=(imported,))

    manifest, checkpoint = service._build_manifest(
        tmp_path / "package", BackupOptions(include_imported=True), None, None)

    rows = [row for row in manifest["files"] if row["provenance"] in {
        "unregistered owned media", "validated audio sidecar"}]
    assert {row["role"] for row in rows} == {"PRIMARY", "SIDECAR"}
    assert all(row["relative_path"].startswith("media/") for row in rows)
    assert all(row["source_identity"]["size"] > 0 for row in rows)
    imported_sources = [item for item in checkpoint["source_map"].values()
                        if str(imported) in item["path"]]
    assert {item["kind"] for item in imported_sources} == {"imported"}
    assert {Path(item["path"]).read_bytes() for item in imported_sources} == {
        media.read_bytes(), sidecar.read_bytes()}


def test_manifest_trash_paths_require_direct_canonical_bundle(tmp_path):
    service, _root = _service(tmp_path)
    manifest, _checkpoint = service._build_manifest(
        tmp_path / "package", BackupOptions(), None, None)
    manifest = json.loads(json.dumps(manifest))
    clip_id = manifest["entries"][0]["clip_id"]
    file_row = manifest["files"][0]
    trash_id = str(uuid4())
    direct = f".fthr-trash/{trash_id}/clip.mp4"
    file_row["relative_path"] = direct
    file_row["path_key"] = direct
    file_row["binding_state"] = "TRASHED"
    manifest["entries"][0]["path"] = None
    member = {key: file_row[key] for key in (
        "file_id", "role", "relative_path", "binding_state", "identity",
        "content_sha256", "provenance")}
    manifest["trash"] = [{
        "trash_id": trash_id, "clip_id": clip_id, "status": "TRASHED",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "original_path": f"media/{clip_id}/clip.mp4", "stored_path": direct,
        "members": [member], "error": None,
    }]
    LibraryBackupService._validate_manifest(manifest)

    nested = f"media/nested/{direct}"
    manifest["files"][0]["relative_path"] = nested
    manifest["files"][0]["path_key"] = nested
    manifest["trash"][0]["stored_path"] = nested
    manifest["trash"][0]["members"][0]["relative_path"] = nested
    with pytest.raises(ValueError, match="canonical|escaped"):
        LibraryBackupService._validate_manifest(manifest)


def test_restore_missing_saved_search_album_gets_deterministic_missing_id(tmp_path):
    service, root = _service(tmp_path)
    old_clip = str(uuid4())
    old_file = str(uuid4())
    old_search = str(uuid4())
    restore_uuid = str(uuid4())
    destination = root / "restored"
    target = destination / "media" / str(uuid5(UUID(restore_uuid), "clip:" + old_clip))
    target.mkdir(parents=True)
    media = target / "clip.mp4"
    media.write_bytes(b"restored")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    manifest = {
        "entries": [{"clip_id": old_clip, "value": {}}],
        "files": [{"file_id": old_file, "clip_id": old_clip, "role": "PRIMARY",
                    "relative_path": f"media/{old_clip}/clip.mp4",
                    "object_sha256": digest, "provenance": "test"}],
        "trash": [], "albums": [], "memberships": [],
        "searches": [{"search_id": old_search, "name": "missing",
                       "created_utc": "2026-01-01T00:00:00+00:00",
                       "criteria": {"album_id": "source-album"}}],
        "covers": [], "objects": [], "assets": [], "excluded": [],
    }
    verified = type("Verified", (), {"manifest": manifest})()

    records, _clip_map = service._restore_records(
        verified, restore_uuid, str(destination), str(destination), None,
        copy_objects=False)

    expected = str(uuid5(UUID(restore_uuid), "missing-album:source-album"))
    assert records["searches"][0]["criteria"]["album_id"] == expected
    assert records["searches"][0]["criteria"]["album_id"] != "source-album"
