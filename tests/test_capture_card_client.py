from __future__ import annotations

from types import SimpleNamespace

import pytest

from ui import capture_card_client


class _FakeStdin:
    def __init__(self):
        self.value = ''
        self.closed = False

    def write(self, value):
        self.value += value

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _BrokenStdin(_FakeStdin):
    def write(self, value):
        raise BrokenPipeError('card helper exited')


class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = _FakeStdin()
        self.wait_calls = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_windows_capture_card_keeps_native_qt_platform(monkeypatch):
    captured = {}
    process = _FakeProcess()

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setattr(capture_card_client.sys, 'platform', 'win32')
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', fake_popen)
    monkeypatch.setenv('QT_QPA_PLATFORM', 'windows')

    client = capture_card_client.CaptureCardClient()

    assert captured['env']['QT_QPA_PLATFORM'] == 'windows'
    client.close()
    assert process.stdin.value == 'quit\n'
    assert process.stdin.closed


def test_close_does_not_respawn_an_already_dead_card(monkeypatch):
    launches = 0

    def fake_popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', fake_popen)
    client = capture_card_client.CaptureCardClient()

    client.close()

    assert launches == 1
    assert client._proc is None


def test_named_notification_commands_are_routed_without_error_fallback(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(
        capture_card_client.subprocess, 'Popen',
        lambda *args, **kwargs: process,
    )
    client = capture_card_client.CaptureCardClient()

    client.play_startup()
    client.show_capturing("Player's Game")
    client.show_background_capture('Desktop')
    client.show_upload_failed('Network unavailable')
    client.show_recording_saved('recording.mp4')
    client.hide_background_capture()

    assert process.stdin.value.splitlines() == [
        'startup',
        "capturing|Player's Game",
        'background|Desktop',
        'upload_failed|Network unavailable',
        'recording_saved|recording.mp4',
        'background_hide',
    ]


def test_capture_card_visual_setting_is_sent_without_affecting_sounds(monkeypatch):
    process = _FakeProcess()
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setattr(
        capture_card_client.subprocess, 'Popen',
        fake_popen,
    )

    class Settings:
        def get(self, key, default=None):
            return {'capture_card_enabled': False}.get(key, default)

    client = capture_card_client.CaptureCardClient(Settings())

    assert client._visuals_enabled is False
    assert captured['env']['FTHR_CARD_VISUALS_ENABLED'] == '0'
    client.set_visuals_enabled(True)

    assert process.stdin.value.splitlines() == ['visuals|1']


def test_explicit_hold_durations_are_transmitted_as_milliseconds(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(
        capture_card_client.subprocess, 'Popen',
        lambda *args, **kwargs: process,
    )
    client = capture_card_client.CaptureCardClient()

    client.show_clip(30, 60, '1080p', hold_duration_ms=250)
    client.show_screenshot(hold_duration_ms=500)
    client.show_error('Encoder unavailable', hold_duration_ms=1000)
    client.show_recording_saved('recording.mp4', hold_duration_ms=1500)

    assert process.stdin.value.splitlines() == [
        'clip|30|60|1080p|250',
        'screenshot|500',
        'error|Encoder unavailable|1000',
        'recording_saved|recording.mp4|1500',
    ]


def test_broken_pipe_relaunches_once_and_preserves_the_popup_event(monkeypatch):
    broken = _FakeProcess()
    broken.stdin = _BrokenStdin()
    replacement = _FakeProcess()
    processes = iter((broken, replacement))
    monkeypatch.setattr(
        capture_card_client.subprocess, 'Popen',
        lambda *args, **kwargs: next(processes),
    )
    client = capture_card_client.CaptureCardClient()

    client.show_screenshot(hold_duration_ms=750)

    assert broken.terminated
    assert replacement.stdin.value == 'screenshot|750\n'


def test_windows_modes_filter_visuals_and_keep_sound_events(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(capture_card_client.sys, 'platform', 'win32')
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', lambda *a, **k: process)
    class Settings:
        mode = 'capture'
        def get(self, key, default=None):
            return self.mode if key == 'capture_card_mode' else default
    settings = Settings()
    client = capture_card_client.CaptureCardClient(settings)
    client.show_clip(30, 60, '1080p')
    client.show_capturing('Game')
    settings.mode = 'detection'
    client.show_clip(30, 60, '1080p')
    client.show_capturing('Game')
    settings.mode = 'off'
    client.show_screenshot()
    assert process.stdin.value.splitlines() == [
        'visuals|1', 'clip|30|60|1080p', 'visuals|0', 'capturing|Game',
        'visuals|0', 'clip|30|60|1080p', 'visuals|1', 'capturing|Game',
        'visuals|0', 'screenshot',
    ]


def test_recording_duration_uses_fourth_field_and_preserves_positional_hold(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', lambda *a, **k: process)
    client = capture_card_client.CaptureCardClient()

    client.show_recording_saved('recording.mp4', duration_seconds=2.48)
    client.show_recording_saved('recording.mp4', 1500, duration_seconds=2.48)
    default, explicit = process.stdin.value.splitlines()
    assert default.split('|')[:3] == ['recording_saved', 'recording.mp4', '']
    assert float(default.split('|')[3]) == pytest.approx(2.48)
    assert explicit.split('|')[:3] == ['recording_saved', 'recording.mp4', '1500']
    assert float(explicit.split('|')[3]) == pytest.approx(2.48)


@pytest.mark.parametrize('duration', [None, True, False, -1, float('nan'), float('inf')])
def test_invalid_recording_duration_keeps_legacy_wire_command(monkeypatch, duration):
    process = _FakeProcess()
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', lambda *a, **k: process)
    client = capture_card_client.CaptureCardClient()

    client.show_recording_saved('recording.mp4', 1500, duration_seconds=duration)

    assert process.stdin.value == 'recording_saved|recording.mp4|1500\n'


@pytest.mark.parametrize(('command', 'expected'), [
    ('saving', ((), {})),
    ('clip|43.917|60|1080p', ((43.917, 60, '1080p'), {})),
    ('clip||60|1080p', ((None, 60, '1080p'), {})),
    ('recording_saved|old.mp4', (('old.mp4',), {})),
    ('captured|60|120|1080p', ((60.0, 120, '1080p'), {})),
    ('recording_saved|old.mp4|1500', (('old.mp4',), {'hold_duration_ms': 1500})),
    ('recording_saved|new.mp4||2.48', (('new.mp4',), {'duration_seconds': 2.48})),
    ('recording_saved|new.mp4|1500|2.48',
     (('new.mp4',), {'hold_duration_ms': 1500, 'duration_seconds': 2.48})),
    ('recording_saved|unknown.mp4|1500|nan',
     (('unknown.mp4',), {'hold_duration_ms': 1500})),
    ('recording_saved|unknown.mp4|1500|inf',
     (('unknown.mp4',), {'hold_duration_ms': 1500})),
    ('recording_saved|unknown.mp4|1500|-1',
     (('unknown.mp4',), {'hold_duration_ms': 1500})),
])
def test_capture_process_decodes_acknowledgement_and_measured_fields(
        monkeypatch, command, expected):
    """Exercise the real nested command handler without spawning its process."""
    from ui import capture_card_process

    callbacks = {}
    received = []
    def receive(*args, **kwargs):
        received.append((args, kwargs))

    card = SimpleNamespace(
        show_recording_saved=receive, show_clip=receive, show_saving=receive,
        show_captured=receive)
    reader = SimpleNamespace(
        command=SimpleNamespace(connect=lambda handler: callbacks.update(handle=handler)),
        eof_ready=SimpleNamespace(connect=lambda _handler: None),
        start=lambda: None, wait=lambda _timeout: None,
    )

    def execute():
        callbacks['handle'](command)
        return 0

    app = SimpleNamespace(exec=execute, quit=lambda: None)
    monkeypatch.setattr(capture_card_process, 'QApplication', lambda _args: app)
    monkeypatch.setattr(capture_card_process, 'CaptureCard', lambda **_kwargs: card)
    monkeypatch.setattr(capture_card_process, '_StdinReader', lambda: reader)
    monkeypatch.setattr(capture_card_process, 'configure_qt_style', lambda: None)
    monkeypatch.setattr(capture_card_process, 'apply_app_style', lambda _app: None)

    with pytest.raises(SystemExit) as exited:
        capture_card_process.main()

    assert exited.value.code == 0
    assert received == [expected]


@pytest.mark.parametrize('mode,visible', [('all', True), ('capture', True),
                                         ('detection', False), ('off', False)])
def test_ultra_mode_lazily_delivers_capture_results_and_respects_modes(
        monkeypatch, mode, visible):
    process = _FakeProcess()
    launches = []
    def launch(*args, **kwargs):
        launches.append(kwargs)
        return process
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', launch)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'ultra_performance_mode': True, 'capture_card_mode': mode,
    }.get(key, default))
    client = capture_card_client.CaptureCardClient(settings)
    client.show_background_capture('Desktop')
    client.play_startup()
    assert launches == []
    client.show_clip(30, 60, '1080p')
    client.show_screenshot()
    client.show_recording_saved('recording.mp4')
    assert len(launches) == 1
    assert launches[0]['env']['FTHR_CARD_VISUALS_ENABLED'] == str(int(visible))
    assert process.stdin.value.splitlines() == [
        f'visuals|{int(visible)}', 'clip|30|60|1080p',
        f'visuals|{int(visible)}', 'screenshot',
        f'visuals|{int(visible)}', 'recording_saved|recording.mp4',
    ]


def test_ultra_mode_preserves_legacy_disabled_visuals(monkeypatch):
    process = _FakeProcess()
    launches = []
    def launch(*args, **kwargs):
        launches.append(kwargs)
        return process
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', launch)
    settings = SimpleNamespace(get=lambda key, default=None: {
        'ultra_performance_mode': True, 'capture_card_enabled': False,
    }.get(key, default))
    client = capture_card_client.CaptureCardClient(settings)
    client.show_clip(30, 60, '1080p')
    assert launches[0]['env']['FTHR_CARD_VISUALS_ENABLED'] == '0'
    assert process.stdin.value == 'clip|30|60|1080p\n'


def test_capture_settings_are_sent_in_one_notification(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(capture_card_client.subprocess, 'Popen', lambda *args, **kwargs: process)
    client = capture_card_client.CaptureCardClient()
    client.show_captured(60, 120, '1080p')
    assert process.stdin.value.splitlines() == ['captured|60|120|1080p']
