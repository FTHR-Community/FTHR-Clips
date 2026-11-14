from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from ui.clip_viewer import TrimSlider
from ui.highlight_markers import HighlightMarkersModel


def _marker(position, label=''):
    return {'marker_id': str(uuid4()), 'position_100ns': position, 'label': label}


def test_model_uses_rows_without_creating_one_widget_per_marker(qtbot):
    model = HighlightMarkersModel()
    markers = [_marker(index * 10_000, f'M{index}') for index in range(100)]
    model.set_markers(markers, {marker['marker_id'] for marker in markers})

    assert model.rowCount() == 100
    assert model.data(model.index(0, 0)) == 'M0  ·  0:00.000'
    assert model.data(model.index(99, 0), model.MarkerRole)['label'] == 'M99'
    assert not model.findChildren(type(model))


def test_timeline_marker_hit_jumps_in_source_time_and_honors_hidden_deleted(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(800, 116)
    timeline.segments = [
        type(timeline.segments[0])(0.0, 0.4),
        type(timeline.segments[0])(0.4, 0.7, True),
        type(timeline.segments[0])(0.7, 1.0),
    ]
    retained = _marker(2_000 * 10_000, 'retained')
    deleted = _marker(5_000 * 10_000, 'deleted')
    timeline.set_highlight_markers([retained, deleted])
    timeline.set_hide_deleted_segments(True)
    timeline.show()

    activated = []
    seeks = []
    timeline.highlight_activated.connect(activated.append)
    timeline.seek_requested.connect(seeks.append)
    marker_x = 14 + int(timeline._source_to_display(0.2)
                        * (timeline.width() - 28))
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(marker_x, 45))

    assert activated and activated[-1]['marker_id'] == retained['marker_id']
    # The viewer's highlight activation slot owns the single canonical seek;
    # the marker hit itself must not emit a second direct seek request.
    assert not seeks
    assert timeline.highlight_available(deleted) is False


def test_marker_membership_is_half_open_in_original_100ns_time(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.segments = [
        type(timeline.segments[0])(0.0, 0.4),
        type(timeline.segments[0])(0.4, 0.7, True),
        type(timeline.segments[0])(0.7, 1.0),
    ]
    before_deleted = _marker(4_000 * 10_000 - 1)
    at_deleted = _marker(4_000 * 10_000)
    at_eof = _marker(10_000 * 10_000)
    assert timeline.highlight_available(before_deleted) is True
    assert timeline.highlight_available(at_deleted) is False
    assert timeline.highlight_available(at_eof) is False
