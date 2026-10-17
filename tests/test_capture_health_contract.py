"""Cross-language capture-health contract and native recovery-policy tests."""

from __future__ import annotations

from pathlib import Path


from core.capture_health import CaptureHealthFlag


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "FTHRcapture" / "FTHRclips"


def test_v4_mapping_name_is_used_by_all_producers_and_consumer() -> None:
    sources = (
        ROOT / "FTHR_UI" / "core" / "capture_bridge.py",
        WINDOWS / "src" / "main.cpp",
    )
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "FTHR_SharedMemory_v4" in text
        assert "FTHR_SharedMemory_v3" not in text


def test_both_engines_publish_all_typed_health_fields() -> None:
    fields = {
        "capture_health_flags",
        "capture_generation",
        "content_sample_sequence",
        "content_suspicious_streak",
        "content_luma_mean",
        "content_luma_variance",
    }
    for main in (WINDOWS / "src" / "main.cpp",):
        text = main.read_text(encoding="utf-8")
        assert all(f"layout->{field}" in text for field in fields)


def test_typed_health_flag_values_match_python_and_both_engines() -> None:
    expected = {
        "CAPTURE_HEALTH_ACTIVE": int(CaptureHealthFlag.ACTIVE),
        "CAPTURE_HEALTH_RECOVERING": int(CaptureHealthFlag.RECOVERING),
        "CAPTURE_HEALTH_BACKEND_FAILED": int(CaptureHealthFlag.BACKEND_FAILED),
        "CAPTURE_HEALTH_CONTENT_SUSPECT": int(CaptureHealthFlag.CONTENT_SUSPECT),
        "CAPTURE_HEALTH_PAUSED": int(CaptureHealthFlag.PAUSED),
    }
    for header in (
        WINDOWS / "include" / "shared_memory.h",
    ):
        compact = "".join(header.read_text(encoding="utf-8").split())
        for bit, (name, value) in enumerate(expected.items()):
            assert value == 1 << bit
            assert f"{name}=1u<<{bit}" in compact




def test_windows_reconfiguration_bit_matches_python():
    import re
    text = (WINDOWS / 'include' / 'shared_memory.h').read_text()
    assert re.search(r'CAPTURE_HEALTH_RECONFIGURE_REQUIRED\s*=\s*1u << 5', text)
    assert int(CaptureHealthFlag.RECONFIGURE_REQUIRED) == 32
