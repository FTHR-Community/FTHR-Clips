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

