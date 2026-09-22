"""Integration test for clip deduplication using FFmpeg."""

from pathlib import Path
import os
import subprocess
import pytest

from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe
from core.clip_deduplicator import (
    ClipRecord,
    find_overlapping_pairs,
    merge_overlapping_pair,
    resolve_unique_output_path,
)


@pytest.fixture
def ffmpeg_exe():
    try:
        return get_ffmpeg_exe()
    except FFmpegUnavailable:
        if os.getenv("CI"):
            pytest.fail("FFmpeg runtime must be available in CI to execute deduplication integration tests")
        pytest.skip("FFmpeg is unavailable on this test host")


def _generate_synthetic_clip(ffmpeg: str, out_path: Path, duration: float, label: str):
    """Generate a valid test MP4 clip with video and silent audio."""
    cmd = [
        ffmpeg, "-y", "-v", "error", "-nostdin",
        "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={duration}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{duration}",
        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-g", "15",
        "-c:a", "aac", "-b:a", "64k",
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        pytest.fail(f"Failed to generate synthetic clip: {res.stderr}")


def test_merge_overlapping_pair_integration(ffmpeg_exe, tmp_path: Path):
    """Verify stream-copy merge with real FFmpeg, quarantine, and overwrite protection."""
    clip1 = tmp_path / "desktop_clip_from_19Sep2026_12-00-00.mp4"
    clip2 = tmp_path / "desktop_clip_from_19Sep2026_12-00-05.mp4"

    # Clip 1: 10s duration (start 0, end 10)
    # Clip 2: 10s duration (start 5, end 15) -> Overlap = 5s
    _generate_synthetic_clip(ffmpeg_exe, clip1, 10.0, "clip1")
    _generate_synthetic_clip(ffmpeg_exe, clip2, 10.0, "clip2")

    r1 = ClipRecord(
        path=clip1,
        start_time=1000.0,
        duration=10.0,
        end_time=1010.0,
        size_bytes=clip1.stat().st_size,
        timestamp_confidence="HIGH",
    )
    r2 = ClipRecord(
        path=clip2,
        start_time=1005.0,
        duration=10.0,
        end_time=1015.0,
        size_bytes=clip2.stat().st_size,
        timestamp_confidence="HIGH",
    )

    pairs = find_overlapping_pairs([r1, r2], min_overlap_seconds=2.0)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.overlap_seconds == 5.0
    assert pair.confidence in ("exact", "HIGH")

    # 1. Test merge WITHOUT removing originals
    out = merge_overlapping_pair(pair, remove_originals=False)
    assert out.exists()
    assert out.stat().st_size > 0
    assert clip1.exists()
    assert clip2.exists()
    assert pair.actual_saved_bytes == 0

    # 2. Test merge WITH quarantine/recycle bin
    out2 = merge_overlapping_pair(pair, remove_originals=True, quarantine=True)
    assert out2.exists()
    assert not clip1.exists()
    assert not clip2.exists()
    assert pair.actual_saved_bytes > 0

    # 3. Test overwrite protection: output path already exists
    p3 = resolve_unique_output_path(out2)
    assert p3 != out2
    assert "_merged_1" in p3.name or "(1)" in p3.name


def test_merge_clip_cluster_integration(ffmpeg_exe, tmp_path: Path):
    """Verify cluster merge of 3 overlapping clips (A->B->C) in a single FFmpeg pass."""
    from core.clip_deduplicator import ClipRecord, merge_clip_cluster

    clip1 = tmp_path / "desktop_clip_from_20Sep2026_08-00-00.mp4"
    clip2 = tmp_path / "desktop_clip_from_20Sep2026_08-00-07.mp4"
    clip3 = tmp_path / "desktop_clip_from_20Sep2026_08-00-14.mp4"

    _generate_synthetic_clip(ffmpeg_exe, clip1, 10.0, "A")
    _generate_synthetic_clip(ffmpeg_exe, clip2, 10.0, "B")
    _generate_synthetic_clip(ffmpeg_exe, clip3, 10.0, "C")

    cluster = [
        ClipRecord(path=clip1, start_time=0.0,  duration=10.0, end_time=10.0, size_bytes=clip1.stat().st_size),
        ClipRecord(path=clip2, start_time=7.0,  duration=10.0, end_time=17.0, size_bytes=clip2.stat().st_size),
        ClipRecord(path=clip3, start_time=14.0, duration=10.0, end_time=24.0, size_bytes=clip3.stat().st_size),
    ]

    # 1. Merge WITHOUT removing originals
    out, saved = merge_clip_cluster(cluster, remove_originals=False)
    assert out.exists()
    assert out.stat().st_size > 0
    assert saved == 0
    assert clip1.exists(), "original should be preserved"
    assert clip2.exists(), "original should be preserved"
    assert clip3.exists(), "original should be preserved"

    # 2. Merge WITH quarantine — originals should disappear
    out2, saved2 = merge_clip_cluster(cluster, remove_originals=True)
    assert out2.exists()
    assert not clip1.exists(), "clip1 should have been quarantined"
    assert not clip2.exists(), "clip2 should have been quarantined"
    assert not clip3.exists(), "clip3 should have been quarantined"
    assert saved2 > 0, "should report positive bytes saved after quarantine"


def test_merge_clip_cluster_with_audio_manifest_sidecars(ffmpeg_exe, tmp_path: Path):
    """Verify cluster merge also quarantines audio manifest sidecars and calculates savings."""
    from core.clip_deduplicator import ClipRecord, find_overlapping_clusters, merge_clip_cluster
    from core.audio_manifest import MANIFEST_SUFFIX

    clip1 = tmp_path / "desktop_clip_from_20Sep2026_09-00-00.mp4"
    clip2 = tmp_path / "desktop_clip_from_20Sep2026_09-00-06.mp4"
    clip3 = tmp_path / "desktop_clip_from_20Sep2026_09-00-12.mp4"

    _generate_synthetic_clip(ffmpeg_exe, clip1, 10.0, "A")
    _generate_synthetic_clip(ffmpeg_exe, clip2, 10.0, "B")
    _generate_synthetic_clip(ffmpeg_exe, clip3, 10.0, "C")

    # Create audio manifest sidecars
    sidecar1 = clip1.with_name(clip1.name + MANIFEST_SUFFIX)
    sidecar2 = clip2.with_name(clip2.name + MANIFEST_SUFFIX)
    sidecar3 = clip3.with_name(clip3.name + MANIFEST_SUFFIX)
    sidecar1.write_text('{"sources": []}')
    sidecar2.write_text('{"sources": []}')
    sidecar3.write_text('{"sources": []}')

    records = [
        ClipRecord(path=clip1, start_time=0.0,  duration=10.0, end_time=10.0, size_bytes=clip1.stat().st_size),
        ClipRecord(path=clip2, start_time=6.0,  duration=10.0, end_time=16.0, size_bytes=clip2.stat().st_size),
        ClipRecord(path=clip3, start_time=12.0, duration=10.0, end_time=22.0, size_bytes=clip3.stat().st_size),
    ]

    clusters = find_overlapping_clusters(records, min_overlap_seconds=3.0)
    assert len(clusters) == 1
    assert len(clusters[0].clips) == 3

    out, saved = merge_clip_cluster(clusters[0].clips, remove_originals=True)
    assert out.exists()
    assert saved > 0

    # Verify both MP4s and sidecars were moved to trash/quarantine
    for p in (clip1, clip2, clip3, sidecar1, sidecar2, sidecar3):
        assert not p.exists(), f"{p.name} should have been quarantined"
