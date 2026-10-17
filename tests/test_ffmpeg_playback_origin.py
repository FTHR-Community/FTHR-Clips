"""Native playback checks for a shared container-origin timeline.

These tests deliberately load one explicitly selected bridge DLL. They do not
exercise the production DLL discovery order, so a stale development artifact
cannot silently make a green build look like validation of a new binary. The
small media fixtures are local files produced by the pinned FFmpeg toolchain;
no capture, audio device, or GUI is involved.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FFMPEG = (
    ROOT / "FTHRcapture" / "FTHRclips" / "third_party" / "ffmpeg" / "bin" / "ffmpeg.exe"
)


def _ffmpeg_path() -> Path:
    configured = os.environ.get("FTHR_FFMPEG_EXE")
    path = Path(configured) if configured else DEFAULT_FFMPEG
    if path.is_file():
        return path
    discovered = shutil.which("ffmpeg")
    if discovered:
        return Path(discovered)
    pytest.fail("pinned/local FFmpeg is required to generate playback-origin fixtures")


def _ffprobe_path() -> Path:
    configured = os.environ.get("FTHR_FFPROBE_EXE")
    path = Path(configured) if configured else _ffmpeg_path().with_name("ffprobe.exe")
    if path.is_file():
        return path
    discovered = shutil.which("ffprobe")
    if discovered:
        return Path(discovered)
    pytest.fail("FFprobe is required to inspect the negative-preroll fixture")


def _marker_expr(sample_rate: int = 48_000, duration: float = 2.0) -> str:
    """Lavfi source with silent/440 Hz/880 Hz timing markers."""

    return (
        "aevalsrc=if(lt(mod(t\\,2)\\,0.5)\\,0\\,"
        "if(lt(mod(t\\,2)\\,1)\\,0.8*sin(2*PI*440*t)\\,"
        f"0.8*sin(2*PI*880*t))):s={sample_rate}:d={duration}:c=mono"
    )


def _make_origin_fixtures(root: Path) -> dict[str, tuple[Path, tuple[int, ...]]]:
    """Create deterministic local fixtures without depending on external media."""

    ffmpeg = _ffmpeg_path()

    def run(name: str, args: list[str], streams: tuple[int, ...]) -> None:
        output = root / name
        subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args, str(output)],
            check=True,
            timeout=30,
        )
        fixtures[name] = (output, streams)

    fixtures: dict[str, tuple[Path, tuple[int, ...]]] = {}
    run(
        "common-origin.mp4",
        [
            "-itsoffset", "5", "-f", "lavfi", "-i", _marker_expr(),
            "-c:a", "aac", "-copyts", "-avoid_negative_ts", "disabled",
        ],
        (0,),
    )
    run(
        "zero-origin.m4a",
        [
            "-f", "lavfi", "-i", _marker_expr(),
            "-c:a", "aac",
        ],
        (0,),
    )
    run(
        "negative-preroll.m4a",
        [
            "-itsoffset", "-0.021333", "-f", "lavfi", "-i", _marker_expr(),
            "-c:a", "aac", "-copyts", "-avoid_negative_ts", "disabled",
        ],
        (0,),
    )
    run(
        "late-audio.mp4",
        [
            "-f", "lavfi", "-i", "color=size=64x64:rate=30:duration=2",
            "-itsoffset", "5", "-f", "lavfi", "-i", _marker_expr(),
            "-map", "0:v", "-map", "1:a", "-t", "7", "-c:v", "mpeg4", "-c:a", "aac",
            "-copyts", "-start_at_zero", "-avoid_negative_ts", "disabled",
        ],
        (1,),
    )
    run(
        "unequal-audio.mp4",
        [
            "-f", "lavfi", "-i", "color=size=64x64:rate=30:duration=3.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3,volume=0.20",
            "-itsoffset", "0.478", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=3,volume=0.80",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "3.5",
            "-c:v", "mpeg4", "-c:a", "aac", "-copyts", "-start_at_zero",
            "-avoid_negative_ts", "disabled",
        ],
        (1, 2),
    )
    return fixtures


@pytest.fixture
def origin_fixtures(tmp_path):
    return _make_origin_fixtures(tmp_path)


def _bridge_path() -> Path:
    value = os.environ.get("FTHR_PLAYBACK_TEST_DLL")
    if not value:
        pytest.skip("set FTHR_PLAYBACK_TEST_DLL to the explicitly selected fresh bridge DLL")
    path = Path(value)
    if not path.is_file():
        pytest.fail(f"selected playback bridge DLL is unavailable: {path}")
    expected = os.environ.get("FTHR_PLAYBACK_EXPECTED_SHA256")
    if expected:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual.casefold() == expected.casefold(), (
            f"selected bridge hash mismatch: {path}\nexpected {expected}\nactual {actual}"
        )
    return path


@pytest.fixture
def selected_bridge(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force the Python wrapper to use the test's explicitly selected DLL."""

    path = _bridge_path()
    import core.ffmpeg_playback as playback

    dll_handles = []
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        ffmpeg_bin = Path(os.environ.get("FTHR_FFMPEG_BIN", str(DEFAULT_FFMPEG.parent)))
        for directory in (path.parent, ffmpeg_bin):
            if directory.is_dir():
                dll_handles.append(os.add_dll_directory(str(directory)))

    monkeypatch.setattr(playback, "_load_bridge", lambda: ctypes.CDLL(str(path)))
    # Keep Windows DLL search handles alive through each mixer's lifetime.
    monkeypatch.setattr(playback, "_fthr_origin_test_dll_handles", dll_handles, raising=False)
    return path


def _source(index: int):
    from core.playback_mix_model import PlaybackSource

    return PlaybackSource(
        source_id=f"origin-test-{index}",
        display_name=f"origin-test-{index}",
        source_type="track",
        container_index=index,
        audio_index=0,
    )


def _pull_with_gains(mixer, frames: int, gains: list[float]) -> np.ndarray:
    chunks: list[np.ndarray] = []
    remaining = frames
    while remaining:
        block = min(1024, remaining)
        pcm = mixer.pull(block, gains, 1.0)
        if pcm is None:
            break
        values = np.frombuffer(pcm, dtype="<f4")
        assert values.size == block * 2
        chunks.append(values.reshape(block, 2).copy())
        remaining -= block
    if not chunks:
        return np.empty((0, 2), dtype=np.float32)
    return np.concatenate(chunks, axis=0)


def _pull(mixer, frames: int, source_count: int = 1) -> np.ndarray:
    return _pull_with_gains(mixer, frames, [1.0] * source_count)


def _first_nonzero(pcm: np.ndarray, threshold: float = 1e-4) -> int | None:
    active = np.flatnonzero(np.max(np.abs(pcm), axis=1) > threshold)
    return int(active[0]) if active.size else None


def _rms(pcm: np.ndarray, start: int, end: int) -> float:
    return float(np.sqrt(np.mean(pcm[start:end] * pcm[start:end])))


def _dominant_frequency(pcm: np.ndarray, start: int, end: int) -> float:
    signal = pcm[start:end, 0]
    window = np.hanning(signal.size)
    spectrum = np.abs(np.fft.rfft(signal * window))
    frequencies = np.fft.rfftfreq(signal.size, 1 / 48_000)
    return float(frequencies[int(np.argmax(spectrum[1:]) + 1)])


def test_common_container_origin_is_removed_once_and_seek_is_relative(
    selected_bridge, origin_fixtures
):
    """Markers prove both relative placement and forward/backward seeking."""

    from core.ffmpeg_playback import InProcessFFmpegMixer

    media, (stream_index,) = origin_fixtures["common-origin.mp4"]
    mixer = InProcessFFmpegMixer(str(media), [_source(stream_index)])
    try:
        first = _pull(mixer, int(1.6 * 48_000))
        assert _rms(first, 0, int(0.4 * 48_000)) < 1e-3
        assert _rms(first, int(0.55 * 48_000), int(0.95 * 48_000)) > 0.01
        assert _dominant_frequency(first, int(0.55 * 48_000), int(0.95 * 48_000)) == pytest.approx(
            440, abs=30
        )
        assert _dominant_frequency(first, int(1.05 * 48_000), int(1.45 * 48_000)) == pytest.approx(
            880, abs=30
        )

        mixer.seek(500)
        after_seek = _pull(mixer, int(0.45 * 48_000))
        assert _dominant_frequency(after_seek, int(0.10 * 48_000), int(0.40 * 48_000)) == pytest.approx(
            440, abs=30
        )

        mixer.seek(1200)
        at_second_marker = _pull(mixer, int(0.35 * 48_000))
        assert _dominant_frequency(at_second_marker, int(0.10 * 48_000), int(0.30 * 48_000)) == pytest.approx(
            880, abs=30
        )

        mixer.seek(0)
        back_to_start = _pull(mixer, int(0.35 * 48_000))
        assert _rms(back_to_start, int(0.10 * 48_000), int(0.30 * 48_000)) < 1e-3
    finally:
        mixer.close()


def test_zero_origin_and_late_audio_controls_keep_relative_stems(
    selected_bridge, origin_fixtures
):
    """Zero-origin audio starts in place; a true late stem stays late."""

    from core.ffmpeg_playback import InProcessFFmpegMixer

    zero_media, (zero_stream_index,) = origin_fixtures["zero-origin.m4a"]
    zero = InProcessFFmpegMixer(str(zero_media), [_source(zero_stream_index)])
    try:
        zero_pcm = _pull(zero, 48_000)
        zero_first = _first_nonzero(zero_pcm)
        assert zero_first is not None and 0.40 * 48_000 < zero_first < 0.65 * 48_000
        assert _rms(zero_pcm, int(0.55 * 48_000), int(0.95 * 48_000)) > 0.01
    finally:
        zero.close()

    late_media, (late_stream_index,) = origin_fixtures["late-audio.mp4"]
    late = InProcessFFmpegMixer(str(late_media), [_source(late_stream_index)])
    try:
        late_pcm = _pull(late, 7 * 48_000)
        late_first = _first_nonzero(late_pcm)
        assert late_first is not None and 5.35 * 48_000 < late_first < 5.75 * 48_000
    finally:
        late.close()


def test_negative_aac_preroll_is_trimmed_once(selected_bridge, origin_fixtures):
    """A negative AAC preroll packet does not shift the relative marker."""

    import json

    from core.ffmpeg_playback import InProcessFFmpegMixer

    media, (stream_index,) = origin_fixtures["negative-preroll.m4a"]
    probe = subprocess.run(
        [
            str(_ffprobe_path()), "-v", "error", "-select_streams", "a:0",
            "-show_packets", "-show_entries", "packet=pts,side_data_list", "-of", "json", str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    packets = json.loads(probe.stdout)["packets"]
    assert int(packets[0]["pts"]) < 0
    assert "side_data_list" in packets[0]

    mixer = InProcessFFmpegMixer(str(media), [_source(stream_index)])
    try:
        pcm = _pull(mixer, int(1.5 * 48_000))
        first = _first_nonzero(pcm)
        # The MP4 format origin remains zero while its first AAC packets are
        # negative; the marker is therefore at 0.5 - 0.021333 s.
        assert first is not None and 0.45 * 48_000 < first < 0.50 * 48_000
    finally:
        mixer.close()


def test_unequal_audio_origins_are_not_compacted_to_earliest_stream(
    selected_bridge, origin_fixtures
):
    """The second selected stem remains approximately 478 ms behind the first."""

    from core.ffmpeg_playback import InProcessFFmpegMixer

    media, stream_indexes = origin_fixtures["unequal-audio.mp4"]
    mixer = InProcessFFmpegMixer(str(media), [_source(index) for index in stream_indexes])
    try:
        # Isolate each selected source. The delayed stream's first audible
        # sample is its actual 478 ms timestamp, not a compacted zero.
        mixer.seek(0)
        first_only = _pull_with_gains(mixer, 2 * 48_000, [1.0, 0.0])
        mixer.seek(0)
        second_only = _pull_with_gains(mixer, 2 * 48_000, [0.0, 1.0])
        assert _first_nonzero(first_only) is not None
        delayed_first = _first_nonzero(second_only)
        assert delayed_first is not None and 0.45 * 48_000 < delayed_first < 0.52 * 48_000
        assert _dominant_frequency(second_only, int(0.60 * 48_000), int(0.90 * 48_000)) == pytest.approx(
            880, abs=30
        )
    finally:
        mixer.close()


def test_resampled_44100_audio_has_relative_progress(selected_bridge, tmp_path):
    """The origin correction also applies when source audio is 44.1 kHz."""

    ffmpeg = Path(os.environ.get("FTHR_FFMPEG_EXE", str(DEFAULT_FFMPEG)))
    if not ffmpeg.is_file():
        pytest.skip(f"pinned FFmpeg is unavailable: {ffmpeg}")
    media = tmp_path / "origin-44100.m4a"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-itsoffset", "5", "-f", "lavfi", "-i", _marker_expr(sample_rate=44_100),
            "-c:a", "aac", "-ar", "44100", "-copyts", "-avoid_negative_ts", "disabled", str(media),
        ],
        check=True,
        timeout=30,
    )

    from core.ffmpeg_playback import InProcessFFmpegMixer

    mixer = InProcessFFmpegMixer(str(media), [_source(0)])
    try:
        pcm = _pull(mixer, int(1.5 * 48_000))
        assert _rms(pcm, int(0.55 * 48_000), int(0.95 * 48_000)) > 0.01
    finally:
        mixer.close()
