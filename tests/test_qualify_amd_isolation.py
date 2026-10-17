"""Pure deterministic tests for the opt-in AMD isolation runner."""

from __future__ import annotations

import sys
import tempfile
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import qualify_amd_isolation as runner
from qualify_amd_isolation import (
    classify_rgb_frames,
    parse_reported_identity,
    validate_helper_identity,
    validate_monotonic_pts,
)


def _synthetic_frames(
    width: int = 16,
    height: int = 8,
    count: int = 6,
    colors=((30, 50, 220), (40, 210, 40), (220, 40, 40), (160, 160, 160)),
) -> bytes:
    frames = bytearray()
    for frame in range(count):
        bar_x = (frame * 2) % width
        for y in range(height):
            for x in range(width):
                quadrant = (0 if x < width // 2 else 1) + (0 if y < height // 2 else 2)
                color = list(colors[quadrant])
                if bar_x <= x < min(width, bar_x + 2):
                    color = [255, 255, 255]
                frames.extend(color)
    return bytes(frames)


def test_pts_are_strictly_monotonic_and_reject_bad_values() -> None:
    assert validate_monotonic_pts([0.0, 0.1, 0.2])["valid"]
    assert not validate_monotonic_pts([0.0, 0.2, 0.2])["valid"]
    assert not validate_monotonic_pts([0.0, float("nan")])["valid"]


def test_controlled_pattern_has_distinct_quadrants_and_motion() -> None:
    result = classify_rgb_frames(16, 8, _synthetic_frames())
    assert result["valid"]
    assert result["nonblack"]
    assert result["quadrants_distinct"]
    assert result["moving"]


def test_controlled_pattern_rejects_static_black_frames() -> None:
    result = classify_rgb_frames(16, 8, bytes(16 * 8 * 3 * 3))
    assert not result["valid"]
    assert not result["nonblack"]


def test_controlled_pattern_rejects_swapped_quadrants() -> None:
    raw = bytearray(_synthetic_frames())
    width, height, count = 16, 8, 6
    frame_size = width * height * 3
    for frame in range(count):
        base = frame * frame_size
        for y in range(height // 2):
            for x in range(width // 2):
                left = base + (y * width + x) * 3
                right = base + (y * width + x + width // 2) * 3
                raw[left:left + 3], raw[right:right + 3] = raw[right:right + 3], raw[left:left + 3]
    result = classify_rgb_frames(width, height, bytes(raw))
    assert not result["valid"]
    assert not result["quadrants_match_expected"]


def test_controlled_pattern_rejects_materially_wrong_near_colors() -> None:
    result = classify_rgb_frames(
        16,
        8,
        _synthetic_frames(
            colors=((0, 100, 130), (100, 130, 0), (130, 0, 100), (80, 80, 80))
        ),
    )
    assert not result["valid"]
    assert not result["quadrants_match_expected"]


def test_identity_fails_closed_for_wrong_vendor_or_mode() -> None:
    valid_a = "AMD_ISOLATION stage=A status=pass vendor=AMD backend=ffmpeg-amf mode=direct_gpu"
    assert validate_helper_identity(valid_a, "a")[0]
    assert not validate_helper_identity(valid_a.replace("vendor=AMD", "vendor=NVIDIA"), "a")[0]
    assert not validate_helper_identity(valid_a, "b")[0]
    valid_c = (
        "AMD_ISOLATION stage=C status=pass vendor=AMD backend=ffmpeg-amf "
        "mode=real_wgc_saveclip capture_backend=WGC_MONITOR"
    )
    assert validate_helper_identity(valid_c, "c")[0]


def test_reported_identity_normalizes_native_stage_case_for_a_b_c_and_probe() -> None:
    a = parse_reported_identity(
        'AMD_ISOLATION stage=A status=pass vendor=AMD backend=ffmpeg-amf '
        'codec=h264_amf fps=60 mode=direct_gpu adapter_luid_high=11 '
        'adapter_luid_low=22 width=640 height=360',
        "a",
    )
    b = parse_reported_identity(
        'AMD_ISOLATION stage=B status=pass vendor=AMD backend=ffmpeg-amf '
        'codec=h264_amf fps=60 mode=production_transform adapter_luid_high=33 '
        'adapter_luid_low=44 width=960 height=540',
        "b",
    )
    c = parse_reported_identity(
        'AMD_ISOLATION stage=C status=transport_decode_pass_content_pending vendor=AMD '
        'backend=ffmpeg-amf codec=h264 fps=60 mode=real_wgc_saveclip '
        'capture_backend=WGC_MONITOR target_width=1920 target_height=1080 '
        'source_width=2560 source_height=1440 monitor_adapter_luid_high=55 '
        'monitor_adapter_luid_low=66 controlled_content=false',
        "c",
    )
    probe = parse_reported_identity(
        "AMD_ISOLATION stage=probe status=pass vendor=AMD backend=ffmpeg-amf adapter_count=1",
        "probe",
    )
    assert (a["fps"], a["width"], a["adapter_luid_low"]) == ("60", "640", "22")
    assert (b["fps"], b["height"], b["adapter_luid_high"]) == ("60", "540", "33")
    assert (c["target_width"], c["source_height"], c["monitor_adapter_luid_low"]) == (
        "1920", "1440", "66"
    )
    assert probe["adapter_count"] == "1"


def test_process_error_is_recorded_in_stage_log() -> None:
    original = runner._run_process

    def fail_process(*_args, **_kwargs):
        raise OSError("synthetic_missing_helper")

    runner._run_process = fail_process
    try:
        with tempfile.TemporaryDirectory() as directory:
            result = runner.run_logged(["missing-helper"], Path(directory) / "stage.log", 1.0)
            assert result["status"] == "error"
            assert "synthetic_missing_helper" in (Path(directory) / "stage.log").read_text()
    finally:
        runner._run_process = original


def test_runner_rejects_fewer_than_twelve_frames_and_counts_only_real_saves() -> None:
    valid = Namespace(frames=12, width=640, height=360, output_width=960, output_height=540, fps=60)
    runner.validate_runner_args(valid)
    invalid = Namespace(frames=11, width=640, height=360, output_width=960, output_height=540, fps=60)
    try:
        runner.validate_runner_args(invalid)
    except ValueError as error:
        assert "at least 12" in str(error)
    else:
        raise AssertionError("runner accepted fewer than twelve frames")
    assert runner.parse_save_attempts("") == {
        "planned": 1, "denominator": 0, "attempted": 0, "completed": 0, "failed": 0,
    }
    marker = "AMD_ISOLATION SAVE_ATTEMPT stage=C attempt=1 denominator=1\n"
    counted = runner.parse_save_attempts(
        marker + "AMD_ISOLATION stage=C status=transport_decode_pass_content_pending vendor=AMD"
    )
    assert counted["attempted"] == 1
    assert counted["denominator"] == 1
    assert counted["completed"] == 1
