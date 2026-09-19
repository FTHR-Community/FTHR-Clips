"""Tests for core.clip_deduplicator."""

from pathlib import Path
from core.clip_deduplicator import (
    ClipRecord,
    find_overlapping_pairs,
    parse_clip_start_time,
)


def test_parse_clip_start_time_from_filename():
    p = Path("desktop_clip_from_2026-09-18_22-30-00.mp4")
    t = parse_clip_start_time(p, duration=30.0)
    assert t > 0


def test_find_overlapping_pairs_detects_overlap():
    # Clip 1: [1000 to 1300] (duration 300s)
    # Clip 2: [1180 to 1480] (duration 300s, overlap 120s)
    # Clip 3: [2000 to 2300] (no overlap)
    r1 = ClipRecord(
        path=Path("clip1.mp4"),
        start_time=1000.0,
        duration=300.0,
        end_time=1300.0,
        size_bytes=100_000_000,
    )
    r2 = ClipRecord(
        path=Path("clip2.mp4"),
        start_time=1180.0,
        duration=300.0,
        end_time=1480.0,
        size_bytes=100_000_000,
    )
    r3 = ClipRecord(
        path=Path("clip3.mp4"),
        start_time=2000.0,
        duration=300.0,
        end_time=2300.0,
        size_bytes=100_000_000,
    )

    pairs = find_overlapping_pairs([r1, r2, r3], min_overlap_seconds=5.0)
    assert len(pairs) == 1
    assert pairs[0].first.path.name == "clip1.mp4"
    assert pairs[0].second.path.name == "clip2.mp4"
    assert abs(pairs[0].overlap_seconds - 120.0) < 0.001
    # 120s / 300s * 100MB = 40MB
    assert pairs[0].estimated_saved_bytes == 40_000_000


def test_parse_clip_start_time_fthr_format():
    p = Path("desktop_clip_from_17Sep2026_21-58-05.mp4")
    t = parse_clip_start_time(p, duration=30.0)
    assert t > 0
    # Also verify that a different date produces an expected timestamp difference
    p2 = Path("desktop_clip_from_19Sep2026_02-13-26.mp4")
    t2 = parse_clip_start_time(p2, duration=30.0)
    assert t2 - t > 100_000  # ~30 hours apart


def test_no_false_positive_when_clips_do_not_overlap():
    r1 = ClipRecord(
        path=Path("clip1.mp4"),
        start_time=1000.0,
        duration=300.0,
        end_time=1300.0,
        size_bytes=100_000_000,
    )
    r2 = ClipRecord(
        path=Path("clip2.mp4"),
        start_time=1300.0,
        duration=300.0,
        end_time=1600.0,
        size_bytes=100_000_000,
    )
    pairs = find_overlapping_pairs([r1, r2], min_overlap_seconds=1.0)
    assert len(pairs) == 0


def test_each_clip_paired_at_most_once():
    # If clip 1 overlaps with clip 2 and clip 3, it should only be paired once in a single pass
    r1 = ClipRecord(
        path=Path("folder/clip1.mp4"),
        start_time=1000.0,
        duration=300.0,
        end_time=1300.0,
        size_bytes=100_000_000,
    )
    r2 = ClipRecord(
        path=Path("folder/clip2.mp4"),
        start_time=1100.0,
        duration=300.0,
        end_time=1400.0,
        size_bytes=100_000_000,
    )
    r3 = ClipRecord(
        path=Path("folder/clip3.mp4"),
        start_time=1200.0,
        duration=300.0,
        end_time=1500.0,
        size_bytes=100_000_000,
    )
    pairs = find_overlapping_pairs([r1, r2, r3], min_overlap_seconds=5.0)
    # r1 & r2 will pair first. Neither r1 nor r2 should appear again.
    assert len(pairs) == 1
    assert pairs[0].first.path.name == "clip1.mp4"
    assert pairs[0].second.path.name == "clip2.mp4"


def test_subsumed_clip_overlap():
    # Clip 2 is completely inside Clip 1
    r1 = ClipRecord(
        path=Path("folder/clip1.mp4"),
        start_time=1000.0,
        duration=600.0,
        end_time=1600.0,
        size_bytes=200_000_000,
    )
    r2 = ClipRecord(
        path=Path("folder/clip2.mp4"),
        start_time=1200.0,
        duration=200.0,
        end_time=1400.0,
        size_bytes=60_000_000,
    )
    pairs = find_overlapping_pairs([r1, r2], min_overlap_seconds=5.0)
    assert len(pairs) == 1
    assert pairs[0].overlap_seconds == 200.0
    assert pairs[0].estimated_saved_bytes == 60_000_000


def test_resolve_unique_output_path(tmp_path: Path):
    from core.clip_deduplicator import resolve_unique_output_path

    target = tmp_path / "clip.mp4"
    assert resolve_unique_output_path(target) == target

    target.write_text("orig")
    p1 = resolve_unique_output_path(target)
    assert p1 == tmp_path / "clip (1).mp4"

    p1.write_text("v1")
    p2 = resolve_unique_output_path(target)
    assert p2 == tmp_path / "clip (2).mp4"


def test_quarantine_and_restore_clips(tmp_path: Path):
    from core.clip_deduplicator import quarantine_clips, restore_quarantined_clips

    clip_file = tmp_path / "test_clip.mp4"
    clip_file.write_text("video_content")
    sidecar = tmp_path / "test_clip.mp4.fthr-manifest"
    sidecar.write_text("manifest_data")

    quarantined = quarantine_clips([clip_file])
    assert len(quarantined) == 1
    assert not clip_file.exists()
    assert not sidecar.exists()
    q_file = quarantined[0]
    assert q_file.exists()
    assert ".fthr_quarantine" in str(q_file)
    q_sidecar = q_file.with_name(q_file.name + ".fthr-manifest")
    assert q_sidecar.exists()

    # Test restore
    restored = restore_quarantined_clips(quarantined, tmp_path)
    assert len(restored) == 1
    assert restored[0].exists()
    assert restored[0].read_text() == "video_content"
    restored_sidecar = restored[0].with_name(restored[0].name + ".fthr-manifest")
    assert restored_sidecar.exists()
    assert restored_sidecar.read_text() == "manifest_data"


def test_parse_clip_start_time_confidence():
    from core.clip_deduplicator import parse_clip_start_time

    exact_path = Path("desktop_clip_from_17Sep2026_21-58-05.mp4")
    t, conf = parse_clip_start_time(exact_path, 30.0, with_confidence=True)
    assert t > 0
    assert conf == "exact"

    unmatched_path = Path("some_random_recording.mp4")
    _, conf2 = parse_clip_start_time(unmatched_path, 30.0, with_confidence=True)
    assert conf2 == "inferred"


def test_chained_overlap_detection():
    # A overlaps B, and B overlaps C
    r1 = ClipRecord(
        path=Path("folder/clip1.mp4"),
        start_time=1000.0,
        duration=300.0,
        end_time=1300.0,
        size_bytes=100_000_000,
        timestamp_confidence="exact",
    )
    r2 = ClipRecord(
        path=Path("folder/clip2.mp4"),
        start_time=1100.0,
        duration=300.0,
        end_time=1400.0,
        size_bytes=100_000_000,
        timestamp_confidence="exact",
    )
    r3 = ClipRecord(
        path=Path("folder/clip3.mp4"),
        start_time=1250.0,
        duration=300.0,
        end_time=1550.0,
        size_bytes=100_000_000,
        timestamp_confidence="inferred",
    )
    pairs = find_overlapping_pairs([r1, r2, r3], min_overlap_seconds=5.0)
    assert len(pairs) == 1
    # Pair (r1, r2) is formed, and because r2 also overlaps with r3, is_chained must be True
    assert pairs[0].is_chained is True
    assert pairs[0].confidence == "exact"


