"""Build and run an isolated Windows CaptureEngine smoke qualification.

Requires VS 2022 C++ tools and the repository's FFmpeg runtime. Captures the
explicitly selected monitor for 15 seconds and saves its last 10 seconds.
All generated projects, binaries, logs, recordings and result.json stay in
--output, which must be outside the repository and contain no prior files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("auto", "dxgi", "wgc")
CAPTURE_IDENTITY = re.compile(
    r"SMOKE requested_backend=(\S+) capture_backend=(\S+)"
)
CAPTURE_DIAGNOSTIC_IDENTITY = re.compile(r'"capture_backend":"([^" ]+)"')
HARNESS_ERROR_STAGE = re.compile(r"SMOKE_ERROR stage=([A-Za-z0-9_]+)")


def build_harness(output: Path, harness_source: Path, name: str) -> Path:
    """Build against the production engine sources in an isolated directory."""
    vswhere = (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
               / "Microsoft Visual Studio/Installer/vswhere.exe")
    msbuild = subprocess.check_output(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.Component.MSBuild", "-find", "MSBuild/**/Bin/MSBuild.exe"],
        text=True).strip().splitlines()[0]
    source_root = ROOT / "FTHRcapture/FTHRclips"
    namespace = "http://schemas.microsoft.com/developer/msbuild/2003"
    ET.register_namespace("", namespace)
    source = (source_root / "FTHRclips.vcxproj").read_text(encoding="utf-8-sig")
    source = source.replace("$(ProjectDir)", str(source_root) + os.sep)
    project = ET.fromstring(source)
    for tag in ("ClCompile", "ClInclude"):
        for item in project.iter(f"{{{namespace}}}{tag}"):
            include_name = item.get("Include")
            if include_name:
                path = (harness_source
                        if include_name == r"src\main.cpp" else source_root / include_name)
                item.set("Include", str(path.resolve()))
    project_path = output / f"{name}.vcxproj"
    ET.ElementTree(project).write(project_path, encoding="utf-8", xml_declaration=True)
    executable = output / f"bin/{name}.exe"
    with (output / "build.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            [msbuild, str(project_path), "/p:Configuration=Release", "/p:Platform=x64",
             f"/p:OutDir={output / 'bin'}{os.sep}",
             f"/p:IntDir={output / 'obj'}{os.sep}", "/v:minimal", "/nologo"],
            stdout=log, stderr=subprocess.STDOUT, check=True)
    return executable


def validate_output_directory(
    output: Path, *, cycles: int, manual: bool,
) -> Path:
    """Resolve a new external output directory without clobbering old evidence."""
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("--output must be outside the repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(
            f"--output must be a new or empty directory; refusing to overwrite: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _new_results(output: Path, cycles: int, backend: str, manual: bool) -> dict:
    return {
        "schema_version": 1,
        "status": "running",
        "stage": "build",
        "requested_backend": backend,
        "attempt_denominator": cycles,
        "attempted_cycles": 0,
        "passed_cycles": 0,
        "failed_cycles": 0,
        "manual_recording_requested": manual,
        "cycles": [
            {
                "cycle": cycle,
                "status": "not_run",
                "requested_backend": backend,
                "actual_backend": None,
                "stages": {},
                "failures": [],
            }
            for cycle in range(1, cycles + 1)
        ],
    }


def _write_results(output: Path, results: dict) -> None:
    (output / "result.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")


def _log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _failure_detail(path: Path, fallback: str) -> str:
    text = _log_text(path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    native = [line for line in lines if "FTHR_STARTUP_ERROR:" in line]
    if native:
        return native[-1]
    harness = [line for line in lines if "SMOKE_ERROR stage=" in line]
    if harness:
        return harness[-1]
    if lines:
        return f"{fallback}; log tail: {' | '.join(lines[-8:])}; log={path}"
    return f"{fallback}; log={path}"


def _capture_backend_identity(path: Path) -> tuple[str | None, str | None]:
    for line in _log_text(path).splitlines():
        match = CAPTURE_IDENTITY.search(line)
        if match:
            return match.group(1), match.group(2)
    for line in _log_text(path).splitlines():
        match = CAPTURE_DIAGNOSTIC_IDENTITY.search(line)
        if match:
            return None, match.group(1)
    return None, None


def _reported_harness_failure_stage(path: Path, fallback: str) -> str:
    for line in reversed(_log_text(path).splitlines()):
        match = HARNESS_ERROR_STAGE.search(line)
        if match:
            return match.group(1)
    return fallback


def _validate_media_artifact(path: Path) -> str | None:
    try:
        if not path.is_file():
            return f"expected media artifact is missing: {path}"
        size = path.stat().st_size
    except OSError as error:
        return f"could not inspect media artifact {path}: {error}"
    if size == 0:
        return f"media artifact is zero-sized: {path}"
    return None


def _backend_matches(requested: str, actual: str | None) -> bool:
    if actual is None:
        return False
    if requested == "auto":
        return actual in ("WGC_MONITOR", "DXGI_OUTPUT_DUPLICATION")
    if requested == "dxgi":
        return actual == "DXGI_OUTPUT_DUPLICATION"
    if requested == "wgc":
        return actual == "WGC_MONITOR"
    return False


def _set_stage(cycle: dict, stage: str, status: str, **details) -> None:
    cycle["stages"][stage] = {"status": status, **details}


def _fail_cycle(cycle: dict, stage: str, detail: str, *, log: Path | None = None) -> None:
    failure = {"stage": stage, "error": detail}
    if log is not None:
        failure["log"] = str(log)
    cycle["failures"].append(failure)
    if cycle["status"] != "failed":
        cycle["status"] = "failed"
        cycle["stage"] = stage
        cycle["error"] = detail


def _run_logged(
    command: list[str], log_path: Path, timeout: int, runner,
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
    except Exception as error:
        return None, (f"runner failed for {command[0]}: "
                      f"{type(error).__name__}: {error}; log={log_path}")
    if result.returncode != 0:
        return result.returncode, _failure_detail(
            log_path, f"process exited with code {result.returncode}")
    return 0, None


def run_capture_cycles(
    *,
    output: Path,
    executable: Path,
    ffmpeg_bin: Path,
    requested_backend: str,
    cycles: int,
    manual: bool,
    runner=None,
    results: dict | None = None,
) -> int:
    """Run each capture/decode attempt and persist every stage as it completes."""
    if runner is None:
        runner = subprocess.run
    results = results or _new_results(output, cycles, requested_backend, manual)
    _write_results(output, results)
    monitor = results.get("monitor_device_path", "")
    results["stage"] = "capture"
    _write_results(output, results)

    for cycle in results["cycles"]:
        number = cycle["cycle"]
        cycle["status"] = "running"
        results["attempted_cycles"] += 1
        capture_log = output / f"capture-{number}.log"
        clip = output / f"capture-{number}.mp4"
        command = [str(executable), monitor, str(clip), "--backend", requested_backend]
        if manual:
            command.append("--manual")
        _set_stage(cycle, "capture", "running", log=str(capture_log))
        _write_results(output, results)
        return_code, error = _run_logged(command, capture_log, 60, runner)
        if return_code != 0:
            _, cycle["actual_backend"] = _capture_backend_identity(capture_log)
            detail = error or f"capture harness exited with code {return_code}"
            failure_stage = _reported_harness_failure_stage(capture_log, "capture")
            _set_stage(cycle, "capture", "failed", exit_code=return_code,
                       actual_failure_stage=failure_stage, error=detail,
                       log=str(capture_log))
            if failure_stage != "capture":
                _set_stage(cycle, failure_stage, "failed", error=detail,
                           log=str(capture_log))
            _fail_cycle(cycle, failure_stage, detail, log=capture_log)
            _write_results(output, results)
            print(f"Cycle {number} failed during {failure_stage}: {detail}",
                  file=sys.stderr)
            continue

        reported_request, actual_backend = _capture_backend_identity(capture_log)
        cycle["actual_backend"] = actual_backend
        _set_stage(cycle, "capture", "passed", log=str(capture_log))
        if reported_request != requested_backend:
            detail = ("harness backend identity mismatch: requested "
                      f"{requested_backend}, harness reported {reported_request!r}")
            _set_stage(cycle, "backend_qualification", "failed", error=detail)
            _fail_cycle(cycle, "backend_qualification", detail, log=capture_log)
        elif not _backend_matches(requested_backend, actual_backend):
            detail = (f"backend qualification refused: requested {requested_backend}, "
                      f"actual {actual_backend!r}")
            _set_stage(cycle, "backend_qualification", "failed", error=detail)
            _fail_cycle(cycle, "backend_qualification", detail, log=capture_log)
        else:
            _set_stage(cycle, "backend_qualification", "passed",
                       requested=requested_backend, actual=actual_backend)
        _write_results(output, results)

        decode_log = output / f"capture-{number}.decode.log"
        artifact_error = _validate_media_artifact(clip)
        if artifact_error is not None:
            _set_stage(cycle, "artifact_validation", "failed", error=artifact_error)
            _fail_cycle(cycle, "artifact_validation", artifact_error)
            _set_stage(cycle, "full_decode", "not_run",
                       reason="replay artifact did not pass validation")
            print(f"Cycle {number} failed during artifact validation: {artifact_error}",
                  file=sys.stderr)
        else:
            decode_command = [
                str(ffmpeg_bin / "ffmpeg.exe"), "-v", "error", "-xerror",
                "-err_detect", "explode", "-i", str(clip),
                "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-",
            ]
            _set_stage(cycle, "full_decode", "running", log=str(decode_log))
            _write_results(output, results)
            return_code, error = _run_logged(decode_command, decode_log, 30, runner)
            if return_code != 0:
                detail = error or f"full-file decode exited with code {return_code}"
                _set_stage(cycle, "full_decode", "failed", exit_code=return_code,
                           error=detail, log=str(decode_log))
                _fail_cycle(cycle, "full_decode", detail, log=decode_log)
                print(f"Cycle {number} failed during full decode: {detail}", file=sys.stderr)
            else:
                _set_stage(cycle, "full_decode", "passed", log=str(decode_log))
            _write_results(output, results)

        if manual:
            recording = clip.with_suffix(".recording.mp4")
            manual_log = output / f"capture-{number}.recording.decode.log"
            artifact_error = _validate_media_artifact(recording)
            if artifact_error is not None:
                _set_stage(cycle, "manual_artifact_validation", "failed",
                           error=artifact_error)
                _fail_cycle(cycle, "manual_artifact_validation", artifact_error)
                _set_stage(cycle, "manual_full_decode", "not_run",
                           reason="manual recording did not pass artifact validation")
                print(f"Cycle {number} failed during manual artifact validation: "
                      f"{artifact_error}", file=sys.stderr)
            else:
                manual_command = [
                    str(ffmpeg_bin / "ffmpeg.exe"), "-v", "error", "-xerror",
                    "-err_detect", "explode", "-i", str(recording),
                    "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-",
                ]
                _set_stage(cycle, "manual_full_decode", "running",
                           log=str(manual_log))
                _write_results(output, results)
                return_code, error = _run_logged(
                    manual_command, manual_log, 30, runner)
                if return_code != 0:
                    detail = error or (
                        f"manual recording decode exited with code {return_code}")
                    _set_stage(cycle, "manual_full_decode", "failed",
                               exit_code=return_code, error=detail,
                               log=str(manual_log))
                    _fail_cycle(cycle, "manual_full_decode", detail, log=manual_log)
                    print(f"Cycle {number} failed during manual recording decode: "
                          f"{detail}", file=sys.stderr)
                else:
                    _set_stage(cycle, "manual_full_decode", "passed",
                               log=str(manual_log))
                _write_results(output, results)

        if cycle["status"] != "failed":
            cycle["status"] = "passed"
            cycle["stage"] = "complete"
        _write_results(output, results)

    results["attempted_cycles"] = sum(
        cycle["status"] != "not_run" for cycle in results["cycles"])
    results["passed_cycles"] = sum(
        cycle["status"] == "passed" for cycle in results["cycles"])
    results["failed_cycles"] = sum(
        cycle["status"] == "failed" for cycle in results["cycles"])
    results["status"] = "passed" if results["failed_cycles"] == 0 else "failed"
    results["stage"] = "complete" if results["status"] == "passed" else "qualification"
    _write_results(output, results)
    print(
        f"Capture qualification: {results['passed_cycles']}/"
        f"{results['attempt_denominator']} passed; "
        f"{results['attempted_cycles']} attempts. Evidence: {output}")
    return 0 if results["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor", required=True,
                        help="stable Windows monitor device path")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--backend", choices=BACKENDS, default="auto",
                        help="capture backend to qualify; forced modes never fall back")
    parser.add_argument("--manual", action="store_true",
                        help="also start/stop a recording while replay capture continues")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows is required")
    if not 1 <= args.cycles <= 20:
        parser.error("--cycles must be between 1 and 20")
    try:
        output = validate_output_directory(
            args.output, cycles=args.cycles, manual=args.manual)
    except (OSError, ValueError) as error:
        print(f"Output preflight failed: {error}", file=sys.stderr)
        return 2

    results = _new_results(output, args.cycles, args.backend, args.manual)
    results["monitor_device_path"] = args.monitor
    _write_results(output, results)
    try:
        executable = build_harness(
            output, ROOT / "tools/windows_capture_smoke.cpp", "capture-smoke")
    except Exception as error:
        detail = _failure_detail(output / "build.log", f"native build failed: {error}")
        results["status"] = "failed"
        results["stage"] = "build"
        results["error"] = detail
        for cycle in results["cycles"]:
            cycle["status"] = "not_run"
            cycle["stage"] = "build"
            cycle["error"] = detail
        _write_results(output, results)
        print(f"Capture smoke build failed: {detail}", file=sys.stderr)
        return 1

    ffmpeg_bin = ROOT / "FTHRcapture/FTHRclips/third_party/ffmpeg/bin"
    return run_capture_cycles(
        output=output,
        executable=executable,
        ffmpeg_bin=ffmpeg_bin,
        requested_backend=args.backend,
        cycles=args.cycles,
        manual=args.manual,
        results=results,
    )


if __name__ == "__main__":
    raise SystemExit(main())
