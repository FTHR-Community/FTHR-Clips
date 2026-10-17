from __future__ import annotations

import threading

import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from ui import clip_viewer


class _Capture:
    def __init__(self, frame=None, entered=None, gate=None, released=None):
        self.frame = frame
        self.entered = entered
        self.gate = gate
        self.released = released or threading.Event()

    def read(self):
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.frame is None:
            return False, None
        return True, self.frame

    def release(self):
        self.released.set()


def _frame():
    return np.full((48, 96, 3), (30, 120, 220), dtype=np.uint8)


def _install_capture(monkeypatch, *, frame=None, entered=None, gate=None):
    capture = _Capture(frame, entered, gate)
    monkeypatch.setattr(clip_viewer.cv2, 'VideoCapture', lambda _path: capture)
    return capture


def test_crop_preview_decodes_off_thread_and_enables_controls_when_ready(
        qtbot, monkeypatch):
    entered = threading.Event()
    gate = threading.Event()
    capture = _install_capture(
        monkeypatch, frame=_frame(), entered=entered, gate=gate)

    try:
        dialog = clip_viewer.CropDialog('slow.mp4', initial_crop=(12, 8, 60, 30))
        qtbot.addWidget(dialog)
        assert all(not button.isEnabled() for button in dialog._crop_controls)

        # The fake decoder is held inside read(); a timer still fires on the GUI
        # thread while it is blocked, proving initialization did not decode inline.
        assert entered.wait(2)
        ticks = []
        timer = QTimer(dialog)
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        qtbot.waitUntil(lambda: len(ticks) >= 2, timeout=1000)
        assert dialog._pixmap is None

        gate.set()
        qtbot.waitUntil(lambda: dialog._pixmap is not None, timeout=2000)
        assert (dialog._src_w, dialog._src_h) == (96, 48)
        assert dialog.crop_rect == (12, 8, 60, 30)
        assert all(button.isEnabled() for button in dialog._crop_controls)
    finally:
        gate.set()
        assert capture.released.wait(2)


def test_crop_preview_decode_failure_keeps_crop_controls_disabled(
        qtbot, monkeypatch):
    capture = _install_capture(monkeypatch)
    dialog = clip_viewer.CropDialog('broken.mp4')
    qtbot.addWidget(dialog)

    try:
        qtbot.waitUntil(
            lambda: dialog.frame_label.text() == 'Could not load frame',
            timeout=2000)
        assert dialog._pixmap is None
        assert all(not button.isEnabled() for button in dialog._crop_controls)
    finally:
        assert capture.released.wait(2)


def test_crop_preview_ignores_late_result_after_close(qtbot, monkeypatch):
    entered = threading.Event()
    gate = threading.Event()
    capture = _install_capture(
        monkeypatch, frame=_frame(), entered=entered, gate=gate)

    try:
        dialog = clip_viewer.CropDialog('slow.mp4')
        qtbot.addWidget(dialog)
        assert entered.wait(2)
        generation = dialog._frame_generation
        worker = dialog._frame_worker

        dialog.reject()
        assert dialog._frame_cancelled.is_set()
        image = QImage(96, 48, QImage.Format.Format_RGB32)
        image.fill(0xFF336699)
        worker.signals.ready.emit((generation, image, None))
        qtbot.wait(20)

        assert dialog._pixmap is None
        assert all(not button.isEnabled() for button in dialog._crop_controls)
    finally:
        gate.set()
        assert capture.released.wait(2)


def test_crop_preview_destruction_cancels_pending_decode(qapp, monkeypatch):
    shiboken6 = pytest.importorskip('shiboken6')
    entered = threading.Event()
    gate = threading.Event()
    capture = _install_capture(
        monkeypatch, frame=_frame(), entered=entered, gate=gate)
    dialog = clip_viewer.CropDialog('slow.mp4')
    cancelled = dialog._frame_cancelled
    worker = dialog._frame_worker
    generation = dialog._frame_generation

    try:
        assert entered.wait(2)
        shiboken6.delete(dialog)
        assert cancelled.is_set()
        image = QImage(96, 48, QImage.Format.Format_RGB32)
        worker.signals.ready.emit((generation, image, None))
    finally:
        gate.set()
        assert capture.released.wait(2)
