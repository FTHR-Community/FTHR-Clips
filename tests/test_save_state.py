"""Drive the save state machine with scripted shared-memory responses.

Use the real CaptureBridge and ctypes layout with an in-memory mapping
to exercise timing, peek/consume ordering, and response decoding.
"""

import ctypes
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from core.capture_bridge import (
    CaptureBridge, CommandType, SharedMemoryLayout, ResponseType,
)
from core.save_state import (
    SaveStateMachine, SaveState, EngineEvent, OutcomeKind, RejectReason,
)


# Harness

class FakeEngine:
    """A shared-memory segment plus the ability to write responses into it."""

    def __init__(self):
        self._buf = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
        self.layout = SharedMemoryLayout.from_buffer(self._buf)
        self.layout.is_initialized = True

    def respond(self, response: ResponseType, detail: str = ''):
        """Write a response the way the engine does: string first, then code.

        The Linux engine's *failure* path writes them the other way round; see
        `test_error_with_empty_string_is_tolerated` for that case.
        """
        self.layout.ui_command = CommandType.NONE
        self._write_string(detail)
        self.layout.engine_response = response

    def respond_code_first(self, response: ResponseType, detail: str = ''):
        """Reproduce the Linux failure-path ordering: code before string."""
        self.layout.ui_command = CommandType.NONE
        self.layout.engine_response = response
        self._write_string(detail)

    def _write_string(self, detail):
        if isinstance(self.layout.engine_string, bytes):
            self.layout.engine_string = detail.encode('utf-8') if isinstance(detail, str) else detail
        else:
            self.layout.engine_string = detail if isinstance(detail, str) else detail.decode('utf-8', 'replace')

    @property
    def response(self):
        return self.layout.engine_response


class FakeBridge(CaptureBridge):
    def __new__(cls, engine: FakeEngine):
        return object.__new__(cls)

    def __init__(self, engine: FakeEngine):
        self._mapping_lock = threading.RLock()
        self._layout = engine.layout
        self._initialized = True
        self._logged_read_errors = set()


def test_fake_bridge_owns_layout_without_reusing_base_singleton(monkeypatch):
    monkeypatch.setattr(CaptureBridge, '_instance', None)
    base = CaptureBridge()
    assert type(base) is CaptureBridge
    assert base._layout is None
    engine = FakeEngine()
    bridge = FakeBridge(engine)

    assert type(bridge) is FakeBridge
    assert bridge is not base
    assert bridge._layout is engine.layout
    assert bridge._mapping_lock is not base._mapping_lock
    assert CaptureBridge._instance is base
    assert bridge.save_clip('/clips/isolated.mp4', 30) is True
    assert engine.layout.ui_command == CommandType.SAVE_CLIP
    assert bridge.save_clip('/clips/pending.mp4', 30) is False
    assert CaptureBridge._instance is base
    assert base._layout is None
    assert base._initialized is False


class Driver:
    """Mirror the main-window poller: read, interpret, then consume.

    The ordering test checks this Qt-free harness against main.py.
    """

    def __init__(self, ack_timeout_s=1.0, completion_timeout_s=60.0):
        self.engine = FakeEngine()
        self.bridge = FakeBridge(self.engine)
        self.log = []
        self.sm = SaveStateMachine(
            ack_timeout_s=ack_timeout_s,
            completion_timeout_s=completion_timeout_s,
            logger=self.log.append,
        )
        self.now = 1000.0
        self.outcomes = []

    def advance(self, seconds):
        self.now += seconds

    def submit(self, path='/clips/a.mp4', duration=30, **ctx):
        self.pump()                      # drain a pending response first
        assert self.bridge.save_clip(path, duration) is True
        return self.sm.submit(path, duration, self.now, **ctx)

    def pump(self):
        """One poll tick — the single reader."""
        peeked = self.bridge.peek_save_response()
        if peeked is None:
            outcome = self.sm.on_tick(self.now)
        else:
            kind, detail = peeked
            event = {'started': EngineEvent.SAVE_STARTED,
                     'saved': EngineEvent.CLIP_SAVED,
                     'error': EngineEvent.ERROR_OCCURRED}[kind]
            outcome = self.sm.on_event(event, detail, self.now)
            self.bridge.consume_save_response(kind)
        if outcome is not None:
            self.outcomes.append(outcome)
        return outcome

    # assertions helpers

    def kinds(self):
        return [o.kind for o in self.outcomes]

    def results(self):
        return [o for o in self.outcomes
                if o.kind in (OutcomeKind.COMPLETED, OutcomeKind.FAILED)]


@pytest.fixture
def d():
    return Driver()


# T1 — normal flow

def test_T1_normal_save_started_then_clip_saved(d):
    d.submit()
    d.pump()                                        # NONE -> WAITING_FOR_ACK
    assert d.sm.state is SaveState.WAITING_FOR_ACK

    d.advance(0.05)
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()
    assert d.sm.state is SaveState.ENGINE_ACCEPTED

    d.advance(0.8)
    d.pump()                                        # NONE, still encoding
    assert d.sm.state is SaveState.ENGINE_ACCEPTED

    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    assert d.sm.state is SaveState.ENGINE_COMPLETED
    assert d.kinds() == [OutcomeKind.ACCEPTED, OutcomeKind.COMPLETED]
    assert len(d.results()) == 1


# T2 — ultra fast: CLIP_SAVED without an observable SAVE_STARTED (AUDIT-015)

def test_T2_clip_saved_without_save_started_succeeds(d):
    d.submit()
    d.advance(0.02)
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()

    assert d.sm.state is SaveState.ENGINE_COMPLETED
    assert d.kinds() == [OutcomeKind.COMPLETED]
    assert d.outcomes[0].late is False


# T3 / T4 — failures

def test_T3_immediate_failure_reports_once_with_detail(d):
    d.submit()
    d.engine.respond(ResponseType.ERROR_OCCURRED, 'SaveClip failed: disk full')
    d.pump()

    assert d.sm.state is SaveState.ENGINE_FAILED
    assert len(d.results()) == 1
    assert d.outcomes[0].kind is OutcomeKind.FAILED
    assert 'disk full' in d.outcomes[0].detail


def test_T4_accepted_then_failure(d):
    d.submit()
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()
    d.advance(0.3)
    d.engine.respond(ResponseType.ERROR_OCCURRED, 'mux failed')
    d.pump()

    assert d.kinds() == [OutcomeKind.ACCEPTED, OutcomeKind.FAILED]
    assert len(d.results()) == 1


# T5 — no response at all

def test_T5_no_response_times_out_then_fails(d):
    d.submit()
    d.pump()
    d.advance(1.5)
    out = d.pump()
    assert out.kind is OutcomeKind.TIMEOUT_NOTICE
    assert d.sm.state is SaveState.TIMED_OUT
    # A timeout notice is not a result — nothing has been reported as failed.
    assert d.results() == []
    # …until the completion deadline passes.
    d.advance(70.0)
    out = d.pump()
    assert out.kind is OutcomeKind.FAILED
    assert len(d.results()) == 1


def test_T5_timeout_does_not_destroy_a_late_success(d):
    d.submit()
    d.advance(1.5)
    assert d.pump().kind is OutcomeKind.TIMEOUT_NOTICE

    d.advance(0.4)
    d.engine.respond(ResponseType.CLIP_SAVED)
    out = d.pump()

    assert out.kind is OutcomeKind.COMPLETED
    assert out.late is True
    assert len(d.results()) == 1, 'late success must be the ONLY result'
    assert any('late success after timeout' in m for m in d.log)


# T6 / T7 — AUDIT-017: an old verdict must survive until it is consumed

def test_T6_old_clip_saved_is_processed_before_the_next_save(d):
    """Regression: save A completes, its CLIP_SAVED is still in the field, and
    the user presses the hotkey again. A's completion must be reported, not
    overwritten by B's request."""
    a = d.submit('/clips/a.mp4').operation
    d.engine.respond(ResponseType.CLIP_SAVED)       # A finished, not yet read

    # Hotkey for B. submit() pumps first, which is what saves A's verdict.
    b = d.submit('/clips/b.mp4').operation

    completions = [o for o in d.outcomes if o.kind is OutcomeKind.COMPLETED]
    assert len(completions) == 1
    assert completions[0].operation is a, "A's completion was lost"
    assert a.state is SaveState.ENGINE_COMPLETED
    assert b is not None and b.state is SaveState.REQUESTED

    # And B still gets its own verdict afterwards.
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    assert b.state is SaveState.ENGINE_COMPLETED
    assert len(d.results()) == 2


def test_T6_save_clip_never_clears_a_pending_response(d):
    """The bridge-level half of AUDIT-017: submitting a command must leave
    engine_response untouched. This is the assertion that fails if anyone
    reintroduces `engine_response = NONE` into save_clip()."""
    d.engine.respond(ResponseType.CLIP_SAVED, 'previous save')

    d.bridge.save_clip('/clips/next.mp4', 30)

    assert d.engine.response == ResponseType.CLIP_SAVED, (
        'save_clip() destroyed an unconsumed engine response (AUDIT-017)')


def test_T7_old_error_is_processed_before_the_next_save(d):
    a = d.submit('/clips/a.mp4').operation
    d.engine.respond(ResponseType.ERROR_OCCURRED, 'disk full')

    b = d.submit('/clips/b.mp4').operation

    failures = [o for o in d.outcomes if o.kind is OutcomeKind.FAILED]
    assert len(failures) == 1
    assert failures[0].operation is a
    assert 'disk full' in failures[0].detail
    assert b.state is SaveState.REQUESTED


# T8 — duplicate visibility across ticks

def test_T8_same_clip_saved_seen_twice_succeeds_once(d):
    d.submit()
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    # Engine (or a stuck field) presents it again.
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    d.pump()

    assert len(d.results()) == 1


def test_T8_duplicate_save_started_emits_one_accepted(d):
    d.submit()
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()

    assert d.kinds().count(OutcomeKind.ACCEPTED) == 1


# T9 — garbled strings

def test_T9_garbled_engine_string_does_not_crash(d):
    d.submit()
    if isinstance(d.engine.layout.engine_string, bytes):
        d.engine.layout.engine_string = b'\xff\xfe invalid \x80utf8'
    else:
        d.engine.layout.engine_string = 'invalid \ud800'.encode(
            'utf-16', 'surrogatepass').decode('utf-16', 'replace')
    d.engine.layout.engine_response = ResponseType.ERROR_OCCURRED

    out = d.pump()
    assert out.kind is OutcomeKind.FAILED
    assert isinstance(out.detail, str)


def test_error_with_empty_string_is_tolerated(d):
    """The Linux engine writes ERROR_OCCURRED before the message (documented
    contract deviation). A peek in that window sees an empty detail — that
    must produce a failure with no detail, never a crash or a wrong message."""
    d.submit()
    d.engine.respond_code_first(ResponseType.ERROR_OCCURRED, '')
    out = d.pump()

    assert out.kind is OutcomeKind.FAILED
    assert out.detail == ''


# T11 — two rapid requests / single flight

def test_T11_second_request_while_in_flight_is_refused(d):
    a = d.submit('/clips/a.mp4').operation
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()

    second = d.sm.submit('/clips/b.mp4', 30, d.now)
    assert second.accepted is False
    assert second.reason is RejectReason.BUSY
    assert second.operation is None
    assert d.sm.current is a

    # A's verdict still lands on A, unambiguously.
    d.engine.respond(ResponseType.CLIP_SAVED)
    out = d.pump()
    assert out.operation is a
    assert len(d.results()) == 1


def test_T11_timed_out_operation_does_not_block_the_next_save(d):
    d.submit('/clips/a.mp4')
    d.advance(1.5)
    d.pump()
    assert d.sm.state is SaveState.TIMED_OUT
    assert d.sm.is_busy() is False, 'a wedged engine must not lock the hotkey'

    result = d.sm.submit('/clips/b.mp4', 30, d.now)
    assert result.accepted is True


# T12 — shutdown

def test_T12_shutdown_with_active_save_emits_no_result(d):
    d.submit()
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()

    d.sm.cancel_active(d.now, reason='shutdown')
    assert d.sm.is_busy() is False

    # A response arriving during teardown must not resurrect anything.
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    assert d.results() == []


# Invariants

@pytest.mark.parametrize('script', [
    [ResponseType.CLIP_SAVED],
    [ResponseType.ERROR_OCCURRED],
    [ResponseType.SAVE_STARTED, ResponseType.CLIP_SAVED],
    [ResponseType.SAVE_STARTED, ResponseType.ERROR_OCCURRED],
    [ResponseType.SAVE_STARTED, ResponseType.CLIP_SAVED, ResponseType.CLIP_SAVED],
    [ResponseType.CLIP_SAVED, ResponseType.ERROR_OCCURRED],
    [ResponseType.ERROR_OCCURRED, ResponseType.CLIP_SAVED],
])
def test_invariant_at_most_one_terminal_result(script):
    """No operation may ever report both success and failure, or either twice."""
    d = Driver()
    op = d.submit().operation
    for response in script:
        d.advance(0.05)
        d.engine.respond(response)
        d.pump()
    d.advance(0.05)
    d.pump()

    assert len(d.results()) <= 1
    assert op.result_emitted is True
    assert op.state in (SaveState.ENGINE_COMPLETED, SaveState.ENGINE_FAILED)


def test_invariant_counts_never_exceed_submissions():
    d = Driver()
    submitted = 0
    for i in range(5):
        r = d.submit(f'/clips/{i}.mp4')
        if r.accepted:
            submitted += 1
        d.advance(0.1)
        d.engine.respond(ResponseType.CLIP_SAVED if i % 2 else ResponseType.ERROR_OCCURRED)
        d.pump()

    completed = sum(1 for o in d.outcomes if o.kind is OutcomeKind.COMPLETED)
    failed = sum(1 for o in d.outcomes if o.kind is OutcomeKind.FAILED)
    assert completed <= submitted
    assert failed <= submitted
    assert completed + failed <= submitted


def test_unattributable_response_is_logged_not_silently_dropped(d):
    """No operation exists, but a response shows up. It must be consumed and
    reported, not treated as a save."""
    d.engine.respond(ResponseType.CLIP_SAVED, 'orphan')
    out = d.pump()

    assert out is None
    assert d.engine.response == ResponseType.NONE, 'orphan was left in the field'
    assert any('no operation to own it' in m for m in d.log)


def test_non_save_responses_are_left_untouched(d):
    """STATUS_UPDATE and friends belong to other code paths. The save reader
    must neither interpret nor consume them."""
    for resp in (ResponseType.STATUS_UPDATE, ResponseType.RECORDING_STARTED,
                 ResponseType.RECORDING_STOPPED):
        d.engine.layout.engine_response = resp
        assert d.bridge.peek_save_response() is None
        assert d.bridge.consume_save_response() is False
        assert d.engine.response == resp


def test_consume_only_clears_what_was_peeked(d):
    """peek → consume must not destroy a response that arrived in between."""
    d.submit()
    d.engine.respond(ResponseType.SAVE_STARTED)
    assert d.bridge.peek_save_response()[0] == 'started'
    assert d.bridge.consume_save_response() is True
    assert d.engine.response == ResponseType.SAVE_STARTED
    assert d.bridge.peek_save_response() is None
    # The ack stays in shared memory but is delivered only once locally.
    assert d.bridge.consume_save_response() is False


def test_ack_and_total_latency_are_recorded(d):
    op = d.submit().operation
    d.advance(0.014)
    d.engine.respond(ResponseType.SAVE_STARTED)
    d.pump()
    assert 13 <= op.ack_latency_ms <= 15

    d.advance(0.828)
    d.engine.respond(ResponseType.CLIP_SAVED)
    d.pump()
    assert 841 <= op.total_latency_ms <= 843
    assert any('ack=14 ms' in m for m in d.log)


def test_operation_carries_request_context(d):
    op = d.submit(mic_end_time=1234.5).operation
    assert op.context['mic_end_time'] == 1234.5
    assert op.output_path == '/clips/a.mp4'
    assert op.duration_seconds == 30
    assert op.ack_deadline == op.requested_at + 1.0


def test_logs_do_not_contain_full_paths(d):
    d.submit('/home/realname/FTHR_Clips/Some Game/clip.mp4')
    assert not any('realname' in m for m in d.log), (
        'log leaked the user profile path')
    assert any('clip.mp4' in m for m in d.log)


def test_pump_mirrors_main_window_ordering():
    """Guard against the test harness drifting from the real poller.

    Both must peek, interpret, then consume. If main.py ever consumes before
    interpreting, these tests would keep passing while production loses
    responses — so assert the real method's source shape.
    """
    main_py = Path(__file__).resolve().parent.parent / 'FTHR_UI' / 'main.py'
    src = main_py.read_text(encoding='utf-8', errors='replace')
    start = src.index('def _pump_save_responses')
    body = src[start:src.index('def _on_save_poll_tick')]

    assert body.index('peek_save_response') < body.index('on_event')
    assert body.index('on_event') < body.index('consume_save_response')
    # And the single-consumer rule: nothing else may consume.
    assert src.count('consume_save_response') == 1
