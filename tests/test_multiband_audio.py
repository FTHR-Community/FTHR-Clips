"""Signal-level checks for independent capture sources and local audio imports."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
import subprocess
import wave

import numpy as np
import pytest

from core.ffmpeg_playback import InProcessFFmpegMixer
from core.ffmpeg_tools import get_ffmpeg_exe
from core.playback_mix_model import (
    PlaybackSource, ProbedAudioStream, SourceMixState, audio_input_arguments,
    build_playback_sources, ffmpeg_mix_filter, mix_interleaved_float, source_gain,
)
from ui.clip_viewer import (
    AudioMixPanel, ClipViewer, EditorSnapshot,
    _editor_snapshot_from_draft, _editor_snapshot_to_draft,
)


def _sources():
    manifest = {'mixing_mode': 'independent', 'sources': [
        {'source_uuid': 'backup', 'stream_index': 1, 'source_type': 'system',
         'display_name': 'System Audio (backup)'},
        {'source_uuid': 'game', 'stream_index': 2, 'source_type': 'application',
         'display_name': 'Game'},
        {'source_uuid': 'chat', 'stream_index': 3, 'source_type': 'application',
         'display_name': 'Chat'},
        {'source_uuid': 'mic', 'stream_index': 4, 'source_type': 'microphone',
         'display_name': 'Microphone'},
    ]}
    return build_playback_sources(manifest, tuple(
        ProbedAudioStream(index, index - 1) for index in range(1, 5)))


def test_new_application_sources_mute_without_subtracting_encoded_audio():
    sources = _sources()
    gains = lambda state: {source.source_id: source_gain(source, state, sources)
                           for source in sources}
    assert gains({}) == {'game': 1, 'chat': 1, 'backup': 0, 'mic': 1}
    assert gains({'game': SourceMixState(muted=True)}) == {
        'game': 0, 'chat': 1, 'backup': 0, 'mic': 1}
    assert gains({'backup': SourceMixState()}) == {
        'game': 0, 'chat': 0, 'backup': 1, 'mic': 1}
    graph, _ = ffmpeg_mix_filter(sources, {'game': SourceMixState(muted=True)}, 100)
    assert 'volume=-' not in ';'.join(graph)


def test_backup_semantics_survive_lost_sidecar():
    sources = build_playback_sources(None, (
        ProbedAudioStream(1, 0, 'System Audio (backup)'),
        ProbedAudioStream(2, 1, 'Game'),
        ProbedAudioStream(3, 2, 'Microphone'),
    ))
    assert {source.display_name: source_gain(source, {}, sources) for source in sources} == {
        'Game': 1, 'System Audio (backup)': 0, 'Microphone': 1}
    backup = next(source for source in sources if source.mix_role == 'backup')
    states = {backup.source_id: SourceMixState()}
    assert [source_gain(source, states, sources) for source in sources] == [0, 1, 1]


def test_muting_an_unrelated_source_does_not_change_other_source_level():
    # No automatic headroom/normalization, and MASTER precedes overflow clipping.
    assert mix_interleaved_float([[.2], [0]], [1, 1]) == [.2]
    assert mix_interleaved_float([[.2], [0]], [1, 0]) == [.2]
    assert mix_interleaved_float([[.8], [.8]], [1, 1], .5) == [.8]


def _wave(path, *, rate=48000, duration=1, frequency=440):
    samples = np.sin(2 * np.pi * frequency * np.arange(round(rate * duration)) / rate) * .15
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(np.repeat((samples * 32767).astype('<i2'), 2).tobytes())


def _run(command):
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')[-2500:]
    return result.stdout


def test_source_cut_only_silences_that_source_in_preview_and_export(tmp_path):
    ffmpeg = get_ffmpeg_exe()
    game, chat, video = [tmp_path / name for name in ('game.wav', 'chat.wav', 'recording.mp4')]
    _wave(game, duration=2, frequency=220)
    _wave(chat, duration=2, frequency=660)
    _run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=30:d=2',
          '-i', str(game), '-i', str(chat), '-map', '0:v:0', '-map', '1:a:0',
          '-map', '2:a:0', '-c:v', 'mpeg4', '-c:a', 'aac', '-y', str(video)])
    sources = (PlaybackSource('game', 'Game', 'application', 1, 0),
               PlaybackSource('chat', 'Discord', 'application', 2, 1))

    def amplitude(data, start_s, frequency):
        offset = round(start_s * 48000)
        samples = np.frombuffer(data, '<f4')[::2][offset:offset + 4800]
        phase = np.arange(len(samples)) * np.pi * 2 * frequency / 48000
        return abs(np.sum(samples * np.exp(phase * 1j))) * 2 / len(samples)

    def verify(data, quiet_interval):
        assert amplitude(data, .1, 220) > .09
        assert amplitude(data, quiet_interval, 220) < .001
        assert amplitude(data, quiet_interval, 660) > .08

    mixer = InProcessFFmpegMixer(str(video), sources)
    try:
        mixer.configure_source_silences({'game': ((500, 1100),)})
        mixer.seek(0)
        pcm = b''.join(mixer.pull(480, [1, 1], 1) for _ in range(140))
        verify(pcm, .7)
        mixer.seek(700)
        seek_pcm = b''.join(mixer.pull(480, [1, 1], 1) for _ in range(15))
        assert amplitude(seek_pcm, .01, 220) < .001
        assert amplitude(seek_pcm, .01, 660) > .08
        mixer.configure_source_silences({})
        mixer.seek(700)
        restored = b''.join(mixer.pull(480, [1, 1], 1) for _ in range(15))
        assert amplitude(restored, .01, 220) > .09
    finally:
        mixer.close()

    out = tmp_path / 'edit.mp4'
    viewer = SimpleNamespace(
        clip_path=str(video), _audio_tracks=(('game', 0), ('chat', 1)),
        _playback_sources=sources, _source_volumes={}, _source_mutes={}, _master_volume=100,
        _source_audio_silences={'game': ((.5, 1.1),)})
    _run(ClipViewer._build_export_cmd(
        viewer, ffmpeg, .2, 1.4, str(out), None, ['-c:v', 'mpeg4'],
        segments=[(.2, .8), (1.0, 1.6)]))
    audio = _run([ffmpeg, '-v', 'error', '-i', str(out), '-map', '0:a:0',
                  '-f', 'f32le', '-ac', '2', '-ar', '48000', '-'])
    verify(audio, .4)
    assert amplitude(audio, .85, 220) > .09


def test_external_mix_matches_native_preview_with_short_44100_track(tmp_path):
    base = tmp_path / 'game.wav'
    imported = tmp_path / 'Musik ä.wav'
    _wave(base)
    _wave(imported, rate=44100, duration=.2, frequency=880)
    sources = (
        PlaybackSource('game', 'Game', 'application', 0, 0),
        PlaybackSource('import:music', 'Music', 'track', 0, 0, media_path=str(imported)),
    )
    graph, label = ffmpeg_mix_filter(sources, {}, 100)
    expected = _run([
        get_ffmpeg_exe(), '-v', 'error', '-i', str(base),
        *audio_input_arguments(sources), '-filter_complex', ';'.join(graph),
        '-map', label, '-t', '0.5', '-f', 'f32le', '-ar', '48000', '-ac', '2', '-'])
    mixer = InProcessFFmpegMixer(str(base), sources)
    try:
        actual = b''.join(mixer.pull(480, [1, 1], 1) for _ in range(50))
        # The import is already over, while the original source is still audible.
        mixer.seek(600)
        assert np.max(np.abs(np.frombuffer(mixer.pull(480, [1, 1], 1), '<f4'))) > .01
        mixer.seek(0)
        assert mixer.set_playback_rate(2, True)
        assert mixer.pull(480, [1, 1], 1)
    finally:
        mixer.close()
    actual = np.frombuffer(actual, '<f4')
    expected = np.frombuffer(expected, '<f4')
    assert len(actual) == len(expected)
    assert np.max(np.abs(actual - expected)) < .002


@pytest.mark.parametrize('segments', [[(.1, .8)], [(0, .15), (.5, .9)]])
def test_import_export_preserves_video_duration_and_originals(tmp_path, segments):
    ffmpeg = get_ffmpeg_exe()
    media = tmp_path / 'clip.mp4'
    imported = tmp_path / 'short.wav'
    _run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=30:d=1',
          '-c:v', 'mpeg4', '-y', str(media)])
    _wave(imported, rate=44100, duration=.2)
    before = [sha256(path.read_bytes()).hexdigest() for path in (media, imported)]
    source = PlaybackSource('import:test', 'Music', 'track', 0, 0, media_path=str(imported))
    viewer = SimpleNamespace(
        clip_path=str(media), _audio_tracks=((source.source_id, 0),),
        _playback_sources=(source,), _source_volumes={}, _source_mutes={}, _master_volume=100)
    out = tmp_path / 'export.mp4'
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, segments[0][0], sum(end - start for start, end in segments),
        str(out), None, ['-c:v', 'mpeg4'], segments=segments)
    _run(command)
    decoded = _run([ffmpeg, '-v', 'error', '-i', str(out), '-map', '0:a:0',
                    '-f', 'f32le', '-ar', '48000', '-ac', '2', '-'])
    duration = len(decoded) / (48000 * 8)
    assert duration == pytest.approx(sum(end - start for start, end in segments), abs=.04)
    assert before == [sha256(path.read_bytes()).hexdigest() for path in (media, imported)]


def test_audio_edit_roundtrip_and_missing_import_are_recoverable(tmp_path):
    path = tmp_path / 'music.wav'
    _wave(path)
    source = PlaybackSource('import:test', 'Music', 'track', 0, 0, media_path=str(path))
    effects = tuple((key, 0) for key in (
        'exposure', 'contrast', 'saturation', 'temperature', 'sharpness', 'vignette'))
    snapshot = EditorSnapshot(0, 1, ((0, 1, False),), 0, None, 1, effects,
                              master_volume=65, source_volumes=(('mic', 40),),
                              source_mutes=(('game', True),), imported_sources=(source,),
                              removed_audio_sources=('game',))
    draft = _editor_snapshot_to_draft(snapshot)
    assert _editor_snapshot_from_draft(draft) == snapshot
    path.unlink()
    restored = _editor_snapshot_from_draft(draft)
    assert restored == replace(snapshot, imported_sources=(replace(source, available=False),))
    draft['imported_audio'][0]['media_path'] = 'https://example.com/music.mp3'
    assert _editor_snapshot_from_draft(draft) is None


def test_mixer_has_remove_controls_for_recorded_and_imported_tracks(qtbot):
    from PySide6.QtWidgets import QPushButton
    sources = _sources()
    panel = AudioMixPanel(100, source_tracks=sources, live_preview=True)
    qtbot.addWidget(panel)
    seen = []
    panel.source_removed.connect(seen.append)
    remove = next(button for button in panel.findChildren(QPushButton)
                   if button.accessibleName() == 'Remove Game')
    remove.click()
    assert seen == ['game']


def test_audio_edit_undo_redo_persistence_and_stale_import(qtbot, monkeypatch, tmp_path):
    from core.clip_metadata_manager import ClipMetadataManager
    from ui import clip_viewer

    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'fixture')
    imported = tmp_path / 'music.wav'
    _wave(imported)
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', lambda *_args: ())
    monkeypatch.setattr(ClipViewer, '_prepare_audio_mixer', lambda self, _sources: None)
    settings = SimpleNamespace(get=lambda _key, default=None: default,
                               set=lambda *_args: None, save_settings=lambda: None)
    metadata = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite',
                                   legacy_path=tmp_path / 'legacy.json')
    viewer = ClipViewer(str(clip), None, settings_manager=settings, metadata_manager=metadata)
    qtbot.addWidget(viewer)
    viewer._playback_sources = _sources()
    viewer._audio_preparation_ready = True
    for source in viewer._playback_sources:
        viewer._source_volumes[source.source_id] = 100
        viewer._source_mutes[source.source_id] = source.mix_role == 'backup'
    viewer._update_audio_track_index()

    viewer._on_source_volume_changed('mic', 42)
    viewer._on_source_muted('chat', True)
    viewer._on_audio_source_removed('game')
    assert 'game' not in viewer._audio_mix_panel._row_order
    viewer._undo()
    assert 'game' in viewer._audio_mix_panel._row_order
    assert not viewer._source_mutes['game']
    viewer._redo()
    assert viewer._source_mutes['game']
    assert viewer._flush_editor_draft() is not False
    saved = metadata.get_editor_draft(str(clip))
    assert saved['source_volumes']['mic'] == 42
    assert saved['source_mutes']['chat'] is True
    assert saved['removed_audio_sources'] == ['game']

    source = PlaybackSource('import:test', 'Music', 'track', 0, 0, media_path=str(imported))
    viewer._on_external_audio_discovered((source,), '', object())
    assert source not in viewer._playback_sources  # stale clip token
    viewer._on_external_audio_discovered((source,), '', viewer._prepare_cancel)
    assert source in viewer._playback_sources
    viewer._undo()
    assert source not in viewer._playback_sources
    viewer._redo()
    assert source in viewer._playback_sources
    viewer._on_audio_source_removed(source.source_id)
    assert source not in viewer._playback_sources
    viewer._undo()
    assert source in viewer._playback_sources
    viewer.close()


def test_duplicate_imports_keep_source_ordinals_and_failed_stream_is_isolated(tmp_path):
    media = tmp_path / 'same.wav'
    _wave(media)
    sources = (
        PlaybackSource('a', 'A', 'track', 0, 0),
        PlaybackSource('b', 'B', 'track', 0, 0, media_path=str(media)),
        PlaybackSource('bad', 'Unavailable', 'track', 99, 9, media_path=str(media)),
    )
    mixer = InProcessFFmpegMixer(str(media), sources)
    try:
        a = mixer.pull(480, [1, 1, 1], .5)
        assert a
        assert mixer.failed_source_indexes() == (2,)
        mixer.seek(400)
        assert mixer.pull(480, [0, 1, 0], 1)
    finally:
        mixer.close()


def test_export_refuses_missing_import_instead_of_silently_dropping_it(tmp_path):
    missing = PlaybackSource('import:missing', 'Missing music', 'track', 0, 0,
                              available=False, media_path=str(tmp_path / 'missing.wav'))
    viewer = SimpleNamespace(clip_path='clip.mp4', _playback_sources=(missing,))
    with pytest.raises(ValueError, match='Imported audio is unavailable'):
        ClipViewer._build_export_cmd(viewer, 'ffmpeg', 0, 1, 'out.mp4', None, [])
