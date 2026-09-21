"""Regression coverage for the pinned Linux FFmpeg link aliases."""

from __future__ import annotations

from pathlib import Path

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


def test_linux_ffmpeg_cli_rpaths_are_repaired(tmp_path: Path, monkeypatch) -> None:
    if __import__('shutil').which('patchelf') is None:
        import pytest
        pytest.skip('patchelf is not installed on this test host')

    import subprocess

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fixture = Path('/bin/true')
    for name in ('ffmpeg', 'ffprobe'):
        target = bin_dir / name
        target.write_bytes(fixture.read_bytes())
        target.chmod(0o755)

    _ensure_linux_ffmpeg_tool_rpaths(bin_dir)

    for name in ('ffmpeg', 'ffprobe'):
        result = subprocess.run(
            ['readelf', '-d', str(bin_dir / name)],
            check=True, capture_output=True, text=True,
        )
        assert 'Library rpath: [$ORIGIN/../lib]' in result.stdout


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
