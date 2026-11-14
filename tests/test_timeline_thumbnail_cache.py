import subprocess

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap

from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe
from ui.clip_viewer import TrimSlider


class CountingPixmap(QPixmap):
    def __init__(self, color):
        super().__init__(180, 100)
        self.fill(QColor(color))
        self.scale_calls = 0

    def scaled(self, *args):
        self.scale_calls += 1
        return super().scaled(*args)


def test_playhead_repaints_reuse_scaled_frames(qtbot):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    slider.resize(800, 116)
    frames = [CountingPixmap('red'), CountingPixmap('blue')]
    slider._thumbnails = frames
    slider.grab()
    calls = sum(frame.scale_calls for frame in frames)
    assert calls > 0
    for position in (0.2, 0.4, 0.8):
        slider.set_playhead(position)
        slider.grab()
    assert sum(frame.scale_calls for frame in frames) == calls


def test_cached_render_matches_original_across_resize_zoom_and_pan(qtbot, monkeypatch):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    slider._thumbnails = [CountingPixmap(color) for color in
                          ('red', 'green', 'blue', 'yellow', 'purple', 'orange')]
    cached_scale = slider._scaled_thumbnail

    def original_scale(source, size):
        return source.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)

    for width, zoom, pan in ((800, 1, 0), (1234, 1, 0), (1234, 4, 1), (640, 2, -1)):
        slider.resize(width, 116)
        slider.set_zoom(zoom)
        slider.pan(pan)
        monkeypatch.setattr(slider, '_scaled_thumbnail', cached_scale)
        cached = slider.grab().toImage()
        monkeypatch.setattr(slider, '_scaled_thumbnail', original_scale)
        assert cached == slider.grab().toImage()


def test_frame_replacement_and_mutation_do_not_show_stale_pixels(qtbot):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    source = CountingPixmap('red')
    size = QSize(92, 70)
    slider._scaled_thumbnail(source, size)
    source.fill(QColor('blue'))
    assert slider._scaled_thumbnail(source, size).toImage().pixelColor(0, 0) == QColor('blue')
    image = QImage(180, 100, QImage.Format.Format_RGB32)
    image.fill(QColor('green'))
    slider._set_thumbnail_images((slider._thumbnail_job, [image], True))
    assert not slider._scaled_thumbnails
    assert slider._scaled_thumbnail(slider._thumbnails[0], size).toImage().pixelColor(0, 0) == QColor('green')


def test_resize_cache_is_bounded_and_preserves_recent_entries(qtbot):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    source = CountingPixmap('red')
    for width in range(80, 280):
        slider._scaled_thumbnail(source, QSize(width, 70))
    assert len(slider._scaled_thumbnails) <= 64
    calls = source.scale_calls
    slider._scaled_thumbnail(source, QSize(279, 70))
    assert source.scale_calls == calls
    slider._scaled_thumbnail(source, QSize(80, 70))
    assert source.scale_calls == calls + 1


def test_partial_filmstrip_frame_stays_at_its_source_time(qtbot):
    slider = TrimSlider(60_000)
    qtbot.addWidget(slider)
    slider.resize(800, 116)
    slider._thumbnail_count = 24
    before = slider.grab().toImage()
    frame = QImage(180, 100, QImage.Format.Format_RGB888)
    frame.fill(QColor('red'))
    slider._set_thumbnail_images((slider._thumbnail_job, [frame], None))
    progressive = slider.grab().toImage()
    assert progressive.pixelColor(45, 50) != before.pixelColor(45, 50)
    assert progressive.pixelColor(450, 50) == before.pixelColor(450, 50)


def test_indexed_filmstrip_decodes_the_requested_frames(qtbot, tmp_path):
    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable as error:
        pytest.skip(str(error))
    clip = tmp_path / 'samples.mp4'
    colors = ('red', 'green', 'blue', 'white')
    filters = ','.join(
        f"drawbox=color={color}:t=fill:enable='eq(t,{3 + i * 6})'"
        for i, color in enumerate(colors))
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=black:s=160x90:r=4:d=24', '-vf', filters,
                    '-c:v', 'mpeg4', '-g', '8', str(clip)], check=True, timeout=10)
    slider = TrimSlider(24_000)
    qtbot.addWidget(slider)
    slider.load_thumbnails(str(clip), 4)
    qtbot.waitUntil(lambda: slider._thumbnail_complete, timeout=10_000)

    assert len(slider._thumbnails) == len(colors)
    for frame, color in zip(slider._thumbnails, colors, strict=True):
        actual = frame.toImage().pixelColor(90, 50)
        expected = QColor(color)
        assert max(abs(a - b) for a, b in zip(actual.getRgb(), expected.getRgb(), strict=True)) < 8
