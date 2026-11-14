"""Pending restore directories stay hidden from media discovery."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from core.clip_files import PENDING_RESTORE_MARKER
from core.library_index import scan_library


def test_pending_restore_marker_hides_root_until_cleanup(tmp_path: Path):
    media = tmp_path / "pending.mp4"
    media.write_bytes(b"pending restore media")
    marker = tmp_path / PENDING_RESTORE_MARKER
    marker.write_text(json.dumps({
        "format_version": 1,
        "restore_uuid": str(uuid4()),
        "manifest_sha256": "0" * 64,
    }), encoding="utf-8")

    assert scan_library(tmp_path).records == ()
    marker.unlink()
    assert [record.path for record in scan_library(tmp_path).records] == [str(media)]
