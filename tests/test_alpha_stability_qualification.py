"""Deterministic checks for the Windows alpha qualification harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import qualify_alpha_stability as qualification


def packet_series(count: int, step: float = 1 / 60) -> list[qualification.PacketPoint]:
    return [qualification.PacketPoint(index * step, step) for index in range(count)]


def test_timeline_accepts_real_60fps_coverage() -> None:
    result = qualification.timeline_health(
        packet_series(301), requested_seconds=5, expected_fps=60)

    assert result["valid"] is True
    assert result["coverage_seconds"] == pytest.approx(5.0)
    assert result["maximum_gap"] == pytest.approx(1 / 60)


@pytest.mark.parametrize("fps", [60, 120, 144])
def test_timeline_reports_suitable_high_fps_metrics(fps: int) -> None:
    result = qualification.timeline_health(
        packet_series(fps * 5 + 1, 1 / fps),
        requested_seconds=5,
        expected_fps=fps,
    )

    assert result["valid"] is True
    assert result["effective_fps"] == pytest.approx(fps)


def test_timeline_rejects_sparse_four_packet_replay() -> None:
    result = qualification.timeline_health(
        [qualification.PacketPoint(0.0, 1.0),
         qualification.PacketPoint(1.34, 1.0),
         qualification.PacketPoint(2.68, 1.0),
         qualification.PacketPoint(4.02, 1.0)],
        requested_seconds=5,
        expected_fps=60,
    )

    assert result["valid"] is False
    assert result["reason"] == "video_gap_too_large"


def test_timeline_rejects_multi_second_frozen_gap_even_with_coverage() -> None:
    packets = packet_series(121)
    packets[60:] = [qualification.PacketPoint(10.0 + index / 60, 1 / 60)
                    for index in range(len(packets) - 60)]
    result = qualification.timeline_health(
        packets, requested_seconds=10, expected_fps=60)

    assert result["valid"] is False
    assert result["reason"] == "video_gap_too_large"


def test_timeline_rejects_low_effective_rate_without_a_multi_second_gap() -> None:
    packets = [qualification.PacketPoint(index * 0.2, 0.2) for index in range(6)]

    result = qualification.timeline_health(
        packets, requested_seconds=1, expected_fps=60)

    assert result["valid"] is False
    assert result["reason"] == "video_effective_rate_too_low"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.25", 1.25), ("", None), ("N/A", None), ("inf", None), ("nan", None)],
)
def test_float_parser_rejects_non_finite_values(value: str, expected: float | None) -> None:
    assert qualification._float_or_none(value) == expected


def test_packet_parser_preserves_order_and_stream_identity() -> None:
    payload = {"packets": [
        {"codec_type": "video", "stream_index": 0, "pts_time": "0", "duration_time": "0.016"},
        {"codec_type": "audio", "stream_index": 1, "pts_time": "0", "duration_time": "0.02"},
        {"codec_type": "video", "stream_index": 0, "pts_time": "0.016", "duration_time": "0.016"},
    ]}

    video = qualification.parse_packet_points(payload, "video")

    assert [packet.pts for packet in video] == [0.0, 0.016]
    assert [packet.stream_index for packet in video] == [0, 0]


@pytest.mark.parametrize(
    ("encoder", "slot", "backend", "vendor"),
    [
        ("auto", "0", None, None),
        ("nvenc", "1", "native-nvenc", "NVIDIA"),
        ("amf", "2", "ffmpeg-amf", "AMD"),
        ("qsv", "3", "ffmpeg-qsv", "Intel"),
    ],
)
def test_encoder_choice_maps_to_native_argv17(
    encoder: str, slot: str, backend: str | None, vendor: str | None,
) -> None:
    args = qualification.parse_args(["--encoder", encoder])
    command = qualification.build_engine_command(args, "h264", "monitor-path")

    assert command[17] == slot
    expected = qualification.ENCODER_EXPECTATIONS[encoder]
    assert expected["backend"] == backend
    assert expected["vendor"] == vendor


def _topology_event(backend: str, vendor: str) -> dict[str, object]:
    luid = {"high_part": 1, "low_part": 2}
    return {
        "event": "adapter_topology_resolved",
        "encoder_backend": backend,
        "encoder_adapter": vendor,
        "monitor_adapter_luid": luid,
        "capture_device_luid": luid,
        "encoder_adapter_luid": luid,
        "encoder_width": 1280,
        "encoder_height": 720,
    }


def test_explicit_encoder_evidence_rejects_backend_vendor_mismatch() -> None:
    result = qualification.verify_encoder_evidence(
        "amf", "h264_nvenc", _topology_event("native-nvenc", "NVIDIA"))

    assert result["valid"] is False
    assert "requested_backend_mismatch" in result["reasons"]
    assert "requested_vendor_mismatch" in result["reasons"]


def test_explicit_encoder_evidence_fails_closed_without_topology_event() -> None:
    result = qualification.verify_encoder_evidence("nvenc", "h264_nvenc", None)

    assert result["valid"] is False
    assert "adapter_topology_event_missing" in result["reasons"]
    assert "hardware_backend_vendor_unproven" in result["reasons"]


def test_encoder_evidence_requires_same_adapter_identity() -> None:
    event = _topology_event("ffmpeg-amf", "AMD")
    event["encoder_adapter_luid"] = {"high_part": 9, "low_part": 9}

    result = qualification.verify_encoder_evidence("amf", "h264_amf", event)

    assert result["valid"] is False
    assert result["same_adapter"] is False
    assert "same_adapter_identity_unproven" in result["reasons"]


def test_same_adapter_amf_evidence_passes() -> None:
    result = qualification.verify_encoder_evidence(
        "amf", "h264_amf", _topology_event("ffmpeg-amf", "AMD"))

    assert result["valid"] is True
    assert result["same_adapter"] is True
    assert result["cross_adapter"] is False
    assert result["encoder_resolution"] == [1280, 720]


def test_explicit_dimensions_must_agree_with_topology_resolution() -> None:
    result = qualification.verify_encoder_evidence(
        "amf",
        "h264_amf",
        _topology_event("ffmpeg-amf", "AMD"),
        requested_width=1920,
        requested_height=1080,
    )

    assert result["valid"] is False
    assert "encoder_resolution_mismatch" in result["reasons"]


def test_explicit_nvenc_cross_adapter_evidence_is_reported_and_allowed() -> None:
    event = _topology_event("native-nvenc", "NVIDIA")
    event["encoder_adapter_luid"] = {"high_part": 9, "low_part": 9}

    result = qualification.verify_encoder_evidence("nvenc", "h264_nvenc", event)

    assert result["valid"] is True
    assert result["same_adapter"] is False
    assert result["cross_adapter"] is True
    assert result["cross_adapter_allowed"] is True
    assert result["adapter_identity_valid"] is True


@pytest.mark.parametrize(
    ("expected", "actual", "valid"),
    [
        ("wgc", "WGC_MONITOR", True),
        ("dxgi", "DXGI_OUTPUT_DUPLICATION", True),
        ("wgc", "DXGI_OUTPUT_DUPLICATION", False),
        ("auto", "WGC_MONITOR", True),
    ],
)
def test_capture_backend_evidence_is_explicit_and_fail_closed(
    expected: str, actual: str, valid: bool,
) -> None:
    result = qualification.verify_capture_backend(
        expected, {"capture_backend": actual})

    assert result["valid"] is valid
    assert result["actual_backend"] == actual


def test_capture_backend_evidence_requires_topology_event() -> None:
    result = qualification.verify_capture_backend("dxgi", None)

    assert result["valid"] is False
    assert "adapter_topology_event_missing" in result["reasons"]


def test_av_timestamp_deltas_are_signed_and_bounded() -> None:
    video = [qualification.PacketPoint(0.0, 0.1), qualification.PacketPoint(0.9, 0.1)]
    audio = [qualification.PacketPoint(0.1, 0.1), qualification.PacketPoint(1.0, 0.1)]

    result = qualification.av_timestamp_deltas(video, audio)

    assert result["start_delta"] == pytest.approx(0.1)
    assert result["end_delta"] == pytest.approx(0.1)
    assert result["bounded"] is True


def test_av_timestamp_deltas_require_every_audio_stream_to_be_bounded() -> None:
    video = [qualification.PacketPoint(0.0, 0.1), qualification.PacketPoint(0.9, 0.1)]
    audio_streams = [
        (1, [qualification.PacketPoint(0.1, 0.1), qualification.PacketPoint(1.0, 0.1)]),
        (2, [qualification.PacketPoint(0.2, 0.1), qualification.PacketPoint(3.0, 0.1)]),
    ]

    result = qualification.av_timestamp_deltas_per_stream(video, audio_streams)

    assert result[0]["bounded"] is True
    assert result[1]["bounded"] is False


def test_moving_source_uses_requested_fps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ffplay = tmp_path / "ffplay.exe"
    ffplay.write_bytes(b"stub")
    log_path = tmp_path / "moving-source.log"
    commands: list[list[str]] = []

    class FakeProcess:
        pass

    def fake_popen(command: list[str], **_: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(qualification.subprocess, "Popen", fake_popen)
    qualification.start_moving_source(ffplay, log_path, fps=144)

    assert "testsrc2=size=1280x720:rate=144" in commands[0]
    assert log_path.is_file()


def _raw_frames(*values: int, width: int = 32, height: int = 18) -> bytes:
    return b"".join(bytes([value]) * (width * height) for value in values)


@pytest.mark.parametrize(
    ("values", "classification"),
    [
        ((0, 0, 0), "black"),
        ((80, 80, 80), "uniform"),
    ],
)
def test_raw_sample_classifier_rejects_black_uniform_and_frozen(
    values: tuple[int, int, int], classification: str,
) -> None:
    result = qualification.classify_raw_sample(_raw_frames(*values))

    assert result["classification"] == classification
    assert result["valid"] is False
    assert result["moving"] is False


def test_raw_sample_classifier_rejects_nonuniform_frozen_picture() -> None:
    frame = bytes((index * 17) % 256 for index in range(32 * 18))
    result = qualification.classify_raw_sample(frame * 3)

    assert result["classification"] == "frozen"
    assert result["valid"] is False
    assert result["frozen"] is True


def test_raw_sample_classifier_proves_moving_picture() -> None:
    result = qualification.classify_raw_sample(_raw_frames(10, 60, 120, 180))

    assert result["classification"] == "moving"
    assert result["valid"] is True
    assert result["moving"] is True
    assert result["max_frame_delta"] > 1.0


def test_raw_sample_classifier_enforces_bounded_input() -> None:
    raw = bytes(qualification.MAX_RAW_SAMPLE_BYTES + 1)

    result = qualification.classify_raw_sample(raw)

    assert result["classification"] == "unavailable"
    assert result["reason"] == "sample_too_large"
    assert result["frame_count"] == 0


def test_save_plan_has_required_default_shape() -> None:
    args = qualification.parse_args([])

    assert qualification._save_plan(args) == [5] * 20 + [30] * 5 + [60] * 3
    assert args.warmup_seconds >= 65
    assert args.soak_seconds >= 15 * 60
    assert args.total_capture_seconds >= args.warmup_seconds + args.soak_seconds
    assert not str(args.output).startswith(str(qualification.ROOT))


def test_save_plan_can_model_gate_short_and_thirty_second_denominators() -> None:
    args = qualification.parse_args([
        "--short-duration-seconds", "15",
        "--short-saves", "10",
        "--thirty-saves", "10",
        "--sixty-saves", "0",
    ])

    assert qualification._save_plan(args) == ([15] * 10) + ([30] * 10)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--warmup-seconds", "70", "--soak-seconds", "900", "--total-capture-seconds", "969"],
        ["--stall-timeout-seconds", "0"],
        ["--short-saves", "-1"],
        ["--short-duration-seconds", "0"],
    ],
)
def test_parse_args_rejects_unsafe_or_incomplete_qualification(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        qualification.parse_args(arguments)


def test_default_output_can_be_configured_outside_repository(tmp_path: Path) -> None:
    args = qualification.parse_args(["--output", str(tmp_path)])

    assert args.output == tmp_path


def test_zero_frame_stream_is_classified_as_a_stall(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Clock:
        values = iter([0.0, 1.0, 17.0])

        def monotonic(self) -> float:
            return next(self.values)

    class HealthBridge:
        def get_status(self) -> dict[str, object]:
            return {"connected": True, "frames_captured": 0, "capture_health_flags": 0}

    monkeypatch.setattr(qualification.time, "monotonic", Clock().monotonic)
    monkeypatch.setattr(
        qualification, "process_snapshot",
        lambda process, now=None: {"captured_monotonic": now, "process_cpu_seconds": 0.0},
    )
    sampler = qualification.QualificationSampler(
        HealthBridge(), FakeProcess(), tmp_path / "samples.jsonl", started_at=0.0)

    sampler.check(max_stall_seconds=15.0)
    with pytest.raises(TimeoutError, match="no fresh frames"):
        sampler.check(max_stall_seconds=15.0)


class FakeProcess:
    _handle = 0
    pid = 123

    def poll(self) -> None:
        return None


class FakeBridge:
    def __init__(self, responses: list[tuple[str, str]]) -> None:
        self.responses_to_publish = list(responses)
        self.responses: list[tuple[str, str]] = []
        self.submitted: list[tuple[str, int]] = []

    def peek_save_response(self) -> tuple[str, str] | None:
        return self.responses[0] if self.responses else None

    def consume_save_response(self, expected_kind=None) -> bool:
        if self.responses:
            self.responses.pop(0)
            return True
        return False

    def save_clip(self, path: str, duration: int) -> bool:
        self.submitted.append((path, duration))
        self.responses.extend(self.responses_to_publish)
        return True


def test_save_and_wait_observes_one_terminal_publication(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"encoded")
    bridge = FakeBridge([("started", ""), ("saved", "")])

    result = qualification.save_and_wait(bridge, path, 5, timeout=1.0)

    assert result["publish_count"] == 1
    assert result["path"] == path
    assert bridge.submitted == [(str(path), 5)]


def test_save_and_wait_rejects_duplicate_terminal_publication(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"encoded")
    bridge = FakeBridge([("saved", ""), ("saved", "again")])

    with pytest.raises(RuntimeError, match="published 2 times"):
        qualification.save_and_wait(bridge, path, 5, timeout=1.0)


def test_strict_probe_requires_audio_when_requested(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"encoded")
    probe_payload = {
        "streams": [{"index": 0, "codec_type": "video", "duration": "5.0"}],
        "format": {"duration": "5.0"},
    }
    packet_payload = {"packets": [
        {"codec_type": "video", "stream_index": 0, "pts_time": str(i / 60), "duration_time": str(1 / 60)}
        for i in range(301)
    ]}
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> str:
        commands.append(command)
        if "-show_streams" in command:
            return json.dumps(probe_payload)
        if "-show_packets" in command:
            return json.dumps(packet_payload)
        return ""

    monkeypatch.setattr(qualification, "run_checked", fake_run)
    result = qualification.strict_probe_clip(
        Path("ffprobe"), Path("ffmpeg"), path,
        name="clip", requested_seconds=5, save_latency=0.1,
        expected_fps=60, require_audio=True,
    )

    assert result.audio_duration_sane is False
    assert result.validation_error == "audio_duration_mismatch"
    assert any("-xerror" in command for command in commands)


@pytest.mark.parametrize(
    ("actual_codec", "expected_error"),
    [("h264", None), ("hevc", "encoded_codec_mismatch")],
)
def test_strict_probe_checks_expected_encoded_codec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actual_codec: str,
    expected_error: str | None,
) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"encoded")
    probe_payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": actual_codec,
                "width": 1280,
                "height": 720,
                "duration": "5.0",
            },
            {"index": 1, "codec_type": "audio", "duration": "5.0"},
        ],
        "format": {"duration": "5.0"},
    }
    packet_payload = {"packets": [
        {
            "codec_type": "video",
            "stream_index": 0,
            "pts_time": str(i / 60),
            "duration_time": str(1 / 60),
        }
        for i in range(301)
    ] + [
        {
            "codec_type": "audio",
            "stream_index": 1,
            "pts_time": str(i / 50),
            "duration_time": str(1 / 50),
        }
        for i in range(251)
    ]}

    def fake_run(command: list[str], **_: object) -> str:
        if "-show_streams" in command:
            return json.dumps(probe_payload)
        if "-show_packets" in command:
            return json.dumps(packet_payload)
        return ""

    monkeypatch.setattr(qualification, "run_checked", fake_run)
    monkeypatch.setattr(
        qualification,
        "_decode_content_sample",
        lambda *args, **kwargs: qualification.classify_raw_sample(
            _raw_frames(10, 60, 120)),
    )
    result = qualification.strict_probe_clip(
        Path("ffprobe"), Path("ffmpeg"), path,
        name="clip", requested_seconds=5, save_latency=0.1,
        expected_fps=60, expected_codec="h264",
        expected_width=1280, expected_height=720,
    )

    assert result.actual_codec == actual_codec
    assert result.validation_error == expected_error
    assert result.dimensions_valid is True


def test_process_handle_count_is_unavailable_off_windows() -> None:
    assert qualification.process_handle_count(FakeProcess()) is None


def test_main_returns_failure_for_a_report_that_did_not_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    executable = Path(__file__).resolve()
    monkeypatch.setattr(qualification.sys, "platform", "win32")
    monkeypatch.setattr(
        qualification,
        "parse_args",
        lambda _: type("Args", (), {
            "engine": executable,
            "ffmpeg": executable,
            "ffprobe": executable,
            "output": tmp_path,
        })(),
    )
    monkeypatch.setattr(
        qualification,
        "qualify",
        lambda args: {"status": "FAILED", "failure": "synthetic failure"},
    )

    assert qualification.main([]) == 1
