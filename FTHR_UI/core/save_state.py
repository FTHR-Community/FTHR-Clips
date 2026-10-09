# Single-flight save state machine driven by the Qt main-thread poller.
#
# The engine has one response slot with no request ID. Only the poller reads
# and consumes it; this module interprets events and deadlines without Qt,
# ctypes, or bridge access. Instances are not thread-safe.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SaveState(Enum):
    """Lifecycle of a single engine save.

    IDLE is the machine's resting state, not an operation state — an operation
    is created in REQUESTED and never returns to IDLE.
    """
    IDLE = 'IDLE'
    REQUESTED = 'REQUESTED'                # command written, not yet polled
    WAITING_FOR_ACK = 'WAITING_FOR_ACK'    # polled at least once, no response
    ENGINE_ACCEPTED = 'ENGINE_ACCEPTED'    # SAVE_STARTED seen
    ENGINE_COMPLETED = 'ENGINE_COMPLETED'  # CLIP_SAVED seen  — terminal
    ENGINE_FAILED = 'ENGINE_FAILED'        # ERROR_OCCURRED   — terminal
    TIMED_OUT = 'TIMED_OUT'                # no ack in time   — see below


#: States in which the engine is still expected to say something.
_IN_FLIGHT = (SaveState.REQUESTED, SaveState.WAITING_FOR_ACK,
              SaveState.ENGINE_ACCEPTED)


class EngineEvent(Enum):
    """What the single reader saw in `engine_response`.

    Deliberately *not* ResponseType: this module must stay usable without
    ctypes and without a mapped shared-memory segment.
    """
    SAVE_STARTED = 'SAVE_STARTED'
    CLIP_SAVED = 'CLIP_SAVED'
    ERROR_OCCURRED = 'ERROR_OCCURRED'


class OutcomeKind(Enum):
    """Save results and non-terminal notices for the UI.

    Each operation emits at most one COMPLETED or FAILED result. TIMEOUT_NOTICE
    keeps the save under observation so a late CLIP_SAVED can still be accepted.
    """
    ACCEPTED = 'ACCEPTED'                # engine acked; UI may show progress
    COMPLETED = 'COMPLETED'              # result — clip written
    FAILED = 'FAILED'                    # result — clip not written
    TIMEOUT_NOTICE = 'TIMEOUT_NOTICE'    # not a result; still watching


@dataclass
class SaveOperation:
    """Everything known about one save. No loose booleans that can disagree."""
    op_id: int
    output_path: str
    duration_seconds: int
    requested_at: float
    ack_deadline: float
    completion_deadline: float
    state: SaveState = SaveState.REQUESTED
    error_detail: str = ''
    #: True once a COMPLETED or FAILED outcome has been handed to the UI. This
    #: is the single guard behind the "at most one terminal result" invariant.
    result_emitted: bool = False
    accepted_at: Optional[float] = None
    finished_at: Optional[float] = None
    timed_out_at: Optional[float] = None
    #: Caller payload (mic window timestamp, duration label, …) carried through
    #: so the completion handler does not have to reconstruct request context.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_in_flight(self) -> bool:
        return self.state in _IN_FLIGHT

    @property
    def ack_latency_ms(self) -> Optional[float]:
        if self.accepted_at is None:
            return None
        return (self.accepted_at - self.requested_at) * 1000.0

    @property
    def total_latency_ms(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.requested_at) * 1000.0


@dataclass
class Outcome:
    """One thing that happened, to be acted on exactly once."""
    kind: OutcomeKind
    operation: SaveOperation
    detail: str = ''
    #: True when this result arrived after the operation had already been
    #: reported as timed out. The UI must not treat it as a second event for
    #: an already-reported save — it is the *first* result for that save.
    late: bool = False


class RejectReason(Enum):
    BUSY = 'BUSY'            # single-flight: another save is in flight


@dataclass
class SubmitResult:
    """Return value of submit(). `accepted` says only that the command was
    handed over locally — never that a clip exists."""
    accepted: bool
    operation: Optional[SaveOperation] = None
    reason: Optional[RejectReason] = None


#: How long the engine gets to say *anything* before we warn the user. Matches
#: the ceiling the old busy-wait used, so the visible behaviour is unchanged.
DEFAULT_ACK_TIMEOUT_S = 1.0

#: How long a timed-out operation stays under observation before we give up and
#: call it failed. A long clip on a slow encoder legitimately takes seconds;
#: killing the operation at the ack deadline is what used to lose those saves.
DEFAULT_COMPLETION_TIMEOUT_S = 60.0


class SaveStateMachine:
    """Track one save at a time.

    Call submit after writing the command, on_event for a response, and on_tick
    when none arrives. Each call returns at most one Outcome for the poller.
    """

    def __init__(self,
                 ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
                 completion_timeout_s: float = DEFAULT_COMPLETION_TIMEOUT_S,
                 logger=None):
        self._ack_timeout_s = ack_timeout_s
        self._completion_timeout_s = completion_timeout_s
        self._log = logger or (lambda msg: print(f'[SaveState] {msg}'))
        self._next_op_id = 1
        #: The operation the engine is currently working on, or the most recent
        #: one still awaiting a verdict after a timeout. None means nothing is
        #: attributable and any response we see is unexpected.
        self._current: Optional[SaveOperation] = None

    # introspection

    @property
    def state(self) -> SaveState:
        return self._current.state if self._current else SaveState.IDLE

    @property
    def current(self) -> Optional[SaveOperation]:
        return self._current

    def is_busy(self) -> bool:
        """True while a save is in flight and a second one must be refused.

        A timed-out operation is deliberately *not* busy: it is still watched
        for a late result, but the user must be able to try again. That keeps
        a wedged engine from locking the hotkey out for a full minute.
        """
        # `result_emitted` must be checked too, not just the state: a cancelled
        # operation keeps its last state (ENGINE_ACCEPTED, say) but is no
        # longer owed anything. Without this an abandoned save would block
        # every later save for the rest of the session.
        return (self._current is not None
                and self._current.is_in_flight
                and not self._current.result_emitted)

    # submission

    def submit(self, output_path: str, duration_seconds: int, now: float,
               **context) -> SubmitResult:
        """Register a save after its command is written to shared memory.

        If a save is already in flight, return accepted=False with RejectReason.BUSY
        without creating a second operation.
        """
        if self.is_busy():
            self._log(f'Rejected save (single-flight, current='
                      f'{self._current.state.value}): {_short(output_path)}')
            return SubmitResult(accepted=False, reason=RejectReason.BUSY)

        # Scale completion deadline with duration: minimum 60s, plus 0.25s per clip second
        # (e.g., 30m / 1800s clip gets 450s completion deadline instead of timing out at 60s)
        completion_timeout = max(self._completion_timeout_s, duration_seconds * 0.25)
        op = SaveOperation(
            op_id=self._next_op_id,
            output_path=output_path,
            duration_seconds=duration_seconds,
            requested_at=now,
            ack_deadline=now + self._ack_timeout_s,
            completion_deadline=now + completion_timeout,
            context=dict(context),
        )
        self._next_op_id += 1
        self._current = op
        self._log(f'Save #{op.op_id} requested: {_short(output_path)} '
                  f'({duration_seconds}s)')
        return SubmitResult(accepted=True, operation=op)

    # events

    def on_event(self, event: EngineEvent, detail: str, now: float) -> Optional[Outcome]:
        """Interpret one engine response. Returns an Outcome to act on, or None.

        The caller must have read `engine_response` and `engine_string`
        together, as one event, before calling this — and must consume the
        response only after this returns.
        """
        op = self._current

        if op is None or op.result_emitted:
            # Nothing to attribute this to. Do not silently drop it: a response
            # with no owner means either an engine that answered a command we
            # did not send, or a save whose result we already reported.
            self._log(f'Unexpected {event.value} with no operation to own it '
                      f'(detail={detail!r}) — consumed and ignored')
            return None

        if event is EngineEvent.SAVE_STARTED:
            if op.state is SaveState.ENGINE_ACCEPTED:
                # Duplicate ack across ticks. Harmless, not worth an outcome.
                return None
            if op.state is SaveState.TIMED_OUT:
                # Engine woke up after we warned. Keep watching for the result;
                # do not resurrect it into an "accepted" UI state, the user has
                # already been told this one is slow.
                self._log(f'Save #{op.op_id}: late SAVE_STARTED after timeout')
                return None
            return self._transition(op, SaveState.ENGINE_ACCEPTED, now,
                                    OutcomeKind.ACCEPTED)

        if event is EngineEvent.CLIP_SAVED:
            # NOTE the missing SAVE_STARTED case: on Linux the engine runs
            # SaveClip synchronously and overwrites SAVE_STARTED with
            # CLIP_SAVED before we ever poll. WAITING_FOR_ACK -> COMPLETED
            # directly is the normal fast path, not an anomaly (AUDIT-015).
            late = op.state is SaveState.TIMED_OUT
            op.finished_at = now
            outcome = self._transition(op, SaveState.ENGINE_COMPLETED, now,
                                       OutcomeKind.COMPLETED, detail=detail,
                                       late=late)
            if late:
                self._log(f'Save #{op.op_id}: late success after timeout '
                          f'({op.total_latency_ms:.0f} ms)')
            return outcome

        if event is EngineEvent.ERROR_OCCURRED:
            late = op.state is SaveState.TIMED_OUT
            op.finished_at = now
            op.error_detail = detail
            if late:
                self._log(f'Save #{op.op_id}: late failure after timeout')
            return self._transition(op, SaveState.ENGINE_FAILED, now,
                                    OutcomeKind.FAILED, detail=detail, late=late)

        return None

    def on_tick(self, now: float) -> Optional[Outcome]:
        """Advance deadlines. Call once per poll when no response was read."""
        op = self._current
        if op is None or op.result_emitted:
            return None

        if op.state is SaveState.REQUESTED:
            op.state = SaveState.WAITING_FOR_ACK

        if op.state in (SaveState.WAITING_FOR_ACK, SaveState.REQUESTED):
            if now >= op.ack_deadline:
                op.state = SaveState.TIMED_OUT
                op.timed_out_at = now
                self._log(f'Save #{op.op_id}: no acknowledgement within '
                          f'{self._ack_timeout_s:.1f}s — still watching for a '
                          f'late result')
                # Not a result: the operation stays alive until the completion
                # deadline so a slow engine can still be honoured.
                return Outcome(kind=OutcomeKind.TIMEOUT_NOTICE, operation=op)

        if now >= op.completion_deadline:
            op.finished_at = now
            op.error_detail = op.error_detail or (
                'The capture engine never reported whether the clip was '
                'written.')
            self._log(f'Save #{op.op_id}: giving up after '
                      f'{self._completion_timeout_s:.0f}s')
            return self._transition(op, SaveState.ENGINE_FAILED, now,
                                    OutcomeKind.FAILED,
                                    detail=op.error_detail)
        return None

    def cancel_active(self, now: float, reason: str = 'shutdown') -> Optional[Outcome]:
        """Abandon the in-flight operation without emitting a UI result.

        Used at shutdown: the save may well complete, we simply stop caring.
        Marking the result as emitted keeps the invariant intact if a stray
        response is read during teardown.
        """
        op = self._current
        if op is None or op.result_emitted:
            return None
        self._log(f'Save #{op.op_id}: abandoned ({reason})')
        op.result_emitted = True
        op.finished_at = now
        return None

    # internals

    def _transition(self, op: SaveOperation, new_state: SaveState, now: float,
                    kind: OutcomeKind, detail: str = '',
                    late: bool = False) -> Outcome:
        old = op.state
        op.state = new_state

        if new_state is SaveState.ENGINE_ACCEPTED:
            op.accepted_at = now
            timing = f' (ack={op.ack_latency_ms:.0f} ms)'
        elif op.total_latency_ms is not None:
            timing = f' (total={op.total_latency_ms:.0f} ms)'
        else:
            timing = ''

        self._log(f'Save #{op.op_id} state: {old.value} -> {new_state.value}{timing}')

        if kind in (OutcomeKind.COMPLETED, OutcomeKind.FAILED):
            # The invariant, enforced in exactly one place: a result is emitted
            # once. Every later event for this operation falls into the
            # "no operation to own it" branch above.
            op.result_emitted = True

        return Outcome(kind=kind, operation=op, detail=detail, late=late)


def _short(path: str) -> str:
    """Log the file name, not the full path — clip folders carry game names
    and the user profile directory carries a real name."""
    if not path:
        return '<no path>'
    return path.replace('\\', '/').rsplit('/', 1)[-1]
