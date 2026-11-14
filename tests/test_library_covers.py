"""Core contracts for immutable library-cover references and assets."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import zlib
from pathlib import Path
from uuid import uuid4

import pytest

from core.clip_metadata_manager import ClipMetadataManager
from core.library_covers import (
    CoverAssetStore,
    CoverCommittedError,
    MAX_COVER_PNG_BYTES,
    cover_reference_from_png,
    cleanup_retired_asset,
    inspect_png,
)


def _png(width=2, height=2, rgba=(220, 30, 40, 255)):
    row = bytes((0,)) + bytes(rgba) * width
    raw = row * height

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _manager(tmp_path: Path, version=5):
    manager = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    manager.migrate(target_version=version)
    return manager


def _media(manager, tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"synthetic media content")
    evidence = manager.inspect_media(path)
    binding = manager.register_media(evidence)
    return path, binding, dict(evidence.stat)


def test_cover_png_validation_and_reference_namespace(tmp_path):
    payload = _png(3, 4)
    assert inspect_png(payload) == (3, 4)
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)

    reference = manager.set_library_cover(
        path, payload, expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    asset = manager.cover_asset_path(binding.clip_id, reference.revision)

    assert asset == tmp_path / "library.sqlite3.covers" / binding.clip_id / (
        reference.revision + ".png")
    assert asset.read_bytes() == payload
    state = manager.cover_bindings()[str(path.resolve()).lower()]
    assert state["clip_id"] == binding.clip_id
    assert state["identity"] == identity
    assert state["reference"] == reference.to_dict()
    assert state["error"] is None


def test_raw_png_limit_is_distinct_from_normalized_limit():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    metadata = b'comment\x00' + b'x' * MAX_COVER_PNG_BYTES
    payload = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
               + chunk(b"tEXt", metadata)
               + chunk(b"IDAT", zlib.compress(bytes((0, 220, 30, 40, 255) * 2 * 2))
                      )
               + chunk(b"IEND", b""))
    assert len(payload) > MAX_COVER_PNG_BYTES
    assert inspect_png(payload) == (2, 2)
    with pytest.raises(ValueError, match="8 MiB"):
        inspect_png(payload, require_normalized=True)


def test_cover_reset_removes_reference_and_owned_asset(tmp_path):
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)
    reference = manager.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    asset = manager.cover_asset_path(binding.clip_id, reference.revision)
    assert manager.clear_library_cover(
        path, expected_identity=identity, expected_clip_id=binding.clip_id,
        expected_cover_revision=reference.revision)
    assert not asset.exists()
    assert manager.cover_bindings()[str(path.resolve()).lower()]["reference"] is None


def test_cover_commit_survives_publish_and_reload_fault(tmp_path):
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)

    def fail_publish(*_args):
        raise RuntimeError('simulated RAM publication failure')

    def fail_reload():
        raise OSError('simulated metadata reload failure')

    manager._publish_primary_reconciliation = fail_publish
    manager.reload = fail_reload
    with pytest.raises(CoverCommittedError) as caught:
        manager.set_library_cover(
            path, _png(), expected_identity=identity,
            expected_clip_id=binding.clip_id, expected_cover_revision=None)
    assert caught.value.committed is True
    with sqlite3.connect(tmp_path / 'library.sqlite3') as database:
        value = database.execute(
            'SELECT value FROM entries WHERE clip_id=?',
            (binding.clip_id,)).fetchone()[0]
    assert json.loads(value)['library_cover']['version'] == 1


def test_cover_writer_revision_is_stale_safe_and_fullsave_preserves_cover(tmp_path):
    first = _manager(tmp_path)
    path, binding, identity = _media(first, tmp_path)
    second = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    reference = first.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    assert second.cover_bindings()[str(path.resolve()).lower()]["reference"] is None

    with pytest.raises(OSError, match="cover changed"):
        second.clear_library_cover(
            path, expected_identity=identity, expected_clip_id=binding.clip_id,
            expected_cover_revision=None)

    first.set(path, description="kept by a private full save")
    assert first.get_by_id(binding.clip_id)["library_cover"] == reference.to_dict()
    second.reload()
    assert second.get_by_id(binding.clip_id)["library_cover"] == reference.to_dict()


def test_same_path_replacement_gets_new_clip_without_inheriting_cover(tmp_path):
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)
    manager.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)

    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement media content")
    os.replace(replacement, path)
    fresh = dict(manager.inspect_media(path).stat)
    with pytest.raises(OSError, match="selected file changed"):
        manager.set_library_cover(
            path, _png(rgba=(20, 40, 220, 255)), expected_identity=identity,
            expected_clip_id=binding.clip_id, expected_cover_revision=None)
    new_reference = manager.set_library_cover(
        path, _png(rgba=(20, 40, 220, 255)), expected_identity=fresh,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    current = manager.resolve_media(path)

    assert current.clip_id != binding.clip_id
    assert manager.get_by_id(current.clip_id)["library_cover"] == new_reference.to_dict()
    assert manager.get_by_id(binding.clip_id)["library_cover"]


def test_cover_bindings_reports_malformed_or_future_reference_without_io(tmp_path):
    manager = _manager(tmp_path)
    path, binding, _ = _media(manager, tmp_path)
    with sqlite3.connect(tmp_path / "library.sqlite3") as database:
        value = {"library_cover": {"version": 99, "revision": "future"}}
        database.execute(
            "UPDATE entries SET value=? WHERE clip_id=?",
            (json.dumps(value), binding.clip_id),
        )
        database.commit()
    manager.reload()
    state = manager.cover_bindings()[str(path.resolve()).lower()]
    assert state["reference"] is None
    assert "unsupported" in state["error"]
    with pytest.raises(ValueError, match="unsupported library cover"):
        manager.set_library_cover(
            path, _png(), expected_identity=dict(manager.inspect_media(path).stat),
            expected_clip_id=binding.clip_id, expected_cover_revision=None)


def test_cover_writes_require_schema_five(tmp_path):
    manager = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    manager.migrate(target_version=4)
    path, binding, identity = _media(manager, tmp_path)
    with pytest.raises(RuntimeError, match="cover schema"):
        manager.set_library_cover(
            path, _png(), expected_identity=identity,
            expected_clip_id=binding.clip_id, expected_cover_revision=None)


def test_cover_schema_six_supports_set_clear_and_cleanup(tmp_path):
    manager = _manager(tmp_path, version=6)
    path, binding, identity = _media(manager, tmp_path)
    reference = manager.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    assert manager.backup_cover_root == tmp_path / "library.sqlite3.covers"
    asset = manager.cover_asset_path(binding.clip_id, reference.revision)
    assert asset.exists()
    assert manager.clear_library_cover(
        path, expected_identity=identity, expected_clip_id=binding.clip_id,
        expected_cover_revision=reference.revision)
    assert not asset.exists()


def test_exclusive_cover_publish_never_replaces_existing_asset(tmp_path):
    store = CoverAssetStore(tmp_path / "library.sqlite3")
    payload = _png()
    clip_id = str(uuid4())
    reference = cover_reference_from_png(payload, revision=str(uuid4()))
    first, _ = store.stage_png(payload)
    store.publish_exclusive(first, clip_id, reference)
    second, _ = store.stage_png(payload)
    with pytest.raises(FileExistsError):
        store.publish_exclusive(second, clip_id, reference)


def test_retired_asset_cleanup_checks_all_entry_references(tmp_path):
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)
    reference = manager.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    asset = manager.cover_asset_path(binding.clip_id, reference.revision)
    assert not cleanup_retired_asset(tmp_path / "library.sqlite3", binding.clip_id, reference)
    assert asset.exists()

    with sqlite3.connect(tmp_path / "library.sqlite3") as database:
        database.execute("UPDATE entries SET value='{}' WHERE clip_id=?", (binding.clip_id,))
        database.commit()
    assert cleanup_retired_asset(tmp_path / "library.sqlite3", binding.clip_id, reference)
    assert not asset.exists()


def test_retired_asset_cleanup_keeps_replacement_during_validation(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    path, binding, identity = _media(manager, tmp_path)
    reference = manager.set_library_cover(
        path, _png(), expected_identity=identity,
        expected_clip_id=binding.clip_id, expected_cover_revision=None)
    asset = manager.cover_asset_path(binding.clip_id, reference.revision)
    with sqlite3.connect(tmp_path / 'library.sqlite3') as database:
        database.execute(
            'UPDATE entries SET value=\'{}\' WHERE clip_id=?',
            (binding.clip_id,))
        database.commit()
    replacement = tmp_path / 'replacement-cover.png'
    replacement.write_bytes(_png(rgba=(20, 40, 220, 255)))

    import core.library_covers as covers

    original_inspect = covers.inspect_png

    def replace_during_inspect(payload, *, require_normalized=False):
        os.replace(replacement, asset)
        return original_inspect(payload, require_normalized=require_normalized)

    monkeypatch.setattr(covers, 'inspect_png', replace_during_inspect)
    assert not cleanup_retired_asset(
        tmp_path / 'library.sqlite3', binding.clip_id, reference)
    assert asset.exists()
    assert asset.read_bytes() == _png(rgba=(20, 40, 220, 255))


def test_cover_asset_store_rejects_symlink_namespace(tmp_path):
    database = tmp_path / "library.sqlite3"
    root = Path(str(database) + ".covers")
    target = tmp_path / "outside"
    target.mkdir()
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    store = CoverAssetStore(database)
    with pytest.raises(OSError, match="reparse points"):
        store.stage_png(_png())
