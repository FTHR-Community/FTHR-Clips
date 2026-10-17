"""One cancellable, bounded subprocess runner for optional media work."""

import subprocess
import sys
import threading
import time

_MEDIA_PROCESS_LOCK = threading.Lock()
_MEDIA_PROCESS_OWNERS: dict[int, subprocess.Popen] = {}
_MEDIA_PROCESS_PEAK = 0
# Interactive frame/probe requests bypass this budget. Optional filmstrips
# and proxy encodes share two slots and run below normal priority on Windows.
# Separate lanes prevent a long proxy from starving timeline thumbnails.
_BACKGROUND_MEDIA_SLOT = threading.Semaphore(2)
_BACKGROUND_MEDIA_LANES = {
    'proxy': threading.Semaphore(1), 'timeline': threading.Semaphore(1),
}


def _media_process_started(process: subprocess.Popen) -> None:
    global _MEDIA_PROCESS_PEAK
    with _MEDIA_PROCESS_LOCK:
        _MEDIA_PROCESS_OWNERS[id(process)] = process
        _MEDIA_PROCESS_PEAK = max(
            _MEDIA_PROCESS_PEAK, len(_MEDIA_PROCESS_OWNERS))


def _media_process_finished(process: subprocess.Popen) -> None:
    with _MEDIA_PROCESS_LOCK:
        _MEDIA_PROCESS_OWNERS.pop(id(process), None)


def media_process_snapshot() -> tuple[int, int]:
    with _MEDIA_PROCESS_LOCK:
        return len(_MEDIA_PROCESS_OWNERS), _MEDIA_PROCESS_PEAK


def _stop_owned_process(process: subprocess.Popen, *, timeout: float = 0.75,
                        drain: bool = True) -> None:
    """Terminate, then kill and reap one library-owned media child.

    ``poll()`` only observes the child.  It does not guarantee that the
    process handle has been waited on, so cleanup must always run the bounded
    wait path, including when the child exited between two polling turns.
    """
    try:
        running = process.poll() is None
    except (AttributeError, OSError):
        running = True
    if running:
        try:
            process.terminate()
        except (AttributeError, OSError):
            # The child may exit between poll and terminate; still reap it.
            pass
    try:
        process.wait(timeout=timeout)
    except (AttributeError, OSError, subprocess.SubprocessError, TimeoutError):
        # A child that ignores terminate is handled by the kill fallback.
        try:
            process.kill()
        except (AttributeError, OSError):
            # A concurrent child exit makes kill unnecessary; still reap below.
            pass
        try:
            process.wait(timeout=timeout)
        except (AttributeError, OSError, subprocess.SubprocessError, TimeoutError):
            # The final wait remains bounded; the ownership register is
            # released only after this synchronous cleanup attempt returns.
            pass

    # Drain any pipe data after wait() has reaped the child.  This is separate
    # from the bounded process wait so a decoder that writes a large payload
    # cannot make cancellation depend on an unbounded pipe read.
    if not drain:
        return
    try:
        process.communicate(timeout=0.05)
    except (AttributeError, OSError, subprocess.SubprocessError, TimeoutError):
        # The bounded wait/kill sequence already completed; optional pipe
        # draining must not turn thumbnail cancellation into an app failure.
        pass


def run_media_process(
        command: list[str], cancel_event: threading.Event, *,
        timeout_seconds: float = 8.0, background: bool | str = False,
        on_stdout=None) -> tuple[int, bytes, bytes] | None:
    acquired = False
    lane = _BACKGROUND_MEDIA_LANES.get(background)
    lane_acquired = False
    if lane is not None:
        while not cancel_event.is_set():
            if lane.acquire(timeout=0.05):
                lane_acquired = True
                break
        if not lane_acquired:
            return None
    if background:
        while not cancel_event.is_set():
            if _BACKGROUND_MEDIA_SLOT.acquire(timeout=0.05):
                acquired = True
                break
        if not acquired:
            if lane_acquired:
                lane.release()
            return None
    try:
        if cancel_event.is_set():
            return None
        return _run_media_process(command, cancel_event,
                                  timeout_seconds=timeout_seconds,
                                  background=background, on_stdout=on_stdout)
    finally:
        if acquired:
            _BACKGROUND_MEDIA_SLOT.release()
        if lane_acquired:
            lane.release()


def _stream_media_process(process, cancel_event, deadline, on_stdout):
    """Deliver bounded raw-frame chunks without buffering the whole filmstrip."""
    stderr_tail = bytearray()
    errors = []

    def read_output():
        try:
            while True:
                chunk = process.stdout.read(54_000)
                if not chunk:
                    break
                if not cancel_event.is_set():
                    on_stdout(chunk)
        except Exception as error:
            errors.append(error)
        finally:
            process.stdout.close()

    def read_errors():
        try:
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                stderr_tail.extend(chunk)
                del stderr_tail[:-32_768]
        except (OSError, ValueError) as error:
            errors.append(error)
        finally:
            process.stderr.close()

    readers = [threading.Thread(target=target, daemon=True)
               for target in (read_output, read_errors)]
    for reader in readers:
        reader.start()
    completed = False
    try:
        while not cancel_event.is_set() and time.monotonic() < deadline and not errors:
            try:
                process.wait(timeout=0.1)
                completed = True
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        _stop_owned_process(process, drain=False)
        # The child is reaped; its pipes reach EOF. Callbacks finish before
        # ownership and the shared decoder slot are released.
        for reader in readers:
            reader.join(timeout=1.0)
            if reader.is_alive():
                errors.append(RuntimeError('media pipe reader did not finish'))
    if completed and not cancel_event.is_set() and not errors:
        return process.returncode or 0, b'', bytes(stderr_tail)
    return None


def _run_media_process(command, cancel_event, *, timeout_seconds,
                       background, on_stdout):
    """Run ffprobe/ffmpeg with polling cancellation and bounded stderr."""
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) \
        if sys.platform == 'win32' else 0
    if background and sys.platform == 'win32':
        creationflags |= getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=creationflags)
    except (OSError, ValueError):
        # Optional enrichment falls back to original media when a child cannot start.
        return None
    _media_process_started(process)
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    try:
        if on_stdout is not None:
            return _stream_media_process(process, cancel_event, deadline, on_stdout)
        while True:
            if cancel_event.is_set() or time.monotonic() >= deadline:
                return None
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                # Keep diagnostics bounded even if an encoder is unusually
                # verbose; stdout is the thumbnail/probe payload and has a
                # separate media-size guard below.
                return process.returncode or 0, stdout, stderr[-32_768:]
            except subprocess.TimeoutExpired:
                continue
    finally:
        # Always perform the reap path.  A non-None poll result can still be
        # an unreaped child, and this is the last point before ownership is
        # removed from the register.
        try:
            _stop_owned_process(process, drain=on_stdout is None)
        finally:
            _media_process_finished(process)
