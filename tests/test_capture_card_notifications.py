from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation

from ui import sound_playback
from ui import capture_card


def test_notification_events_route_only_to_their_named_sound(qtbot, monkeypatch):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    calls = []
    monkeypatch.setattr(
        card, '_show',
        lambda sound_key, **options: calls.append((sound_key, options)),
    )

    card.show_clip(30, 60, 'Source')
    card.show_screenshot()
    card.show_error('Encoder unavailable')
    card.show_upload('clip.mp4')
    card.show_upload_failed('Network unavailable')
    card.show_recording_saved('recording.mp4')
    card.show_prompt("Player's game detected")
    card.show_background_capture('Desktop')

    assert calls == [
        ('clip_captured', {'hold_duration_ms': 2400}),
        ('screenshot_captured', {'compact': True, 'hold_duration_ms': 2400}),
        ('error', {'compact': True, 'hold_duration_ms': 2400}),
        ('upload_successful', {'compact': True, 'hold_duration_ms': 2400}),
        ('upload_failed', {'compact': True, 'hold_duration_ms': 2400}),
        (None, {'compact': True, 'hold_duration_ms': 2400}),
        (None, {'compact': True, 'hold_duration_ms': 2400}),
        (None, {'compact': True, 'hold_duration_ms': 2400}),
    ]
    assert card._detail == 'Desktop  ·  Running in the background'


def test_manual_recording_saved_card_has_specific_copy(qtbot, monkeypatch):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    calls = []
    monkeypatch.setattr(
        card, '_show', lambda sound_key, **options: calls.append((sound_key, options)))

    card.show_recording_saved('recording.mp4')

    assert card._headline == 'MANUAL RECORDING SAVED'
    assert card._detail == 'recording.mp4  ·  Ready to trim and export.'
    assert calls == [(
        None, {'compact': True, 'hold_duration_ms': 2400})]


def test_capture_acknowledges_immediately_and_saved_preview_has_measured_duration(qtbot, monkeypatch):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    sounds = []
    monkeypatch.setattr(card, '_show', lambda sound, **_options: sounds.append(sound))
    card.show_captured()
    assert card._headline == 'CLIP CAPTURED'
    assert card._stats == []
    card.show_clip(43.917, 60, '1080p')
    assert card._headline == 'CLIP SAVED'
    assert ('43.92s', 'Duration') in card._stats
    assert sounds == ['clip_captured', 'clip_captured']
    card.show_clip(None, 60, '1080p')
    assert all(label != 'Duration' for _value, label in card._stats)


def test_manual_recording_card_shows_measured_video_seconds_without_changing_hold(
        qtbot, monkeypatch):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    calls = []
    monkeypatch.setattr(card, '_show', lambda *args, **kwargs:
                        calls.append((args, kwargs)))

    card.show_recording_saved('recording.mp4', 1500, duration_seconds=2.48)

    assert 'Video 2.48 s' in card._detail
    assert 'recording.mp4' in card._detail
    assert calls[0][1]['hold_duration_ms'] == 1500


@pytest.mark.parametrize('duration', [None, True, False, -1, float('nan'), float('inf')])
def test_unknown_or_invalid_recording_duration_does_not_invent_a_length(
        qtbot, monkeypatch, duration):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    monkeypatch.setattr(card, '_show', lambda *_args, **_kwargs: None)
    card.show_recording_saved('recording.mp4')
    unknown = card._detail

    card.show_recording_saved('recording.mp4', duration_seconds=duration)

    assert card._detail == unknown
    assert 'Video' not in card._detail


def test_default_sound_filenames_match_the_event_names():
    assert {
        key: path.name for key, path in capture_card._SOUND_FILES.items()
    } == {
        'clip_captured': 'clip_captured.wav',
        'screenshot_captured': 'screenshot_saved.wav',
        'error': 'error.wav',
        'startup': 'startup.wav',
        'upload_successful': 'upload_successful.wav',
        'upload_failed': 'upload_failed.wav',
    }


def test_notification_sound_is_delegated_to_preloaded_playback(
        monkeypatch, tmp_path):
    calls = []
    playback = type(
        'Playback', (),
        {'play': lambda _self, path, volume: calls.append((path, volume))},
    )()
    monkeypatch.setattr(capture_card, '_SOUND_PLAYBACK', playback)
    sound_path = tmp_path / 'notification.wav'
    sound_path.write_bytes(b'placeholder')

    capture_card._play_sound(sound_path, 37)

    assert calls == [(sound_path, 37)]


def test_zero_windows_notification_volume_stays_silent(
        monkeypatch, tmp_path):
    calls = []
    playback = type(
        'Playback', (),
        {'play': lambda _self, path, volume: calls.append((path, volume))},
    )()
    monkeypatch.setattr(capture_card, '_SOUND_PLAYBACK', playback)
    sound_path = tmp_path / 'notification.wav'
    sound_path.write_bytes(b'placeholder')

    capture_card._play_sound(sound_path, 0)

    assert calls == [(sound_path, 0)]


def test_disabled_capture_card_still_plays_notification_sound(qtbot, monkeypatch):
    class Settings:
        @staticmethod
        def get(_key, default=None):
            return default

    monkeypatch.setattr(capture_card, 'SettingsManager', Settings)
    card = capture_card.CaptureCard(visuals_enabled=False)
    qtbot.addWidget(card)
    sound_path = capture_card._SOUND_FILES['clip_captured']
    calls = []
    monkeypatch.setattr(
        capture_card, '_resolve_sound', lambda _key: sound_path)
    monkeypatch.setattr(
        capture_card, '_play_sound',
        lambda path, volume: calls.append((path, volume)),
    )

    card.show_clip(30, 60, 'Source')

    assert calls == [(sound_path, 100)]
    assert not card.isVisible()


def test_preloading_notification_cues_never_plays_them(monkeypatch, tmp_path):
    instances = []

    class StatusSignal:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

    class Effect:
        def __init__(self, _parent):
            self.statusChanged = StatusSignal()
            self.loaded = False
            self.plays = 0
            instances.append(self)

        def setSource(self, _url):
            self.loaded = True
            self.statusChanged.callback()

        def isLoaded(self):
            return self.loaded

        def setVolume(self, _volume):
            pass

        def play(self):
            self.plays += 1

        def stop(self):
            pass

    monkeypatch.setattr(sound_playback, 'QSoundEffect', Effect)
    cue = tmp_path / 'startup.wav'
    cue.write_bytes(b'placeholder')

    player = sound_playback.SoundPlayback(preload_paths=(cue,))

    assert instances[0].plays == 0
    assert player.play(cue) is True
    assert instances[0].plays == 1


@pytest.mark.parametrize('hold_duration_ms', (250, 500, 750, 1000, 1500, 2000))
def test_popup_hold_duration_is_exactly_milliseconds(qtbot, hold_duration_ms):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)

    card._show(None, hold_duration_ms=hold_duration_ms)

    screen = card.screen().availableGeometry()
    assert card.isVisible()
    assert card._seq.state() == QAbstractAnimation.State.Running
    card._seq.setCurrentTime(capture_card._SLIDE_IN_DURATION_MS)
    assert card.x() == screen.right() - card.width() - capture_card._MARGIN
    assert card.y() == screen.top() + capture_card._MARGIN
    assert card._seq.animationAt(1).duration() == hold_duration_ms
    assert card._seq.duration() == (capture_card._SLIDE_IN_DURATION_MS
                                    + hold_duration_ms
                                    + capture_card._SLIDE_OUT_DURATION_MS)
    card._stop_display()


def test_repeated_popup_replaces_old_timers_without_hiding_new_card(qtbot):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    card._show(None, hold_duration_ms=100)
    old_sequence = card._seq

    card._show(None, hold_duration_ms=300)
    qtbot.wait(150)

    assert card.isVisible()
    assert old_sequence.state() == QAbstractAnimation.State.Stopped
    assert card._seq is not old_sequence
    qtbot.waitUntil(lambda: not card.isVisible())


def test_disabling_card_cancels_animation_and_paint_timers(qtbot):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    card._show(None)
    qtbot.wait(200)
    card.set_visuals_enabled(False)
    assert card._seq is None
    assert not card.isVisible()
    assert not any(timer.isActive() for timer in (
        card._prog_timer, card._shim_timer,
        card._shimmer_delay, card._progress_delay))


def test_capture_popup_never_accepts_input_or_focus(qtbot):
    from PySide6.QtCore import Qt

    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    card._show(None)
    assert card.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert card.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert card.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert card.focusPolicy() == Qt.FocusPolicy.NoFocus
    card._stop_display()


def test_immediate_capture_card_shows_requested_settings(qtbot, monkeypatch):
    card = capture_card.CaptureCard()
    qtbot.addWidget(card)
    calls = []
    monkeypatch.setattr(card, '_show', lambda sound, **options: calls.append((sound, options)))
    card.show_captured(30, 120, '1080p')
    assert card._headline == 'CLIP CAPTURED'
    assert card._stats == [('30s', 'Clip length'), ('120 FPS', 'Framerate'), ('1080p', 'Resolution')]
    assert calls == [('clip_captured', {'compact': False})]
