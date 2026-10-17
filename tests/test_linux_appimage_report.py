"""Regression coverage for the redacted Linux AppImage diagnostic helper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "tools" / "linux_appimage_report.sh"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="requires the Linux AppImage shell helper"
)


def run_report(
    image: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    report_env = os.environ.copy()
    report_env.update(
        {
            "HOME": "/home/test-user",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "WAYLAND_DISPLAY": "wayland-private",
            "DISPLAY": ":private",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
    )
    if env:
        report_env.update(env)
    return subprocess.run(
        ["bash", str(REPORT), str(image), *args],
        check=False,
        capture_output=True,
        text=True,
        env=report_env,
    )


def test_missing_image_is_a_usage_error(tmp_path: Path) -> None:
    result = run_report(tmp_path / "missing.AppImage")

    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_invalid_image_reports_diagnostics_and_nonzero_status(tmp_path: Path) -> None:
    image = tmp_path / "capture.AppImage"
    image.write_bytes(b"not an ELF AppImage")

    result = run_report(image)

    assert result.returncode == 1
    assert "sha256:" in result.stdout
    assert "architecture:" in result.stdout
    assert "fuse:" in result.stdout
    assert "appimage_extract_and_run=1" in result.stdout
    assert "not a valid" in result.stdout.lower()


def test_checksum_output_is_optional_and_uses_image_basename(tmp_path: Path) -> None:
    image = tmp_path / "capture.AppImage"
    image.write_bytes(b"not an ELF AppImage")
    checksum = tmp_path / "capture.sha256"

    result = run_report(image, "--write-checksum", str(checksum))

    assert result.returncode == 1
    assert checksum.read_text(encoding="utf-8").endswith("  capture.AppImage\n")


def test_environment_values_and_helper_paths_are_redacted(tmp_path: Path) -> None:
    image = tmp_path / "capture.AppImage"
    image.write_bytes(b"not an ELF AppImage")

    result = run_report(image)
    output = result.stdout

    assert "session: set" in output
    assert "desktop: set" in output
    assert "wayland_display: set" in output
    assert "x_display: set" in output
    assert "runtime_dir: set" in output
    for private_value in (
        "/home/test-user",
        "KDE",
        "wayland-private",
        ":private",
        "/run/user/1000",
    ):
        assert private_value not in output
    assert str(Path.home()) not in output
