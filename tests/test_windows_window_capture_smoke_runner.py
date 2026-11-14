import json
import subprocess

import pytest

from tools import run_windows_window_capture_smoke as smoke


def _native_log(*, capture_backend="WGC_WINDOW", phases=None, active_vendor="AMD"):
    phases = phases or {phase: True for phase in smoke.EXPECTED_PHASES}
    lines = [
        "WINDOW_SMOKE identity schema=1 capture_mode=WINDOW "
        f"capture_backend={capture_backend} requested_encoder=amd "
        "active_encoder_backend=ffmpeg-amf active_vendor=" + active_vendor + " "
        "video_codec=H264 fps=60 target_width=1280 target_height=720 "
        "audio=disabled video_only=true amd_hardware_proof=not_claimed",
    ]
    states = {
        "initial": ["start", "progress", "save_start", "save_success", "pass"],
        "resize": [
            "changed", "reconfigure_required", "save_rejected", "shutdown",
            "restarted", "progress", "save_start", "save_success", "pass",
        ],
        "minimize": ["minimized", "paused", "pass"],
        "restore": [
            "reconfigure_required", "save_rejected", "shutdown", "restarted",
            "progress", "save_start", "save_success", "pass",
        ],
        "close": ["start", "invalidated", "save_rejected", "shutdown", "pass"],
    }
    for phase, phase_states in states.items():
        if not phases.get(phase, True):
            continue
        for state in phase_states:
            suffix = " response=CLIP_SAVED" if state == "save_success" else ""
            if state == "restarted":
                suffix = " capture_backend=WGC_WINDOW"
            if state == "shutdown":
                suffix = " elapsed_ms=14" if phase == "resize" else " elapsed_ms=160"
            lines.append(f"WINDOW_SMOKE phase={phase} state={state}{suffix}")
    return "\n".join(lines) + "\n"


def _runner(tmp_path, *, native_text=None, native_returncode=0,
            missing=(), corrupt=(), timeout=False, capture_backend="WGC_WINDOW",
            phases=None, replay_storage=None, remaining_replay_file=False):
    output = tmp_path / "evidence"
    output.mkdir(parents=True)
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"helper")
    ffmpeg_bin = tmp_path / "ffmpeg"
    ffmpeg_bin.mkdir()
    calls = []

    def fake_run(command, *, stdout, **kwargs):
        calls.append(command)
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial")
        if "helper.exe" in command[0]:
            stdout.write((native_text or _native_log(
                capture_backend=capture_backend, phases=phases)).encode())
            if remaining_replay_file:
                spool = output / "replay-buffer"
                spool.mkdir()
                (spool / "orphan.tmp").write_bytes(b"orphan")
            for phase, name in smoke.MEDIA_NAMES.items():
                if phase not in missing:
                    path = output / name
                    if phase in corrupt:
                        path.write_bytes(b"corrupt")
                    else:
                        path.write_bytes(b"valid-media")
            return subprocess.CompletedProcess(command, native_returncode)
        if "ffmpeg.exe" in command[0]:
            if any(f"window-{phase}.mp4" in " ".join(command) for phase in corrupt):
                stdout.write(b"Invalid data found when processing input\n")
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)
        if "ffprobe.exe" in command[0]:
            stdout.write(json.dumps({
                "streams": [{
                    "index": 0, "codec_type": "video", "width": 1280,
                    "height": 720, "duration": "2.0", "nb_read_frames": "120",
                }],
            }).encode())
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(command)

    status = smoke.qualify_run(
        output=output, executable=helper, ffmpeg_bin=ffmpeg_bin,
        requested_encoder="amd", runner=fake_run,
        replay_storage=replay_storage,
    )
    return status, json.loads((output / "result.json").read_text()), calls, output


def test_storage_qualification_requires_native_backend_identity(tmp_path):
    status, result, calls, _output = _runner(tmp_path, replay_storage="disk")
    assert status != 0
    assert calls[0][-2:] == ["--replay-storage", "disk"]
    assert any("identity replay_storage" in item["error"] for item in result["failures"])


@pytest.mark.parametrize("mode", ["memory", "disk"])
def test_storage_qualification_checks_cleanup_and_media(tmp_path, mode):
    native = _native_log().replace("identity schema=1", f"identity schema=1 replay_storage={mode}")
    status, result, _calls, _output = _runner(
        tmp_path / "clean", native_text=native, replay_storage=mode)
    assert status == 0
    assert result["requested_replay_storage"] == mode
    status, result, _calls, _output = _runner(
        tmp_path / "orphan", native_text=native, replay_storage=mode,
        remaining_replay_file=True)
    assert status != 0
    assert any("temporary replay files" in item["error"] for item in result["failures"])


def _probe_payload(tmp_path, payload):
    output = tmp_path / "probe"
    output.mkdir(parents=True)
    clip = output / "clip.mp4"
    clip.write_bytes(b"media")
    ffmpeg_bin = tmp_path / "ffmpeg"
    ffmpeg_bin.mkdir()

    def fake_run(command, *, stdout, **kwargs):
        if "ffmpeg.exe" in command[0]:
            return subprocess.CompletedProcess(command, 0)
        stdout.write(json.dumps(payload).encode())
        return subprocess.CompletedProcess(command, 0)

    return smoke._probe_media(
        output=output, phase="probe", clip=clip, ffmpeg_bin=ffmpeg_bin,
        runner=fake_run,
    )


def test_success_requires_all_lifecycle_phases_and_media_validation(tmp_path):
    status, result, calls, _output = _runner(tmp_path)
    assert status == 0
    assert result["status"] == "passed"
    assert result["evidence_scope"] == {
        "media": "video_only", "audio": "disabled", "amd_hardware_proof": "not_claimed",
    }
    assert all(result["phases"][phase]["status"] == "passed"
               for phase in smoke.EXPECTED_PHASES)
    assert sum("ffmpeg.exe" in command[0] for command in calls) == 3
    assert sum("ffprobe.exe" in command[0] for command in calls) == 3


def test_wrong_capture_backend_and_missing_phase_evidence_cannot_pass(tmp_path):
    status, result, calls, _output = _runner(
        tmp_path, capture_backend="DXGI_OUTPUT_DUPLICATION",
        phases={"close": False},
    )
    assert status != 0
    assert result["status"] == "failed"
    assert any("capture_backend" in item["error"] for item in result["failures"])
    assert any("phase close missing evidence" in item["error"]
               for item in result["failures"])
    assert not any("ffmpeg.exe" in command[0] for command in calls)


def test_native_error_and_timeout_always_write_failed_result(tmp_path):
    error_status, error_result, _calls, error_output = _runner(
        tmp_path / "native-error", native_returncode=4,
        native_text="WINDOW_SMOKE_ERROR phase=initialize detail=CAPTURE_TARGET_UNAVAILABLE\n",
    )
    assert error_status != 0
    assert error_result["status"] == "failed"
    assert (error_output / "result.json").is_file()
    timeout_status, timeout_result, _calls, timeout_output = _runner(
        tmp_path / "timeout", timeout=True,
    )
    assert timeout_status != 0
    assert timeout_result["status"] == "failed"
    assert any("timed out" in item["error"] for item in timeout_result["failures"])
    assert (timeout_output / "result.json").is_file()


@pytest.mark.parametrize("missing,corrupt", [
    (("resize",), ()),
    ((), ("initial",)),
])
def test_missing_or_corrupt_media_cannot_pass(tmp_path, missing, corrupt):
    status, result, _calls, _output = _runner(
        tmp_path, missing=missing, corrupt=corrupt,
    )
    assert status != 0
    assert result["status"] == "failed"
    assert result["failed_runs"] == 1
    assert result["passed_runs"] == 0


def test_output_protection_preserves_existing_evidence(tmp_path):
    output = tmp_path / "old-evidence"
    output.mkdir()
    marker = output / "window-initial.mp4"
    marker.write_bytes(b"keep-me")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        smoke.validate_output_directory(output)
    assert marker.read_bytes() == b"keep-me"


def test_malformed_ffprobe_shapes_fail_as_validation_errors(tmp_path):
    evidence, error = _probe_payload(tmp_path / "top-level", [])
    assert evidence == {}
    assert "top-level" in error
    evidence, error = _probe_payload(
        tmp_path / "stream-type", {"streams": ["video"]}
    )
    assert evidence == {}
    assert "stream" in error and "type" in error


@pytest.mark.parametrize("duration", ["nan", "inf", "-inf", "1.0", "4.0"])
def test_nonfinite_or_implausible_duration_fails(tmp_path, duration):
    evidence, error = _probe_payload(tmp_path, {
        "streams": [{
            "codec_type": "video", "width": 1280, "height": 720,
            "duration": duration, "nb_read_frames": "120",
        }],
    })
    assert "actual_video_duration_seconds" in evidence
    assert error is not None


def test_too_few_frames_fails_media_validation(tmp_path):
    _evidence, error = _probe_payload(tmp_path, {
        "streams": [{
            "codec_type": "video", "width": 1280, "height": 720,
            "duration": "2.0", "nb_read_frames": "12",
        }],
    })
    assert error is not None and "frames" in error


def test_resize_reconfigure_without_restart_cannot_pass(tmp_path):
    native_text = _native_log().replace(
        "WINDOW_SMOKE phase=resize state=restarted capture_backend=WGC_WINDOW\n", ""
    )
    status, result, _calls, _output = _runner(tmp_path, native_text=native_text)
    assert status != 0
    assert any("missing restart evidence" in item["error"]
               for item in result["failures"])


def test_shutdown_elapsed_time_is_bounded(tmp_path):
    native_text = _native_log().replace(
        "WINDOW_SMOKE phase=close state=shutdown elapsed_ms=160",
        "WINDOW_SMOKE phase=close state=shutdown elapsed_ms=10001",
    )
    status, result, _calls, _output = _runner(tmp_path, native_text=native_text)
    assert status != 0
    assert any("shutdown exceeded 10s" in item["error"]
               for item in result["failures"])
