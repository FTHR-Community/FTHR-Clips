"""F08 saved-search API and schema-5 compatibility contracts."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.clip_metadata_manager import ClipMetadataManager
from core.library_searches import SearchCriteria, normalize_search_criteria


def manager(tmp_path: Path, *, version: int = 5) -> ClipMetadataManager:
    result = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    result.migrate(target_version=version)
    return result


def media(manager: ClipMetadataManager, root: Path, name: str = "clip.mp4"):
    path = root / name
    path.write_bytes(("synthetic:" + name).encode())
    evidence = manager.inspect_media(path)
    binding = manager.register_media(evidence)
    return path, binding, dict(evidence.stat)

def test_schema4_to5_backup_wal_and_default_target_remain_2(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, binding, _ = media(mgr, tmp_path)
    mgr.set(str(path), tag="legacy, comma", description="retain")
    db = sqlite3.connect((tmp_path / "library.sqlite3"))
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        value["opaque"] = {"wal": ["committed", None]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
        db.commit()
        result = mgr.migrate(target_version=5)
        assert result.backup_path is not None
        with sqlite3.connect(result.backup_path) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
            saved = json.loads(backup.execute(
                "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
            ).fetchone()[0])
            assert saved["opaque"] == {"wal": ["committed", None]}
        assert mgr.inspect_schema().version == 5
        assert mgr.get_tags(str(path)) == ("legacy, comma",)
    finally:
        db.close()
    with pytest.raises(ValueError, match="downgrade"):
        mgr.migrate()


def test_default_migration_target_is_still_schema_two(tmp_path):
    mgr = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    assert mgr.migrate().to_version == 2


def test_schema5_ddl_failure_rolls_back_schema4_and_preserves_backup(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, binding, _ = media(mgr, tmp_path)
    mgr.set(str(path), tag="before failed upgrade", description="keep")
    before = mgr.get_by_id(binding.clip_id)
    database = tmp_path / "library.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE saved_searches (foreign_name TEXT)")
        db.execute("INSERT INTO saved_searches VALUES ('owned by another extension')")
        old_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='saved_searches'"
        ).fetchone()[0]

    with pytest.raises(OSError):
        mgr.migrate(target_version=5)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='saved_searches'"
        ).fetchone()[0] == old_sql
        assert db.execute("SELECT foreign_name FROM saved_searches").fetchall() == [
            ("owned by another extension",)]
    assert mgr.get_by_id(binding.clip_id) == before
    backups = list(tmp_path.glob("library.sqlite3.pre-v5-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 4


def test_future_schema5_catalog_is_never_treated_as_empty_or_changed(tmp_path):
    database = tmp_path / "library.sqlite3"
    manager(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=99")
    reopened = ClipMetadataManager(database_path=database)
    from core.clip_metadata_types import UnsupportedSchemaVersion
    with pytest.raises(UnsupportedSchemaVersion):
        reopened.list_saved_searches()
    with pytest.raises(UnsupportedSchemaVersion):
        reopened.create_saved_search("Must not write", {})
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_saved_searches_are_versioned_immutable_and_round_trip(tmp_path):
    mgr = manager(tmp_path)
    criteria = {
        "version": 1, "text": "frag", "tags_all": (" Blue ", "blue", "capture"),
        "album_id": None, "kind": "screenshots", "favorites_only": True,
        "sort": "oldest",
    }
    normalized = normalize_search_criteria(criteria)
    assert isinstance(normalized, SearchCriteria)
    assert normalized.tags_all == ("blue", "capture")
    with pytest.raises(FrozenInstanceError):
        normalized.text = "mutate"
    saved = mgr.create_saved_search("Screenshots I like", normalized)
    assert saved.criteria == normalized
    assert mgr.list_saved_searches() == (saved,)
    assert mgr.rename_saved_search(saved.search_id, "  Screenshots   I like  ").name == "Screenshots I like"
    changed = {
        "version": 1, "text": "new", "tags_all": (), "album_id": None,
        "kind": "all", "favorites_only": False, "sort": "newest",
    }
    normalized_changed = normalize_search_criteria(changed)
    updated = mgr.update_saved_search(saved.search_id, normalized_changed)
    assert updated.criteria == normalized_changed
    assert mgr.list_saved_searches() == (updated,)
    mgr.delete_saved_search(saved.search_id)
    assert mgr.list_saved_searches() == ()


def test_search_criteria_default_value_is_version_one_all_clips():
    criteria = normalize_search_criteria(SearchCriteria())
    assert criteria == SearchCriteria(
        version=1, text="", tags_all=(), album_id=None, kind="all",
        favorites_only=False, sort="newest")


def test_saved_search_names_are_normalized_unique_and_rename_is_atomic(tmp_path):
    mgr = manager(tmp_path)
    criteria = normalize_search_criteria({
        "version": 1, "text": "", "tags_all": (), "album_id": None,
        "kind": "all", "favorites_only": False, "sort": "newest",
    })
    saved = mgr.create_saved_search("  Café   captures ", criteria)
    assert saved.name == "Café captures"
    with pytest.raises(ValueError):
        mgr.create_saved_search("CAFÉ CAPTURES", criteria)
    before = mgr.list_saved_searches()
    with pytest.raises(ValueError):
        mgr.rename_saved_search(saved.search_id, "   ")
    assert mgr.list_saved_searches() == before


@pytest.mark.parametrize("bad", [
    {"version": 99, "text": "", "tags_all": [], "album_id": None,
     "kind": "all", "favorites_only": False, "sort": "newest"},
    {"version": 1, "text": "", "tags_all": [], "album_id": None,
     "kind": "all", "favorites_only": 0, "sort": "newest"},
    {"version": 1, "text": "", "tags_all": [], "album_id": None,
     "kind": "all", "favorites_only": False, "sort": "newest", "where": "1=1"},
    {"version": 1, "text": "", "tags_all": [], "album_id": None,
     "kind": "all", "favorites_only": False},
])
def test_saved_search_rejects_unknown_version_types_and_keys(tmp_path, bad):
    mgr = manager(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        mgr.create_saved_search("Invalid criteria", normalize_search_criteria(bad))


def test_search_criteria_bounds_text_and_retains_legacy_long_tag_filters():
    long_legacy_value = "legacy," + ("x" * 300)
    criteria = normalize_search_criteria({
        "version": 1, "text": "q" * 200, "tags_all": (long_legacy_value,),
        "album_id": None, "kind": "all", "favorites_only": False,
        "sort": "newest",
    })
    assert criteria.text == "q" * 200
    assert criteria.tags_all == (long_legacy_value.casefold(),)
    with pytest.raises((TypeError, ValueError)):
        normalize_search_criteria({
            "version": 1, "text": "q" * 201, "tags_all": (),
            "album_id": None, "kind": "all", "favorites_only": False,
            "sort": "newest",
        })
    with pytest.raises((TypeError, ValueError)):
        normalize_search_criteria({
            "version": 1, "text": "", "tags_all": ("x" * 32769,),
            "album_id": None, "kind": "all", "favorites_only": False,
            "sort": "newest",
        })
    with pytest.raises((TypeError, ValueError)):
        normalize_search_criteria({
            "version": 1, "text": "", "tags_all": tuple(f"tag-{i}" for i in range(33)),
            "album_id": None, "kind": "all", "favorites_only": False,
            "sort": "newest",
        })


def test_invalid_optional_saved_search_stays_unavailable_and_never_broadens(tmp_path):
    mgr = manager(tmp_path)
    criteria = normalize_search_criteria({
        "version": 1, "text": "specific", "tags_all": [], "album_id": None,
        "kind": "screenshots", "favorites_only": True, "sort": "newest"})
    saved = mgr.create_saved_search("Keep broken row", criteria)
    raw_invalid = json.dumps({"version": 999, "kind": "all"}, separators=(",", ":"))
    with sqlite3.connect((tmp_path / "library.sqlite3")) as db:
        db.execute("UPDATE saved_searches SET criteria_json=? WHERE search_id=?",
                   (raw_invalid, saved.search_id))
    mgr.reload()
    unavailable = next(item for item in mgr.list_saved_searches()
                       if item.search_id == saved.search_id)
    assert unavailable.criteria is None
    assert unavailable.error
    assert mgr.inspect_schema().version == 5
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        assert db.execute("SELECT criteria_json FROM saved_searches WHERE search_id=?",
                          (saved.search_id,)).fetchone()[0] == raw_invalid


def test_deeply_nested_optional_criteria_row_is_retained_as_unavailable(tmp_path):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path, "usable-after-invalid-search.mp4")
    criteria = normalize_search_criteria({
        "version": 1, "text": "safe", "tags_all": (), "album_id": None,
        "kind": "clips", "favorites_only": False, "sort": "newest",
    })
    saved = mgr.create_saved_search("Deep invalid", criteria)
    raw_invalid = "[" * 1200 + "0" + "]" * 1200
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        db.execute("UPDATE saved_searches SET criteria_json=? WHERE search_id=?",
                   (raw_invalid, saved.search_id))

    # Corrupt optional saved-search criteria must not abort loading the library.
    mgr.reload()
    unavailable = next(item for item in mgr.list_saved_searches()
                       if item.search_id == saved.search_id)
    assert unavailable.criteria is None
    assert unavailable.error
    renamed = mgr.rename_saved_search(saved.search_id, "Still unavailable after rename")
    assert renamed.criteria is None
    assert renamed.error
    assert mgr.list_saved_searches() == (renamed,)
    # Invalid optional criteria do not prevent normal catalog reads/writes.
    assert mgr.set_details(str(path), tags=("library remains usable",),
                           expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("library remains usable",)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        assert db.execute("SELECT criteria_json FROM saved_searches WHERE search_id=?",
                          (saved.search_id,)).fetchone()[0] == raw_invalid
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_deleted_album_reference_is_retained_in_saved_search(tmp_path):
    mgr = manager(tmp_path)
    album = mgr.create_album("Temporary")
    criteria = normalize_search_criteria({
        "version": 1, "text": "", "tags_all": [], "album_id": album.album_id,
        "kind": "all", "favorites_only": False, "sort": "newest"})
    saved = mgr.create_saved_search("Uses album", criteria)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        original_json = db.execute(
            "SELECT criteria_json FROM saved_searches WHERE search_id=?",
            (saved.search_id,)).fetchone()[0]
    mgr.delete_album(album.album_id)
    reloaded = next(item for item in mgr.list_saved_searches()
                    if item.search_id == saved.search_id)
    assert reloaded.criteria == criteria
    assert reloaded.criteria.album_id == album.album_id
    assert reloaded.error
    renamed = mgr.rename_saved_search(saved.search_id, "Still unavailable")
    assert renamed.criteria == criteria
    assert renamed.criteria.album_id == album.album_id
    assert renamed.error
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current_json = db.execute(
            "SELECT criteria_json FROM saved_searches WHERE search_id=?",
            (saved.search_id,)).fetchone()[0]
    assert current_json == original_json
