"""F08 tag persistence, writer merge, and compatibility contracts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.clip_metadata_manager import ClipMetadataManager
from core.library_tags import format_tags_text, normalize_tags, parse_tags_text
from core import clip_metadata_manager as manager_module
from core.library_index import canonical_media_path


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


def test_legacy_single_tag_is_verbatim_until_explicit_edit(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, binding, _ = media(mgr, tmp_path)
    raw = "  alpha, beta  " + ("x" * 90)
    mgr.set(str(path), tag=raw, description="before")
    mgr.migrate(target_version=5)
    mgr.reload()

    assert mgr.get_tags(str(path)) == (raw,)
    assert mgr.get(str(path)) == {"tag": raw, "description": "before"}
    assert mgr.set_details(str(path), description="description only")
    assert mgr.get_tags(str(path)) == (raw,)
    assert mgr.get(str(path))["tag"] == raw

    with sqlite3.connect((tmp_path / "library.sqlite3")) as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert value["tag"] == raw
    assert value["description"] == "description only"


def test_explicit_tags_normalize_and_quoted_csv_keeps_embedded_comma(tmp_path):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path)

    assert mgr.set_details(
        str(path), tags=("  Cafe\u0301  ", "alpha,beta", "CAFÉ"),
        expected_identity=identity)
    tags = mgr.get_tags(str(path))
    assert tags == ("Café", "alpha,beta")
    assert mgr.update_tags(str(path), add=("new tag",), expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("Café", "alpha,beta", "new tag")
    assert mgr.update_tags(str(path), remove=("ALPHA,BETA",), expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("Café", "new tag")


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_explicit_tag_apis_remain_available_on_legacy_schema_versions(tmp_path, version):
    mgr = manager(tmp_path, version=version)
    path, _, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("legacy schema",),
                           expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("legacy schema",)


def test_legacy_set_before_schema5_preserves_exact_raw_tag_without_canonical_field(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, binding, _ = media(mgr, tmp_path)
    raw = "  short legacy tag  "

    mgr.set(str(path), tag=raw, description="description")

    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert value["tag"] == raw
    assert "library_tags" not in value
    reopened = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    assert reopened.get_tags(str(path)) == (raw,)


@pytest.mark.parametrize(
    ("raw", "expected_canonical"),
    [("A selected", ["A selected"]),
     ("  legacy, " + ("z" * 84) + "\t", None)],
    ids=["valid-legacy-input", "invalid-legacy-input"],
)
def test_pre5_stale_legacy_set_uses_fresh_canonical_presence(
        tmp_path, raw, expected_canonical):
    a = manager(tmp_path, version=4)
    path, binding, identity = media(a, tmp_path)
    b = manager(tmp_path, version=4)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        initial = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert "library_tags" not in initial

    assert b.set_details(str(path), tags=("B canonical tag",),
                         expected_identity=identity)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert current["library_tags"] == ["B canonical tag"]

    a.set(str(path), tag=raw)
    a.reload()
    assert a.get_tags(str(path)) == (raw,)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert current["tag"] == raw
    if expected_canonical is None:
        assert "library_tags" not in current
    else:
        assert current["library_tags"] == expected_canonical


def test_pre5_stale_set_does_not_restore_canonical_field_removed_by_other_manager(tmp_path):
    a = manager(tmp_path, version=4)
    path, binding, identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("old canonical",), description="initial",
                         expected_identity=identity)
    b = manager(tmp_path, version=4)
    raw_from_b = "legacy, " + ("x" * 90) + "\t"
    b.set(str(path), tag=raw_from_b, description="B fresh description")
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        assert "library_tags" not in current
        current["opaque_from_b"] = {"preserve": ["B", None]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(current), binding.clip_id))

    raw_from_a = "  A new  "
    a.set(str(path), tag=raw_from_a)
    a.reload()

    assert a.get_tags(str(path)) == (raw_from_a,)
    assert a.get(str(path)) == {
        "tag": raw_from_a, "description": "B fresh description"}
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert "library_tags" not in current
    assert current["opaque_from_b"] == {"preserve": ["B", None]}


def test_library_tag_codec_limits_and_quoted_csv_contract():
    assert parse_tags_text('"alpha,beta", gamma, GAMMA') == ("alpha,beta", "gamma")
    assert format_tags_text(("alpha,beta", "gamma")) == '"alpha,beta", gamma'
    assert parse_tags_text("") == ()
    assert parse_tags_text("   ") == ()
    assert parse_tags_text(format_tags_text(())) == ()
    second_quoted = ("first", "second,with comma")
    assert parse_tags_text(format_tags_text(second_quoted)) == second_quoted
    raw_legacy = "legacy, " + ("x" * 90)
    assert format_tags_text((raw_legacy,)) == f'"{raw_legacy}"'
    assert normalize_tags(("  Cafe\u0301  ", "CAFÉ")) == ("Café",)
    with pytest.raises((ValueError, TypeError)):
        normalize_tags(("x" * 81,))
    with pytest.raises((ValueError, TypeError)):
        parse_tags_text("bad\tvalue")


@pytest.mark.parametrize("bad", ["", "x" * 81, "bad\tvalue", "bad\u200bvalue"])
def test_explicit_tag_edits_reject_empty_overlong_or_control_values(tmp_path, bad):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path)
    before = mgr.get_tags(str(path))
    with pytest.raises((ValueError, TypeError)):
        mgr.set_details(str(path), tags=(bad,), expected_identity=identity)
    assert mgr.get_tags(str(path)) == before


def test_legacy_set_on_schema4_replaces_canonical_tags_with_raw_single_value(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, binding, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("normalized old",),
                           expected_identity=identity)
    raw = "  legacy, " + ("z" * 84) + "\t"

    mgr.set(str(path), tag=raw)

    assert mgr.get_tags(str(path)) == (raw,)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert value["tag"] == raw
    assert "library_tags" not in value


def test_tag_delta_can_replace_one_of_32_and_remove_wins_on_collision(tmp_path):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path)
    initial = tuple(f"tag-{index:02}" for index in range(32))
    assert mgr.set_details(str(path), tags=initial, expected_identity=identity)

    assert mgr.update_tags(str(path), add=("tag-32",), remove=("tag-00",),
                           expected_identity=identity)
    current = mgr.get_tags(str(path))
    assert len(current) == 32
    assert "tag-00" not in current
    assert "tag-32" in current

    assert mgr.update_tags(str(path), add=("tag-01",), remove=("tag-01",),
                           expected_identity=identity)
    current = mgr.get_tags(str(path))
    assert len(current) == 31
    assert "tag-01" not in current


def test_invalid_legacy_value_is_read_verbatim_but_blocks_add_until_replace(tmp_path):
    mgr = manager(tmp_path, version=4)
    path, _, identity = media(mgr, tmp_path)
    raw = "legacy, " + ("z" * 84) + "\t"
    mgr.set(str(path), tag=raw, description="old")
    mgr.migrate(target_version=5)
    mgr.reload()

    assert mgr.get_tags(str(path)) == (raw,)
    assert mgr.set_details(str(path), description="description still editable")
    assert mgr.get_tags(str(path)) == (raw,)
    assert mgr.set_editor_draft(str(path), {"preserve_raw_tag": True})
    assert mgr.get_tags(str(path)) == (raw,)
    mgr.clear_editor_draft(str(path))
    assert mgr.get_tags(str(path)) == (raw,)
    with pytest.raises((ValueError, TypeError)):
        mgr.update_tags(str(path), add=("new",), expected_identity=identity)
    assert mgr.set_details(str(path), tags=("replacement",),
                           expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("replacement",)


def test_reads_of_unregistered_paths_do_not_register_or_create_catalog(tmp_path):
    mgr = ClipMetadataManager(database_path=tmp_path / "library.sqlite3")
    path = tmp_path / "unregistered.mp4"
    path.write_bytes(b"scanned locally but not registered")

    assert mgr.get_tags(str(path)) == ()
    assert mgr.metadata_bindings() == {}
    assert not (tmp_path / "library.sqlite3").exists()
    assert not (tmp_path / "library.json").exists()


def test_stale_writer_merges_only_owned_fields_and_preserves_opaque_data(tmp_path):
    a = manager(tmp_path)
    path, binding, identity = media(a, tmp_path)
    a.set(str(path), tag="legacy", description="initial")
    a.set_library_flags(path, favorite=True, protected=True)
    a.set_editor_draft(str(path), {"opaque_draft": [1, None]})
    with sqlite3.connect((tmp_path / "library.sqlite3")) as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        value["opaque_future"] = {"retain": ["v", None, 4]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
    a.reload()
    b = manager(tmp_path)
    b.set_details(str(path), tags=("current", "set by B"), expected_identity=identity)

    # A has an old cache and changes only the description; it must not restore
    # the old alias/canonical tags, flags, draft, or unknown JSON keys.
    assert a.set_details(str(path), description="A description", expected_identity=identity)
    assert a.get_tags(str(path)) == ("current", "set by B")
    assert a.get(str(path))["description"] == "A description"
    assert a.get_library_flags(path) == {"favorite": True, "protected": True}
    assert a.get_editor_draft(str(path)) == {"opaque_draft": [1, None]}
    with sqlite3.connect((tmp_path / "library.sqlite3")) as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert current["opaque_future"] == {"retain": ["v", None, 4]}


def test_private_stale_full_save_preserves_fresh_canonical_tags(tmp_path):
    a = manager(tmp_path)
    path, binding, identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("old tag",), expected_identity=identity)
    b = manager(tmp_path)
    assert b.set_details(str(path), tags=("fresh tag", "from B"),
                         expected_identity=identity)

    key = canonical_media_path(path)
    stale_full_record = dict(a._data[key])
    stale_full_record["description"] = "private full-save changed description"
    a._save(paths={key}, staged_data={key: stale_full_record})

    b.reload()
    assert b.get_tags(str(path)) == ("fresh tag", "from B")
    assert b.get(str(path))["description"] == "private full-save changed description"
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        saved = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert saved["library_tags"] == ["fresh tag", "from B"]


@pytest.mark.parametrize("writer", ["legacy-description", "editor-draft"])
def test_stale_writer_on_unregistered_replacement_creates_only_owned_metadata(
        tmp_path, writer):
    a = manager(tmp_path)
    path, old_binding, old_identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("old tag",), description="old description",
                         expected_identity=old_identity)
    a.set_library_flags(path, favorite=True, protected=True)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (old_binding.clip_id,)
        ).fetchone()[0])
        value["opaque_old"] = {"must_not_leak": [1, None]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), old_binding.clip_id))
    a.reload()

    replacement = path.with_name(path.stem + "-replacement.mp4")
    replacement.write_bytes(b"new media object with different identity and size")
    replacement.replace(path)
    fresh_identity = dict(a.inspect_media(path).stat)
    assert fresh_identity != old_identity

    if writer == "legacy-description":
        a.set(str(path), description="new identity description")
    else:
        a.set_editor_draft(str(path), {"draft": "new identity draft"})

    current_binding = a.resolve_media(path)
    assert current_binding is not None
    assert current_binding.clip_id != old_binding.clip_id
    current = a.get_by_id(current_binding.clip_id)
    assert a.get_tags(str(path)) == ()
    assert current.get("library_flags", {"favorite": False, "protected": False}) == {
        "favorite": False, "protected": False}
    assert "opaque_old" not in current
    if writer == "legacy-description":
        assert current["description"] == "new identity description"
        assert "editor_draft" not in current
    else:
        draft = current["editor_draft"]
        assert draft["state"] == {"draft": "new identity draft"}
        assert draft["file"] == fresh_identity
        assert "description" not in current


@pytest.mark.parametrize("writer", ["legacy-description", "editor-draft"])
def test_stale_writer_refuses_after_other_manager_commits_replacement(
        tmp_path, writer):
    a = manager(tmp_path)
    path, old_binding, old_identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("old tag",), description="old description",
                         expected_identity=old_identity)
    a.set_library_flags(path, favorite=True, protected=True)
    b = manager(tmp_path)
    replacement = path.with_name(path.stem + "-replacement.mp4")
    replacement.write_bytes(b"B current media object with different identity")
    replacement.replace(path)
    fresh_identity = dict(b.inspect_media(path).stat)
    assert fresh_identity != old_identity
    assert b.set_details(str(path), tags=("fresh B tag",),
                         expected_identity=fresh_identity)
    fresh_binding = b.resolve_media(path)
    assert fresh_binding is not None
    assert fresh_binding.clip_id != old_binding.clip_id
    old_data_before = json.loads(json.dumps(a._id_data))
    old_files_before = dict(a._files)

    with pytest.raises((OSError, ValueError)):
        if writer == "legacy-description":
            a.set(str(path), description="must be refused")
        else:
            a.set_editor_draft(str(path), {"draft": "must be refused"})

    b.reload()
    assert b.resolve_media(path).clip_id == fresh_binding.clip_id
    assert b.get_tags(str(path)) == ("fresh B tag",)
    assert a._id_data == old_data_before
    assert a._files == old_files_before
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        fresh = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (fresh_binding.clip_id,)
        ).fetchone()[0])
    assert fresh["library_tags"] == ["fresh B tag"]
    assert "must be refused" not in json.dumps(fresh)


def test_failed_final_evidence_recheck_rolls_back_and_does_not_publish(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    path, binding, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("before",), expected_identity=identity)

    def reject_recheck(_evidence):
        raise OSError("synthetic final evidence changed")

    monkeypatch.setattr(mgr, "_recheck", reject_recheck)
    with pytest.raises(OSError, match="synthetic final evidence changed"):
        mgr.set_details(str(path), tags=("must roll back",),
                        expected_identity=identity)

    assert mgr.get_tags(str(path)) == ("before",)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        saved = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert saved["library_tags"] == ["before"]


def test_prepared_file_operation_blocks_tag_write(tmp_path):
    mgr = manager(tmp_path)
    path, binding, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("before",), expected_identity=identity)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        db.execute(
            "INSERT INTO file_operations "
            "(op_id,version,kind,clip_id,state,plan_json,error,cleanup_done) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("qa-prepared-tag-guard", 1, "rename", binding.clip_id, "PREPARED",
             json.dumps({"reserved_paths": [str(path)]}), None, 0),
        )

    with pytest.raises(OSError, match="reserved"):
        mgr.set_details(str(path), tags=("blocked",), expected_identity=identity)
    assert mgr.get_tags(str(path)) == ("before",)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        saved = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
    assert saved["library_tags"] == ["before"]


@pytest.mark.parametrize("write_draft", [True, False], ids=["set-draft", "clear-draft"])
def test_stale_draft_writer_preserves_newer_tags_flags_and_opaque_metadata(
        tmp_path, write_draft):
    a = manager(tmp_path)
    path, binding, identity = media(a, tmp_path)
    a.set_details(str(path), tags=("old",), description="same", expected_identity=identity)
    a.set_library_flags(path, favorite=True, protected=False)
    a.set_editor_draft(str(path), {"draft": "old"})
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        value["opaque"] = {"must": ["survive", None]}
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
    a.reload()
    b = manager(tmp_path)
    b.set_details(str(path), tags=("from B", "current"), expected_identity=identity)

    if write_draft:
        assert a.set_editor_draft(str(path), {"draft": "A changed only draft"})
    else:
        a.clear_editor_draft(str(path))
    assert a.get_tags(str(path)) == ("from B", "current")
    assert a.get_library_flags(path) == {"favorite": True, "protected": True}
    current = a.get_by_id(binding.clip_id)
    assert current["opaque"] == {"must": ["survive", None]}
    if write_draft:
        draft = current["editor_draft"]
        assert draft["state"] == {"draft": "A changed only draft"}
        assert draft["version"] == 1
        assert draft["file"] == identity
        assert draft["updated_at"]
    else:
        assert "editor_draft" not in current


def test_new_file_identity_is_never_inherited_without_explicit_fresh_evidence(tmp_path):
    mgr = manager(tmp_path)
    path, old_binding, old_identity = media(mgr, tmp_path)
    mgr.set_details(str(path), tags=("old",), expected_identity=old_identity)
    mgr.set_library_flags(path, favorite=True, protected=True)
    old_album = mgr.create_album("historical")
    mgr.add_album_member(old_album.album_id, path, expected_identity=old_identity)

    path.write_bytes(b"replacement with another identity")
    new_identity = dict(mgr.inspect_media(path).stat)
    assert new_identity != old_identity
    with pytest.raises((OSError, ValueError)):
        mgr.set_details(str(path), tags=("must refuse",))
    with pytest.raises((OSError, ValueError)):
        mgr.update_tags(str(path), add=("stale selection",),
                        expected_identity=old_identity)
    assert mgr.get_by_id(old_binding.clip_id)["library_flags"] == {
        "favorite": True, "protected": True}
    assert mgr.album_bindings(old_album.album_id) == {}

    # Explicitly selected current evidence can register/tag the replacement,
    # but it receives no metadata, flags, draft, or membership from old ID.
    assert mgr.set_details(str(path), tags=("new",), expected_identity=new_identity)
    new_binding = mgr.resolve_media(path)
    assert new_binding.clip_id != old_binding.clip_id
    assert mgr.get_tags(str(path)) == ("new",)
    assert mgr.get_library_flags(path) == {"favorite": False, "protected": False}
    assert mgr.album_bindings(old_album.album_id) == {}


def test_equal_canonical_tags_on_replacement_are_persisted_for_new_identity(tmp_path):
    mgr = manager(tmp_path)
    path, old_binding, old_identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("same",),
                           expected_identity=old_identity)

    replacement = path.with_name(path.stem + "-replacement.mp4")
    replacement.write_bytes(b"different replacement identity")
    replacement.replace(path)
    fresh_identity = dict(mgr.inspect_media(path).stat)
    assert fresh_identity != old_identity
    assert mgr.set_details(str(path), tags=("same",),
                           expected_identity=fresh_identity)

    new_binding = mgr.resolve_media(path)
    assert new_binding is not None
    assert new_binding.clip_id != old_binding.clip_id
    mgr.reload()
    assert mgr.get_tags(str(path)) == ("same",)
    current = mgr.get_by_id(new_binding.clip_id)
    assert current["library_tags"] == ["same"]
    assert current["tag"] == "same"


def test_stale_manager_add_preserves_tags_committed_by_another_manager(tmp_path):
    a = manager(tmp_path)
    path, _, identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("base",), expected_identity=identity)
    b = manager(tmp_path)

    assert b.update_tags(str(path), add=("from B",), expected_identity=identity) is True
    assert a.update_tags(str(path), add=("from A",), expected_identity=identity) is True
    a.reload()
    assert a.get_tags(str(path)) == ("base", "from B", "from A")


def test_stale_manager_remove_preserves_concurrent_addition(tmp_path):
    a = manager(tmp_path)
    path, _, identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("remove-me", "keep"),
                         expected_identity=identity)
    b = manager(tmp_path)

    assert b.update_tags(str(path), add=("from B",), expected_identity=identity) is True
    assert a.update_tags(str(path), remove=("remove-me",),
                         expected_identity=identity) is True
    a.reload()
    assert a.get_tags(str(path)) == ("keep", "from B")


def test_stale_manager_cannot_reapply_old_tags_to_fresh_replacement_identity(tmp_path):
    a = manager(tmp_path)
    path, old_binding, old_identity = media(a, tmp_path)
    assert a.set_details(str(path), tags=("old tag",), expected_identity=old_identity)
    b = manager(tmp_path)
    path.write_bytes(b"different replacement payload")
    new_identity = dict(b.inspect_media(path).stat)
    assert new_identity != old_identity
    assert b.set_details(str(path), tags=("fresh tag",), expected_identity=new_identity)
    new_binding = b.resolve_media(path)
    assert new_binding.clip_id != old_binding.clip_id

    # A carries old tags in RAM, but the expected identity names B's current
    # replacement. The mutation must merge with the fresh SQL value.
    assert a.update_tags(str(path), add=("A addition",),
                         expected_identity=new_identity) is True
    a.reload()
    assert a.get_tags(str(path)) == ("fresh tag", "A addition")


def test_tag_updates_report_changed_and_successful_noops_false(tmp_path):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path)
    assert mgr.update_tags(str(path), add=("one",), expected_identity=identity) is True
    assert mgr.update_tags(str(path), add=("one",), expected_identity=identity) is False
    assert mgr.update_tags(str(path), remove=("missing",), expected_identity=identity) is False
    assert mgr.update_tags(str(path), remove=("one",), expected_identity=identity) is True
    assert mgr.update_tags(str(path), remove=("one",), expected_identity=identity) is False


def test_explicit_tagging_adopts_unregistered_file_and_publishes_snapshot(tmp_path):
    mgr = manager(tmp_path)
    path = tmp_path / "unregistered.mp4"
    path.write_bytes(b"explicitly tagged media")
    identity = dict(mgr.inspect_media(path).stat)
    assert mgr.resolve_media(path) is None

    assert mgr.set_details(str(path), tags=("adopted",), expected_identity=identity) is True

    binding = mgr.resolve_media(path)
    assert binding is not None
    assert mgr.get_tags(str(path)) == ("adopted",)
    snapshot = mgr.metadata_bindings()
    assert tuple(snapshot[canonical_media_path(path)]["tags"]) == ("adopted",)


def test_explicit_tagging_adopts_legacy_entry_without_losing_opaque_fields(tmp_path):
    database = tmp_path / "library.sqlite3"
    legacy = tmp_path / "metadata.json"
    path = tmp_path / "legacy.mp4"
    path.write_bytes(b"legacy media")
    legacy.write_text(json.dumps({str(path): {
        "tag": "legacy, comma", "description": "kept",
        "opaque": {"nested": [1, None]},
    }}), encoding="utf-8")
    mgr = ClipMetadataManager(database_path=database, legacy_path=legacy)
    mgr.migrate(target_version=5)
    identity = dict(mgr.inspect_media(path).stat)

    assert mgr.set_details(str(path), tags=("new",), expected_identity=identity) is True
    binding = mgr.resolve_media(path)
    assert binding is not None
    current = mgr.get_by_id(binding.clip_id)
    assert current["tag"] == "new"
    assert current["description"] == "kept"
    assert current["opaque"] == {"nested": [1, None]}
    assert tuple(mgr.metadata_bindings()[canonical_media_path(path)]["tags"]) == ("new",)


def test_entry_and_primary_path_mismatch_refuses_tag_write_without_repair(tmp_path):
    mgr = manager(tmp_path)
    path, binding, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("before",), expected_identity=identity)
    other = tmp_path / "wrong-path.mp4"
    other.write_bytes(b"different path")
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        old_value = db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0]
        db.execute("UPDATE entries SET path=? WHERE clip_id=?",
                   (str(other), binding.clip_id))
    mgr.reload()

    with pytest.raises((OSError, ValueError)):
        mgr.set_details(str(path), tags=("must not repair",), expected_identity=identity)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        assert db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0] == old_value
        assert db.execute(
            "SELECT path FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0] == str(other)


def test_metadata_snapshot_is_defensive_and_has_no_sql_or_stat_reads(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    path, _, identity = media(mgr, tmp_path)
    mgr.set_details(str(path), tags=("one", "two"), description="memo",
                    expected_identity=identity)
    first = mgr.metadata_bindings()
    key, item = next(iter(first.items()))
    if isinstance(item["tags"], list):
        item["tags"].append("caller mutation")
    else:
        item["tags"] = (*item["tags"], "caller mutation")
    first[key]["description"] = "caller mutation"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata_bindings must return a defensive RAM snapshot")

    with monkeypatch.context() as patch:
        patch.setattr(manager_module.sqlite3, "connect", forbidden)
        patch.setattr(Path, "stat", forbidden)
        again = mgr.metadata_bindings()
    assert tuple(again[key]["tags"]) == ("one", "two")
    assert again[key]["description"] == "memo"


@pytest.mark.parametrize("malformed", ["not-a-list", {"key-that-looks-like-a-tag": "value"}])
def test_malformed_canonical_tags_refuse_incremental_edits_but_allow_explicit_replace(
        tmp_path, malformed):
    mgr = manager(tmp_path)
    path, binding, identity = media(mgr, tmp_path)
    assert mgr.set_details(str(path), tags=("valid",), expected_identity=identity)
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        value = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        value["tag"] = "legacy value that must not hide canonical corruption"
        value["library_tags"] = malformed
        db.execute("UPDATE entries SET value=? WHERE clip_id=?",
                   (json.dumps(value), binding.clip_id))
    mgr.reload()

    assert mgr.set_details(str(path), description="description preserved",
                           expected_identity=identity)
    assert mgr.set_editor_draft(str(path), {"keep malformed bytes": True})
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        current = json.loads(db.execute(
            "SELECT value FROM entries WHERE clip_id=?", (binding.clip_id,)
        ).fetchone()[0])
        assert current["library_tags"] == malformed
        assert current["description"] == "description preserved"
    with pytest.raises((ValueError, OSError)):
        mgr.get_tags(str(path))
    with pytest.raises((ValueError, OSError)):
        mgr.update_tags(str(path), add=("new",), expected_identity=identity)
    with pytest.raises((ValueError, OSError)):
        mgr.update_tags(str(path), remove=("valid",), expected_identity=identity)
    snapshot = mgr.metadata_bindings()[canonical_media_path(path)]
    assert snapshot["error"]
    assert mgr.set_details(str(path), tags=("repaired",),
                           expected_identity=identity) is True
    assert mgr.get_tags(str(path)) == ("repaired",)
    repaired = mgr.get_by_id(binding.clip_id)
    assert repaired["description"] == "description preserved"
    assert mgr.get_editor_draft(str(path)) == {"keep malformed bytes": True}
