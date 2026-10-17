from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4
import json
import subprocess
import threading

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

from core.playback_mix_model import (
    LIMITER_CEILING,
    PlaybackSource,
    ProbedAudioStream,
    SourceMixState,
    build_playback_sources,
    ffmpeg_mix_filter,
    mix_interleaved_float,
    mono_to_stereo,
    resample_linear,
    source_display_name,
    source_icon_key,
)
from core.ffmpeg_playback import (
    _DecodeWorker,
    _AudioPullDevice,
    BoundedPCMQueue,
    FFmpegPlaybackController,
    InProcessFFmpegMixer,
    PlaybackError,
    probe_audio_streams,
)


def _stream(index: int, title: str | None = None) -> ProbedAudioStream:
    return ProbedAudioStream(container_index=index, audio_index=index - 1, title=title)


def _manifest(*entries: tuple[int, str, str]) -> dict:
    return {'sources': [
        {'source_uuid': str(uuid4()), 'stream_index': index,
         'source_type': source_type, 'display_name': name,
         'icon_reference': None}
        for index, source_type, name in entries
    ]}


def test_current_windows_default_mix_and_microphone_are_both_editable():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'), (2, 'microphone', 'Microphone')),
        (_stream(1), _stream(2)),
    )
    assert [(source.display_name, source.source_type) for source in sources] == [
        ('Default Mix', 'system'), ('Microphone', 'microphone')]
    assert source_display_name(sources[0]) == 'System Audio'


def test_combined_capture_hides_all_source_sliders_even_with_a_legacy_manifest():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'), (2, 'microphone', 'Microphone')),
        (_stream(1), ProbedAudioStream(2, 1, 'Microphone', 'combined')),
    )
    assert len(sources) == 2
    assert not any(source.editable for source in sources)


def test_separated_capture_keeps_system_and_microphone_sliders_editable():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'), (2, 'microphone', 'Microphone')),
        (_stream(1), ProbedAudioStream(2, 1, 'Microphone', 'separated')),
    )
    assert all(source.editable for source in sources)


def test_rich_app_stems_exclude_only_compatibility_default_mix():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'), (2, 'application', 'VALORANT'),
                  (3, 'application', 'Discord'), (4, 'microphone', 'Microphone')),
        tuple(_stream(index) for index in range(1, 5)),
    )
    assert [source.display_name for source in sources] == [
        'VALORANT', 'Discord', 'Default Mix', 'Microphone']
    assert [source.display_name for source in sources if source.editable] == [
        'VALORANT', 'Discord', 'Microphone']
    assert [source.mix_role for source in sources] == [
        'delta', 'delta', 'base', 'direct']


def test_dynamic_source_order_is_semantic_and_never_uses_display_name_as_identity():
    sources = build_playback_sources(
        _manifest((1, 'microphone', 'Microphone'), (2, 'system', 'Default Mix'),
                  (3, 'application', 'Discord'), (4, 'application', 'Discord')),
        tuple(_stream(index) for index in range(1, 5)),
    )
    # Two duplicate human labels are distinct rows; stable stream order is the
    # tiebreaker. The compatibility Default Mix is retained as the hidden base
    # needed for reversible subtraction; microphone follows it.
    assert [(source.display_name, source.source_type, source.container_index)
            for source in sources] == [
        ('Discord', 'application', 3), ('Discord', 'application', 4),
        ('Default Mix', 'system', 2),
        ('Microphone', 'microphone', 1),
    ]
    assert sources[0].source_id != sources[1].source_id


def test_manifest_keeps_unavailable_source_visible_but_not_mixable():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'), (2, 'microphone', 'Microphone')),
        (_stream(1),),
    )
    assert sources[1].available is False
    filters, label = ffmpeg_mix_filter(sources, {}, 100)
    assert label == '[aout]'
    assert '0:a:0' in filters[0]
    assert 'Microphone' not in ''.join(filters)


def test_imported_tracks_use_titles_then_generic_names():
    sources = build_playback_sources(None, (
        ProbedAudioStream(container_index=3, audio_index=0, title='Spanish'),
        ProbedAudioStream(container_index=5, audio_index=1),
    ))
    assert [source.display_name for source in sources] == ['Spanish', 'Track 2']
    assert [source.audio_index for source in sources] == [0, 1]


def test_source_icon_keys_use_manifest_references_then_safe_fallbacks():
    assert source_icon_key(PlaybackSource(
        'mic', 'Microphone', 'microphone', 1, 0, 'microphone')) == 'microphone'
    assert source_icon_key(PlaybackSource(
        'system', 'Default Mix', 'system', 2, 1, 'system-audio')) == 'system'
    assert source_icon_key(PlaybackSource(
        'app', 'Discord', 'application', 3, 2, 'windows-app-icon')) == 'application'
    assert source_icon_key(PlaybackSource(
        'import', 'Track 1', 'track', 4, 3)) == 'track'


def test_legacy_kovaak_manifest_identity_gets_friendly_name():
    source = PlaybackSource(
        'game', 'Fpsaimtrainer-win64-shipping', 'application', 3, 2,
        persistent_identity='fpsaimtrainer-win64-shipping')
    assert source_display_name(source) == "KovaaK's"


def test_one_two_four_and_eight_tracks_follow_equal_power_headroom():
    for tracks in (1, 2, 4, 8):
        output = mix_interleaved_float([[0.5, 0.5]] * tracks, [1.0] * tracks)
        assert output == [min(LIMITER_CEILING, 0.5 * tracks / tracks ** 0.5)] * 2


def test_gain_mute_master_and_limiter_are_non_destructive_mix_state():
    sources = (PlaybackSource('system', 'System Audio', 'system', 1, 0),
               PlaybackSource('mic', 'Microphone', 'microphone', 2, 1))
    states = {'mic': SourceMixState(gain_percent=100, muted=True)}
    filters, label = ffmpeg_mix_filter(sources, states, 50)
    assert label == '[aout]'
    assert 'volume=0.000000' in filters[1]
    assert 'volume=0.500000' in filters[-1]
    assert 'alimiter=limit=0.98' in filters[-1]
    assert mix_interleaved_float([[2.0], [2.0]], [1.0, 1.0]) == [LIMITER_CEILING]


def test_application_gain_is_a_delta_against_hidden_default_mix():
    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'),
                  (2, 'application', 'Google Chrome'),
                  (3, 'application', 'Discord')),
        tuple(_stream(index) for index in range(1, 4)),
    )
    chrome, discord, base = sources

    filters, _ = ffmpeg_mix_filter(sources, {}, 100)
    assert filters[0] == (
        '[0:a:1]aresample=first_pts=0:async=0,volume=0.000000[src0]')
    assert filters[1] == (
        '[0:a:2]aresample=first_pts=0:async=0,volume=0.000000[src1]')
    assert filters[2] == (
        '[0:a:0]aresample=first_pts=0:async=0,volume=1.000000[src2]')

    states = {chrome.source_id: SourceMixState(muted=True)}
    filters, _ = ffmpeg_mix_filter(sources, states, 100)
    assert filters[0] == (
        '[0:a:1]aresample=first_pts=0:async=0,volume=-1.000000[src0]')
    assert filters[1] == (
        '[0:a:2]aresample=first_pts=0:async=0,volume=0.000000[src1]')
    assert base.editable is False
    assert discord.editable is True


def test_live_controller_uses_same_application_delta_gains_as_export(qtbot):
    from PySide6.QtCore import QObject

    sources = build_playback_sources(
        _manifest((1, 'system', 'Default Mix'),
                  (2, 'application', 'Helium'),
                  (3, 'application', "KovaaK's"),
                  (4, 'microphone', 'Microphone')),
        tuple(_stream(index) for index in range(1, 5)),
    )
    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller.sources = sources
    controller._states = {source.source_id: SourceMixState() for source in sources}
    controller._master_gain = 1.0
    controller._mix_lock = threading.Lock()

    gains, master = controller._mix_values()
    assert gains == [0.0, 0.0, 1.0, 1.0]
    assert master == 1.0

    controller.set_source_state(sources[0].source_id, muted=True)
    gains, _ = controller._mix_values()
    assert gains == [-1.0, 0.0, 1.0, 1.0]


def test_mono_and_44100_reference_contracts():
    assert mono_to_stereo([0.25, -0.5]) == [0.25, 0.25, -0.5, -0.5]
    resampled = resample_linear([0.0, 1.0], 44_100, 48_000)
    assert len(resampled) == 2
    assert resampled[0] == 0.0


def test_bounded_pcm_queue_reports_underruns_without_unbounded_growth():
    queue = BoundedPCMQueue(bytes_per_frame=8)
    assert queue.read(8) == b''
    assert queue.underruns == 1
    assert queue.append(b'12345678') is True
    assert queue.frames == 1
    assert queue.read(16) == b'12345678'
    assert queue.frames == 0
    assert queue.append(b'broken') is False


def test_invalidated_pull_device_cannot_consume_pcm_for_a_new_sink(qtbot):
    """A late Windows endpoint callback must not create duplicate playback."""
    from PySide6.QtCore import QObject

    queue = BoundedPCMQueue(bytes_per_frame=8)
    parent = QObject()
    device = _AudioPullDevice(queue, parent)
    queue.append(b'new-pcm!')
    device.invalidate()

    assert device.readData(8) == b''
    assert queue.read(8) == b'new-pcm!'


def test_audio_output_fatal_error_is_reported_without_crashing_qt(qtbot):
    """The controller must surface a disappeared/broken device cleanly."""
    from PySide6.QtCore import QObject
    from PySide6.QtMultimedia import QAudio

    class FailedSink:
        def error(self):
            return QAudio.Error.FatalError

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._sink = FailedSink()
    received = []
    controller.audio_failed.connect(received.append)
    controller._on_sink_state(QAudio.State.StoppedState)
    assert received == ['Audio output device stopped unexpectedly.']


def test_video_sync_accounts_for_qaudio_device_buffer(qtbot):
    """Queued device audio is pending, not part of the audible position yet."""
    from PySide6.QtCore import QObject

    class Worker:
        estimated_position_ms = 1_250

    class OutputFormat:
        @staticmethod
        def bytesPerFrame():
            return 8

    class Sink:
        suspended = 0

        @staticmethod
        def bufferSize():
            return 96_000  # 12,000 float-stereo frames = 250 ms

        @staticmethod
        def bytesFree():
            return 0

        def suspend(self):
            self.suspended += 1

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._worker = Worker()
    controller._output_format = OutputFormat()
    controller._sink = Sink()
    controller._sync_guard_until = 0.0
    sought = []
    controller.seek = sought.append

    assert controller._estimated_output_position_ms() == 1_000
    controller.sync_to_video_position(1_000)
    assert sought == []
    controller.sync_to_video_position(700)
    assert sought == []
    assert controller._sink.suspended == 1


def test_video_sync_waits_for_seek_pipeline_to_recover(qtbot):
    from PySide6.QtCore import QObject

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._sync_guard_until = float('inf')
    controller._estimated_output_position_ms = lambda: 0
    sought = []
    controller.seek = sought.append

    controller.sync_to_video_position(2_000)

    assert sought == []


def test_video_sync_holds_early_audio_instead_of_replaying_it(qtbot):
    """Automatic clock correction must never seek already-heard PCM backwards."""
    from PySide6.QtCore import QObject
    from PySide6.QtMultimedia import QAudio

    class Sink:
        suspended = 0

        @staticmethod
        def state():
            return QAudio.State.ActiveState

        def suspend(self):
            self.suspended += 1

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._sync_guard_until = 0.0
    controller._sync_holding = False
    controller._sink = Sink()
    controller._estimated_output_position_ms = lambda: 1_250
    sought = []
    controller.seek = sought.append

    controller.sync_to_video_position(1_000)

    assert sought == []
    assert controller._sink.suspended == 1
    assert controller._sync_holding is True


def test_video_sync_resumes_held_audio_after_video_catches_up(qtbot):
    from PySide6.QtCore import QObject
    from PySide6.QtMultimedia import QAudio

    class Sink:
        resumed = 0

        @staticmethod
        def state():
            return QAudio.State.SuspendedState

        def resume(self):
            self.resumed += 1

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._sync_guard_until = 0.0
    controller._sync_holding = True
    controller._sink = Sink()
    controller._estimated_output_position_ms = lambda: 1_250
    sought = []
    controller.seek = sought.append

    controller.sync_to_video_position(1_220)

    assert sought == []
    assert controller._sink.resumed == 1
    assert controller._sync_holding is False


def test_video_sync_only_seeks_forward_when_audio_is_late(qtbot):
    from PySide6.QtCore import QObject

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._sync_guard_until = 0.0
    controller._sync_holding = False
    controller._sink = None
    controller._estimated_output_position_ms = lambda: 700
    sought = []
    controller.seek = sought.append

    controller.sync_to_video_position(1_000)

    assert sought == [1_000]


def test_seek_invalidates_decoder_before_releasing_qaudio_buffer(qtbot):
    from PySide6.QtCore import QObject

    events = []

    class Queue:
        def clear(self):
            events.append('python-clear')

    class Sink:
        def stop(self):
            events.append('sink-stop')

        def deleteLater(self):
            events.append('sink-delete')

    class Worker:
        def seek(self, position_ms):
            # The production worker clears its queue while holding the same
            # command lock used to reject an in-flight obsolete decode.
            controller._queue.clear()
            events.append(('decoder-seek', position_ms))

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._queue = Queue()
    controller._sink = Sink()
    controller._worker = Worker()

    controller.seek(1_750)

    assert controller._sink is None
    assert events == [
        'python-clear', ('decoder-seek', 1_750),
        'sink-stop', 'sink-delete',
    ]


def test_in_flight_pre_seek_decode_cannot_reenter_cleared_queue(monkeypatch):
    import core.ffmpeg_playback as playback

    old_pull_started = threading.Event()
    release_old_pull = threading.Event()
    fresh_pull_finished = threading.Event()
    output_woken = threading.Event()

    class Mixer:
        pulls = 0

        def __init__(self, *_args):
            pass

        def seek(self, _position_ms):
            pass

        def pull(self, frames, _gains, _master):
            self.pulls += 1
            if self.pulls == 1:
                old_pull_started.set()
                assert release_old_pull.wait(2)
                return b'o' * (frames * 8)
            fresh_pull_finished.set()
            return b'n' * (frames * 8)

        def failed_source_indexes(self):
            return ()

        def close(self):
            pass

    monkeypatch.setattr(playback, 'InProcessFFmpegMixer', Mixer)
    queue = BoundedPCMQueue(bytes_per_frame=8)
    worker = _DecodeWorker(
        'clip.mp4', (PlaybackSource('track', 'Track', 'track', 1, 0),),
        queue, lambda pcm: pcm, lambda: ([1.0], 1.0),
        lambda _error: None, lambda _error: None, lambda _source: None,
        lambda: None, output_woken.set)
    worker.start()
    try:
        worker.play(0)
        assert old_pull_started.wait(2)
        worker.seek(1_000)
        release_old_pull.set()
        assert fresh_pull_finished.wait(2)
        assert output_woken.wait(2)
        # Only the new-generation marker may be audible after the seek.
        for _ in range(100):
            if queue.frames:
                break
            threading.Event().wait(0.005)
        assert queue.read(8) == b'n' * 8
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_stopped_sink_is_recreated_after_fresh_pcm_arrives(qtbot, monkeypatch):
    from PySide6.QtCore import QObject
    from PySide6.QtMultimedia import QAudio
    import core.ffmpeg_playback as playback

    class Queue:
        frames = 1_024

    class Sink:
        def __init__(self):
            self.starts = 0
            self.stops = 0
            self.deleted = 0

        class StateChanged:
            @staticmethod
            def connect(_callback):
                pass

        stateChanged = StateChanged()

        @staticmethod
        def state():
            return QAudio.State.StoppedState

        @staticmethod
        def error():
            return QAudio.Error.NoError

        def start(self, _device):
            self.starts += 1

        def stop(self):
            self.stops += 1

        def deleteLater(self):
            self.deleted += 1

        @staticmethod
        def setBufferSize(_size):
            pass

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller._queue = Queue()
    old_sink = Sink()
    controller._sink = old_sink
    controller._device = object()
    controller._output_device = object()
    controller._output_format = type('Format', (), {'bytesPerFrame': lambda self: 8})()
    created = []

    def make_sink(*_args):
        sink = Sink()
        created.append(sink)
        return sink

    monkeypatch.setattr(playback, 'QAudioSink', make_sink)

    controller._keep_sink_running()

    assert old_sink.stops == 1
    assert old_sink.deleted == 1
    assert len(created) == 1
    assert controller._sink is created[0]
    assert controller._sink.starts == 1


def test_master_zero_to_audible_discards_buffered_silence_and_reseeks(qtbot):
    from PySide6.QtCore import QObject

    events = []

    class Queue:
        frames = 0

        def clear(self):
            events.append('python-clear')

    class Worker:
        def seek(self, position_ms):
            controller._queue.clear()
            events.append(('decoder-seek', position_ms))

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller.sources = (
        PlaybackSource('track', 'Track', 'track', 1, 0),)
    controller._states = {'track': SourceMixState()}
    controller._master_gain = 0.0
    controller._mix_lock = threading.Lock()
    controller._worker_started = True
    controller._worker = Worker()
    controller._queue = Queue()
    controller._sink = None
    controller._sync_guard_until = 0.0
    controller._estimated_output_position_ms = lambda: 1_250
    controller._discard_output_buffer = lambda: events.append('sink-reset')

    controller.set_master_percent(75)
    controller.refresh_mix(1_250)

    assert controller._master_gain == 0.75
    assert events == ['python-clear', ('decoder-seek', 1_250), 'sink-reset']


def test_last_source_unmute_uses_same_immediate_audio_restart(qtbot):
    from PySide6.QtCore import QObject

    events = []

    class Queue:
        frames = 0

        def clear(self):
            events.append('python-clear')

    class Worker:
        def seek(self, position_ms):
            controller._queue.clear()
            events.append(('decoder-seek', position_ms))

    controller = FFmpegPlaybackController.__new__(FFmpegPlaybackController)
    QObject.__init__(controller)
    controller.sources = (
        PlaybackSource('track', 'Track', 'track', 1, 0),)
    controller._states = {'track': SourceMixState(muted=True)}
    controller._master_gain = 1.0
    controller._mix_lock = threading.Lock()
    controller._worker_started = True
    controller._worker = Worker()
    controller._queue = Queue()
    controller._sink = None
    controller._sync_guard_until = 0.0
    controller._estimated_output_position_ms = lambda: 900
    controller._discard_output_buffer = lambda: events.append('sink-reset')

    controller.set_source_state('track', muted=False)
    controller.refresh_mix(900)

    assert controller.source_state('track').muted is False
    assert events == ['python-clear', ('decoder-seek', 900), 'sink-reset']


def test_probe_uses_actual_container_indexes_and_titles(monkeypatch):
    class Result:
        returncode = 0
        stdout = json.dumps({'format': {'tags': {'comment': 'fthr-audio-mode=COMBINED'}},
                             'streams': [
            {'index': 0, 'codec_type': 'video'},
            {'index': 2, 'codec_type': 'audio', 'tags': {'title': 'System'}},
            {'index': 5, 'codec_type': 'audio', 'tags': {'handler_name': 'Mic'}},
        ]})

    monkeypatch.setattr('core.ffmpeg_playback.get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr('core.ffmpeg_playback.subprocess.run', lambda *_args, **_kwargs: Result())
    streams = probe_audio_streams('clip.mp4')
    assert [(stream.container_index, stream.audio_index, stream.title) for stream in streams] == [
        (2, 0, 'System'), (5, 1, 'Mic')]
    assert {stream.audio_mode for stream in streams} == {'combined'}


def test_in_process_bridge_decodes_mixes_mutes_and_seeks_real_media(tmp_path):
    """Integration coverage for the production bridge; skips before native build."""
    try:
        from core.ffmpeg_tools import get_ffmpeg_exe
        ffmpeg = get_ffmpeg_exe()
    except Exception as error:
        import pytest
        pytest.skip(f'no reviewed FFmpeg runtime: {error}')
    media = tmp_path / 'two-stem.mp4'
    command = [
        ffmpeg, '-y', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
        '-t', '2', '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0',
        '-c:v', 'mpeg4', '-c:a', 'aac', str(media),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=20)
    sources = (
        PlaybackSource('system', 'System Audio', 'system', 1, 0),
        PlaybackSource('mic', 'Microphone', 'microphone', 2, 1),
    )
    try:
        mixer = InProcessFFmpegMixer(str(media), sources)
    except PlaybackError as error:
        import pytest
        pytest.skip(str(error))
    try:
        def _collect(gains: tuple[float, float]) -> bytes:
            mixer.seek(250)
            blocks = [mixer.pull(1024, gains, 1.0) for _ in range(16)]
            assert all(blocks)
            return b''.join(blocks)

        mixed = _collect((1.0, 1.0))
        system_only = _collect((1.0, 0.0))
        mic_only = _collect((0.0, 1.0))
        inverted_mic = _collect((0.0, -1.0))
        mixer.seek(1_000)
        sought = mixer.pull(1024, (1.0, 1.0), 1.0)
        # The production seek sequence flushes every decoder/resampler.  Make
        # several direction changes before pulling again so stale PCM cannot
        # leak from an older source position.
        for position_ms in (0, 1_400, 500, 1_700, 250):
            mixer.seek(position_ms)
        rapidly_sought = mixer.pull(1024, (1.0, 1.0), 1.0)
    finally:
        mixer.close()
    assert mixed and system_only and mic_only and inverted_mic and sought and rapidly_sought
    assert len(sought) == 1024 * 2 * 4
    # A seek must land on an audio packet near the requested time. Seeking on
    # the global/video timeline used to return correctly-sized blocks of zeros
    # while the AAC decoders caught up from a distant keyframe.
    assert float(np.sqrt(np.mean(np.frombuffer(sought, dtype='<f4') ** 2))) > 0.001
    assert float(np.sqrt(np.mean(np.frombuffer(rapidly_sought, dtype='<f4') ** 2))) > 0.001
    positive = np.frombuffer(mic_only, dtype='<f4')
    negative = np.frombuffer(inverted_mic, dtype='<f4')
    # AAC seek priming can differ by a few quantization bits between decoder
    # flushes, so verify phase inversion by correlation rather than byte-level
    # equality.
    assert float(np.vdot(positive, negative)) < 0
    assert np.isclose(np.linalg.norm(positive), np.linalg.norm(negative), rtol=0.01)

    def _tone_energy(pcm: bytes, frequency: float) -> float:
        mono = np.frombuffer(pcm, dtype='<f4').reshape(-1, 2).mean(axis=1)
        phase = np.exp(-2j * np.pi * frequency * np.arange(len(mono)) / 48_000)
        return float(abs(np.vdot(phase, mono)))

    assert _tone_energy(mixed, 440) > 10 and _tone_energy(mixed, 880) > 10
    assert _tone_energy(system_only, 440) > _tone_energy(system_only, 880) * 5
    assert _tone_energy(mic_only, 880) > _tone_energy(mic_only, 440) * 5


def test_in_process_bridge_preserves_pitch_at_changed_preview_speed(tmp_path):
    """The live controlled path must not rely on MediaFoundation for pitch."""
    try:
        from core.ffmpeg_tools import get_ffmpeg_exe
        ffmpeg = get_ffmpeg_exe()
    except Exception as error:
        import pytest
        pytest.skip(f'no reviewed FFmpeg runtime: {error}')

    media = tmp_path / 'pitch.mp4'
    subprocess.run([
        ffmpeg, '-y', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-t', '2', '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'mpeg4', '-c:a', 'aac', str(media),
    ], check=True, capture_output=True, timeout=20)
    source = PlaybackSource('track', 'Track', 'track', 1, 0)
    try:
        mixer = InProcessFFmpegMixer(str(media), (source,))
    except PlaybackError as error:
        import pytest
        pytest.skip(str(error))
    try:
        assert mixer.set_playback_rate(2.0, preserve_pitch=True) is True
        mixer.seek(200)
        blocks = [mixer.pull(1024, (1.0,), 1.0) for _ in range(12)]
    finally:
        mixer.close()
    pcm = b''.join(block for block in blocks if block)
    assert pcm
    mono = np.frombuffer(pcm, dtype='<f4').reshape(-1, 2).mean(axis=1)

    def _tone_energy(frequency: float) -> float:
        phase = np.exp(-2j * np.pi * frequency * np.arange(len(mono)) / 48_000)
        return float(abs(np.vdot(phase, mono)))

    assert _tone_energy(440) > _tone_energy(880) * 5
