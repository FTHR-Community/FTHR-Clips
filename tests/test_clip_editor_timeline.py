from __future__ import annotations

from types import MethodType, SimpleNamespace
import pytest

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QSizePolicy

from ui.clip_viewer import (
    ClipViewer,
    ClickableSlider,
    EditorSnapshot,
    LiveVideoPreview,
    StretchDialog,
    TrimSlider,
    TimelineSegment,
    _editor_snapshot_from_draft,
    _editor_snapshot_to_draft,
    _video_filter_chain,
)


def test_editor_slider_uses_a_larger_hit_area_and_absolute_clicks(qtbot):
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setRange(0, 100)
    slider.resize(200, slider.minimumHeight())
    slider.show()

    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(150, 2))
    assert 70 <= slider.value() <= 85

    QTest.mouseClick(
        slider, Qt.MouseButton.LeftButton,
        pos=QPoint(50, slider.height() - 2))
    assert 15 <= slider.value() <= 30


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


def test_editor_draft_snapshot_roundtrips_and_rejects_broken_timelines():
    state = EditorSnapshot(
        trim_start=0.1,
        trim_end=0.9,
        segments=((0.0, 0.4, False), (0.4, 1.0, True)),
        selected_segment=1,
        crop_rect=(10, 20, 1280, 720),
        stretch_ratio=1.2,
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
    assert restored.preserve_pitch is False
    assert dict(restored.effects)['contrast'] == 25

    corrupt = _editor_snapshot_to_draft(state)
    corrupt['segments'] = [[0.0, 0.4, False], [0.6, 1.0, False]]
    assert _editor_snapshot_from_draft(corrupt) is None


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
