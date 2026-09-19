"""
Integration tests for FTHRclips backend auto-detection.
Requires the binary to be built at FTHRcapture_linux/build/FTHRclips.
Skips gracefully if binary is missing.
"""
import subprocess
import sys
import time
import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux',
    reason="Linux capture backend integration tests (ELF binary)",
)

BINARY = Path(__file__).parent.parent / 'FTHRcapture_linux/build/FTHRclips'
KNOWN_BACKENDS = ['wlr-screencopy', 'ext-image-copy-capture-v1', 'ScreenCast portal']


def test_binary_exists():
    assert BINARY.exists(), f"Binary not built: {BINARY}"


def test_backend_auto_detects():
    """Engine must start and log a known backend within 3 seconds."""
    if not BINARY.exists():
        pytest.skip("Binary not built")
    if not os.environ.get('WAYLAND_DISPLAY'):
        pytest.skip('Public alpha backends require a real Wayland session')

    proc = subprocess.Popen(
        [str(BINARY), '30', '5', '1280', '720', '4000', '0',
         '', '0', '', '0', '4', '0', '0'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(2.5)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=3)
        combined = stdout + stderr
        detected = any(b in combined for b in KNOWN_BACKENDS)
        assert detected, (
            f"No known backend in output.\nstdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )
        assert 'No capture backend available' not in combined
    finally:
        proc.kill()
        proc.wait()


def test_unreachable_pure_x11_display_exits_bounded():
    if not BINARY.exists():
        pytest.skip('Binary not built')
    env = dict(os.environ)
    env.pop('WAYLAND_DISPLAY', None)
    env['DISPLAY'] = ':9876'
    proc = subprocess.Popen(
        [str(BINARY), '30', '5', '0', '0', '4000', '0',
         '0', '0', '0', '@x11:0,0,640,480', '0', '4', '0', '0'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=8)
        combined = stdout + stderr
        assert ('avformat_open_input failed' in combined or
                'No capture backend available' in combined)
        assert 'Using x11grab' not in combined
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail('Engine hung while opening an unreachable X11 display')


def test_no_backend_exits_cleanly():
    """Engine must exit cleanly when neither DISPLAY nor WAYLAND_DISPLAY is set."""
    if not BINARY.exists():
        pytest.skip("Binary not built")

    env = {k: v for k, v in os.environ.items()
           if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
    proc = subprocess.Popen(
        [str(BINARY), '30', '5', '0', '0', '4000', '0', '', '0', '', '0', '4', '0', '0'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        _, stderr = proc.communicate(timeout=8)
        # Must exit (not hang) and report failure gracefully
        assert proc.returncode is not None, "Process did not exit"
        assert ('No capture backend' in stderr or
                'wl_display_connect failed' in stderr or
                'DISPLAY not set' in stderr), \
            f"Expected graceful failure message. stderr: {stderr[:300]}"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("Engine hung instead of exiting cleanly with no display")
