"""Check that saves keep the Qt event loop responsive.

A fast QTimer counts ticks while a fake engine never answers; blocking
submission would stall that counter.
"""

import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

pytest.importorskip('PySide6.QtCore')
from PySide6.QtCore import QTimer, QEventLoop, QElapsedTimer, Qt

from core.capture_bridge import CaptureBridge, SharedMemoryLayout, ResponseType
from core.save_state import SaveStateMachine, EngineEvent, OutcomeKind


class SilentEngineBridge(CaptureBridge):
    """A connected bridge whose engine never writes a response."""

    def __init__(self):
        self._buf = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
        self._layout = SharedMemoryLayout.from_buffer(self._buf)
        self._layout.is_initialized = True
        self._initialized = True
        self._logged_read_errors = set()


def _a_path():
    return 'C:/clips/t10.mp4' if sys.platform == 'win32' else '/tmp/t10.mp4'


def _run_loop_for(ms: int):
    """Spin a real event loop for `ms`, counting 30 ms ticks."""
    ticks = [0]
    counter = QTimer()
    # Stay above Windows' usual 15.6 ms clock resolution.
    counter.setInterval(30)
    counter.setTimerType(Qt.TimerType.PreciseTimer)
    counter.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    counter.start()

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    counter.stop()
    return ticks[0]


def test_save_submit_does_not_stall_the_event_loop(qapp):
    """Submit against a silent engine, then let the loop run for ~600 ms.

    A 30 ms timer should fire ~20 times. The old blocking save_clip() would
    have eaten a full second of that window before the loop even started.
    """
    bridge = SilentEngineBridge()

    elapsed = QElapsedTimer()
    elapsed.start()
    assert bridge.save_clip(_a_path(), 30) is True
    submit_ms = elapsed.elapsed()

    assert submit_ms < 50, (
        f'save_clip() blocked the main thread for {submit_ms} ms — '
        f'it must return immediately (AUDIT-011)')

    ticks = _run_loop_for(600)
    assert ticks >= 14, (
        f'event loop only ticked {ticks} times in 600 ms — something on the '
        f'save path is blocking it')


def test_poller_keeps_the_loop_responsive_while_engine_is_silent(qapp):
    """The full poller, running against an engine that says nothing for a
    second — including across the ack timeout. The loop must stay smooth and
    the timeout must be reached by the timer, not by a sleep."""
    bridge = SilentEngineBridge()
    sm = SaveStateMachine(logger=lambda m: None)
    outcomes = []

    import time as _time

    def pump():
        peeked = bridge.peek_save_response()
        now = _time.monotonic()
        if peeked is None:
            out = sm.on_tick(now)
        else:
            kind, detail = peeked
            event = {'started': EngineEvent.SAVE_STARTED,
                     'saved': EngineEvent.CLIP_SAVED,
                     'error': EngineEvent.ERROR_OCCURRED}[kind]
            out = sm.on_event(event, detail, now)
            bridge.consume_save_response(kind)
        if out is not None:
            outcomes.append(out)

    bridge.save_clip(_a_path(), 30)
    sm.submit(_a_path(), 30, _time.monotonic())

    save_timer = QTimer()
    save_timer.setInterval(50)
    save_timer.timeout.connect(pump)
    save_timer.start()

    ticks = _run_loop_for(1300)
    save_timer.stop()

    assert ticks >= 30, (
        f'event loop only ticked {ticks} times in 1300 ms — the save poller '
        f'is blocking')
    # The ack timeout fired from the timer, on schedule, without a sleep.
    assert any(o.kind is OutcomeKind.TIMEOUT_NOTICE for o in outcomes), (
        'ack timeout never fired from the poll timer')
    # And it stayed a warning: no result was invented for a save whose fate is
    # still unknown.
    assert not any(o.kind in (OutcomeKind.COMPLETED, OutcomeKind.FAILED)
                   for o in outcomes)


def test_late_completion_after_timeout_is_still_delivered(qapp):
    """The engine answers 300 ms after we warned. That result must land, and
    it must be the only result for the operation."""
    import time as _time

    bridge = SilentEngineBridge()
    sm = SaveStateMachine(logger=lambda m: None)
    outcomes = []

    def pump():
        peeked = bridge.peek_save_response()
        now = _time.monotonic()
        if peeked is None:
            out = sm.on_tick(now)
        else:
            kind, detail = peeked
            event = {'started': EngineEvent.SAVE_STARTED,
                     'saved': EngineEvent.CLIP_SAVED,
                     'error': EngineEvent.ERROR_OCCURRED}[kind]
            out = sm.on_event(event, detail, now)
            bridge.consume_save_response(kind)
        if out is not None:
            outcomes.append(out)

    bridge.save_clip(_a_path(), 30)
    sm.submit(_a_path(), 30, _time.monotonic())

    save_timer = QTimer()
    save_timer.setInterval(20)
    save_timer.timeout.connect(pump)
    save_timer.start()

    # Engine finally answers, well after the 1 s ack deadline.
    QTimer.singleShot(1300, lambda: setattr(
        bridge._layout, 'engine_response', ResponseType.CLIP_SAVED))

    _run_loop_for(1600)
    save_timer.stop()

    completed = [o for o in outcomes if o.kind is OutcomeKind.COMPLETED]
    assert len(completed) == 1, 'late success was lost or delivered twice'
    assert completed[0].late is True
    results = [o for o in outcomes
               if o.kind in (OutcomeKind.COMPLETED, OutcomeKind.FAILED)]
    assert len(results) == 1, 'operation produced more than one result'
