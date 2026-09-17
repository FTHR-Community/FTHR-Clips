"""Clean-clone contracts for the release CI workflow."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_linux_release_ci_fetches_and_uses_pinned_ffmpeg() -> None:
    workflow = _workflow()
    linux_job = workflow[workflow.index("  linux-engine:") : workflow.index(
        "  windows-engine:"
    )]

    assert "fetch_third_party.py --ffmpeg-linux" in linux_job
    assert "-DFTHR_FFMPEG_ROOT=${{ github.workspace }}" in linux_job
    assert "ctest --test-dir" in linux_job
    assert "verify_release_licenses.py --tree ." in linux_job
    assert "git diff --exit-code" in linux_job


def test_windows_bundle_is_built_only_after_the_engine() -> None:
    workflow = _workflow()
    python_job = workflow[workflow.index("  python:") : workflow.index(
        "  linux-engine:"
    )]
    windows_job = workflow[workflow.index("  windows-engine:") : workflow.index(
        "  release-verification:"
    )]

    assert "PyInstaller" not in python_job
    assert windows_job.index("msbuild FTHRcapture") < windows_job.index(
        "python -m PyInstaller"
    )
    assert "verify_release_licenses.py --windows-dist" in windows_job
    assert windows_job.index('python tools/build_optional_uploaders.py') < windows_job.index(
        'python -m PyInstaller'
    )
    assert 'python -m pytest tests/test_uploader_plugin_boundary.py' in windows_job


def test_windows_package_and_source_loader_use_supported_mixer_outputs() -> None:
    spec = (ROOT / "FTHR.spec").read_text(encoding="utf-8")
    loader = (ROOT / "FTHR_UI" / "core" / "ffmpeg_playback.py").read_text(
        encoding="utf-8"
    )

    project_path = "'FTHRcapture' / 'FTHRPlaybackMixer' / 'x64'"
    solution_path = "'FTHRcapture' / 'x64' / 'Release'"
    assert solution_path in spec
    assert solution_path in loader
    assert project_path not in spec
    assert project_path in loader


def test_windows_bundle_excludes_path_injected_icu_runtime() -> None:
    spec = (ROOT / "FTHR.spec").read_text(encoding="utf-8")
    verifier = (ROOT / "tools" / "verify_windows_installer_lifecycle.py").read_text(
        encoding="utf-8"
    )

    for dll_marker in ("icuuc.dll", "icudt", "icuin"):
        assert dll_marker in spec
        assert dll_marker in verifier


def test_windows_bundle_does_not_require_retired_input_overlay_module() -> None:
    spec = (ROOT / "FTHR.spec").read_text(encoding="utf-8")

    assert "core.input_overlay" not in spec


def test_windows_installer_replaces_only_its_internal_runtime_tree() -> None:
    installer = (ROOT / "installer_windows.iss").read_text(encoding="utf-8")

    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in installer
    assert 'Type: filesandordirs; Name: "{app}"' not in installer


def test_windows_hardware_gate_requires_ten_saves_and_fresh_frames() -> None:
    qualifier = (ROOT / "tools" / "qualify_windows_hardware.py").read_text(
        encoding="utf-8"
    )

    assert "for index in range(1, 11):" in qualifier
    rapid = qualifier[qualifier.index("frames_before_rapid") :]
    assert "wait_for_frames(" in rapid
    assert "frames_before_rapid + max(args.fps, 1)" in rapid


def test_release_ci_runs_response_and_exception_contracts() -> None:
    workflow = _workflow()
    release_job = workflow[workflow.index("  release-verification:") :]

    assert "verify_engine_response_contract.py" in release_job
    assert "verify_exception_handling.py" in release_job
