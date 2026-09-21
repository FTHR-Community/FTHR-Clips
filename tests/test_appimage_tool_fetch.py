"""Offline release-contract tests for pinned AppImage build inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools import fetch_third_party


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tools" / "appimage_tool_manifest.json"


def _manifest_with_payloads(tmp_path: Path) -> tuple[dict[str, object], bytes, bytes]:
    tool_bytes = b"verified appimagetool fixture\n"
    runtime_bytes = b"verified runtime fixture\n"
    data = {
        "component": "AppImage packaging tools",
        "platform": "linux",
        "arch": "x86_64",
        "appimagetool": {
            "version": "test-tool",
            "url": "https://example.invalid/tool",
            "filename": "appimagetool-x86_64.AppImage",
            "size_bytes": len(tool_bytes),
            "sha256": hashlib.sha256(tool_bytes).hexdigest(),
        },
        "runtime": {
            "version": "test-runtime",
            "url": "https://example.invalid/runtime",
            "filename": "runtime-x86_64",
            "size_bytes": len(runtime_bytes),
            "sha256": hashlib.sha256(runtime_bytes).hexdigest(),
        },
    }
    return data, tool_bytes, runtime_bytes


def _use_fixture_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], bytes, bytes, Path]:
    data, tool_bytes, runtime_bytes = _manifest_with_payloads(tmp_path)
    manifest_path = tmp_path / "appimage_tool_manifest.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    output_dir = tmp_path / "build_output"
    monkeypatch.setattr(fetch_third_party, "APPIMAGE_MANIFEST", manifest_path)
    monkeypatch.setattr(fetch_third_party, "APPIMAGE_OUTPUT_DIR", output_dir)
    return data, tool_bytes, runtime_bytes, output_dir


def test_appimage_fetch_reuses_two_valid_cached_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, tool_bytes, runtime_bytes, output_dir = _use_fixture_manifest(
        tmp_path, monkeypatch
    )
    output_dir.mkdir()
    (output_dir / data["appimagetool"]["filename"]).write_bytes(tool_bytes)  # type: ignore[index]
    (output_dir / data["runtime"]["filename"]).write_bytes(runtime_bytes)  # type: ignore[index]

    def unexpected_download(*_: object) -> None:
        raise AssertionError("a verified cache must not be downloaded")

    monkeypatch.setattr(fetch_third_party, "_download", unexpected_download)
    assert fetch_third_party.fetch_appimage_tools(force=False) == 0


def test_appimage_fetch_replaces_corrupt_cache_only_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, tool_bytes, runtime_bytes, output_dir = _use_fixture_manifest(
        tmp_path, monkeypatch
    )
    output_dir.mkdir()
    tool_path = output_dir / data["appimagetool"]["filename"]  # type: ignore[index]
    runtime_path = output_dir / data["runtime"]["filename"]  # type: ignore[index]
    tool_path.write_bytes(b"corrupt cache")
    runtime_path.write_bytes(runtime_bytes)

    def download(url: str, destination: Path) -> None:
        destination.write_bytes(tool_bytes if url.endswith("tool") else runtime_bytes)

    monkeypatch.setattr(fetch_third_party, "_download", download)
    assert fetch_third_party.fetch_appimage_tools(force=False) == 0
    assert tool_path.read_bytes() == tool_bytes
    assert runtime_path.read_bytes() == runtime_bytes


def test_appimage_fetch_rejects_bad_download_without_replacing_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, tool_bytes, runtime_bytes, output_dir = _use_fixture_manifest(
        tmp_path, monkeypatch
    )
    output_dir.mkdir()
    tool_path = output_dir / data["appimagetool"]["filename"]  # type: ignore[index]
    runtime_path = output_dir / data["runtime"]["filename"]  # type: ignore[index]
    tool_path.write_bytes(tool_bytes)
    runtime_path.write_bytes(runtime_bytes)

    monkeypatch.setattr(
        fetch_third_party,
        "_download",
        lambda _url, destination: destination.write_bytes(b"tampered bytes"),
    )
    with pytest.raises(RuntimeError, match="size mismatch|sha256 mismatch"):
        fetch_third_party.fetch_appimage_tools(force=True)
    assert tool_path.read_bytes() == tool_bytes
    assert runtime_path.read_bytes() == runtime_bytes
    assert not list(output_dir.glob("*.download"))


def test_appimage_manifest_pins_requested_release_inputs() -> None:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data["platform"] == "linux"
    assert data["arch"] == "x86_64"
    assert data["appimagetool"]["release_tag"] == "1.9.1"
    assert data["appimagetool"]["commit"] == (
        "8c8c91f762b412a19f4e8d2c4b35afb98f2d7c81"
    )
    assert data["appimagetool"]["sha256"] == (
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
    )
    assert data["runtime"]["release_tag"] == "20251108"
    assert data["runtime"]["commit"] == (
        "dd6cebedcbddde9c82f89b011e8e1d40b6e43868"
    )
    assert data["runtime"]["sha256"] == (
        "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
    )


@pytest.mark.parametrize(
    ("asset_key", "field", "value", "message"),
    [
        ("appimagetool", "url", "http://example.invalid/tool", "URL"),
        ("runtime", "size_bytes", True, "size_bytes"),
        ("runtime", "size_bytes", 0, "size_bytes"),
        ("appimagetool", "sha256", "not-a-sha256", "sha256"),
    ],
)
def test_invalid_appimage_manifest_values_fail_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_key: str,
    field: str,
    value: object,
    message: str,
) -> None:
    data, _tool_bytes, _runtime_bytes, _output_dir = _use_fixture_manifest(
        tmp_path, monkeypatch
    )
    data[asset_key][field] = value  # type: ignore[index]
    Path(fetch_third_party.APPIMAGE_MANIFEST).write_text(
        json.dumps(data), encoding="utf-8"
    )

    def unexpected_download(*_: object) -> None:
        raise AssertionError("invalid manifest must fail before download")

    monkeypatch.setattr(fetch_third_party, "_download", unexpected_download)
    with pytest.raises(RuntimeError, match=message):
        fetch_third_party.fetch_appimage_tools(force=False)
    assert not fetch_third_party.APPIMAGE_OUTPUT_DIR.exists()


def test_linux_build_uses_verified_tool_and_explicit_runtime() -> None:
    build = (ROOT / "build_linux.sh").read_text(encoding="utf-8")
    assert 'fetch_third_party.py" --appimagetool-linux' in build
    assert '--runtime-file "$NATIVE_RUNTIME"' in build
    assert 'rm -f "$NATIVE_OUTPUT" "$OUTPUT" "$OUTPUT.sha256"' in build
    assert "AppImageKit/releases/download/continuous" not in build
    assert "type2-runtime/releases/download/continuous" not in build


def test_runtime_license_is_required_and_documents_embedded_components() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import verify_release_licenses as vrl  # noqa: PLC0415

    license_path = ROOT / "licenses" / "AppImage-type2-runtime-LICENSE.txt"
    assert "Copyright (c) 2004-23 probonopd" in license_path.read_text(
        encoding="utf-8"
    )
    assert "licenses/AppImage-type2-runtime-LICENSE.txt" in vrl.REQUIRED_TREE_FILES
    assert "tools/appimage_tool_manifest.json" in vrl.REQUIRED_TREE_FILES
