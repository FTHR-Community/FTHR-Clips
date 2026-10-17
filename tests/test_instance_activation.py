from __future__ import annotations

from contextlib import suppress
import uuid
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from core.instance_activation import (
    InstanceActivationServer,
    request_existing_instance_activation,
)


def test_second_launch_can_request_existing_window_activation(qtbot):
    server = InstanceActivationServer(f'FTHRClips_Test_{uuid.uuid4().hex}')
    received: list[bool] = []
    server.activation_requested.connect(lambda: received.append(True))

    assert server.start()
    try:
        ui_root = Path(__file__).resolve().parents[1] / 'FTHR_UI'
        child_code = (
            'import sys\n'
            f'sys.path.insert(0, {str(ui_root)!r})\n'
            'from PySide6.QtCore import QCoreApplication\n'
            'app = QCoreApplication([])\n'
            'from core.instance_activation import request_existing_instance_activation\n'
            "print('FTHR_INSTANCE_TEST_READY', flush=True)\n"
            "if sys.stdin.readline().strip() != 'GO':\n"
            '    raise SystemExit(2)\n'
            f'raise SystemExit(0 if request_existing_instance_activation({server._name!r}) else 1)\n'
        )
        child = subprocess.Popen(
            [sys.executable, '-c', child_code],
            env={**os.environ, 'PYTHONPATH': str(ui_root)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output: queue.Queue[str] = queue.Queue()

        def drain_child_output() -> None:
            assert child.stdout is not None
            for line in child.stdout:
                output.put(line.rstrip('\n'))

        output_reader = threading.Thread(
            target=drain_child_output,
            name='fthr-instance-test-output',
            daemon=True,
        )
        output_reader.start()
        try:
            # Importing PySide6 and the application module is intentionally
            # outside the IPC completion bound below.  This keeps the test's
            # 2-second contract about the activation request itself rather
            # than machine-dependent child startup scheduling.
            ready = False

            def child_is_ready() -> bool:
                nonlocal ready
                while not ready:
                    try:
                        ready = output.get_nowait() == 'FTHR_INSTANCE_TEST_READY'
                    except queue.Empty:
                        return False
                return True

            qtbot.waitUntil(child_is_ready, timeout=10000)
            assert child.stdin is not None
            child.stdin.write('GO\n')
            child.stdin.flush()
            qtbot.waitUntil(lambda: child.poll() is not None, timeout=2000)
            assert child.returncode == 0
            qtbot.waitUntil(lambda: received == [True], timeout=1000)
        finally:
            if child.poll() is None:
                if child.stdin is not None:
                    with suppress(OSError):
                        child.stdin.close()
                if child.poll() is None:
                    with suppress(ProcessLookupError):
                        child.kill()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        child.kill()
                    child.wait(timeout=2)
            output_reader.join(timeout=1)
            if child.stdin is not None and not child.stdin.closed:
                with suppress(OSError):
                    child.stdin.close()
            if child.stdout is not None:
                with suppress(OSError):
                    child.stdout.close()
    finally:
        server.stop()


def test_activation_request_fails_bounded_when_no_instance_owns_the_endpoint():
    assert not request_existing_instance_activation(
        f'FTHRClips_Missing_{uuid.uuid4().hex}', timeout_ms=10)
