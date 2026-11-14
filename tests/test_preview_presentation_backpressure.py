from __future__ import annotations

import threading

from PySide6.QtCore import QRunnable, QThreadPool, Qt
from PySide6.QtGui import QImage

from ui import clip_viewer
from ui.clip_viewer import LiveVideoPreview


def _image(width=16, height=9, color=Qt.GlobalColor.red):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def _capture_preview_tasks(monkeypatch):
    started = []

    class _Pool:
        def start(self, task):
            started.append(task)

    monkeypatch.setattr(clip_viewer, '_preview_frame_pool', _Pool)
    return started


def test_preview_progresses_while_the_global_pool_is_saturated(qtbot):
    entered = threading.Event()
    release = threading.Event()

    class Blocker(QRunnable):
        def run(self):
            entered.set()
            release.wait(5)

    pool = QThreadPool.globalInstance()
    previous_limit = pool.maxThreadCount()
    pool.setMaxThreadCount(1)
    pool.start(Blocker())
    try:
        qtbot.waitUntil(entered.is_set, timeout=2000)
        preview = LiveVideoPreview(16, 9)
        qtbot.addWidget(preview)
        preview.resize(160, 90)
        preview.show()
        preview.set_edit_state(None, {'brightness': 20}, 1.0)
        assert preview.publish_initial_decoded_image(0, _image())
        qtbot.waitUntil(lambda: preview._has_real_frame, timeout=1000)
        assert not release.is_set()
        assert clip_viewer._preview_frame_pool().maxThreadCount() == 1
    finally:
        release.set()
        pool.setMaxThreadCount(previous_limit)


def test_reused_preview_waits_for_the_retired_conversion_slot(
        monkeypatch, qtbot, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview.resize(160, 90)
    preview.show()
    started = _capture_preview_tasks(monkeypatch)
    red = _image()
    green = _image(color=Qt.GlobalColor.green)
    assert preview.publish_initial_decoded_image(0, red)
    assert len(started) == 1
    first = started[0]

    preview.shutdown()
    preview._preview_shutdown = False
    preview.video_sink.videoFrameChanged.connect(preview._on_video_frame)
    preview.begin_source_generation()
    assert preview.publish_initial_decoded_image(1, green)
    assert len(started) == 1

    preview._on_preview_result(first.generation, first.serial, red, 1.0, None)
    assert not preview._has_real_frame
    assert len(started) == 2
    latest = started[-1]
    preview._on_preview_result(latest.generation, latest.serial, green, 1.0, None)
    assert preview._prepared_frame.cacheKey() == green.cacheKey()


def test_software_playback_quality_caps_work_and_pause_refreshes_full_viewport(qtbot):
    preview = LiveVideoPreview(2560, 1440)
    qtbot.addWidget(preview)
    preview.resize(1600, 900)
    preview.show()
    preview.set_edit_state((320, 160, 1280, 960), {'contrast': 25}, 1.25)
    viewport = preview._video_display_rect()
    crop = preview._effect_display_rect(viewport)
    preview.set_playback_height_limit(540)
    assert preview.publish_initial_decoded_image(0, _image(2560, 1440))
    qtbot.waitUntil(lambda: preview._has_real_frame)
    assert preview._prepared_frame.height() <= 540

    preview.set_playback_height_limit(None)
    qtbot.waitUntil(lambda: preview._prepared_frame.width() == viewport.width())
    assert preview._video_display_rect() == viewport
    assert preview._effect_display_rect(viewport) == crop


def test_prepared_image_blocks_next_conversion_until_postpaint_drain(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview.resize(160, 90)
    preview.show()

    preview._preview_generation = 4
    preview._frame_serial = 2
    preview._active_preview_request = {'generation': 4, 'serial': 1}
    pending = {'generation': 4, 'serial': 2, 'frame': object()}
    preview._pending_preview_request = pending
    started = _capture_preview_tasks(monkeypatch)

    current = _image(color=Qt.GlobalColor.blue)
    preview._on_preview_result(4, 1, current, 1.0, None)

    # Keep presenting the accepted result, but don't start the queued
    # conversion until this image has actually gone through paintEvent.
    assert preview._prepared_frame.cacheKey() == current.cacheKey()
    assert started == []
    assert preview._pending_preview_request is pending

    # New sink frames replace the one pending request rather than bypassing
    # the prepared-image presentation barrier.
    preview._latest_video_frame = object()
    preview._frame_serial = 3
    preview._enqueue_latest_frame()
    latest = preview._pending_preview_request
    assert latest['serial'] == 3
    assert started == []

    preview.render(QImage(160, 90, QImage.Format.Format_RGB32))
    assert started == []  # Drain is queued out of paintEvent.
    qtbot.waitUntil(lambda: bool(started), timeout=250)
    assert len(started) == 1
    assert started[0].serial == latest['serial']


def test_hidden_preview_keeps_only_latest_frame_until_it_is_shown(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    started = _capture_preview_tasks(monkeypatch)

    preview._latest_video_frame = object()
    preview._frame_serial = 1
    preview._enqueue_latest_frame()
    preview._latest_video_frame = object()
    preview._frame_serial = 2
    preview._enqueue_latest_frame()

    assert started == []
    assert preview._pending_preview_request['serial'] == 2

    preview.show()
    qtbot.waitUntil(lambda: bool(started), timeout=250)
    assert [task.serial for task in started] == [2]


def test_forced_poster_is_allowed_while_preview_surface_is_hidden(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    started = _capture_preview_tasks(monkeypatch)
    preview.set_software_preview_enabled(False)
    preview._latest_video_frame = _image()
    preview._frame_serial = 1

    preview._enqueue_latest_frame(force=True)

    assert len(started) == 1
    assert started[0].force is True


def test_seek_generation_clears_pending_presentation_barrier(qtbot, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview._awaiting_paint_serial = 1
    preview._pending_preview_request = {
        'generation': preview._preview_generation, 'serial': 2,
        'frame': object(),
    }
    preview._presentation_barrier_timer.start()

    preview.begin_seek_generation(1)

    assert preview._awaiting_paint_serial is None
    assert not preview._presentation_barrier_timer.isActive()
    assert preview._pending_preview_request is None


def test_direct_start_request_obeys_active_paint_barrier(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview.show()
    preview._latest_video_frame = object()
    preview._frame_serial = 8
    preview._awaiting_paint_serial = 7
    preview._prepared_frame_serial = 7
    request = preview._make_preview_request()
    started = _capture_preview_tasks(monkeypatch)
    preview._start_preview_request(request)

    assert started == []
    assert preview._pending_preview_request is request


def test_visible_unpainted_barrier_has_one_bounded_wakeup(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview.show()
    started = _capture_preview_tasks(monkeypatch)

    preview._latest_video_frame = _image()
    preview._frame_serial = 2
    preview._prepared_frame_serial = 1
    preview._awaiting_paint_serial = 1
    pending = preview._make_preview_request()
    preview._queue_pending_preview_request(pending)
    preview._presentation_barrier_timer.start()

    preview._on_presentation_barrier_timeout()
    assert preview._awaiting_paint_serial is None
    assert len(started) == 1
    assert started[0].serial == 2

    preview._on_presentation_barrier_timeout()
    assert len(started) == 1


def test_new_frame_replaces_pending_before_queued_postpaint_drain(
        qtbot, monkeypatch, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(preview.shutdown)
    preview.resize(160, 90)
    preview.show()
    started = _capture_preview_tasks(monkeypatch)

    preview._preview_generation = 4
    preview._active_preview_request = {'generation': 4, 'serial': 1}
    preview._latest_video_frame = _image()
    preview._frame_serial = 2
    request_2 = preview._make_preview_request()
    preview._pending_preview_request = request_2
    preview._on_preview_result(4, 1, _image(color=Qt.GlobalColor.blue), 1.0, None)
    preview.render(QImage(160, 90, QImage.Format.Format_RGB32))

    # A sink callback can arrive after paintEvent has queued its zero-delay
    # drain but before that timer runs. The incoming frame must replace serial
    # 2, not start around it and leave it to regress presentation later.
    preview._latest_video_frame = _image(color=Qt.GlobalColor.green)
    preview._frame_serial = 3
    preview._enqueue_latest_frame()

    assert [task.serial for task in started] == [3]
    assert preview._pending_preview_request is None
    qtbot.wait(25)
    assert [task.serial for task in started] == [3]


def test_empty_and_repeated_paints_survive_shutdown_teardown(qtbot, request):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    request.addfinalizer(
        lambda: preview.shutdown() if not preview._preview_shutdown else None)
    preview.resize(160, 90)
    preview.show()

    target = QImage(160, 90, QImage.Format.Format_RGB32)
    preview.render(target)

    preview._prepared_frame = _image()
    preview._frame_image = preview._prepared_frame
    preview._prepared_frame_serial = 1
    preview._last_painted_serial = 1
    preview._has_real_frame = True
    preview.render(target)
    preview.shutdown()
    preview.render(target)
