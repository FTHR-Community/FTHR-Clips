"""Regression coverage for the pinned Linux FFmpeg link aliases."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import fetch_third_party
from tools.fetch_third_party import (
    _ensure_linux_ffmpeg_aliases,
    _ensure_linux_ffmpeg_tool_rpaths,
)


ROOT = Path(__file__).resolve().parents[1]


def test_linux_ffmpeg_aliases_are_created_and_repaired(tmp_path: Path) -> None:
    versioned = tmp_path / "libavcodec.so.62.28.102"
    versioned.write_bytes(b"verified pinned library")
    soname = tmp_path / "libavcodec.so.62"
    linker = tmp_path / "libavcodec.so"
    linker.write_bytes(b"stale system-generation alias")

    _ensure_linux_ffmpeg_aliases(
        tmp_path, {versioned.name: soname.name}
    )

    assert soname.read_bytes() == versioned.read_bytes()
    assert linker.read_bytes() == versioned.read_bytes()

    # Idempotence matters because the normal fetch path returns early once the
    # pinned versioned libraries have already passed their manifest hashes.
    _ensure_linux_ffmpeg_aliases(
        tmp_path, {versioned.name: soname.name}
    )
    assert linker.read_bytes() == versioned.read_bytes()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux ELF tooling")
def test_linux_ffmpeg_cli_rpaths_are_repaired_without_touching_libraries(
    tmp_path: Path,
) -> None:
    patchelf = shutil.which("patchelf")
    readelf = shutil.which("readelf")
    true = shutil.which("true")
    if not patchelf or not readelf or not true:
        pytest.skip("patchelf, readelf and a test ELF are required")

    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib"
    bin_dir.mkdir()
    lib_dir.mkdir()
    library = lib_dir / "libsentinel.so.1"
    library.write_bytes(b"library bytes must not change")
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(true, bin_dir / name)

    before = library.read_bytes()
    _ensure_linux_ffmpeg_tool_rpaths(bin_dir)

    for name in ("ffmpeg", "ffprobe"):
        result = subprocess.run(
            [readelf, "-d", str(bin_dir / name)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Library rpath: [$ORIGIN/../lib]" in result.stdout
    assert library.read_bytes() == before


def test_linux_ffmpeg_rpath_repair_requires_patchelf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_third_party.shutil, "which", lambda _: None)

    with pytest.raises(RuntimeError, match="patchelf is required"):
        _ensure_linux_ffmpeg_tool_rpaths(tmp_path / "bin")


def test_linux_ffmpeg_rpath_repair_reports_patchelf_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (bin_dir / name).write_bytes(b"not an ELF; the mocked command fails")

    monkeypatch.setattr(fetch_third_party.shutil, "which", lambda _: "patchelf")

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1, ["patchelf"], stderr="bad rpath"
        )

    monkeypatch.setattr(fetch_third_party.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="patchelf failed.*ffmpeg.*bad rpath"):
        _ensure_linux_ffmpeg_tool_rpaths(bin_dir)


def test_linux_ffmpeg_rpath_repair_rejects_missing_cli_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(fetch_third_party.shutil, "which", lambda _: "patchelf")

    with pytest.raises(RuntimeError, match="missing bin/ffmpeg"):
        _ensure_linux_ffmpeg_tool_rpaths(bin_dir)


def test_linux_release_link_is_confined_to_pinned_ffmpeg_tree() -> None:
    cmake = (ROOT / "FTHRcapture_linux" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    assert "NO_DEFAULT_PATH" in cmake
    assert "FTHR_FFMPEG_LINK_LIBRARIES" in cmake
    assert "-Wl,-rpath-link,${FTHR_FFMPEG_ROOT}/lib" in cmake


def test_wayland_protocol_generation_stays_in_build_tree() -> None:
    cmake = (ROOT / "FTHRcapture_linux" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    assert "set(PROTO_XML_DIR ${CMAKE_CURRENT_SOURCE_DIR}/protocols)" in cmake
    assert "set(PROTO_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/protocols)" in cmake
    assert "OUTPUT ${PROTO_GEN_DIR}/wlr-screencopy-client-protocol.h" in cmake
    assert "DEPENDS ${PROTO_XML_DIR}/wlr-screencopy-unstable-v1.xml" in cmake
    assert "set(PROTO_DIR ${CMAKE_SOURCE_DIR}/protocols)" not in cmake
