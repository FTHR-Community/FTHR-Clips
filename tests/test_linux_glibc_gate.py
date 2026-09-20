"""Deterministic tests for the Linux release glibc ceiling gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.verify_linux_glibc import GlibcVersion, Manifest, verify_targets


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Manifest.load(ROOT / "tools" / "linux_release_baseline.json")
WORKFLOWS = ROOT / ".github" / "workflows"


def fake_runner(output: str, returncode: int = 0):
    def run(command):
        assert command[:3] == ("readelf", "--version-info", "--wide")
        return subprocess.CompletedProcess(
            command, returncode, output, "readelf error" if returncode else ""
        )

    return run


def elf_file(tmp_path: Path, name: str = "bundle.so") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x7fELF" + b"test")
    return path


def test_baseline_values_are_single_source() -> None:
    assert MANIFEST.architecture == "x86_64"
    assert MANIFEST.build_runner == "ubuntu-22.04"
    assert MANIFEST.max_glibc == GlibcVersion((2, 35))


def test_versions_at_or_below_235_pass(tmp_path: Path) -> None:
    report = verify_targets(
        [elf_file(tmp_path)],
        MANIFEST,
        readelf_runner=fake_runner("Name: GLIBC_2.34\nName: GLIBC_2.35\n"),
    )
    assert report.ok
    assert report.highest == GlibcVersion((2, 35))


def test_244_fails_and_names_offending_file(tmp_path: Path) -> None:
    path = elf_file(tmp_path)
    report = verify_targets([path], MANIFEST, readelf_runner=fake_runner("GLIBC_2.44"))
    assert not report.ok
    assert report.offending[0].path == path
    assert report.highest == GlibcVersion((2, 44))


def test_non_glibc_names_are_ignored(tmp_path: Path) -> None:
    report = verify_targets(
        [elf_file(tmp_path)],
        MANIFEST,
        readelf_runner=fake_runner("GLIBCXX_3.4.30 GLIBC_PRIVATE GLIBC_2.35"),
    )
    assert report.ok
    assert report.highest == GlibcVersion((2, 35))


def test_patch_versions_compare_numerically(tmp_path: Path) -> None:
    report = verify_targets(
        [elf_file(tmp_path)], MANIFEST, readelf_runner=fake_runner("GLIBC_2.35.1")
    )
    assert not report.ok
    assert report.highest == GlibcVersion((2, 35, 1))


def test_trailing_zero_patch_is_equal_to_ceiling(tmp_path: Path) -> None:
    report = verify_targets(
        [elf_file(tmp_path)], MANIFEST, readelf_runner=fake_runner("GLIBC_2.35.0")
    )
    assert report.ok


def test_missing_and_empty_targets_fail(tmp_path: Path) -> None:
    report = verify_targets(
        [tmp_path / "missing", tmp_path / "empty"],
        MANIFEST,
        readelf_runner=fake_runner(""),
    )
    assert not report.ok
    assert any("target does not exist" in error for error in report.errors)
    assert any("no ELF files" in error for error in report.errors)


def test_readelf_error_fails(tmp_path: Path) -> None:
    report = verify_targets(
        [elf_file(tmp_path)], MANIFEST, readelf_runner=fake_runner("", returncode=2)
    )
    assert not report.ok
    assert any("readelf failed" in error for error in report.errors)


def test_symlinked_elf_is_scanned_once(tmp_path: Path) -> None:
    real = elf_file(tmp_path, "real.so")
    link = tmp_path / "link.so"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        return
    calls = []

    def runner(command):
        calls.append(command[-1])
        return subprocess.CompletedProcess(command, 0, "GLIBC_2.35", "")

    report = verify_targets([tmp_path], MANIFEST, readelf_runner=runner)
    assert report.ok
    assert len(calls) == 1


def test_ci_uses_ubuntu_2204_glibc_gate_without_artifact_upload() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    start = workflow.index("  linux-appimage-package:")
    end = workflow.index("  #", start + len("  linux-appimage-package:"))
    job = workflow[start:end]
    assert "runs-on: ubuntu-22.04" in job
    assert "bash build_linux.sh" in job
    assert "python tools/verify_linux_glibc.py" in job
    assert "actions/upload-artifact@v4" not in workflow
    assert "gh release create" not in workflow
    assert not (WORKFLOWS / "linux-release.yml").exists()
