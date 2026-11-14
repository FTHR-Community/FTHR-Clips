"""Pure deterministic tests for the opt-in AMD isolation runner."""

from __future__ import annotations

import sys
import tempfile
import hashlib
import subprocess
import shutil
import json
from argparse import Namespace
from pathlib import Path

import pytest


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
                if y >= height * 7 // 8:
                    color = [runner.GRAY_LEVELS[min(3, x * 4 // width)]] * 3
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
    valid_a = _stage_identity("a")
    assert validate_helper_identity(valid_a, "a")[0]
    assert not validate_helper_identity(valid_a.replace("vendor=AMD", "vendor=NVIDIA"), "a")[0]
    assert not validate_helper_identity(valid_a, "b")[0]
    valid_c = _stage_identity("c")
    assert validate_helper_identity(valid_c, "c")[0]


def _stage_identity(mode: str) -> str:
    if mode == "probe":
        return ("AMD_ISOLATION stage=probe status=pass vendor=AMD "
                "backend=ffmpeg-amf protocol=3")
    stage = mode.upper().replace("-", "_")
    native_mode = {"a": "direct_gpu", "a-array": "direct_gpu",
                   "b": "production_transform", "c": "real_wgc_saveclip",
                   "b-bgra": "comparison_transform", "b-nv12": "comparison_transform"}[mode]
    status = "transport_decode_pass_content_pending" if mode == "c" else "pass"
    layout = "texture_array" if mode == "a-array" else "single_texture"
    identity = (f"AMD_ISOLATION stage={stage} status={status} vendor=AMD "
                f"backend=ffmpeg-amf protocol=3 mode={native_mode} input_layout={layout}")
    if mode == "c":
        return identity + " capture_backend=WGC_MONITOR"
    array_size = 8 if mode == "a-array" else 1
    input_format = "NV12" if mode == "b-nv12" else "BGRA"
    identity += f" input_format={input_format} input_array_size={array_size} input_slice=0"
    if mode in {"b-bgra", "b-nv12"}:
        conversion = "bt709_full_to_studio" if mode == "b-nv12" else "production_bgra"
        identity += (f" color_conversion={conversion} pattern=quadrants_gray_bar_v2 codec=h264_amf"
                     " bitrate_kbps=8000 width=960 height=540 source_width=640 source_height=360"
                     " fps=60 frames=120 warmup_frames=12 measured_frames=108 submitted_frames=120"
                     " driver_version=12345 adapter_luid_high=1 adapter_luid_low=2"
                     " timing_scope=cpu_api_wall end_to_end_ms=500")
        for phase in ("upload", "prepare", "encode"):
            identity += f" {phase}_p50_ms=0.1 {phase}_p95_ms=0.2 {phase}_p99_ms=0.3"
    return identity


@pytest.mark.parametrize("mode", ["a", "a-array", "b", "c"])
def test_identity_requires_new_helper_and_exact_layout(mode) -> None:
    identity = _stage_identity(mode)
    assert validate_helper_identity(identity, mode)[0]
    assert not validate_helper_identity(identity.replace("protocol=3", ""), mode)[0]
    wrong_layout = "single_texture" if mode == "a-array" else "texture_array"
    expected_layout = "texture_array" if mode == "a-array" else "single_texture"
    assert not validate_helper_identity(
        identity.replace(f"input_layout={expected_layout}", f"input_layout={wrong_layout}"),
        mode)[0]


@pytest.mark.parametrize("mode,size,slice_index", [
    ("a", 8, 0), ("b", 1, 1), ("a-array", 1, 0), ("a-array", 8, 8),
])
def test_actual_texture_descriptor_must_match_claimed_layout(mode, size, slice_index) -> None:
    identity = _stage_identity(mode)
    expected_size = 8 if mode == "a-array" else 1
    identity = identity.replace(f"input_array_size={expected_size}", f"input_array_size={size}")
    identity = identity.replace("input_slice=0", f"input_slice={slice_index}")
    assert not validate_helper_identity(identity, mode)[0]


def test_identity_cannot_be_assembled_from_other_stage_or_device_lines() -> None:
    identity = _stage_identity("a").replace("vendor=AMD", "vendor=NVIDIA")
    assert not validate_helper_identity("AMD_ISOLATION_DEVICE vendor=AMD\n" + identity, "a")[0]
    assert parse_reported_identity(_stage_identity("a-array"), "a-array")["input_layout"] == "texture_array"


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
    valid = Namespace(frames=12, width=640, height=360, output_width=960, output_height=540,
                      fps=60, comparison_rounds=2)
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


def test_default_geometry_is_native_1080p60_and_field_is_explicit() -> None:
    args = runner._argument_parser().parse_args(["--output", "unused"])
    assert (args.width, args.height, args.output_width, args.output_height, args.fps) == (
        1920, 1080, 1920, 1080, 60)
    assert not args.field_geometry
    field = runner._argument_parser().parse_args(["--output", "unused", "--field-geometry"])
    runner.apply_geometry_profile(field)
    assert (field.width, field.height, field.output_width, field.output_height) == (
        1920, 1080, 2560, 1440)


def test_hash_evidence_matches_actual_bytes_and_missing_is_explicit(tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"actual-evidence")
    assert runner.file_evidence(artifact) == {
        "path": str(artifact), "size_bytes": 15,
        "sha256": hashlib.sha256(b"actual-evidence").hexdigest(), "available": True,
    }
    assert runner.file_evidence(tmp_path / "missing")["available"] is False


def test_timeout_retains_byte_output_and_never_reports_pass(monkeypatch, tmp_path) -> None:
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("helper", 0.01, output=b"partial native evidence", stderr=b"native error")

    monkeypatch.setattr(runner, "_run_process", timed_out)
    log = tmp_path / "timeout.log"
    result = runner.run_logged(["helper"], log, 0.01)
    assert result["status"] == "timeout"
    assert "partial native evidence" in log.read_text()
    assert "b'" not in log.read_text()


def test_portable_helper_skips_build_and_missing_amd_is_not_a_pass(monkeypatch, tmp_path) -> None:
    helper = tmp_path / "portable.exe"
    helper.write_bytes(b"test-only-helper")

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("portable mode must not require Visual Studio")

    monkeypatch.setattr(runner, "build_harness", forbidden_build)
    monkeypatch.setattr(runner, "_run_stage", lambda *_args: {
        "status": "AMD_HARDWARE_REQUIRED", "run": {"returncode": runner.AMD_REQUIRED_EXIT}})
    output = tmp_path / "evidence"
    assert runner.main(["--output", str(output), "--helper", str(helper)]) == runner.AMD_REQUIRED_EXIT
    import json
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "AMD_HARDWARE_REQUIRED"
    assert result["production_status"] == "AMD_HARDWARE_REQUIRED"
    assert result["array_control_status"] == "not_run"
    assert all(result["stages"][mode]["status"] == "not_run" for mode in ("a", "b", "c"))
    assert result["build"]["status"] == "not_run:prebuilt_helper"


def test_native_amf_results_require_observed_correlated_ffmpeg_log() -> None:
    assert runner.parse_amf_failure_evidence("avcodec_send_frame failed: Unknown error occurred")["native_amf_results"] == []
    log = "[h264_amf @ 00000123] SubmitInput() failed with error 18\n"
    evidence = runner.parse_amf_failure_evidence(log * 100)
    assert len(evidence["native_amf_results"]) == 8
    native = evidence["native_amf_results"][0]
    assert native["result"] == 18
    assert native["ffmpeg_context"] == "00000123"
    assert native["domain"] == "AMF_RESULT"
    assert "hresult" not in native
    assert native["sdk_header_revision"] == "unavailable:not_recorded"


def test_portable_runner_help_has_no_source_tree_or_build_import_dependency(tmp_path) -> None:
    copied = tmp_path / "standalone" / "qualify_amd_isolation.py"
    copied.parent.mkdir()
    shutil.copyfile(runner.__file__, copied)
    completed = subprocess.run([sys.executable, str(copied), "--help"],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert "--ffmpeg-bin" in completed.stdout


def test_explicit_runtime_missing_tools_fails_before_launch(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    def forbidden_stage(*_args, **_kwargs):
        raise AssertionError("missing runtime must not launch helper")
    monkeypatch.setattr(runner, "_run_stage", forbidden_stage)
    assert runner.main(["--output", str(tmp_path / "evidence"),
                        "--helper", "unused.exe", "--ffmpeg-bin", str(runtime)]) == 2


def test_child_runtime_path_is_transient_and_not_a_system_path_change(tmp_path, monkeypatch) -> None:
    import os
    original_path = os.environ.get("PATH", "")
    seen = {}
    def run(*_args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["helper"], 0, "", "")
    monkeypatch.setattr(runner.subprocess, "run", run)
    runner._run_process(["helper"], timeout=1.0, runtime_dir=tmp_path)
    assert seen["env"]["PATH"].startswith(str(tmp_path) + os.pathsep)
    assert os.environ.get("PATH", "") == original_path


def _simulate_qualification(monkeypatch, tmp_path, *, failed_modes=(), compare_array=True,
                            monitor=True, old_helper=False, timed_out_modes=(),
                            save_attempt_count=1, compare_nv12=False,
                            identity_changes=None, decoded_frame_count=120):
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"test helper identity")
    modes = []
    decoded_sizes = {}

    def native_process(command, **_kwargs):
        mode = command[command.index("--mode") + 1]
        modes.append(mode)
        if mode in timed_out_modes:
            raise subprocess.TimeoutExpired(command, 1, output=b"partial evidence")
        identity = _stage_identity(mode)
        if identity_changes and mode in identity_changes:
            before, after = identity_changes[mode]
            identity = identity.replace(before, after)
        if mode == "c":
            identity = ("AMD_ISOLATION SAVE_ATTEMPT stage=C attempt=1 denominator=1\n"
                        * save_attempt_count) + identity
        if old_helper:
            identity = identity.replace("protocol=3", "")
        return subprocess.CompletedProcess(command, 0, identity, "")

    def decode(_ffmpeg, _ffprobe, artifact, expected_size, *_args):
        mode = artifact.stem
        decoded_sizes[mode] = expected_size
        return {"valid": mode not in failed_modes, "reason": f"bad_content:{mode}",
                "pts": {"count": decoded_frame_count}}

    monkeypatch.setattr(runner, "_run_process", native_process)
    monkeypatch.setattr(runner, "_decode_media", decode)
    output = tmp_path / "result"
    args = ["--output", str(output), "--helper", str(helper),
            "--width", "640", "--height", "360",
            "--output-width", "960", "--output-height", "540"]
    if compare_array:
        args.append("--compare-array")
    if compare_nv12:
        args.append("--compare-nv12")
    if monitor:
        args += ["--monitor-path", "explicit-test-monitor"]
    status = runner.main(args)
    return status, json.loads((output / "result.json").read_text()), modes, decoded_sizes


@pytest.mark.parametrize("failed_modes,expected_exit", [
    ((), 0), (("a-array",), 0), (("b",), 1), (("a", "a-array"), 1), (("c",), 1),
])
def test_array_control_never_changes_production_result(
        monkeypatch, tmp_path, failed_modes, expected_exit):
    status, result, modes, sizes = _simulate_qualification(
        monkeypatch, tmp_path, failed_modes=failed_modes)
    assert status == expected_exit
    assert result["schema_version"] == 3
    assert result["production_status"] == ("PARTIAL" if expected_exit == 0 else "failed")
    assert result["array_control_status"] == ("failed" if "a-array" in failed_modes else "pass")
    assert result["stages"]["a-array"]["role"] == "comparison_control"
    assert modes == ["probe", "a", "b", "c", "a-array"]
    assert sizes["a"] == sizes["a-array"] == (640, 360)
    assert sizes["b"] == sizes["c"] == (960, 540)


def test_default_run_excludes_legacy_array_control(monkeypatch, tmp_path):
    status, result, modes, _sizes = _simulate_qualification(
        monkeypatch, tmp_path, compare_array=False)
    assert status == 0
    assert modes == ["probe", "a", "b", "c"]
    assert result["array_control_status"] == "not_run"
    assert result["stages"]["a-array"]["reason"] == "comparison_not_requested"


def test_missing_monitor_never_claims_completed_production_run(monkeypatch, tmp_path):
    status, result, modes, _sizes = _simulate_qualification(
        monkeypatch, tmp_path, monitor=False)
    assert status == 1
    assert result["production_status"] == "failed"
    assert result["stages"]["c"]["reason"] == "monitor_path_required"
    assert result["save_attempts"]["denominator"] == 0
    assert "c" not in modes


def test_old_helper_stops_before_capture_and_requires_rebuild(monkeypatch, tmp_path):
    status, result, modes, sizes = _simulate_qualification(
        monkeypatch, tmp_path, old_helper=True)
    assert status == 1
    assert modes == ["probe"]
    assert not sizes
    assert result["production_status"] == "not_run"
    assert result["stages"]["probe"]["reason"] == "helper_protocol_mismatch:rebuild_helper"


def test_array_control_timeout_keeps_production_result(monkeypatch, tmp_path):
    status, result, modes, sizes = _simulate_qualification(
        monkeypatch, tmp_path, timed_out_modes={"a-array"})
    assert status == 0
    assert result["production_status"] == "PARTIAL"
    assert result["array_control_status"] == "timeout"
    assert modes[-1] == "a-array"
    assert "a-array" not in sizes


@pytest.mark.parametrize("save_attempt_count", [0, 2])
def test_capture_stage_requires_one_completed_save_attempt(
        monkeypatch, tmp_path, save_attempt_count):
    status, result, _modes, sizes = _simulate_qualification(
        monkeypatch, tmp_path, save_attempt_count=save_attempt_count)
    assert status == 1
    assert result["stages"]["c"]["reason"] == "missing_completed_save_attempt"
    assert "c" not in sizes
    assert result["save_attempts"]["denominator"] == save_attempt_count


def test_capture_identity_uses_final_record_instead_of_initialization():
    completed = _stage_identity("c")
    initialized = completed.replace(
        "status=transport_decode_pass_content_pending", "status=initialized")
    assert not validate_helper_identity(initialized, "c")[0]
    assert validate_helper_identity(initialized + "\n" + completed, "c")[0]


def test_controlled_pattern_rejects_incorrect_full_limited_range():
    raw = _synthetic_frames()
    limited = bytes(round(16 + value * 219 / 255) for value in raw)
    evidence = classify_rgb_frames(16, 8, limited)
    assert evidence["moving"] and evidence["quadrants_match_expected"]
    assert not evidence["valid"]
    assert not evidence["gray_levels_match_expected"]
    assert evidence["max_gray_level_error"] >= 16


def test_controlled_pattern_rejects_full_range_expanded_as_studio_range():
    raw = _synthetic_frames()
    expanded = bytes(max(0, min(255, round((value - 16) * 255 / 219))) for value in raw)
    evidence = classify_rgb_frames(16, 8, expanded)
    assert evidence["quadrants_match_expected"]
    assert not evidence["valid"]
    assert not evidence["gray_levels_match_expected"]


@pytest.mark.parametrize("mode", ["b-bgra", "b-nv12"])
def test_format_comparison_requires_exact_input_format_and_conversion(mode):
    identity = _stage_identity(mode)
    assert validate_helper_identity(identity, mode)[0]
    requested = "NV12" if mode == "b-nv12" else "BGRA"
    other = "BGRA" if mode == "b-nv12" else "NV12"
    assert not validate_helper_identity(identity.replace(f"input_format={requested}",
                                                       f"input_format={other}"), mode)[0]
    assert not validate_helper_identity(identity.replace("single_texture", "texture_array"), mode)[0]
    assert not validate_helper_identity(identity.replace("color_conversion=", "wrong_conversion="), mode)[0]


def _comparison_args():
    return Namespace(width=640, height=360, output_width=960, output_height=540,
                     fps=60, frames=120)


def test_comparison_parses_numeric_measurement_keys_from_final_identity():
    fields = parse_reported_identity(_stage_identity("b-nv12"), "b-nv12")
    timings = runner.parse_comparison_timings(fields, _comparison_args())
    assert timings["valid"]
    assert timings["prepare_p99_ms"] == 0.3
    assert timings["measured_frames"] == 108
    assert not timings["gpu_execution_time_measured"]


@pytest.mark.parametrize("field,value", [
    ("end_to_end_ms", "0"), ("end_to_end_ms", "nan"), ("end_to_end_ms", "inf"),
    ("prepare_p95_ms", "-1"), ("encode_p99_ms", "0.01"),
    ("prepare_p99_ms", "501"),
    ("upload_p50_ms", "nan"), ("measured_frames", "120"),
    ("warmup_frames", "0"), ("submitted_frames", "119"),
    ("fps", "30"), ("source_width", "960"), ("timing_scope", "gpu_execution"),
])
def test_comparison_rejects_invalid_or_unrelated_measurements(field, value):
    fields = parse_reported_identity(_stage_identity("b-nv12"), "b-nv12")
    fields[field] = value
    assert not runner.parse_comparison_timings(fields, _comparison_args())["valid"]


def test_comparison_requires_all_phase_measurements():
    fields = parse_reported_identity(_stage_identity("b-nv12"), "b-nv12")
    del fields["upload_p99_ms"]
    assert not runner.parse_comparison_timings(fields, _comparison_args())["valid"]


def test_optional_nv12_comparison_alternates_order_and_keeps_production_default(monkeypatch, tmp_path):
    status, result, modes, sizes = _simulate_qualification(monkeypatch, tmp_path, compare_nv12=True)
    assert status == 0 and result["production_status"] == "PARTIAL"
    assert modes == ["probe", "a", "b", "c", "b-bgra", "b-nv12", "b-nv12", "b-bgra", "a-array"]
    comparison = result["nv12_comparison"]
    assert comparison["status"] == "pass"
    assert comparison["production_default"] == "BGRA"
    assert not comparison["automatic_format_selection"]
    assert not comparison["gpu_execution_time_measured"]
    assert not comparison["gameplay_performance_proven"]
    assert comparison["median_end_to_end_nv12_over_bgra"] == 1.0
    assert [run["order"] for run in comparison["rounds"]] == [
        ["b-bgra", "b-nv12"], ["b-nv12", "b-bgra"]]
    assert sizes["b-bgra"] == sizes["b-nv12"] == (960, 540)
    for run in comparison["rounds"]:
        assert all(stage["role"] == "format_comparison" for stage in run["stages"].values())


@pytest.mark.parametrize("failed,timed_out", [
    (("b-nv12",), ()), (("b-bgra",), ()), ((), ("b-nv12",)),
])
def test_format_failure_does_not_change_the_production_result(monkeypatch, tmp_path, failed, timed_out):
    status, result, modes, _ = _simulate_qualification(
        monkeypatch, tmp_path, compare_nv12=True, failed_modes=failed, timed_out_modes=timed_out)
    assert status == 0 and result["production_status"] == "PARTIAL"
    if "b-bgra" in failed:
        assert "b-nv12" not in modes
    else:
        assert "b-nv12" in modes
    assert result["nv12_comparison"]["status"] == "failed"
    assert result["nv12_comparison"]["stopped_after_failure"]
    assert len(result["nv12_comparison"]["rounds"]) == 1
    assert "median_end_to_end_nv12_over_bgra" not in result["nv12_comparison"]


@pytest.mark.parametrize("before,after", [
    ("adapter_luid_low=2", "adapter_luid_low=3"),
    ("driver_version=12345", "driver_version=54321"),
    ("driver_version=12345", "driver_version=unavailable"),
])
def test_comparison_requires_the_same_actual_adapter_and_driver(monkeypatch, tmp_path, before, after):
    status, result, _, _ = _simulate_qualification(
        monkeypatch, tmp_path, compare_nv12=True, identity_changes={"b-nv12": (before, after)})
    assert status == 0
    assert result["nv12_comparison"]["status"] == "failed"
    assert "median_end_to_end_nv12_over_bgra" not in result["nv12_comparison"]


def test_end_to_end_ratio_uses_nv12_over_bgra_without_claiming_gpu_latency(monkeypatch, tmp_path):
    _, result, _, _ = _simulate_qualification(
        monkeypatch, tmp_path, compare_nv12=True,
        identity_changes={"b-nv12": ("end_to_end_ms=500", "end_to_end_ms=250")})
    comparison = result["nv12_comparison"]
    assert comparison["status"] == "pass"
    assert comparison["end_to_end_nv12_over_bgra"] == [0.5, 0.5]
    assert comparison["median_end_to_end_nv12_over_bgra"] == 0.5
    assert not comparison["gpu_execution_time_measured"]


def test_previous_protocol_two_helper_is_rejected_before_comparisons(monkeypatch, tmp_path):
    status, result, modes, _ = _simulate_qualification(
        monkeypatch, tmp_path, compare_nv12=True,
        identity_changes={"probe": ("protocol=3", "protocol=2")})
    assert status == 1 and modes == ["probe"]
    assert result["nv12_comparison"]["status"] == "not_run"


def test_nv12_comparison_is_not_run_by_default(monkeypatch, tmp_path):
    _, result, modes, _ = _simulate_qualification(monkeypatch, tmp_path)
    assert result["nv12_comparison"]["reason"] == "comparison_not_requested"
    assert "b-bgra" not in modes and "b-nv12" not in modes


def test_extra_decoded_frames_cannot_pass_as_a_faster_comparison(monkeypatch, tmp_path):
    _, result, modes, _ = _simulate_qualification(
        monkeypatch, tmp_path, compare_nv12=True, decoded_frame_count=121)
    comparison = result["nv12_comparison"]
    assert comparison["status"] == "failed"
    assert comparison["first_failed_mode"] == "b-bgra"
    assert "b-nv12" not in modes
    assert comparison["rounds"][0]["stages"]["b-bgra"]["reason"] == "comparison_decoded_frame_count_mismatch"


@pytest.mark.parametrize("rounds", [0, 4])
def test_comparison_round_count_is_bounded_before_launch(tmp_path, rounds):
    output = tmp_path / "not-created"
    assert runner.main(["--output", str(output), "--comparison-rounds", str(rounds)]) == 2
    assert not output.exists()
