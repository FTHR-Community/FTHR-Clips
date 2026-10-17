from __future__ import annotations

from hashlib import sha256
import math
import json
from pathlib import Path
import subprocess
import sys
from array import array

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

from core.playback_mix_model import PlaybackSource
from core.transactional_output import commit_staged_output, create_staged_output_path
from ui.clip_viewer import ClipViewer, VolumePopup


def _run_export_probe(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode:
        pytest.fail(result.stderr.decode(errors='replace')[-3000:])
    return result


def _marker_expression(frequency: float, markers: tuple[float, ...],
                       sample_rate: int, duration: float) -> str:
    windows = '+'.join(
        f'between(t\\,{marker}\\,{marker + 0.05})'
        for marker in markers)
    return (
        f'aevalsrc=0.7*sin(2*PI*{frequency}*t)*if({windows}\\,1\\,0):'
        f's={sample_rate}:d={duration}')


def _make_late_source(ffmpeg: str, path: Path, *, system_offset: float = 0.0,
                      mic_offset: float = 0.5, mic_sample_rate: int = 48000,
                      mic_duration: float = 3.2) -> None:
    video = (
        'color=c=black:s=64x48:r=60:d=4,drawbox=x=0:y=0:w=64:h=48:'
        'color=white:t=fill:enable=between(t\\,0.4\\,0.45)+'
        'between(t\\,1.8\\,1.85)+between(t\\,3.0\\,3.05)')
    system = _marker_expression(440.0, (0.4, 1.8, 3.0), 48000, 4.0)
    microphone = _marker_expression(
        880.0, (0.8, 1.8, 2.8), mic_sample_rate, mic_duration)
    _run_export_probe([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', video,
        '-itsoffset', str(system_offset), '-f', 'lavfi', '-i', system,
        '-itsoffset', str(mic_offset), '-f', 'lavfi', '-i', microphone,
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'mpeg4', '-q:v', '5', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-shortest', str(path),
    ])


def _decode_marker_centers(ffmpeg: str, media: Path, audio_ordinal: int,
                           output: Path, frequency: float) -> list[float]:
    _run_export_probe([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(media),
        '-map', f'0:a:{audio_ordinal}', '-f', 'f32le', '-ac', '1', '-ar',
        '48000', str(output),
    ])
    samples = array('f')
    with output.open('rb') as stream:
        samples.fromfile(stream, output.stat().st_size // samples.itemsize)
    rate = 48000
    block_size = rate // 100
    kernel = tuple(
        (math.cos(2.0 * math.pi * frequency * index / rate),
         -math.sin(2.0 * math.pi * frequency * index / rate))
        for index in range(block_size))
    magnitudes = []
    for offset in range(0, len(samples) - block_size + 1, block_size):
        block = samples[offset:offset + block_size]
        real = sum(sample * cosine
                   for sample, (cosine, _sine) in zip(block, kernel,
                                                       strict=True))
        imag = sum(sample * sine
                   for sample, (_cosine, sine) in zip(block, kernel,
                                                      strict=True))
        magnitudes.append(math.hypot(real, imag) / block_size)
    active = [index for index, value in enumerate(magnitudes) if value > 0.05]
    groups: list[list[int]] = []
    for index in active:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    return [
        (group[0] + group[-1] + 1) * block_size / (2 * rate)
        for group in groups
    ]


def _first_decoded_audio_timestamp(ffprobe: str, media: Path,
                                   ordinal: int) -> float:
    result = _run_export_probe([
        ffprobe, '-v', 'error', '-select_streams', f'a:{ordinal}',
        '-show_frames', '-show_entries',
        'frame=best_effort_timestamp_time,pts_time', '-of', 'json',
        str(media),
    ])
    frames = json.loads(result.stdout).get('frames', [])
    for frame in frames:
        for field in ('best_effort_timestamp_time', 'pts_time'):
            value = frame.get(field)
            if value not in (None, 'N/A'):
                return float(value)
    raise AssertionError(f'no decoded timestamp for audio stream {ordinal}')


def _export_viewer(source: Path):
    return type('Viewer', (), {
        'clip_path': str(source),
        '_audio_tracks': (('system', 0), ('mic', 1)),
        '_playback_sources': (
            PlaybackSource('system', 'System Audio', 'system', 1, 0),
            PlaybackSource('mic', 'Microphone', 'microphone', 2, 1),
        ),
        '_source_volumes': {'system': 100, 'mic': 100},
        '_source_mutes': {'system': False, 'mic': False},
        '_master_volume': 100,
    })()


def _markers_in_ranges(markers: list[float],
                       ranges: list[tuple[float, float]]) -> list[float]:
    result: list[float] = []
    output_offset = 0.0
    for start, end in ranges:
        result.extend(output_offset + marker - start for marker in markers
                      if start <= marker < end)
        output_offset += end - start
    return result


def test_dynamic_volume_popup_has_real_source_mute_controls(qtbot):
    source = PlaybackSource('mic', 'Microphone', 'microphone', 2, 1)
    popup = VolumePopup(80, {'mic': 70}, {'mic': True}, (source,), live_preview=True)
    qtbot.addWidget(popup)
    seen = []
    popup.source_muted.connect(lambda key, muted: seen.append((key, muted)))
    popup._mute_buttons['mic'].click()
    assert seen == [('mic', False)]
    assert popup._mute_buttons['mic'].text() == 'MUTE'
    assert popup._sliders['mic'].value() == 70
    popup._mute_buttons['mic'].click()
    assert seen[-1] == ('mic', True)
    assert popup._mute_buttons['mic'].text() == 'MUTED'
    assert popup._sliders['mic'].value() == 70


def test_volume_popup_handles_long_names_eight_rows_and_unavailable_source(qtbot):
    sources = tuple(
        PlaybackSource(f'app-{index}', f'Long Application Source Name {index}',
                       'application', index + 1, index)
        for index in range(8)
    )
    unavailable = PlaybackSource(
        'missing', 'A Source That Cannot Be Decoded', 'track', 10, 9, available=False)
    popup = VolumePopup(80, source_tracks=sources + (unavailable,), live_preview=True)
    qtbot.addWidget(popup)
    assert popup._row_order[0] == 'master'
    assert popup._row_order[1:9] == [source.source_id for source in sources]
    assert popup.width() >= 520
    assert popup._labels['app-0'].toolTip() == 'Long Application Source Name 0'
    assert popup._sliders['missing'].isEnabled() is False
    assert popup._mute_buttons['missing'].isEnabled() is False
    assert popup._values['missing'].text() == '—'


def test_volume_popup_hides_default_mix_base_when_application_rows_exist(qtbot):
    base = PlaybackSource(
        'base', 'Default Mix', 'system', 1, 0,
        mix_role='base', editable=False)
    chrome = PlaybackSource(
        'chrome', 'Google Chrome', 'application', 2, 1,
        mix_role='delta')
    discord = PlaybackSource(
        'discord', 'Discord', 'application', 3, 2,
        mix_role='delta')

    popup = VolumePopup(100, source_tracks=(base, chrome, discord), live_preview=True)
    qtbot.addWidget(popup)

    assert popup._row_order == ['master', 'chrome', 'discord']
    assert 'base' not in popup._sliders
    assert popup._labels['chrome'].toolTip() == 'Google Chrome'


def test_volume_popup_disables_source_controls_when_live_mixer_is_unavailable(qtbot):
    source = PlaybackSource('mic', 'Microphone', 'microphone', 2, 1)
    popup = VolumePopup(80, source_tracks=(source,), live_preview=False)
    qtbot.addWidget(popup)

    assert popup._sliders['mic'].isEnabled() is False
    assert popup._mute_buttons['mic'].isEnabled() is False


def test_volume_popup_uses_real_running_app_icon_when_available(qtbot, monkeypatch):
    from PySide6.QtGui import QColor, QIcon, QPixmap

    pixmap = QPixmap(18, 18)
    pixmap.fill(QColor('#00d9c0'))
    monkeypatch.setattr(
        'ui.clip_viewer._running_windows_app_icon', lambda identity: QIcon(pixmap))
    source = PlaybackSource(
        'kovaak', "KovaaK's", 'application', 2, 1,
        persistent_identity='fpsaimtrainer-win64-shipping')
    popup = VolumePopup(100, source_tracks=(source,), live_preview=True)
    qtbot.addWidget(popup)

    assert popup._icons['kovaak'].pixmap() is not None
    assert not popup._icons['kovaak'].pixmap().isNull()


def test_one_failed_source_is_disabled_without_removing_healthy_source():
    system = PlaybackSource('system', 'System Audio', 'system', 1, 0)
    mic = PlaybackSource('mic', 'Microphone', 'microphone', 2, 1)
    viewer = type('Viewer', (), {
        '_playback_sources': (system, mic),
        '_volume_popup': None,
    })()
    ClipViewer._on_audio_source_failed(viewer, 'mic')
    assert viewer._playback_sources[0].available is True
    assert viewer._playback_sources[1].available is False


def test_export_uses_current_source_gain_mute_master_and_limiter():
    viewer = type('Viewer', (), {
        'clip_path': 'clip.mp4',
        '_audio_tracks': (('system', 0), ('mic', 1)),
        '_playback_sources': (
            PlaybackSource('system', 'System Audio', 'system', 1, 0),
            PlaybackSource('mic', 'Microphone', 'microphone', 2, 1),
        ),
        '_source_volumes': {'system': 100, 'mic': 80},
        '_source_mutes': {'system': False, 'mic': True},
        '_master_volume': 50,
    })()
    command = ClipViewer._build_export_cmd(
        viewer, 'ffmpeg', 0.0, 10.0, 'out.mp4', None, ['-c:v', 'copy'])
    joined = ' '.join(command)
    assert '0:a:0' in joined and '0:a:1' in joined
    assert 'volume=0.000000' in joined
    assert 'alimiter=limit=0.98' in joined
    assert 'volume=0.500000[aout]' in joined


@pytest.mark.parametrize('late_stream', ('microphone', 'system'))
def test_build_export_cmd_preserves_late_audio_stream_origin(tmp_path, late_stream):
    """A delayed source marker must retain its native stream offset."""
    try:
        from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
    except Exception as error:
        pytest.skip(f'no reviewed FFmpeg runtime: {error}')

    source = tmp_path / f'late-{late_stream}-origin-source.mp4'
    offsets = {
        'microphone': {'system_offset': 0.0, 'mic_offset': 0.5},
        'system': {'system_offset': 0.5, 'mic_offset': 0.0,
                   'mic_duration': 4.0},
    }
    _make_late_source(ffmpeg, source, **offsets[late_stream])
    viewer = _export_viewer(source)
    output = tmp_path / 'late-origin-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 4.0, str(output), None, ['-c:v', 'copy'])
    _run_export_probe(command)

    target_ordinal = 1 if late_stream == 'microphone' else 0
    target_frequency = 880.0 if late_stream == 'microphone' else 440.0
    native_pcm = tmp_path / f'{late_stream}.f32'
    exported_pcm = tmp_path / 'exported.f32'
    native = _decode_marker_centers(
        ffmpeg, source, target_ordinal, native_pcm, target_frequency)
    origin = _first_decoded_audio_timestamp(ffprobe, source, target_ordinal)
    exported = _decode_marker_centers(
        ffmpeg, output, 0, exported_pcm, target_frequency)

    expected = [center + origin for center in native]
    reference = ([1.325, 2.325, 3.325] if late_stream == 'microphone'
                 else [0.925, 2.325, 3.525])
    assert expected == pytest.approx(reference, abs=0.03)
    assert exported == pytest.approx(expected, abs=0.03)


def test_build_export_cmd_keeps_aligned_audio_markers(tmp_path):
    """The timestamp normalization must not move already aligned streams."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'aligned-source.mp4'
    _make_late_source(ffmpeg, source, mic_offset=0.0)
    viewer = _export_viewer(source)
    output = tmp_path / 'aligned-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 4.0, str(output), None, ['-c:v', 'copy'])
    _run_export_probe(command)

    native_pcm = tmp_path / 'aligned-native.f32'
    exported_pcm = tmp_path / 'aligned-exported.f32'
    native = _decode_marker_centers(ffmpeg, source, 1, native_pcm, 880.0)
    origin = _first_decoded_audio_timestamp(ffprobe, source, 1)
    exported = _decode_marker_centers(
        ffmpeg, output, 0, exported_pcm, 880.0)
    assert exported == pytest.approx(
        [marker + origin for marker in native], abs=0.03)


def test_build_export_cmd_single_late_stream_keeps_origin(tmp_path):
    """A filtered export with only the delayed source retains its origin."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'single-late-source.mp4'
    _make_late_source(ffmpeg, source)
    viewer = _export_viewer(source)
    viewer._audio_tracks = (('mic', 1),)
    viewer._playback_sources = (viewer._playback_sources[1],)
    output = tmp_path / 'single-late-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 4.0, str(output), None, ['-c:v', 'copy'])
    _run_export_probe(command)

    native_pcm = tmp_path / 'single-native.f32'
    exported_pcm = tmp_path / 'single-exported.f32'
    native = _decode_marker_centers(ffmpeg, source, 1, native_pcm, 880.0)
    origin = _first_decoded_audio_timestamp(ffprobe, source, 1)
    exported = _decode_marker_centers(
        ffmpeg, output, 0, exported_pcm, 880.0)
    assert exported == pytest.approx(
        [marker + origin for marker in native], abs=0.03)


def test_build_export_cmd_preserves_origins_across_multiple_segments(tmp_path):
    """The shared mix is normalized before segment trim/concat."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'multi-segment-source.mp4'
    _make_late_source(ffmpeg, source)
    viewer = _export_viewer(source)
    segments = [(0.0, 1.0), (1.5, 3.5)]
    output = tmp_path / 'multi-segment-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 3.5, str(output), None,
        ['-c:v', 'mpeg4', '-q:v', '5'],
        segments=segments)
    _run_export_probe(command)

    expected: dict[float, list[float]] = {}
    for frequency, ordinal in ((440.0, 0), (880.0, 1)):
        native_pcm = tmp_path / f'native-{int(frequency)}.f32'
        native = _decode_marker_centers(
            ffmpeg, source, ordinal, native_pcm, frequency)
        origin = _first_decoded_audio_timestamp(ffprobe, source, ordinal)
        expected[frequency] = _markers_in_ranges(
            [marker + origin for marker in native], segments)
    for frequency in expected:
        exported_pcm = tmp_path / f'exported-{int(frequency)}.f32'
        exported = _decode_marker_centers(
            ffmpeg, output, 0, exported_pcm, frequency)
        assert exported == pytest.approx(expected[frequency], abs=0.03)


@pytest.mark.parametrize('start_s,end_s', ((0.25, 3.75), (1.5, 3.5)))
def test_build_export_cmd_preserves_origins_when_trim_starts_before_or_after(
        tmp_path, start_s, end_s):
    """Input seeking keeps markers relative to either trim boundary."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / f'trim-{start_s}.mp4'
    _make_late_source(ffmpeg, source)
    viewer = _export_viewer(source)
    output = tmp_path / f'trim-{start_s}-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, start_s, end_s - start_s, str(output), None,
        ['-c:v', 'copy'])
    _run_export_probe(command)

    native_pcm = tmp_path / 'trim-native.f32'
    exported_pcm = tmp_path / 'trim-exported.f32'
    native = _decode_marker_centers(ffmpeg, source, 1, native_pcm, 880.0)
    origin = _first_decoded_audio_timestamp(ffprobe, source, 1)
    expected = _markers_in_ranges(
        [marker + origin for marker in native], [(start_s, end_s)])
    exported = _decode_marker_centers(
        ffmpeg, output, 0, exported_pcm, 880.0)
    assert exported == pytest.approx(expected, abs=0.03)


def test_build_export_cmd_preserves_origins_when_resampling_mic(tmp_path):
    """Per-input timestamp normalization also survives 44.1 kHz input."""
    from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe

    ffmpeg = get_ffmpeg_exe()
    ffprobe = get_ffprobe_exe()
    source = tmp_path / 'resampled-source.mp4'
    _make_late_source(ffmpeg, source, mic_sample_rate=44100)
    viewer = _export_viewer(source)
    output = tmp_path / 'resampled-export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 4.0, str(output), None, ['-c:v', 'copy'])
    _run_export_probe(command)

    native_pcm = tmp_path / 'resampled-native.f32'
    exported_pcm = tmp_path / 'resampled-exported.f32'
    native = _decode_marker_centers(ffmpeg, source, 1, native_pcm, 880.0)
    origin = _first_decoded_audio_timestamp(ffprobe, source, 1)
    exported = _decode_marker_centers(
        ffmpeg, output, 0, exported_pcm, 880.0)
    assert exported == pytest.approx(
        [marker + origin for marker in native], abs=0.03)


def test_mixed_export_is_transactional_and_preserve_mapping_keeps_stems(tmp_path):
    """Exercise the reviewed FFmpeg command against real two-stem media."""
    try:
        from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
    except Exception as error:
        import pytest
        pytest.skip(f'no reviewed FFmpeg runtime: {error}')

    source = tmp_path / 'source.mp4'
    subprocess.run([
        ffmpeg, '-y', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
        '-t', '1', '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0',
        '-c:v', 'mpeg4', '-c:a', 'aac', str(source),
    ], check=True, capture_output=True, timeout=20)
    source_hash = sha256(source.read_bytes()).hexdigest()
    viewer = type('Viewer', (), {
        'clip_path': str(source),
        '_audio_tracks': (('system', 0), ('mic', 1)),
        '_playback_sources': (
            PlaybackSource('system', 'System Audio', 'system', 1, 0),
            PlaybackSource('mic', 'Microphone', 'microphone', 2, 1),
        ),
        '_source_volumes': {'system': 100, 'mic': 100},
        '_source_mutes': {'system': False, 'mic': True},
        '_master_volume': 100,
    })()
    final = tmp_path / 'mixed.mp4'
    staged = create_staged_output_path(final)
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0.0, 1.0, str(staged), None, ['-c:v', 'copy'])
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    commit_staged_output(staged, final)

    def _audio_stream_count(path: Path) -> int:
        result = subprocess.run([
            ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(path),
        ], check=True, capture_output=True, text=True, timeout=20)
        return sum(stream.get('codec_type') == 'audio'
                   for stream in json.loads(result.stdout)['streams'])

    assert final.is_file()
    assert _audio_stream_count(final) == 1
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(final), '-f', 'null', '-'],
                   check=True, capture_output=True, timeout=20)
    assert sha256(source.read_bytes()).hexdigest() == source_hash

    # Full-quality Share's explicit map is deliberately all stems, not
    # FFmpeg's automatic one-stream selection.  Validate the actual mapping
    # used by its no-edit path with the same source media.
    preserved = tmp_path / 'preserved.mp4'
    subprocess.run([
        ffmpeg, '-y', '-ss', '0', '-i', str(source), '-t', '1',
        '-map', '0:v?', '-map', '0:a?', '-c', 'copy', str(preserved),
    ], check=True, capture_output=True, timeout=30)
    assert _audio_stream_count(preserved) == 2
