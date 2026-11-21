"""Opt-in UI save markers on the Windows QueryPerformanceCounter clock."""

import atexit
from collections import deque
import json
import os
import sys
from threading import get_native_id
from time import perf_counter_ns


_TRACE_PATH = os.environ.get('FTHR_UI_TRACE_PATH') if sys.platform == 'win32' else None
SAVE_TRACE_ENABLED = bool(_TRACE_PATH)
_CAPACITY = 8192
_events = deque(maxlen=_CAPACITY) if SAVE_TRACE_ENABLED else None


def mark_save_trace(event, *, save_id=None, **details):
    if _events is not None:
        _events.append((perf_counter_ns(), get_native_id(), event, save_id, details))


def _write_trace():
    # Save-time work stays in memory and never performs file I/O on the UI
    # thread. A forced process exit can lose the trace.
    try:
        events = _events.copy()
        with open(_TRACE_PATH, 'x', encoding='utf-8') as output:
            pid = os.getpid()
            for qpc_ns, tid, event, save_id, details in events:
                record = dict(qpc_ns=qpc_ns, pid=pid, tid=tid,
                              event=event,
                              save_id=str(save_id) if save_id is not None else None,
                              **details)
                output.write(json.dumps(record, separators=(',', ':')) + '\n')
        if len(events) == _CAPACITY:
            print(f'[SaveTrace] Trace reached capacity ({_CAPACITY} events)', file=sys.stderr)
    except OSError as error:
        print(f'[SaveTrace] Could not export UI save trace: {error}', file=sys.stderr)


if SAVE_TRACE_ENABLED:
    atexit.register(_write_trace)
