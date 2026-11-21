from __future__ import annotations

import json
import subprocess
from types import MethodType, SimpleNamespace
import pytest

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QSizePolicy

from core.ffmpeg_tools import (
    FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe, maximum_quality_video_args,
)
from ui.clip_viewer import (
    ClipViewer,
    EditorSnapshot,
    LiveVideoPreview,
    StretchDialog,
    TrimSlider,
    TimelineSegment,
    _editor_snapshot_from_draft,
    _editor_snapshot_to_draft,
    _video_filter_chain,
)
from ui.sliders import ClickToSeekSlider


def test_editor_effect_slider_clicks_and_drags_as_one_edit(qtbot):
    events = []
    viewer = SimpleNamespace(
        _effect_sliders={},
        _effect_value_labels={},
        _begin_effect_adjustment=lambda: events.append('start'),
        _finish_effect_adjustment=lambda: events.append('finish'),
        _on_effect_changed=lambda key, value: events.append((key, value)),
    )
    row = ClipViewer._build_effect_control(viewer, 'brightness', 'Brightness', 0, 100)
    qtbot.addWidget(row)
    row.resize(200, row.sizeHint().height())
    row.show()
    slider = viewer._effect_sliders['brightness']
    assert isinstance(slider, ClickToSeekSlider)
    assert slider.minimumHeight() == 26

    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(150, 2))
    assert 70 <= slider.value() <= 85
    assert events == ['start', ('brightness', slider.value()), 'finish']

    events.clear()
    bottom = slider.height() - 2
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(50, bottom))
    assert 15 <= slider.value() <= 30
    QTest.mouseMove(slider, QPoint(100, bottom))
    assert 40 <= slider.value() <= 60
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(170, bottom))
    assert 80 <= slider.value() <= 95
    assert events[0] == 'start'
    assert events[-2:] == [('brightness', slider.value()), 'finish']


def test_timeline_click_jumps_playhead_instead_of_moving_nearest_trim_handle(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(800, 116)
    timeline.show()
    seen = []
    timeline.seek_requested.connect(seen.append)

    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(480, 56))

    assert seen
    assert 0.55 < seen[-1] < 0.65
    assert timeline.start_pct == 0.0
    assert timeline.end_pct == 1.0


def test_timeline_drag_seeks_through_final_pointer_position(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(800, 116)
    timeline.show()
    positions = []
    scrub_finished = []
    timeline.seek_requested.connect(positions.append)
    timeline.playhead_scrub_finished.connect(lambda: scrub_finished.append(len(positions)))

    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=QPoint(300, 56))
    QTest.mouseMove(timeline, QPoint(450, 56))
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=QPoint(550, 56))

    assert positions == pytest.approx([(x - 14) / (800 - 28) for x in (300, 450, 550)])
    assert scrub_finished == [3]
    assert (timeline.start_pct, timeline.end_pct) == (0.0, 1.0)


def test_timeline_trim_handle_drag_updates_range_without_seeking(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(800, 116)
    timeline.show()
    events = []
    timeline.range_edit_started.connect(lambda: events.append('start'))
    timeline.range_changed.connect(lambda start, end: events.append((start, end)))
    timeline.range_edit_finished.connect(lambda: events.append('finish'))
    timeline.seek_requested.connect(lambda _: events.append('seek'))

    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=QPoint(14, 56))
    QTest.mouseMove(timeline, QPoint(200, 56))
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=QPoint(200, 56))

    assert timeline.start_pct == pytest.approx((200 - 14) / (800 - 28))
    assert events == ['start', (timeline.start_pct, 1.0), 'finish']


def test_repeated_scrub_requests_restart_the_seek_debounce():
    class Timer:
        starts = 0

        def start(self):
            self.starts += 1

    playheads = []
    viewer = SimpleNamespace(
        duration_ms=10_000,
        _pending_seek_ms=None,
        _resume_anchor_ms=500,
        _last_stable_position_ms=0,
        trim_slider=SimpleNamespace(set_playhead=playheads.append,
                                    cancel_thumbnail_loading=lambda: None),
        _timeline_prepare_timer=SimpleNamespace(stop=lambda: None),
        _seek_timer=Timer(),
    )

    ClipViewer._on_seek_requested(viewer, 0.2)
    ClipViewer._on_seek_requested(viewer, 0.8)

    assert viewer._pending_seek_ms == 8_000
    assert viewer._last_stable_position_ms == 8_000
    assert viewer._resume_anchor_ms is None
    assert playheads == [0.2, 0.8]
    assert viewer._seek_timer.starts == 2


def test_hidden_segments_seek_zoom_and_restore_in_source_time(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(828, 116)
    timeline.segments = [TimelineSegment(0, .2), TimelineSegment(.2, .6, True),
                         TimelineSegment(.6, 1)]
    original = [(s.start_pct, s.end_pct, s.deleted) for s in timeline.segments]
    timeline.set_hide_deleted_segments(True)
    assert timeline._pct_from_x(414) == pytest.approx(.7)
    assert timeline._overview_pct_from_x(414) == pytest.approx(.7)
    assert timeline._display_to_source(1 / 3) == pytest.approx(.6)
    timeline.show()
    seeks = []
    timeline.seek_requested.connect(seeks.append)
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(414, 56))
    assert seeks[-1] == pytest.approx(.7)
    assert timeline.selected_segment == 2
    timeline.set_zoom(2, .7)
    assert timeline._pct_from_x(414) == pytest.approx(.7)
    timeline.pan(1)
    timeline.ensure_visible(.1)
    assert timeline.view_start_pct <= timeline._source_to_display(.1)
    assert not timeline.grab().isNull()
    timeline.set_hide_deleted_segments(False)
    assert [(s.start_pct, s.end_pct, s.deleted) for s in timeline.segments] == original
    timeline.selected_segment = 1
    assert timeline.toggle_selected_deleted()
    assert timeline.kept_ranges() == [(0, .2), (.2, .6), (.6, 1)]


def test_hidden_leading_and_trailing_segments_leave_endpoints_selectable(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.segments = [TimelineSegment(0, .2, True), TimelineSegment(.2, .8),
                         TimelineSegment(.8, 1, True)]
    timeline.set_hide_deleted_segments(True)
    assert timeline._display_to_source(0) == .2
    assert timeline._display_to_source(1) == .8
    assert timeline._segment_index_at(.2) == 1
    assert timeline._segment_index_at(.8) == 1


def test_split_delete_and_restore_are_non_destructive(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.set_playhead(0.4)

    assert timeline.split_at_playhead() is True
    assert [(segment.start_pct, segment.end_pct) for segment in timeline.segments] == [
        (0.0, 0.4), (0.4, 1.0)]
    assert timeline.selected_segment == 1

    assert timeline.toggle_selected_deleted() is True
    assert timeline.segments[1].deleted is True
    assert timeline.kept_ranges() == [(0.0, 0.4)]

    assert timeline.toggle_selected_deleted() is True
    assert timeline.segments[1].deleted is False
    assert timeline.kept_ranges() == [(0.0, 0.4), (0.4, 1.0)]


def test_timeline_context_menu_targets_the_right_clicked_segment(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.set_playhead(0.4)
    assert timeline.split_at_playhead() is True

    split_requests = []
    toggle_requests = []
    timeline.context_split_requested.connect(split_requests.append)
    timeline.context_toggle_requested.connect(toggle_requests.append)

    menu = timeline._context_menu_for_pct(0.7)
    actions = menu.actions()

    assert timeline.selected_segment == 1
    assert [action.text() for action in actions if not action.isSeparator()] == [
        'Split here', 'Delete segment', 'Hide deleted segments']
    assert all(action.isEnabled() for action in actions)

    actions[0].trigger()
    actions[1].trigger()
    assert split_requests == [0.7]
    assert toggle_requests == [1]


def test_live_preview_applies_color_and_stretch_without_native_video_surface(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview.resize(400, 400)

    poster = QPixmap(16, 9)
    poster.fill(QColor(40, 80, 120))
    preview.set_poster(poster)
    poster_frame = preview._display_frame(preview._video_display_rect())
    assert not poster_frame.isNull()
    assert poster_frame.pixelColor(10, 10) == QColor(40, 80, 120)

    neutral = QImage(8, 8, QImage.Format.Format_RGB888)
    neutral.fill(QColor(80, 80, 80))
    brighter = LiveVideoPreview._apply_color_effects(neutral, {'exposure': 50})

    assert brighter.pixelColor(4, 4).red() > neutral.pixelColor(4, 4).red()

    source_rect = preview._video_display_rect()
    preview.set_edit_state((100, 100, 600, 800), {'saturation': 25}, 0.5)
    stretched_rect = preview._video_display_rect()

    assert source_rect.size() == QSize(400, 225)
    assert stretched_rect.height() == 400
    assert 354 <= stretched_rect.width() <= 356
    assert preview._crop_rect == (100, 100, 600, 800)


def test_live_preview_can_keep_visual_edits_for_export_only(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview.resize(400, 400)

    poster = QPixmap(16, 9)
    poster.fill(QColor(40, 80, 120))
    preview.set_poster(poster)
    preview.set_edit_state(
        (100, 100, 600, 800), {'exposure': 80}, 0.5,
        preview_enabled=False)

    assert preview._crop_rect is None
    assert preview._effects == {}
    assert preview._stretch_ratio == 1.0
    assert preview._video_display_rect().size() == QSize(400, 225)
    assert preview._display_frame(preview._video_display_rect()).pixelColor(
        10, 10) == QColor(40, 80, 120)


def test_custom_resolution_preview_matches_crop_aspect_beyond_legacy_ratio(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview.resize(800, 800)
    preview.set_edit_state((100, 100, 640, 720), {}, 1.0, stretch_size=(160, 2880))
    display = preview._video_display_rect()
    crop_aspect = (display.width() * (640 / 1920)) / (display.height() * (720 / 1080))
    assert crop_aspect == pytest.approx(160 / 2880, abs=0.001)


def test_collapsible_panels_shrink_to_the_sidebar_instead_of_expanding_sideways(qtbot):
    panel, body_layout = ClipViewer._build_collapsible_panel(
        SimpleNamespace(), 'CLIP DETAILS', expanded=False)
    qtbot.addWidget(panel)
    panel.resize(252, panel.sizeHint().height())
    panel.show()

    header = panel.findChild(QPushButton, 'panelHeader')
    body = body_layout.parentWidget()
    QTest.mouseClick(header, Qt.MouseButton.LeftButton)

    assert panel.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert body.isVisible()
    assert panel.width() == 252


def test_timeline_zoom_keeps_anchor_visible_and_supports_offscreen_trim_jump(qtbot):
    timeline = TrimSlider(60_000)
    qtbot.addWidget(timeline)
    timeline.set_playhead(0.75)
    timeline.set_zoom(4.0, 0.75)

    assert timeline.view_span_pct == 0.25
    assert timeline.view_start_pct <= 0.75 <= timeline.view_start_pct + 0.25

    timeline.ensure_visible(0.1, align_start=True)
    assert timeline.view_start_pct <= 0.1 <= timeline.view_start_pct + 0.25


def test_manual_recording_timeline_can_zoom_to_clip_scale(qtbot):
    timeline = TrimSlider(3_600_000)
    qtbot.addWidget(timeline)

    timeline.set_zoom(10_000)

    assert timeline.zoom_factor == timeline.max_zoom_factor
    assert timeline.max_zoom_factor >= 720
    assert timeline.view_span_pct * timeline.duration_ms <= 5_000


def test_color_crop_and_stretch_build_one_deterministic_video_chain():
    filters = _video_filter_chain(
        (10, 20, 1281, 721),
        {
            'exposure': 25,
            'contrast': 10,
            'saturation': 30,
            'temperature': -20,
            'sharpness': 50,
            'vignette': 40,
        },
        1.5,
    )
    joined = ','.join(filters)

    assert 'crop=1280:720:10:20' in joined
    assert 'curves=all=' in joined
    assert 'hue=s=1.300' in joined
    assert 'colorbalance=' in joined
    assert 'unsharp=' in joined
    assert 'vignette=' in joined
    assert 'scale=trunc(iw*1.5000/2)*2' in joined
    assert 'setsar=1' in joined


def test_multi_segment_export_uses_concat_and_applies_video_edits():
    viewer = SimpleNamespace(
        clip_path='clip.mp4',
        _audio_tracks=(),
        _playback_sources=(),
        _source_volumes={},
        _source_mutes={},
        _master_volume=100,
    )

    command = ClipViewer._build_export_cmd(
        viewer,
        'ffmpeg',
        0.0,
        10.0,
        'out.mp4',
        (0, 0, 1920, 1080),
        ['-c:v', 'libx264'],
        segments=[(0.0, 2.0), (5.0, 8.0)],
        effects={'saturation': 20},
        stretch_ratio=0.75,
    )
    joined = ' '.join(command)

    assert 'trim=start=0.000000:end=2.000000' in joined
    assert 'trim=start=5.000000:end=8.000000' in joined
    assert 'concat=n=2:v=1:a=0[vout]' in joined
    assert 'crop=1920:1080:0:0' in joined
    assert 'hue=s=1.200' in joined
    assert 'scale=trunc(iw*0.7500/2)*2' in joined


def test_size_constrained_export_forces_encode_without_visual_filters():
    viewer = SimpleNamespace(
        clip_path='clip.mp4',
        _audio_tracks=(),
        _playback_sources=(),
        _source_volumes={},
        _source_mutes={},
        _master_volume=100,
    )

    command = ClipViewer._build_export_cmd(
        viewer, 'ffmpeg', 0.0, 10.0, 'out.mp4', None,
        ['-c:v', 'libx264', '-b:v', '900k'],
        force_video_encode=True,
    )

    assert '-c:v' in command
    assert 'libx264' in command
    assert command[command.index('-c:v') + 1] != 'copy'


def test_space_key_always_routes_to_play_pause():
    calls = []
    viewer = SimpleNamespace(_toggle_play=lambda: calls.append('toggle'))
    event = SimpleNamespace(
        key=lambda: Qt.Key.Key_Space,
        accept=lambda: calls.append('accepted'),
    )

    ClipViewer.keyPressEvent(viewer, event)

    assert calls == ['toggle', 'accepted']


def test_editor_history_undoes_and_redoes_segment_edits(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    undo_btn = QPushButton()
    redo_btn = QPushButton()
    qtbot.addWidget(undo_btn)
    qtbot.addWidget(redo_btn)
    viewer = SimpleNamespace(
        trim_slider=timeline,
        _crop_rect=None,
        _stretch_ratio=1.0,
        _effects={'saturation': 0},
        _effect_sliders={},
        _undo_stack=[],
        _redo_stack=[],
        _restoring_state=False,
        undo_btn=undo_btn,
        redo_btn=redo_btn,
        duration_ms=10_000,
        player=SimpleNamespace(position=lambda: 0),
        _on_segments_changed=lambda: None,
        _update_crop_ui=lambda: None,
        _on_stretch_changed=lambda _ratio: None,
        _on_seek_requested=lambda _pct: None,
    )
    for method_name in (
            '_capture_editor_state', '_commit_editor_change',
            '_restore_editor_state', '_update_history_controls',
            '_kept_segments_ms', '_undo', '_redo'):
        setattr(viewer, method_name, MethodType(getattr(ClipViewer, method_name), viewer))

    before = viewer._capture_editor_state()
    timeline.set_playhead(0.4)
    assert timeline.split_at_playhead() is True
    viewer._commit_editor_change(before)
    assert len(timeline.segments) == 2
    assert undo_btn.isEnabled() is True

    viewer._undo()
    assert len(timeline.segments) == 1
    assert redo_btn.isEnabled() is True

    viewer._redo()
    assert len(timeline.segments) == 2
    assert undo_btn.isEnabled() is True

    before = viewer._capture_editor_state()
    viewer._stretch_size = (960, 540)
    viewer._commit_editor_change(before)
    viewer._undo()
    assert viewer._stretch_size is None
    viewer._redo()
    assert viewer._stretch_size == (960, 540)


def test_editor_draft_snapshot_roundtrips_and_rejects_broken_timelines():
    state = EditorSnapshot(
        trim_start=0.1,
        trim_end=0.9,
        segments=((0.0, 0.4, False), (0.4, 1.0, True)),
        selected_segment=1,
        crop_rect=(10, 20, 1280, 720),
        stretch_ratio=1.2,
        stretch_size=(720, 1280),
        effects=(('contrast', 25), ('exposure', -10)),
        preserve_pitch=False,
    )

    restored = _editor_snapshot_from_draft(_editor_snapshot_to_draft(state))

    assert restored is not None
    assert restored.trim_start == state.trim_start
    assert restored.trim_end == state.trim_end
    assert restored.segments == state.segments
    assert restored.crop_rect == state.crop_rect
    assert restored.stretch_ratio == state.stretch_ratio
    assert restored.stretch_size == state.stretch_size
    assert restored.preserve_pitch is False
    assert dict(restored.effects)['contrast'] == 25

    corrupt = _editor_snapshot_to_draft(state)
    corrupt['segments'] = [[0.0, 0.4, False], [0.6, 1.0, False]]
    assert _editor_snapshot_from_draft(corrupt) is None

    legacy = _editor_snapshot_to_draft(state)
    del legacy['stretch_size']
    assert _editor_snapshot_from_draft(legacy).stretch_size is None

    invalid_size = _editor_snapshot_to_draft(state)
    invalid_size['stretch_size'] = [721, 1280]
    assert _editor_snapshot_from_draft(invalid_size) is None


def test_stretch_dialog_uses_cropped_dimensions_and_visible_drag_preview(
        qtbot, monkeypatch):
    preview_pixmap = QPixmap(320, 180)
    preview_pixmap.fill(QColor('#2856d8'))
    monkeypatch.setattr(
        StretchDialog, '_extract_preview_frame',
        lambda *_args, **_kwargs: preview_pixmap)
    dialog = StretchDialog(
        'clip.mp4', 0, 1920, 1080,
        crop_rect=(100, 0, 606, 1077), initial_ratio=1.0)
    qtbot.addWidget(dialog)
    dialog.resize(760, 540)
    dialog.show()
    qtbot.wait(20)

    assert dialog._base_w == 606
    assert dialog._base_h == 1077
    assert not dialog.preview._preview_pixmap.isNull()

    rect = dialog.preview._rect_for_ratio(1.0)
    handle = QPoint(rect.right(), rect.center().y())
    QTest.mousePress(dialog.preview, Qt.MouseButton.LeftButton, pos=handle)
    QTest.mouseMove(dialog.preview, QPoint(handle.x() + 90, handle.y()), delay=10)
    QTest.mouseRelease(
        dialog.preview, Qt.MouseButton.LeftButton,
        pos=QPoint(handle.x() + 90, handle.y()))

    assert dialog.stretch_ratio > 1.4
    assert '×' in dialog._dimensions.text()


def test_custom_stretch_fields_validate_and_update_preview(qtbot, monkeypatch):
    monkeypatch.setattr(StretchDialog, '_extract_preview_frame', lambda *_: QPixmap())
    dialog = StretchDialog('clip.mp4', 0, 1920, 1080, crop_rect=(100, 0, 606, 1077))
    qtbot.addWidget(dialog)
    dialog.show()

    def enter(field, value):
        field.selectAll()
        QTest.keyClicks(field, value)

    enter(dialog._width_input, '540')
    enter(dialog._height_input, '960')
    assert dialog.stretch_size == (540, 960)
    assert dialog._apply_btn.isEnabled()
    rect = dialog.preview._rect_for_ratio(dialog.preview.ratio)
    assert rect.width() / rect.height() == pytest.approx(540 / 960, abs=0.01)

    enter(dialog._height_input, '961')
    assert not dialog._apply_btn.isEnabled()
    assert 'even numbers' in dialog._range_hint.text()
    assert dialog.stretch_size == (540, 960)
    QTest.keyClick(dialog._height_input, Qt.Key.Key_Return)
    assert dialog.isVisible()
    enter(dialog._height_input, '4310')
    assert not dialog._apply_btn.isEnabled()
    assert '4,308' in dialog._range_hint.text()
    enter(dialog._height_input, '4308')
    enter(dialog._width_input, '2424')
    assert not dialog._apply_btn.isEnabled()
    assert 'total pixels' in dialog._range_hint.text()

    enter(dialog._width_input, '540')
    enter(dialog._height_input, '960')
    dialog._on_ratio_changed(1.2)
    assert dialog.stretch_size == (726, 960)
    assert dialog._width_input.text() == '726'

    dialog._reset()
    assert dialog.stretch_size is None
    assert dialog._width_input.text() == '606'
    assert dialog._height_input.text() == '1076'
    assert dialog._apply_btn.isEnabled()


@pytest.mark.parametrize('segments,size', [([(0, 1)], (48, 48)),
                                          ([(0, 0.4), (0.6, 1)], (64, 128))])
def test_custom_stretch_export_writes_exact_square_pixel_dimensions(tmp_path, segments, size):
    try:
        ffmpeg = get_ffmpeg_exe()
        ffprobe = get_ffprobe_exe()
    except FFmpegUnavailable as error:
        pytest.skip(str(error))
    source = tmp_path / 'input.mp4'
    output = tmp_path / 'stretched.mp4'
    subprocess.run([
        ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x80:rate=20:duration=1',
        '-c:v', 'mpeg4', str(source),
    ], check=True, capture_output=True, timeout=20)
    viewer = SimpleNamespace(clip_path=str(source))
    command = ClipViewer._build_export_cmd(
        viewer, ffmpeg, 0, 1, str(output), (4, 6, 64, 64),
        maximum_quality_video_args(ffmpeg), segments=segments, stretch_size=size)
    subprocess.run(command, check=True, capture_output=True, timeout=20)
    probe = subprocess.run([
        ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=width,height,sample_aspect_ratio', '-of', 'json', str(output),
    ], check=True, capture_output=True, text=True, timeout=20)
    stream = json.loads(probe.stdout)['streams'][0]
    assert (stream['width'], stream['height']) == size
    assert stream['sample_aspect_ratio'] == '1:1'


def test_tall_filmstrip_click_is_not_mistaken_for_overview(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.setFixedHeight(260)
    timeline.resize(800, 260)
    timeline.set_zoom(2.0, 0.25)
    timeline.show()
    before = timeline.view_start_pct
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(600, 180))
    assert timeline.view_start_pct == before
    assert timeline._playhead_rect().bottom() > 220
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(600, 250))
    assert timeline.view_start_pct > before
