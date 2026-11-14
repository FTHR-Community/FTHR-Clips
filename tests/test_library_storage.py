"""Pure storage accounting and policy tests; no Qt or application startup."""

from __future__ import annotations

import os
import time
import errno
import stat
from types import SimpleNamespace

import pytest

import core.library_storage as storage
from core.library_storage import (
    StoragePolicy,
    StoragePolicyError,
    build_storage_inventory,
    make_preview,
    policy_for_root,
    quota_admission,
    select_storage_candidates,
    _walk_files,
)


def _write(path, size, byte=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(byte * size)
    old = time.time() - 120
    os.utime(path, (old, old))


def test_default_and_future_policies_fail_closed_without_mutating_settings(tmp_path):
    settings = {"storage_policies": {
        os.path.normcase(os.path.realpath(tmp_path)): {
            "version": 99, "max_library_bytes": 1,
            "automatic_cleanup": True,
        }
    }}
    assert policy_for_root(settings, tmp_path).max_library_bytes == 0
    assert settings["storage_policies"][os.path.normcase(os.path.realpath(tmp_path))]["version"] == 99
    with pytest.raises(StoragePolicyError):
        StoragePolicy.from_mapping({"version": 1, "automatic_cleanup": 1})


def test_inventory_counts_trash_other_and_deduplicates_hardlinks(tmp_path):
    _write(tmp_path / "Desktop" / "old.mp4", 7)
    _write(tmp_path / ".fthr-trash" / "trash-id" / "old.mp4", 11)
    _write(tmp_path / "Exported" / "copy.mp4", 13)
    shared = tmp_path / "Shared" / "alias.mp4"
    shared.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp_path / "Desktop" / "old.mp4", shared)
    except OSError as error:
        if (error.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
                or getattr(error, "winerror", None) in {1, 50}):
            pytest.skip("hardlinks are unavailable on this filesystem")
        raise
    inventory = build_storage_inventory(tmp_path, free_bytes=False)
    assert inventory.active_bytes == 7
    assert inventory.trash_bytes == 11
    assert inventory.other_bytes == 13
    assert inventory.total_bytes == 31
    assert len(inventory.files) == 3


def test_inventory_never_follows_symlinks(tmp_path):
    _write(tmp_path / "outside.mp4", 19)
    (tmp_path / "inside").mkdir()
    try:
        (tmp_path / "inside" / "linked.mp4").symlink_to(tmp_path / "outside.mp4")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this filesystem")
    inventory = build_storage_inventory(tmp_path, free_bytes=False)
    assert all(not item.path.endswith("linked.mp4") for item in inventory.files)


def test_inventory_uses_authoritative_identity_when_direntry_ids_are_zero(tmp_path, monkeypatch):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    _write(first, 7)
    _write(second, 11)
    real_scandir = os.scandir

    class _ZeroIdentityEntry:
        def __init__(self, entry):
            self._entry = entry
            self.path = entry.path
            self.name = entry.name

        def is_symlink(self):
            return self._entry.is_symlink()

        def stat(self, *, follow_symlinks=False):
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(
                st_mode=info.st_mode, st_file_attributes=0,
                st_dev=0, st_ino=0, st_nlink=0,
                st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
            )

    class _Scan:
        def __init__(self, path):
            with real_scandir(path) as entries:
                self._entries = [_ZeroIdentityEntry(entry) for entry in entries]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(os, "scandir", lambda path: _Scan(path))
    inventory = build_storage_inventory(tmp_path, free_bytes=False)
    assert inventory.complete
    assert inventory.active_bytes == 18
    assert {item.path for item in inventory.files} == {str(first), str(second)}


def test_queued_directory_replacement_is_incomplete_and_not_traversed(tmp_path, monkeypatch):
    child = tmp_path / "nested"
    child.mkdir()
    _write(child / "clip.mp4", 9)
    real_stat = os.stat
    replaced = False

    def stat_with_replacement(path, *, follow_symlinks=True):
        nonlocal replaced
        if os.fspath(path) == str(child):
            if not replaced:
                replaced = True
                return real_stat(path, follow_symlinks=follow_symlinks)
            return SimpleNamespace(
                st_mode=stat.S_IFLNK, st_file_attributes=0,
                st_dev=1, st_ino=2, st_nlink=1,
                st_size=0, st_mtime_ns=0,
            )
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", stat_with_replacement)
    rows = tuple(_walk_files(tmp_path))
    assert any(path == str(child) and info is None for path, info, _ in rows)
    assert not any(path.endswith("clip.mp4") and info is not None for path, info, _ in rows)


def test_preview_is_bounded_oldest_first_and_requires_complete_inventory(tmp_path):
    first = tmp_path / "Desktop" / "first.mp4"
    second = tmp_path / "Desktop" / "second.mp4"
    _write(first, 10)
    _write(second, 20)
    snapshot = {"by_path": {
        os.path.normcase(os.path.realpath(first)): {
            "clip_id": "a", "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
        os.path.normcase(os.path.realpath(second)): {
            "clip_id": "b", "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
    }}
    inventory = build_storage_inventory(tmp_path, metadata_snapshot=snapshot, free_bytes=False)
    preview = make_preview(inventory, StoragePolicy(max_library_bytes=1))
    assert [item.path for item in preview.candidates] == [str(first), str(second)]
    assert len(preview.candidates) <= 20
    incomplete = inventory.__class__(
        inventory.root, inventory.root_identity, inventory.files,
        inventory.active_bytes, inventory.trash_bytes, inventory.companion_bytes,
        inventory.other_bytes, inventory.imported_bytes, inventory.cover_bytes,
        inventory.free_bytes, False, ("permission denied",), inventory.scanned_at)
    assert select_storage_candidates(incomplete, StoragePolicy(max_library_bytes=1)) == ()


def test_legacy_by_path_hint_is_manual_primary_but_never_automatic(tmp_path):
    clip = tmp_path / "Desktop" / "legacy.mp4"
    _write(clip, 10)
    hint = {"by_path": {
        os.path.normcase(os.path.realpath(clip)): {
            "clip_id": "legacy-hint",
            "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
    }}
    manual_policy = StoragePolicy(max_library_bytes=1)
    inventory = build_storage_inventory(
        tmp_path, metadata_snapshot=hint, free_bytes=False)
    assert [item.path for item in make_preview(inventory, manual_policy).candidates] == [str(clip)]
    automatic_policy = StoragePolicy(max_library_bytes=1, automatic_cleanup=True)
    assert make_preview(inventory, automatic_policy, automatic=True).candidates == ()

    bad_role = {"by_path": {
        os.path.normcase(os.path.realpath(clip)): {
            "clip_id": "legacy-hint",
            "role": "UNKNOWN", "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
    }}
    blocked = build_storage_inventory(
        tmp_path, metadata_snapshot=bad_role, free_bytes=False)
    assert make_preview(blocked, manual_policy).candidates == ()


def test_identity_representative_priority_is_scan_order_independent(tmp_path, monkeypatch):
    active = tmp_path / "Desktop" / "old.mp4"
    alias = tmp_path / "Shared" / "alias.mp4"
    _write(active, 7)
    identity = os.stat(active, follow_symlinks=False)

    def row(path, relative):
        info = SimpleNamespace(
            st_dev=identity.st_dev, st_ino=identity.st_ino,
            st_size=identity.st_size, st_mtime_ns=identity.st_mtime_ns,
            st_nlink=2, st_mode=stat.S_IFREG, st_file_attributes=0,
        )
        return str(path), info, relative

    rows = (row(alias, "Shared/alias.mp4"), row(active, "Desktop/old.mp4"))
    for scan_rows in (rows, tuple(reversed(rows))):
        monkeypatch.setattr(
            storage, "_walk_files",
            lambda _root, _cancel=None, scan_rows=scan_rows: iter(scan_rows),
        )
        inventory = build_storage_inventory(tmp_path, free_bytes=False)
        assert inventory.active_bytes == 7
        assert inventory.other_bytes == 0
        assert [item.path for item in inventory.files] == [str(active)]
        assert inventory.files[0].nlink == 2
        assert make_preview(inventory, StoragePolicy(max_library_bytes=1)).candidates == ()


def test_busy_registered_clip_is_skipped_while_other_clip_remains_reviewable(tmp_path):
    first = tmp_path / "Desktop" / "busy.mp4"
    second = tmp_path / "Desktop" / "free.mp4"
    _write(first, 10)
    _write(second, 20)
    snapshot = {"by_path": {
        os.path.normcase(os.path.realpath(first)): {
            "clip_id": "busy", "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
        os.path.normcase(os.path.realpath(second)): {
            "clip_id": "free", "flags": {"protected": False, "favorite": False},
            "complete": True,
        },
    }}
    policy = StoragePolicy(max_library_bytes=1)
    inventory = build_storage_inventory(
        tmp_path, metadata_snapshot=snapshot, free_bytes=False,
        busy_paths=(str(first),))
    preview = make_preview(inventory, policy)
    assert [item.path for item in preview.candidates] == [str(second)]
    assert first.exists() and second.exists()

    all_busy = build_storage_inventory(
        tmp_path, metadata_snapshot=snapshot, free_bytes=False,
        busy_paths=(str(first), str(second)))
    assert make_preview(all_busy, policy).candidates == ()
    assert first.exists() and second.exists()


def test_quota_admission_uses_total_root_usage(tmp_path):
    _write(tmp_path / "Desktop" / "clip.mp4", 12)
    inventory = build_storage_inventory(tmp_path, free_bytes=False)
    allowed, reason = quota_admission(inventory, StoragePolicy(max_library_bytes=12), 1)
    assert not allowed
    assert "limit" in reason.casefold()
    assert quota_admission(inventory, StoragePolicy(), 100)[0]
