"""Current SQLite editor drafts remain durable beyond the legacy cache bound."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "FTHR_UI"))

from core.clip_metadata_manager import ClipMetadataManager


def _manager(directory):
    return ClipMetadataManager(
        database_path=directory / "metadata.sqlite3",
        legacy_path=directory / "metadata.json",
    )


def test_registered_sqlite_drafts_survive_limit_and_followup_autosave(tmp_path):
    manager = _manager(tmp_path)
    manager.migrate(target_version=1)
    records = []

    for index in range(251):
        clip = tmp_path / f"clip-{index:03d}.mp4"
        clip.write_bytes(f"media-{index}".encode())
        binding = manager.register_media(manager.inspect_media(str(clip)))
        manager.set(
            str(clip),
            tag=f"Tag {index}",
            description=f"Description {index}",
        )
        state = {"draft_index": index, "value": f"Draft {index}"}
        assert manager.set_editor_draft(str(clip), state)
        records.append((clip, binding, state))

    reopened = _manager(tmp_path)
    expected_ids = {binding.clip_id for _clip, binding, _state in records}
    observed_ids = {
        reopened.resolve_media(str(clip)).clip_id
        for clip, _binding, _state in records
    }
    assert observed_ids == expected_ids

    for index, (clip, binding, state) in enumerate(records):
        metadata = reopened.get_by_id(binding.clip_id)
        assert metadata["tag"] == f"Tag {index}"
        assert metadata["description"] == f"Description {index}"
        assert metadata["editor_draft"]["state"] == state
        assert reopened.get_editor_draft(str(clip)) == state

    first_clip, first_binding, _first_state = records[0]
    last_clip, last_binding, last_state = records[-1]
    assert reopened.resolve_media(str(first_clip)).clip_id == first_binding.clip_id
    assert reopened.resolve_media(str(last_clip)).clip_id == last_binding.clip_id
    assert reopened.get_editor_draft(str(last_clip)) == last_state

    updated_state = {"draft_index": 0, "value": "Draft 0 updated"}
    reopened.set(
        str(first_clip), tag="Tag 0 updated", description="Description 0 updated")
    assert reopened.set_editor_draft(str(first_clip), updated_state)

    final = _manager(tmp_path)
    first_metadata = final.get_by_id(first_binding.clip_id)
    assert first_metadata["tag"] == "Tag 0 updated"
    assert first_metadata["description"] == "Description 0 updated"
    assert first_metadata["editor_draft"]["state"] == updated_state
    for index, (clip, binding, state) in enumerate(records[1:], start=1):
        assert final.resolve_media(str(clip)).clip_id == binding.clip_id
        assert final.get_editor_draft(str(clip)) == state
        metadata = final.get_by_id(binding.clip_id)
        assert metadata["tag"] == f"Tag {index}"
        assert metadata["description"] == f"Description {index}"
