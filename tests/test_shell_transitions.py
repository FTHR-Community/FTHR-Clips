"""Shell animation interruption, cleanup, and restart-status regressions."""

from types import SimpleNamespace

import pytest

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from ui.shell_transitions import ShellPageStack, SnapshotFlight, SnapshotSlide


def test_page_slide_handles_reversal_resize_and_hidden_navigation(qtbot):
    stack = ShellPageStack()
    qtbot.addWidget(stack)
    stack.addWidget(QLabel('Clips'))
    stack.addWidget(QLabel('Settings'))
    stack.resize(400, 300)
    stack.show()
    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1
    assert stack._slide._direction == 1
    first_slide = stack._slide
    stack.setCurrentIndex(0)
    assert not first_slide.isVisible()
    assert stack._slide._direction == -1
    qtbot.waitUntil(lambda: stack._slide is None)
    assert stack.currentWidget().isVisible()
    assert stack.currentWidget().pos() == QPoint(0, 0)
    stack.setCurrentIndex(1)
    stack.resize(500, 400)
    assert stack._slide is None
    stack.hide()
    stack.setCurrentIndex(0)
    assert stack._slide is None
    assert stack.currentIndex() == 0


def test_editor_snapshot_reaches_sidebar_and_releases_frame(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    pixmap = QPixmap(400, 300)
    pixmap.fill()
    target = QRect(10, 100, 78, 78)
    flight = SnapshotFlight(owner, pixmap, QRect(0, 0, 400, 300), target)
    completed = []
    deleted = []
    canvas = flight.geometry()
    flight._animation.finished.connect(
        lambda: completed.append(flight._frame_rect.translated(canvas.topLeft())))
    flight.destroyed.connect(lambda: deleted.append(True))
    flight.start()
    qtbot.wait(60)
    assert flight.geometry() == canvas
    qtbot.waitUntil(lambda: bool(deleted))
    assert completed == [target]


@pytest.mark.parametrize('dpr', [1.0, 2.0])
def test_compact_editor_flight_bounds_native_surface_and_reaches_sidebar(qtbot, dpr):
    owner = QWidget()
    qtbot.addWidget(owner)
    pixmap = QPixmap(3840, 2160)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(QColor('white'))
    start = QRect(-1920, 0, 3840, 2160)
    target = QRect(-1800, 100, 78, 78)
    flight = SnapshotFlight(owner, pixmap, start, target, compact=True)
    qtbot.addWidget(flight)
    surface_size = flight.size()
    assert surface_size.width() <= 560 and surface_size.height() <= 350
    assert flight._pixmap.width() <= 560 * dpr
    assert flight._pixmap.height() <= 350 * dpr
    assert flight._pixmap.devicePixelRatio() == dpr
    for time in (0, 55, 110, 165, 220):
        flight._animation.setCurrentTime(time)
        assert flight.size() == surface_size
        assert flight.rect().contains(flight._frame_rect)
    assert flight._frame_rect.translated(flight.pos()) == target
    assert flight._opacity == 0.0


@pytest.mark.parametrize('expanding', [False, True])
def test_editor_flight_renders_a_fade_while_scaling(qtbot, expanding):
    owner = QWidget()
    qtbot.addWidget(owner)
    pixmap = QPixmap(400, 300)
    pixmap.fill(QColor('white'))
    small, large = QRect(10, 100, 78, 78), QRect(0, 0, 400, 300)
    start, end = (small, large) if expanding else (large, small)
    flight = SnapshotFlight(owner, pixmap, start, end)
    qtbot.addWidget(flight)
    flight._animation.setCurrentTime(flight._animation.duration() // 2)
    frame = flight.grab().toImage()
    center = flight._frame_rect.center()
    assert 120 <= frame.pixelColor(center).alpha() <= 135
    flight._animation.setCurrentTime(flight._animation.duration())
    frame = flight.grab().toImage()
    assert frame.pixelColor(flight._frame_rect.center()).alpha() == (255 if expanding else 0)


def test_settings_category_slide_cleans_up_on_reversal_and_resize(qtbot):
    from main import SlidingStackedWidget

    stack = SlidingStackedWidget()
    qtbot.addWidget(stack)
    for name in ('General', 'Clip', 'Audio'):
        stack.addWidget(QLabel(name))
    stack.resize(500, 400)
    stack.show()
    stack.setCurrentIndex(2)
    assert stack._slide._horizontal
    assert stack._slide._direction == 1
    old_slide = stack._slide
    stack.setCurrentIndex(0)
    assert not old_slide.isVisible()
    assert stack._slide._direction == -1
    qtbot.waitUntil(lambda: stack._slide is None)
    stack.setCurrentIndex(1)
    stack.resize(600, 450)
    assert stack._slide is None
    assert stack.currentWidget().size() == stack.size()
    stack.hide()
    stack.setCurrentIndex(2)
    assert stack._slide is None


@pytest.mark.parametrize('direction', [-1, 1])
def test_horizontal_slide_moves_outgoing_and_incoming_in_arrow_direction(qtbot, direction):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(400, 300)
    owner.show()
    outgoing, incoming = QPixmap(400, 300), QPixmap(400, 300)
    outgoing.fill(QColor('red'))
    incoming.fill(QColor('blue'))
    slide = SnapshotSlide(owner, outgoing, incoming, direction, horizontal=True)
    slide.show()
    slide._advance(0.5)
    frame = slide.grab().toImage()
    assert frame.pixelColor(50, 150) == QColor('red' if direction > 0 else 'blue')
    assert frame.pixelColor(350, 150) == QColor('blue' if direction > 0 else 'red')


def test_restart_shortcut_status_survives_handshake_and_resets(qtbot, monkeypatch):
    from main import MainWindow

    label = QLabel()
    qtbot.addWidget(label)
    host = SimpleNamespace(
        status_label=label, _manual_record_state='idle',
        _background_ui_paused=False, _capture_settings_applying=False,
    )
    host._set_status = lambda text, style: MainWindow._set_status(host, text, style)
    host._capture_pending_text = lambda: MainWindow._capture_pending_text(host)
    host._set_capture_apply_state = lambda state: MainWindow._set_capture_apply_state(host, state)
    host._restart_capture_engine = lambda: host._set_capture_apply_state(True)
    MainWindow._restart_engine_shortcut(host)
    assert label.text() == 'Restarting engine'
    host._set_status('CONNECTING 1/10', '')
    assert label.text() == 'Restarting engine'
    host._set_status('STARTING CAPTURE', '')
    assert label.text() == 'Restarting engine'
    host._set_capture_apply_state(False)
    host._set_status('CAPTURING', '')
    assert label.text() == 'Capturing'
    host._set_capture_apply_state(True)
    assert label.text() == 'Applying settings'
    host._set_capture_apply_state(False)
    host._manual_record_state = 'recording'
    MainWindow._restart_engine_shortcut(host)
    assert not host._engine_restart_requested


def test_apply_timeout_unlocks_controls_and_invalidates_pending_launch(qtbot):
    from main import MainWindow
    from core.capture_settings import ApplyStatus, CaptureConfigTracker
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton

    owner = QWidget()
    qtbot.addWidget(owner)
    button = QPushButton(owner)
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.setInterval(20)
    errors = []
    stopped = []
    host = SimpleNamespace(
        _capture_settings_applying=False, _capture_apply_timer=timer,
        _capture_config=CaptureConfigTracker(), _restart_pending=True,
        _pending_launch_config=object(), _pending_launch_generation=12,
        _engine_gen=12, _manual_record_state='idle',
        cap_settings_popup=SimpleNamespace(
            set_apply_state=lambda applying: button.setEnabled(not applying)),
        _set_status=lambda *_: None, _capture_pending_text=lambda: 'APPLYING SETTINGS',
        push_error=lambda *args, **kwargs: errors.append(args),
        _restart_capture_engine=lambda: None,
    )
    host._set_capture_apply_state = lambda state: MainWindow._set_capture_apply_state(host, state)

    def stop():
        stopped.append(True)
        host._engine_gen += 1

    host.stop_engine = stop
    timer.timeout.connect(lambda: MainWindow._capture_apply_timed_out(host))
    host._set_capture_apply_state(True)
    assert not button.isEnabled()
    qtbot.waitUntil(lambda: not host._capture_settings_applying)
    assert stopped == [] and host._engine_gen == 12
    assert button.isEnabled() and not timer.isActive()
    assert host._pending_launch_config is None
    assert host._pending_launch_generation is None
    assert not host._restart_pending
    assert host._capture_config.status is ApplyStatus.FAILED
    assert errors[0][0] == 'SETTINGS APPLY TIMED OUT'
    # A completed attempt cancels the deadline; later delivery is harmless.
    host._set_capture_apply_state(True)
    host._set_capture_apply_state(False)
    MainWindow._capture_apply_timed_out(host)
    assert stopped == []
