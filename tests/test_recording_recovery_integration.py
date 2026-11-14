import threading
from types import SimpleNamespace

import pytest

from core import recording_recovery as recovery


@pytest.fixture
def main():
    pytest.importorskip('PySide6.QtCore')
    import main

    return main


@pytest.fixture
def window():
    workers, ui_calls, events = [], [], []
    return SimpleNamespace(
        _file_worker_stop=threading.Event(),
        _launch_file_worker=workers.append,
        _ui_call=SimpleNamespace(emit=ui_calls.append),
        _open_recordings_folder=lambda: None,
        library_storage_controller=SimpleNamespace(
            observe_completed_clip=lambda path, identity, **kwargs: events.append(
                ('observed', path, identity, kwargs))),
        clip_grid=SimpleNamespace(force_refresh=lambda: events.append(('refreshed',))),
        push_error=lambda title, detail, **kwargs: events.append(
            ('notification', title, detail, kwargs)),
        workers=workers,
        ui_calls=ui_calls,
        events=events,
    )


@pytest.fixture
def recording(tmp_path):
    source = tmp_path / 'Recording.mp4'
    source.write_bytes(b'original recording fragments')
    return source


@pytest.fixture
def remux(monkeypatch, recording):
    calls = []
    destination = recording.with_name('Recording (recovered).mp4')

    def salvage(source, **kwargs):
        calls.append((source, kwargs))
        destination.write_bytes(b'playable prefix')
        return recovery.RecoveredRecording(destination, 3.2)

    monkeypatch.setattr(recovery, 'recover_manual_recording', salvage)
    return calls, destination


def test_closed_source_is_recovered_and_published_as_partial(
        main, window, recording, remux):
    calls, destination = remux
    original = recording.read_bytes()
    source_identity = main.media_identity(recording)

    main.MainWindow._recover_recording_fragments(
        window, recording, writer_closed=True, process=None)
    assert not calls
    assert not window.ui_calls
    assert len(window.workers) == 1

    window.workers.pop()()
    assert calls == [(recording, {
        'writer_closed': True, 'expected_identity': source_identity,
        'cancel_event': window._file_worker_stop,
    })]
    assert recording.read_bytes() == original
    assert window.events == []

    window.ui_calls.pop()()
    assert window.events[:2] == [
        ('observed', destination, main.media_identity(destination), {'publish_ui': True}),
        ('refreshed',),
    ]
    kind, title, detail, kwargs = window.events[2]
    assert kind == 'notification'
    assert title == 'RECORDING PARTIALLY RECOVERED'
    assert '3.2 seconds' in detail and destination.name in detail
    assert kwargs['level'] == 'warning'


def test_waits_only_for_original_process_and_never_repairs_while_alive(
        main, window, recording, remux, monkeypatch):
    calls, destination = remux
    fake_clock = iter((100.0, 100.0, 116.0))
    waits = []
    polls = []
    window._file_worker_stop = SimpleNamespace(
        is_set=lambda: False, wait=lambda seconds: waits.append(seconds) or False)
    window.engine_process = SimpleNamespace(
        poll=lambda: pytest.fail('The replacement engine is not the recording owner.'))
    original_process = SimpleNamespace(poll=lambda: polls.append(None))
    monkeypatch.setattr(main, 'time', SimpleNamespace(monotonic=lambda: next(fake_clock)))

    main.MainWindow._recover_recording_fragments(
        window, recording, writer_closed=False, process=original_process)
    window.workers.pop()()

    assert polls == [None, None]
    assert waits == [0.1]
    assert not calls
    assert not window.ui_calls
    assert not destination.exists()


def test_refuses_source_replaced_after_recovery_was_queued(main, window, recording, remux):
    calls, destination = remux
    main.MainWindow._recover_recording_fragments(
        window, recording, writer_closed=True, process=None)
    archived = recording.with_name('original.mp4')
    recording.rename(archived)
    recording.write_bytes(b'unrelated replacement')

    window.workers.pop()()

    assert not calls
    assert not window.ui_calls
    assert not destination.exists()
    assert archived.read_bytes() == b'original recording fragments'


@pytest.mark.parametrize('shutdown_signal', ['event', 'window'])
def test_shutdown_prevents_queued_publication(main, window, recording, remux, shutdown_signal):
    _, destination = remux
    main.MainWindow._recover_recording_fragments(
        window, recording, writer_closed=True, process=None)
    window.workers.pop()()
    if shutdown_signal == 'event':
        window._file_worker_stop.set()
    else:
        window._shutdown_requested = True

    window.ui_calls.pop()()

    assert window.events == []
    assert destination.read_bytes() == b'playable prefix'
    assert recording.read_bytes() == b'original recording fragments'
