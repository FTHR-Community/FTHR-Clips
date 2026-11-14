"""Qualify the WGC WINDOW lifecycle against the production CaptureEngine.

The native helper owns one visible animated HWND and runs the same selected
window through startup, resize, minimize, restore, save, and close phases. The
runner keeps all evidence in a new external directory and validates every
saved clip with a complete FFmpeg decode plus ffprobe frame and duration
metadata. This is video-only evidence; it does not establish an AMD result on
a non-AMD adapter.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.run_windows_capture_smoke import build_harness


BACKENDS = ("nvidia", "amd", "intel", "software")
EXPECTED_CAPTURE_BACKEND = "WGC_WINDOW"
EXPECTED_PHASES = ("initial", "resize", "minimize", "restore", "close")
MEDIA_PHASES = ("initial", "resize", "restore")
MEDIA_NAMES = {
    "initial": "window-initial.mp4",
    "resize": "window-resize.mp4",
    "restore": "window-restore.mp4",
}
NATIVE_TIMEOUT_SECONDS = 180
FFMPEG_TIMEOUT_SECONDS = 60
FFPROBE_TIMEOUT_SECONDS = 30
IDENTITY_RE = re.compile(r"^WINDOW_SMOKE identity\s+(?P<body>.*)$")
PHASE_RE = re.compile(r"^WINDOW_SMOKE phase=(?P<phase>\S+) state=(?P<state>\S+)(?:\s+(?P<body>.*))?$")
ERROR_RE = re.compile(r"^WINDOW_SMOKE_ERROR\s+(?P<body>.*)$")


def _parse_key_values(body: str | None) -> dict[str, str]:
    if not body:
        return {}
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)", body)
    }


def _new_results(output: Path, encoder: str) -> dict:
    return {
        "schema_version": 1,
        "status": "running",
        "stage": "preflight",
        "requested_encoder": encoder,
        "requested_capture_mode": "WINDOW",
        "attempt_denominator": 1,
        "attempted_runs": 0,
        "passed_runs": 0,
        "failed_runs": 0,
        "evidence_scope": {
            "media": "video_only",
            "audio": "disabled",
            "amd_hardware_proof": "not_claimed",
        },
        "identity": {},
        "phases": {
            phase: {
                "status": "not_run",
                "states": [],
                "artifact": MEDIA_NAMES.get(phase),
                "failures": [],
            }
            for phase in EXPECTED_PHASES
        },
        "failures": [],
        "output": str(output),
    }


def _write_results(output: Path, results: dict) -> None:
    (output / "result.json").write_text(
        json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
    )


def validate_output_directory(output: Path) -> Path:
    """Require a new external directory and never overwrite old evidence."""
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("--output must be outside the repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(
            f"--output must be a new or empty directory; refusing to overwrite: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    return output


def _log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _failure_detail(path: Path, fallback: str) -> str:
    lines = [line.strip() for line in _log_text(path).splitlines() if line.strip()]
    errors = [line for line in lines if "WINDOW_SMOKE_ERROR" in line]
    if errors:
        return errors[-1]
    native = [line for line in lines if "FTHR_STARTUP_ERROR:" in line]
    if native:
        return native[-1]
    if lines:
        return f"{fallback}; log tail: {' | '.join(lines[-8:])}; log={path}"
    return f"{fallback}; log={path}"


def _run_logged(
    command: list[str], log_path: Path, timeout: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[int | None, str | None]:
    try:
        with log_path.open("wb") as log:
            result = runner(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
    except subprocess.TimeoutExpired:
        return None, _failure_detail(log_path, f"timed out after {timeout}s")
    except Exception as error:  # launcher failures are evidence too
        return None, (
            f"runner failed for {command[0]}: {type(error).__name__}: {error}; "
            f"log={log_path}"
        )
    if result.returncode != 0:
        return result.returncode, _failure_detail(
            log_path, f"process exited with code {result.returncode}"
        )
    return 0, None


def parse_native_evidence(log: str) -> tuple[dict[str, str], dict[str, list[dict[str, str]]], list[str]]:
    """Parse only complete helper markers; arbitrary log text never passes a phase."""
    identity: dict[str, str] = {}
    phases: dict[str, list[dict[str, str]]] = {phase: [] for phase in EXPECTED_PHASES}
    errors: list[str] = []
    for line in log.splitlines():
        identity_match = IDENTITY_RE.match(line.strip())
        if identity_match:
            identity = _parse_key_values(identity_match.group("body"))
            continue
        phase_match = PHASE_RE.match(line.strip())
        if phase_match and phase_match.group("phase") in phases:
            phases[phase_match.group("phase")].append({
                "state": phase_match.group("state"),
                **_parse_key_values(phase_match.group("body")),
            })
            continue
        error_match = ERROR_RE.match(line.strip())
        if error_match:
            errors.append(line.strip())
    return identity, phases, errors


def _expected_encoder_backend(encoder: str) -> str:
    return {
        "nvidia": "native-nvenc",
        "amd": "ffmpeg-amf",
        "intel": "ffmpeg-qsv",
        "software": "software-openh264",
    }[encoder]


def _update_phase_results(results: dict, phase_records: dict[str, list[dict[str, str]]]) -> None:
    for phase, records in phase_records.items():
        entry = results["phases"][phase]
        entry["states"] = [record["state"] for record in records]
        entry["evidence"] = records
        if any(record["state"] == "pass" for record in records):
            entry["status"] = "passed"


def _validate_identity(results: dict, identity: dict[str, str]) -> list[str]:
    expected_backend = _expected_encoder_backend(results["requested_encoder"])
    required = {
        "capture_mode": "WINDOW",
        "capture_backend": EXPECTED_CAPTURE_BACKEND,
        "requested_encoder": results["requested_encoder"],
        "active_encoder_backend": expected_backend,
        "video_codec": "H264",
        "fps": "60",
        "target_width": "1280",
        "target_height": "720",
        "audio": "disabled",
        "video_only": "true",
    }
    if results.get("requested_replay_storage"):
        required["replay_storage"] = results["requested_replay_storage"]
    failures = [
        f"identity {key}={identity.get(key)!r}, expected {value!r}"
        for key, value in required.items()
        if identity.get(key) != value
    ]
    if identity.get("amd_hardware_proof") not in {
        "not_claimed", "unavailable_on_non_amd_adapter"
    }:
        failures.append("identity amd_hardware_proof is not explicitly bounded")
    return failures


def _validate_phases(results: dict, phase_records: dict[str, list[dict[str, str]]]) -> list[str]:
    failures: list[str] = []
    required_states = {
        "initial": {"start", "progress", "save_start", "save_success", "pass"},
        "resize": {"changed", "pass"},
        "minimize": {"minimized", "paused", "pass"},
        "restore": {"progress", "save_start", "save_success", "pass"},
        "close": {"start", "invalidated", "save_rejected", "shutdown", "pass"},
    }
    for phase in EXPECTED_PHASES:
        observed = {record["state"] for record in phase_records.get(phase, [])}
        missing = sorted(required_states[phase] - observed)
        if missing:
            failures.append(f"phase {phase} missing evidence: {', '.join(missing)}")
        if phase in {"resize", "restore"}:
            if "reconfigure_required" in observed:
                restart_states = {
                    "reconfigure_required", "save_rejected", "shutdown", "restarted",
                    "progress", "save_start", "save_success",
                }
                missing_restart = sorted(restart_states - observed)
                if missing_restart:
                    failures.append(
                        f"phase {phase} missing restart evidence: "
                        + ", ".join(missing_restart)
                    )
                restarted = [
                    record for record in phase_records[phase]
                    if record["state"] == "restarted"
                ]
                if not any(record.get("capture_backend") == EXPECTED_CAPTURE_BACKEND
                           for record in restarted):
                    failures.append(
                        f"phase {phase} restarted without capture_backend=WGC_WINDOW"
                    )
            elif phase == "resize":
                in_place_states = {"progress", "save_start", "save_success"}
                missing_in_place = sorted(in_place_states - observed)
                if missing_in_place:
                    failures.append(
                        f"phase {phase} missing in-place evidence: "
                        + ", ".join(missing_in_place)
                    )
        if phase in {"resize", "close"}:
            for record in phase_records.get(phase, []):
                if record["state"] != "shutdown":
                    continue
                try:
                    elapsed_ms = int(record.get("elapsed_ms", ""))
                except (TypeError, ValueError):
                    failures.append(
                        f"phase {phase} shutdown lacks a numeric elapsed_ms"
                    )
                    continue
                if elapsed_ms < 0 or elapsed_ms > 10_000:
                    failures.append(
                        f"phase {phase} shutdown exceeded 10s bound: {elapsed_ms}ms"
                    )
        for record in phase_records.get(phase, []):
            if record["state"] == "save_success" and record.get("response") != "CLIP_SAVED":
                failures.append(f"phase {phase} save_success lacks CLIP_SAVED response")
    return failures


def _validate_media_artifact(path: Path) -> str | None:
    try:
        if not path.is_file():
            return f"expected media artifact is missing: {path}"
        if path.stat().st_size == 0:
            return f"media artifact is zero-sized: {path}"
    except OSError as error:
        return f"could not inspect media artifact {path}: {error}"
    return None


def _run_media_command(
    command: list[str], log_path: Path, timeout: int,
    runner: Callable[..., subprocess.CompletedProcess],
) -> tuple[int | None, str | None]:
    return _run_logged(command, log_path, timeout, runner)


def _probe_media(
    *, output: Path, phase: str, clip: Path, ffmpeg_bin: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> tuple[dict, str | None]:
    decode_log = output / f"{phase}.decode.log"
    decode_command = [
        str(ffmpeg_bin / "ffmpeg.exe"), "-v", "error", "-xerror",
        "-err_detect", "explode", "-i", str(clip),
        "-map", "0:v:0", "-f", "null", "-",
    ]
    return_code, error = _run_media_command(
        decode_command, decode_log, FFMPEG_TIMEOUT_SECONDS, runner
    )
    if return_code != 0:
        return {}, error or f"strict full decode failed with code {return_code}"

    probe_log = output / f"{phase}.ffprobe.log"
    probe_command = [
        str(ffmpeg_bin / "ffprobe.exe"), "-v", "error", "-count_frames",
        "-show_entries",
        "stream=index,codec_type,width,height,avg_frame_rate,duration,nb_read_frames",
        "-of", "json", str(clip),
    ]
    return_code, error = _run_media_command(
        probe_command, probe_log, FFPROBE_TIMEOUT_SECONDS, runner
    )
    if return_code != 0:
        return {}, error or f"ffprobe failed with code {return_code}"
    try:
        payload = json.loads(_log_text(probe_log))
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"ffprobe did not produce valid JSON: {error}"
    if not isinstance(payload, dict):
        return {}, "ffprobe returned a non-object top-level JSON value"
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return {}, "ffprobe returned no streams"
    if any(not isinstance(stream, dict) for stream in streams):
        return {}, "ffprobe returned a stream with an invalid JSON type"
    if any(stream.get("codec_type") != "video" for stream in streams):
        return {}, "media is not video-only"
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        return {}, "ffprobe returned no video stream"
    try:
        frames = int(video.get("nb_read_frames", "0"))
    except (TypeError, ValueError):
        frames = 0
    try:
        duration = float(video.get("duration", "0"))
    except (TypeError, ValueError):
        duration = 0.0
    evidence = {
        "requested_duration_seconds": 2,
        "width": video.get("width"),
        "height": video.get("height"),
        "frames": frames,
        "duration_seconds": duration,
        "actual_video_duration_seconds": duration,
        "codec_type": video.get("codec_type"),
        "decode_log": str(decode_log),
        "ffprobe_log": str(probe_log),
    }
    if evidence["width"] != 1280 or evidence["height"] != 720:
        return evidence, "media geometry is not the fixed 1280x720 target"
    if not math.isfinite(duration):
        return evidence, f"ffprobe reported non-finite video duration: {duration!r}"
    if frames < 90 or frames > 180:
        return evidence, (
            f"ffprobe counted implausible video frames for a 2s clip: {frames}"
        )
    if duration < 1.8 or duration > 2.3:
        return evidence, (
            f"ffprobe reported implausible video duration for a 2s clip: {duration}"
        )
    return evidence, None


def qualify_run(
    *, output: Path, executable: Path, ffmpeg_bin: Path, requested_encoder: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    results: dict | None = None,
    replay_storage: str | None = None,
) -> int:
    results = results or _new_results(output, requested_encoder)
    if replay_storage:
        results["requested_replay_storage"] = replay_storage
    results["stage"] = "native"
    _write_results(output, results)
    native_log = output / "window-capture-smoke.log"
    command = [str(executable), "--output", str(output), "--encoder", requested_encoder]
    if replay_storage:
        command.extend(["--replay-storage", replay_storage])
    return_code, error = _run_logged(
        command, native_log, NATIVE_TIMEOUT_SECONDS, runner
    )
    results["attempted_runs"] = 1
    native_text = _log_text(native_log)
    identity, phase_records, native_errors = parse_native_evidence(native_text)
    results["identity"] = identity
    results["evidence_scope"]["amd_hardware_proof"] = (
        "unavailable_on_non_amd_adapter"
        if identity.get("active_vendor") and identity.get("active_vendor") != "AMD"
        else "not_claimed"
    )
    _update_phase_results(results, phase_records)
    failures: list[str] = []
    if return_code != 0:
        failures.append(error or f"native helper exited with code {return_code}")
    if native_errors:
        failures.extend(native_errors)
    if replay_storage and (output / "replay-buffer").exists():
        remaining = list((output / "replay-buffer").rglob("*"))
        results["remaining_replay_files"] = [str(path) for path in remaining if path.is_file()]
        if results["remaining_replay_files"]:
            failures.append("temporary replay files remained after native shutdown")
    failures.extend(_validate_identity(results, identity))
    failures.extend(_validate_phases(results, phase_records))
    if failures:
        results["status"] = "failed"
        results["stage"] = "native"
        results["failures"].extend({"stage": "native", "error": failure} for failure in failures)
        results["failed_runs"] = 1
        _write_results(output, results)
        return 1

    results["stage"] = "media"
    for phase in MEDIA_PHASES:
        clip = output / MEDIA_NAMES[phase]
        entry = results["phases"][phase]
        artifact_error = _validate_media_artifact(clip)
        if artifact_error:
            entry["status"] = "failed"
            entry["failures"].append(artifact_error)
            failures.append(artifact_error)
            _write_results(output, results)
            continue
        evidence, media_error = _probe_media(
            output=output, phase=phase, clip=clip, ffmpeg_bin=ffmpeg_bin,
            runner=runner,
        )
        entry["media"] = evidence
        if media_error:
            entry["status"] = "failed"
            entry["failures"].append(media_error)
            failures.append(f"{phase}: {media_error}")
        else:
            entry["media_status"] = "passed"
        _write_results(output, results)

    results["failed_runs"] = 1 if failures else 0
    results["passed_runs"] = 0 if failures else 1
    results["status"] = "failed" if failures else "passed"
    results["stage"] = "qualification" if failures else "complete"
    results["failures"].extend({"stage": "media", "error": failure} for failure in failures)
    _write_results(output, results)
    return 1 if failures else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--encoder", choices=BACKENDS, default="amd")
    parser.add_argument("--replay-storage", choices=("memory", "disk"),
                        help="qualify an explicit replay backend and temporary-file cleanup")
    parser.add_argument("--helper", type=Path,
                        help="prebuilt helper; otherwise build it with MSBuild")
    parser.add_argument("--ffmpeg-bin", type=Path,
                        default=ROOT / "FTHRcapture/FTHRclips/third_party/ffmpeg/bin")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows is required")
    try:
        output = validate_output_directory(args.output)
    except (OSError, ValueError) as error:
        print(f"Output preflight failed: {error}", file=sys.stderr)
        return 2
    results = _new_results(output, args.encoder)
    _write_results(output, results)
    if args.helper:
        executable = args.helper.resolve()
        if not executable.is_file():
            results["status"] = "failed"
            results["stage"] = "helper"
            results["failures"].append({"stage": "helper", "error": f"helper is missing: {executable}"})
            _write_results(output, results)
            print(f"Helper preflight failed: missing {executable}", file=sys.stderr)
            return 1
    else:
        try:
            executable = build_harness(
                output, ROOT / "tools/windows_window_capture_smoke.cpp",
                "window-capture-smoke", timeout_seconds=NATIVE_TIMEOUT_SECONDS,
            )
        except Exception as error:
            detail = _failure_detail(output / "build.log", f"native build failed: {error}")
            results["status"] = "failed"
            results["stage"] = "build"
            results["failures"].append({"stage": "build", "error": detail})
            _write_results(output, results)
            print(f"Window capture smoke build failed: {detail}", file=sys.stderr)
            return 1
    if not (args.ffmpeg_bin / "ffmpeg.exe").is_file() or not (args.ffmpeg_bin / "ffprobe.exe").is_file():
        detail = f"FFmpeg runtime is incomplete: expected ffmpeg.exe and ffprobe.exe in {args.ffmpeg_bin}"
        results["status"] = "failed"
        results["stage"] = "runtime"
        results["failures"].append({"stage": "runtime", "error": detail})
        _write_results(output, results)
        print(detail, file=sys.stderr)
        return 1
    return qualify_run(
        output=output, executable=executable, ffmpeg_bin=args.ffmpeg_bin,
        requested_encoder=args.encoder, results=results,
        replay_storage=args.replay_storage,
    )


if __name__ == "__main__":
    raise SystemExit(main())
