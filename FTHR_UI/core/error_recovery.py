"""Monotonic recovery evidence for keyed, recoverable UI incidents.

The tracker deliberately consumes status-poll facts only. A single frame or a
wall-clock delay cannot clear an incident: the same generation must show
repeated progress for a sustained interval.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from collections.abc import Callable


@dataclass
class _ProgressWindow:
    process_generation: int
    backend_generation: int
    last_frame_count: int
    started_at: float | None = None
    progressing_polls: int = 0


class RecoveryTracker:
    """Require stable, repeated frame progress before clearing an incident."""

    MIN_PROGRESS_POLLS = 3
    STABLE_FOR_SECONDS = 3.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, _ProgressWindow] = {}

    def reset(self, incident_key: str | None = None) -> None:
        """Reset one incident window, or all windows when key is omitted."""

        if incident_key is None:
            self._windows.clear()
        else:
            self._windows.pop(str(incident_key), None)

    def observe(
        self,
        incident_key: str,
        *,
        connected: bool,
        process_generation: int,
        backend_generation: int,
        frame_count: int,
        health_state: str,
        now: float | None = None,
        allow_content_suspect: bool = False,
    ) -> bool:
        """Return true only after eligible stable progress for this incident.

        Failure, recovery, initialization, disconnect, content suspicion (by
        default), generation changes, counter decreases, and non-progressing
        polls all reset the stable window. Content suspicion may be accepted
        only by callers recovering a capture-backend incident; it is never
        sufficient evidence for encoder-init or content-quality incidents.
        """

        key = str(incident_key)
        state = str(getattr(health_state, 'value', health_state)).lower()
        eligible_states = {'healthy'}
        if allow_content_suspect:
            eligible_states.add('content_suspect')
        if not connected or state not in eligible_states:
            self._windows.pop(key, None)
            return False

        timestamp = self._clock() if now is None else float(now)
        process_gen = int(process_generation)
        backend_gen = int(backend_generation)
        count = max(0, int(frame_count))
        window = self._windows.get(key)
        if (window is None
                or window.process_generation != process_gen
                or window.backend_generation != backend_gen
                or count < window.last_frame_count):
            self._windows[key] = _ProgressWindow(
                process_generation=process_gen,
                backend_generation=backend_gen,
                last_frame_count=count,
            )
            return False

        if count <= window.last_frame_count:
            window.started_at = None
            window.progressing_polls = 0
            return False

        window.last_frame_count = count
        if window.started_at is None:
            window.started_at = timestamp
            window.progressing_polls = 1
            return False

        window.progressing_polls += 1
        if (window.progressing_polls >= self.MIN_PROGRESS_POLLS
                and timestamp - window.started_at >= self.STABLE_FOR_SECONDS):
            self._windows.pop(key, None)
            return True
        return False
