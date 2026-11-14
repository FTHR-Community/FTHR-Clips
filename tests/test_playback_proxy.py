import hashlib
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtMultimedia import QMediaPlayer

from core import playback_proxy
from core.media_identity import file_identity


def _proxy_probe(command, *, duration='60', packets=None, video=None):
    stream = {'codec_type': 'video', 'index': 0, 'width': 640, 'height': 360,
              'color_transfer': 'bt709', **(video or {})}
    if packets is None:
        packets = [{'pts_time': '0', 'flags': 'K_'}, {'pts_time': '7', 'flags': '__'}]
    document = {'format': {'duration': duration}, 'streams': [stream]}
    if '-read_intervals' in command:
        document['packets'] = [{'stream_index': 0, **packet} for packet in packets]
    return 0, json.dumps(document).encode(), b''


def test_keyframe_gap_requires_timing_evidence():
    assert playback_proxy.needs_seek_proxy([
        {'pts_time': '0', 'flags': 'K_'}, {'pts_time': '4', 'flags': '__'}])
    assert not playback_proxy.needs_seek_proxy([
        {'pts_time': str(i), 'flags': 'K_'} for i in range(8)])
    assert not playback_proxy.needs_seek_proxy([
        {'pts_time': 'nan', 'flags': 'K_'}, {'pts_time': '8', 'flags': '__'}])
    assert not playback_proxy.needs_seek_proxy([
        {'pts_time': '0', 'flags': None}, [], {'pts_time': '8', 'flags': '__'}])


def test_preview_switch_waits_for_pause_and_preserves_pending_seek():
    from ui.clip_viewer import ClipViewer
    sources = []
    state = [QMediaPlayer.PlayingState]
    viewer = SimpleNamespace(
        _closing=False, clip_path='original.mp4', _playback_path='original.mp4',
        _prepared_playback_path='preview.mp4', _pending_seek_ms=4500,
        player=SimpleNamespace(playbackState=lambda: state[0], position=lambda: 100),
        _seek_timer=SimpleNamespace(stop=lambda: None),
        _set_player_state=lambda *_: None,
    )
    viewer._set_media_source = lambda: sources.append(viewer._playback_path)
    ClipViewer._apply_prepared_playback_source(viewer)
    assert sources == []
    state[0] = QMediaPlayer.PausedState
    ClipViewer._apply_prepared_playback_source(viewer)
    assert sources == ['preview.mp4']
    assert viewer.clip_path == 'original.mp4'
    assert viewer._renderer_resume_position_ms == 4500
    assert viewer._pending_seek_ms is None


def test_proxy_is_published_atomically_and_invalidated_by_source_change(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'previews')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(playback_proxy, 'software_video_args', lambda *_: ['-g', '30'])
    calls = []

    def run(command, cancel, **kwargs):
        calls.append(command)
        if command[0] == 'ffprobe':
            return _proxy_probe(command)
        assert playback_proxy.cached_playback_path(str(source)) == str(source)
        Path(command[-1]).write_bytes(b'complete preview')
        return 0, b'', b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    result = playback_proxy.prepare_playback_path(str(source), threading.Event())
    assert result != str(source)
    assert Path(result).read_bytes() == b'complete preview'
    assert source.read_bytes() == b'original'
    assert playback_proxy.prepare_playback_path(str(source), threading.Event()) == result
    assert len(calls) == 3
    source.write_bytes(b'new source generation')
    assert playback_proxy.cached_playback_path(str(source)) == str(source)


def test_cancelled_proxy_never_replaces_source_or_leaves_partial(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'previews')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(playback_proxy, 'software_video_args', lambda *_: [])
    cancel = threading.Event()

    def run(command, *args, **kwargs):
        if command[0] == 'ffprobe':
            return _proxy_probe(command)
        Path(command[-1]).write_bytes(b'incomplete')
        cancel.set()
        return None

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    assert playback_proxy.prepare_playback_path(str(source), cancel) == str(source)
    assert source.read_bytes() == b'original'
    assert list((tmp_path / 'previews').iterdir()) == []


def _configure_fake_gop_proxy(source, monkeypatch, *, platform='win32'):
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', source.parent / 'previews')
    monkeypatch.setattr(playback_proxy.sys, 'platform', platform)
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(playback_proxy, 'software_video_args', lambda *_: [])

    def run(command, cancel, **kwargs):
        if command[0] == 'ffprobe':
            return _proxy_probe(command)
        Path(command[-1]).write_bytes(b'full proxy')
        return 0, b'', b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)


def test_windows_gop_proxy_normalizes_before_publish_and_ignores_legacy_cache(
        tmp_path, monkeypatch):
    from core import mp4_timing

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    _configure_fake_gop_proxy(source, monkeypatch)
    order = []

    def normalize(path, cancelled):
        order.append('normalize')
        assert Path(path).is_file()
        Path(path).write_bytes(b'normalized full proxy')
        return True

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_in_place', normalize)
    original_replace = playback_proxy.os.replace

    def replace(src, dst):
        order.append('replace')
        assert order == ['normalize', 'replace']
        return original_replace(src, dst)

    monkeypatch.setattr(playback_proxy.os, 'replace', replace)
    result = playback_proxy.prepare_playback_path(str(source), threading.Event())

    assert order == ['normalize', 'replace']
    assert Path(result).read_bytes() == b'normalized full proxy'
    assert result == str(playback_proxy._cache_path(str(source)))
    for version in ('gop30-v3', 'gop30-v4'):
        marker = f'{file_identity(str(source))!r}|{version}'
        assert Path(result).name != hashlib.sha256(marker.encode('utf-8')).hexdigest() + '.mp4'


def test_non_windows_gop_proxy_does_not_call_presentation_normalizer(
        tmp_path, monkeypatch):
    from core import mp4_timing

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    _configure_fake_gop_proxy(source, monkeypatch, platform='linux')
    calls = []

    def unexpected(*_args):
        calls.append(True)
        raise AssertionError('Linux full proxy must not use the MP4 normalizer')

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_in_place', unexpected)
    result = playback_proxy.prepare_playback_path(str(source), threading.Event())

    assert Path(result).read_bytes() == b'full proxy'
    assert calls == []


@pytest.mark.parametrize('mode', ['cancel', 'error', 'unsupported'])
def test_windows_gop_normalization_gate_handles_cancel_error_and_false(
        tmp_path, monkeypatch, mode):
    from core import mp4_timing

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    _configure_fake_gop_proxy(source, monkeypatch)
    cancelled = threading.Event()

    def normalize(path, cancel):
        if mode == 'cancel':
            cancel.set()
            return True
        if mode == 'error':
            raise OSError('normalizer failed')
        return False

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_in_place', normalize)
    result = playback_proxy.prepare_playback_path(str(source), cancelled)

    if mode == 'unsupported':
        assert result == str(playback_proxy._cache_path(str(source)))
        assert Path(result).read_bytes() == b'full proxy'
    else:
        assert result == str(source)
        assert not playback_proxy._cache_path(str(source)).exists()


def test_compatibility_cache_is_atomic_identity_bound_and_preferred_after_full_proxy(
        tmp_path, monkeypatch):
    from core import mp4_timing

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    marker = f'{file_identity(str(source))!r}|presentation-v4'
    old_marker = f'{file_identity(str(source))!r}|zero-empty-edit-v2'
    expected = hashlib.sha256(marker.encode('utf-8')).hexdigest() + '.mp4'
    old_expected = hashlib.sha256(old_marker.encode('utf-8')).hexdigest() + '.mp4'
    assert playback_proxy._compatibility_cache_path(str(source)).name == expected
    assert expected != old_expected
    calls = []

    def normalize(input_path, output_path, cancelled):
        calls.append((input_path, output_path))
        if cancelled.is_set():
            return False
        Path(output_path).write_bytes(b'zero-empty normalized')
        return True

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', normalize)
    result = playback_proxy.prepare_compatibility_path(str(source), threading.Event())
    assert result != str(source)
    assert Path(result).read_bytes() == b'zero-empty normalized'
    assert source.read_bytes() == b'original'
    assert playback_proxy.cached_playback_path(str(source)) == result
    assert len(calls) == 1

    full = playback_proxy._cache_path(str(source))
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b'full proxy')
    assert playback_proxy.cached_playback_path(str(source)) == str(full)

    source.write_bytes(b'new generation')
    assert playback_proxy.cached_playback_path(str(source)) == str(source)


def test_compatibility_preparation_is_windows_only_and_cancellation_leaves_no_partial(
        tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    from core import mp4_timing
    calls = []

    def unexpected(*_args):
        calls.append(True)
        raise AssertionError('Linux playback must keep the native path')

    monkeypatch.setattr(playback_proxy.sys, 'platform', 'linux')
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', unexpected)
    assert playback_proxy.prepare_compatibility_path(
        str(source), threading.Event()) == str(source)
    assert calls == []

    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    cancelled = threading.Event()
    cancelled.set()
    assert playback_proxy.prepare_compatibility_path(str(source), cancelled) == str(source)
    assert not (tmp_path / 'playback').exists()


def _write_compatibility_cache(source, monkeypatch, tmp_path):
    from core import mp4_timing

    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')

    def normalize(input_path, output_path, cancelled):
        assert input_path == str(source)
        Path(output_path).write_bytes(b'normalized')
        return not cancelled.is_set()

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', normalize)
    return playback_proxy.prepare_compatibility_path(
        str(source), threading.Event())


def test_gop_proxy_uses_compatibility_cache_and_keeps_it_as_fallback(
        tmp_path, monkeypatch):
    from core import mp4_timing

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    compatibility = _write_compatibility_cache(source, monkeypatch, tmp_path)
    monkeypatch.setattr(
        mp4_timing, 'normalize_mp4_presentation_in_place',
        lambda *_args: False)
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(playback_proxy, 'software_video_args', lambda *_: [])
    calls = []

    def run(command, cancel, **kwargs):
        calls.append(command)
        if command[0] == 'ffprobe' and '-show_entries' in command:
            return _proxy_probe(command)
        assert command[0] == 'ffmpeg'
        assert command[command.index('-i') + 1] == compatibility
        Path(command[-1]).write_bytes(b'full proxy')
        return 0, b'', b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    result = playback_proxy.prepare_playback_path(str(source), threading.Event())

    assert result == str(playback_proxy._cache_path(str(source)))
    assert Path(result).read_bytes() == b'full proxy'
    assert source.read_bytes() == b'original'
    assert len(calls) == 3


def test_gop_proxy_keeps_compatibility_cache_when_probe_says_no_proxy(
        tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    compatibility = _write_compatibility_cache(source, monkeypatch, tmp_path)
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    calls = []

    def run(command, cancel, **kwargs):
        calls.append(command)
        return _proxy_probe(command, packets=[
            {'pts_time': '0', 'flags': 'K_'}, {'pts_time': '1', 'flags': 'K_'}])

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    assert playback_proxy.prepare_playback_path(
        str(source), threading.Event()) == compatibility
    assert len(calls) == 1


def test_full_proxy_cache_still_short_circuits_gop_probe(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    full = playback_proxy._cache_path(str(source))
    full.parent.mkdir(parents=True)
    full.write_bytes(b'full proxy')
    monkeypatch.setattr(
        playback_proxy, 'run_media_process',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('full proxy cache must skip probing')))

    assert playback_proxy.prepare_playback_path(
        str(source), threading.Event()) == str(full)


@pytest.mark.parametrize(('video', 'duration', 'mode', 'encoded'), [
    ({'width': 1920, 'height': 1080, 'avg_frame_rate': '60000/1001'}, '60', 'auto', True),
    ({'width': 1080, 'height': 1920, 'avg_frame_rate': '30/1', 'codec_name': 'av1'},
     '60', 'auto', True),
    ({'width': 1920, 'height': 1080, 'avg_frame_rate': '0/0'}, '60', 'auto', False),
    ({'width': 'invalid', 'height': 1440}, '60', '540p', False),
    ({'width': 2560, 'height': 1440}, 'NaN', 'auto', False),
    ({'width': 2560, 'height': 1440}, '600', 'auto', True),
    ({'width': 2560, 'height': 1440}, '901', '720p', False),
    ({'width': 2560, 'height': 1440, 'color_transfer': 'arib-std-b67'}, '60', '720p', False),
])
def test_proxy_eligibility_uses_workload_and_validated_metadata(
        tmp_path, monkeypatch, video, duration, mode, encoded):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'previews')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(playback_proxy, 'software_video_args', lambda *_: [])
    calls = []

    def run(command, cancel, **kwargs):
        calls.append(command)
        if command[0] == 'ffmpeg':
            assert kwargs['background'] == 'proxy'
            assert kwargs['timeout_seconds'] == 120
            assert '-fs' in command
            return None
        return _proxy_probe(command, duration=duration, video=video, packets=[
            {'pts_time': '0', 'flags': 'K_'}, {'pts_time': '1', 'flags': 'K_'}])

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    assert playback_proxy.prepare_playback_path(str(source), threading.Event(), mode) == str(source)
    assert any(command[0] == 'ffmpeg' for command in calls) is encoded
    assert not list((tmp_path / 'previews').glob('*.mp4'))


def test_proxy_modes_do_not_reuse_one_anothers_cache(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    compatibility = _write_compatibility_cache(source, monkeypatch, tmp_path)
    monkeypatch.setattr(
        playback_proxy, 'run_media_process',
        lambda *_args, **_kwargs: pytest.fail('cache lookup must not probe'))
    paths = [playback_proxy._cache_path(str(source), mode) for mode in ('auto', '720p', '540p')]
    assert len(set(paths)) == 3
    for mode, path in zip(('auto', '720p', '540p'), paths, strict=True):
        assert playback_proxy.cached_playback_path(str(source), mode) == compatibility
        path.write_bytes(mode.encode())
        assert playback_proxy.prepare_playback_path(str(source), threading.Event(), mode) == str(path)
    assert playback_proxy.prepare_playback_path(
        str(source), threading.Event(), 'original') == compatibility
    playback_proxy.discard_playback_cache(str(source), str(paths[-1]))
    assert not paths[-1].exists()
    assert all(path.exists() for path in paths[:2])


@pytest.mark.parametrize(('size', 'mode', 'dimensions'), [
    ('2560x1440', 'auto', (1280, 720)),
    ('1200x1600', '540p', (540, 720)),
    ('640x360', '720p', (640, 360)),
])
def test_real_proxy_preserves_aspect_timeline_and_original(
        tmp_path, monkeypatch, size, mode, dimensions):
    ffmpeg = playback_proxy.get_ffmpeg_exe()
    ffprobe = playback_proxy.get_ffprobe_exe()
    source = tmp_path / 'clip.mp4'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=s={size}:r=12:d=2.25',
         '-f', 'lavfi', '-i', 'sine=f=440:d=2.25', '-itsoffset', '0.35',
         '-f', 'lavfi', '-i', 'sine=f=880:d=1.5', '-map', '0:v', '-map', '1:a', '-map', '2:a',
         '-c:v', 'mpeg4', '-threads:v', '2', '-g', '12', '-c:a:0', 'aac',
         '-c:a:1', 'pcm_f32le', str(source)],
        check=True, capture_output=True, timeout=20)
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    original = source.read_bytes()

    preview = playback_proxy.prepare_playback_path(str(source), threading.Event(), mode)

    def probe(path, *args):
        return json.loads(subprocess.check_output(
            [ffprobe, '-v', 'error', *args, '-of', 'json', path], timeout=10))

    assert preview != str(source)
    assert preview == playback_proxy.cached_playback_path(str(source), mode)
    assert playback_proxy.cached_playback_path(str(source), 'original') == str(source)
    assert source.read_bytes() == original
    before = probe(str(source), '-show_streams', '-show_format')
    after = probe(preview, '-show_streams', '-show_format')
    video, *audio = after['streams']
    assert (video['width'], video['height']) == dimensions
    assert video['nb_frames'] == before['streams'][0]['nb_frames']
    assert float(video['duration']) == pytest.approx(2.25, abs=0.002)
    assert float(after['format']['duration']) == pytest.approx(2.25, abs=0.05)
    assert [track['codec_name'] for track in audio] == ['aac', 'aac']
    for source_audio, preview_audio in zip(before['streams'][1:], audio, strict=True):
        assert float(preview_audio['start_time']) == pytest.approx(
            float(source_audio['start_time']), abs=0.03)
    packets = probe(preview, '-select_streams', 'v:0', '-show_entries', 'packet=pts_time,flags')
    keyframes = [float(packet['pts_time']) for packet in packets['packets'] if 'K' in packet['flags']]
    assert keyframes == pytest.approx([0, 1, 2])
    aac_packet_args = ('-select_streams', 'a:0', '-show_packets', '-show_data_hash', 'sha256',
                       '-show_entries', 'packet=pts_time,duration_time,data_hash')
    assert probe(preview, *aac_packet_args) == probe(str(source), *aac_packet_args)


def test_compatibility_cache_rejects_source_or_output_over_size_bound(
        tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'x' * 81)
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    monkeypatch.setattr(playback_proxy, '_MAX_CACHE_BYTES', 100)
    called = []

    def unexpected(*_args):
        called.append(True)
        raise AssertionError('oversized source must be rejected before copy')

    from core import mp4_timing
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', unexpected)
    assert playback_proxy.prepare_compatibility_path(
        str(source), threading.Event()) == str(source)
    assert called == []

    source.write_bytes(b'small')

    def too_large(input_path, output_path, cancelled):
        Path(output_path).write_bytes(b'x' * 101)
        return True

    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', too_large)
    assert playback_proxy.prepare_compatibility_path(
        str(source), threading.Event()) == str(source)
    assert not list((tmp_path / 'playback' / 'compatibility').glob('*.mp4'))


@pytest.mark.parametrize('tags', [[], ['mp4a']])
def test_native_audio_does_not_need_compatibility_remux(
        tmp_path, monkeypatch, tags):
    from core import mp4_timing

    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation', lambda *_: False)

    def run(command, *_args, **_kwargs):
        assert command[0] == 'ffprobe'
        return 0, json.dumps({'streams': [
            {'codec_type': 'audio', 'codec_tag_string': tag} for tag in tags]}).encode(), b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    assert playback_proxy.prepare_compatibility_path(
        str(source), threading.Event()) == str(source)
    assert not list((tmp_path / 'playback').rglob('*.mp4'))


@pytest.mark.parametrize('mode', ['failure', 'cancel', 'invalid-output'])
def test_incomplete_pcm_remux_never_enters_cache(
        tmp_path, monkeypatch, mode):
    from core import mp4_timing

    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'original')
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    monkeypatch.setattr(playback_proxy, '_probe_playback_streams',
                        lambda *_: [{'codec_type': 'audio', 'codec_tag_string': 'fpcm'}])
    monkeypatch.setattr(playback_proxy, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_in_place', lambda *_: False)

    def run(command, cancel, **_kwargs):
        Path(command[-1]).write_bytes(b'incomplete')
        if mode == 'cancel':
            cancel.set()
        return int(mode == 'failure'), b'', b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', run)
    assert playback_proxy.prepare_compatibility_path(
        str(source), threading.Event()) == str(source)
    assert source.read_bytes() == b'original'
    assert not list((tmp_path / 'playback').rglob('*.mp4'))


@pytest.mark.parametrize(
    ('output_duration', 'output_frames', 'output_start', 'expected'),
    [
        ('14.967000', '491', '0.000000', True),
        ('14.967000', '490', '0.000000', False),
        ('14.933000', '491', '0.000000', True),
        ('14.700000', '491', '0.000000', False),
        ('15.033000', '491', '0.000000', False),
        ('14.967000', '491', '0.033333', False),
    ],
)
def test_pcm_remux_accepts_bounded_tail_padding_with_all_samples_retained(
        output_duration, output_frames, output_start, expected):
    video = {
        'codec_type': 'video', 'codec_tag_string': 'avc1',
        'duration': '15.000000', 'start_time': '0.000000',
        'nb_frames': '491', 'r_frame_rate': '30/1',
    }
    source = [video, {'codec_type': 'audio', 'codec_tag_string': 'fpcm'}]
    output = [
        dict(video, duration=output_duration, nb_frames=output_frames,
             start_time=output_start),
        {'codec_type': 'audio', 'codec_tag_string': 'mp4a'},
    ]
    assert playback_proxy._compatible_remux_matches(source, output) is expected


@pytest.mark.parametrize('fragmented', [False, True])
def test_pcm_remux_preserves_video_packet_timing_and_data(tmp_path, monkeypatch, fragmented):
    ffmpeg = playback_proxy.get_ffmpeg_exe()
    ffprobe = playback_proxy.get_ffprobe_exe()
    source = tmp_path / 'clip.mp4'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=30:d=1',
         '-f', 'lavfi', '-i', 'sine=f=440:d=1', '-f', 'lavfi', '-i', 'sine=f=880:d=1',
         '-map', '0:v', '-map', '1:a', '-map', '2:a',
         '-c:v', 'mpeg4', '-bf', '0' if fragmented else '2', '-c:a', 'pcm_f32le',
         *(['-movflags', '+frag_keyframe+empty_moov+default_base_moof'] if fragmented else []),
         str(source)],
        check=True, capture_output=True, timeout=20)
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    monkeypatch.setattr(playback_proxy.sys, 'platform', 'win32')
    if fragmented:
        # Exercise required recording compatibility above the optional cache
        # budget without needing a multi-gigabyte regression fixture.
        monkeypatch.setattr(playback_proxy, '_MAX_CACHE_BYTES', 1024)
        header = json.loads(subprocess.check_output(
            [ffprobe, '-v', 'error', '-nofind_stream_info', '-show_streams',
             '-of', 'json', str(source)], timeout=10))
        assert 'start_time' not in header['streams'][0]
    original = source.read_bytes()
    result = playback_proxy.prepare_compatibility_path(str(source), threading.Event())

    def probe(path, *args):
        return json.loads(subprocess.check_output(
            [ffprobe, '-v', 'error', *args, '-of', 'json', path], timeout=10))

    assert result != str(source)
    assert playback_proxy.cached_playback_path(str(source)) == result
    assert source.read_bytes() == original
    streams = probe(result, '-select_streams', 'a', '-show_entries', 'stream=codec_name')
    assert [stream['codec_name'] for stream in streams['streams']] == ['aac', 'aac']
    packet_args = (
        '-select_streams', 'v:0', '-show_packets', '-show_data_hash', 'sha256',
        '-show_entries', 'packet=pts_time,dts_time,duration_time,data_hash',
    )
    assert probe(str(source), *packet_args) == probe(result, *packet_args)


@pytest.mark.parametrize('reject_gpu_filter', [False, True])
def test_hdr_playback_preview_tone_maps_without_mutating_original(
        tmp_path, monkeypatch, reject_gpu_filter):
    ffmpeg = playback_proxy.get_ffmpeg_exe()
    source = tmp_path / 'hdr.mkv'
    subprocess.run(
        [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=30:d=1',
         '-f', 'lavfi', '-i', 'sine=f=440:d=1', '-map', '0:v', '-map', '1:a',
         '-vf', 'setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
         '-c:v', 'ffv1', '-pix_fmt', 'yuv420p10le', '-color_trc', 'smpte2084',
         '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc',
         '-c:a', 'pcm_f32le', str(source)],
        check=True, capture_output=True, timeout=20)
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'playback')
    original = source.read_bytes()
    cancelled = threading.Event()
    if reject_gpu_filter:
        run = playback_proxy.run_media_process

        def without_gpu(command, *args, **kwargs):
            if any('libplacebo=' in arg for arg in command):
                return 1, b'', b'Vulkan unavailable'
            return run(command, *args, **kwargs)

        monkeypatch.setattr(playback_proxy, 'run_media_process', without_gpu)

    preview = playback_proxy.prepare_compatibility_path(str(source), cancelled)

    assert playback_proxy.is_sdr_preview_path(str(source), preview)
    for mode in ('auto', 'original', '540p', '720p'):
        assert playback_proxy.cached_playback_path(str(source), mode) == preview
        assert playback_proxy.prepare_playback_path(str(source), cancelled, mode) == preview
    assert source.read_bytes() == original
    video, audio = playback_proxy._probe_playback_streams(preview, cancelled)
    assert video['color_transfer'] == 'bt709'
    assert float(video['duration']) == pytest.approx(1, abs=0.04)
    assert audio['codec_tag_string'] == 'mp4a'


def test_pcm_remux_accepts_irregular_60fps_replay_tail_without_dropping_frames():
    # Field failure: all 2733 sparse-cadence video samples survive the remux,
    # but MP4 edit rounding reduces the nominal 60s track to 59.917s.
    video = {
        'codec_type': 'video', 'codec_tag_string': 'avc1',
        'duration': '60.000000', 'start_time': '0.000000',
        'nb_frames': '2733', 'r_frame_rate': '60/1',
    }
    source = [video, *[{'codec_type': 'audio', 'codec_tag_string': 'fpcm'}] * 3]
    output = [dict(video, duration='59.917000'),
              *[{'codec_type': 'audio', 'codec_tag_string': 'mp4a'}] * 3]
    assert playback_proxy._compatible_remux_matches(source, output)
    output[0] = dict(output[0], nb_frames='2732')
    assert not playback_proxy._compatible_remux_matches(source, output)
