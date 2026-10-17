import json
from pathlib import Path

import pytest

from tools import run_windows_capture_smoke as smoke


def _run_capture_cycles(tmp_path, fake_run, *, backend="auto", cycles=1, manual=False):
    output = tmp_path / "evidence"
    output.mkdir()
    return smoke.run_capture_cycles(
        output=output,
        executable=tmp_path / "capture-smoke.exe",
        ffmpeg_bin=tmp_path / "ffmpeg",
        requested_backend=backend,
        cycles=cycles,
        manual=manual,
        runner=fake_run,
    ), output / "result.json"


def test_success_records_requested_and_actual_backend_and_denominator(tmp_path):
    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            in_progress = json.loads(
                (Path(stdout.name).parent / "result.json").read_text(
                    encoding="utf-8"))
            cycle_index = int(Path(command[2]).stem.split("-")[-1]) - 1
            assert in_progress["attempt_denominator"] == 2
            assert in_progress["cycles"][cycle_index]["status"] == "running"
            Path(command[2]).write_bytes(b"clip")
            stdout.write(b"SMOKE requested_backend=auto capture_backend=WGC_MONITOR\n")
            return smoke.subprocess.CompletedProcess(command, 0)
        return smoke.subprocess.CompletedProcess(command, 0)

    status, result_path = _run_capture_cycles(tmp_path, fake_run, cycles=2)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status == 0
    assert result["attempt_denominator"] == 2
    assert result["attempted_cycles"] == 2
    assert result["passed_cycles"] == 2
    assert [cycle["actual_backend"] for cycle in result["cycles"]] == [
        "WGC_MONITOR", "WGC_MONITOR"
    ]


def test_failed_native_capture_is_persisted_with_exact_error_and_nonzero_status(
    tmp_path,
):
    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            stdout.write(
                b"FTHR_STARTUP_ERROR: CAPTURE_TARGET_UNAVAILABLE: "
                b"api_call=CreateForWindow result=E_ACCESSDENIED "
                b'startup_context={"capture_backend":"WGC_WINDOW"}\n'
                b"SMOKE_ERROR stage=initialize native_startup_code="
                b"CAPTURE_TARGET_UNAVAILABLE detail=CreateForWindow failed\n"
            )
            return smoke.subprocess.CompletedProcess(command, 3)
        raise AssertionError("decode must not run after native capture failure")

    status, result_path = _run_capture_cycles(tmp_path, fake_run, cycles=2)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status != 0
    assert result["attempt_denominator"] == 2
    assert result["attempted_cycles"] == 2
    assert result["cycles"][0]["stage"] == "initialize"
    assert "CAPTURE_TARGET_UNAVAILABLE" in result["cycles"][0]["error"]
    assert result["cycles"][0]["actual_backend"] == "WGC_WINDOW"
    assert result["cycles"][0]["status"] == "failed"


def test_timeout_is_persisted_and_keeps_attempt_denominator(tmp_path):
    def fake_run(command, *, stdout, **kwargs):
        raise smoke.subprocess.TimeoutExpired(command, kwargs["timeout"])

    status, result_path = _run_capture_cycles(tmp_path, fake_run, cycles=3)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status != 0
    assert result["attempt_denominator"] == 3
    assert result["attempted_cycles"] == 3
    assert all(cycle["status"] == "failed" for cycle in result["cycles"])
    assert all("timed out" in cycle["error"] for cycle in result["cycles"])


def test_decode_failure_is_persisted_not_reported_as_pass(tmp_path):
    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            Path(command[2]).write_bytes(b"clip")
            stdout.write(b"SMOKE requested_backend=auto capture_backend=DXGI_OUTPUT_DUPLICATION\n")
            return smoke.subprocess.CompletedProcess(command, 0)
        stdout.write(b"Invalid data found when processing input\n")
        return smoke.subprocess.CompletedProcess(command, 1)

    status, result_path = _run_capture_cycles(tmp_path, fake_run)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status != 0
    assert result["cycles"][0]["status"] == "failed"
    assert result["cycles"][0]["stage"] == "full_decode"
    assert "Invalid data" in result["cycles"][0]["error"]


def test_replay_and_manual_decode_commands_are_strict_and_map_all_audio(tmp_path):
    decode_commands = []

    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            clip = Path(command[2])
            clip.write_bytes(b"replay")
            clip.with_suffix(".recording.mp4").write_bytes(b"manual")
            stdout.write(b"SMOKE requested_backend=auto capture_backend=WGC_MONITOR\n")
        else:
            decode_commands.append(command)
        return smoke.subprocess.CompletedProcess(command, 0)

    status, result_path = _run_capture_cycles(tmp_path, fake_run, manual=True)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status == 0
    assert len(decode_commands) == 2
    for command in decode_commands:
        assert "-xerror" in command
        assert command[command.index("-err_detect") + 1] == "explode"
        assert command[command.index("-map") + 1] == "0:v:0"
        assert command[command.index("-map", command.index("-map") + 1) + 1] == "0:a?"
    assert result["cycles"][0]["stages"]["full_decode"]["status"] == "passed"
    assert result["cycles"][0]["stages"]["manual_full_decode"]["status"] == "passed"


@pytest.mark.parametrize("artifact_state", ["missing", "zero_sized"])
def test_replay_artifact_must_exist_and_be_nonzero_before_decode(
    tmp_path, artifact_state,
):
    decode_started = False

    def fake_run(command, *, stdout, **kwargs):
        nonlocal decode_started
        if "capture-smoke.exe" in command[0]:
            if artifact_state == "zero_sized":
                Path(command[2]).write_bytes(b"")
            stdout.write(b"SMOKE requested_backend=auto capture_backend=WGC_MONITOR\n")
            return smoke.subprocess.CompletedProcess(command, 0)
        decode_started = True
        return smoke.subprocess.CompletedProcess(command, 0)

    status, result_path = _run_capture_cycles(tmp_path, fake_run)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    cycle = result["cycles"][0]
    assert status != 0
    assert not decode_started
    assert cycle["stage"] == "artifact_validation"
    assert cycle["stages"]["full_decode"]["status"] == "not_run"
    assert cycle["status"] == "failed"


@pytest.mark.parametrize("artifact_state", ["missing", "zero_sized"])
def test_invalid_manual_recording_is_not_marked_fully_decoded(
    tmp_path, artifact_state,
):
    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            Path(command[2]).write_bytes(b"replay")
            if artifact_state == "zero_sized":
                Path(command[2]).with_suffix(".recording.mp4").write_bytes(b"")
            stdout.write(b"SMOKE requested_backend=auto capture_backend=WGC_MONITOR\n")
        return smoke.subprocess.CompletedProcess(command, 0)

    status, result_path = _run_capture_cycles(tmp_path, fake_run, manual=True)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    cycle = result["cycles"][0]
    assert status != 0
    assert cycle["stages"]["full_decode"]["status"] == "passed"
    assert cycle["stage"] == "manual_artifact_validation"
    assert cycle["stages"]["manual_full_decode"]["status"] == "not_run"


def test_forced_backend_mismatch_refuses_qualification(tmp_path):
    def fake_run(command, *, stdout, **kwargs):
        if "capture-smoke.exe" in command[0]:
            Path(command[2]).write_bytes(b"clip")
            stdout.write(b"SMOKE requested_backend=dxgi capture_backend=WGC_MONITOR\n")
            return smoke.subprocess.CompletedProcess(command, 0)
        return smoke.subprocess.CompletedProcess(command, 0)

    status, result_path = _run_capture_cycles(tmp_path, fake_run, backend="dxgi")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status != 0
    assert result["cycles"][0]["stage"] == "backend_qualification"
    assert result["cycles"][0]["actual_backend"] == "WGC_MONITOR"


def test_output_preflight_refuses_existing_generated_targets(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    marker = output / "capture-1.mp4"
    marker.write_bytes(b"preserve")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        smoke.validate_output_directory(output, cycles=1, manual=False)
    assert marker.read_bytes() == b"preserve"


def test_native_build_failure_is_persisted_without_fake_attempts(tmp_path, monkeypatch):
    output = tmp_path / "build-failure"
    monkeypatch.setattr(smoke.sys, "platform", "win32")

    def fail_build(*args, **kwargs):
        raise RuntimeError("mocked MSBuild error")

    monkeypatch.setattr(smoke, "build_harness", fail_build)
    status = smoke.main([
        "--monitor", "test-monitor", "--output", str(output), "--cycles", "2",
    ])
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert status != 0
    assert result["status"] == "failed"
    assert result["stage"] == "build"
    assert result["attempt_denominator"] == 2
    assert result["attempted_cycles"] == 0
    assert all(cycle["status"] == "not_run" for cycle in result["cycles"])
    assert "mocked MSBuild error" in result["error"]
