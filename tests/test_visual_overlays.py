from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtGui import QColor, QPixmap

from core.camera_overlay import (
    DEFAULT_IMAGE_OVERLAY_RECT,
    clamp_overlay_rect,
)
from core.settings_manager import SettingsManager
from core.ffmpeg_tools import get_ffmpeg_exe
from ui.camera_overlay_editor import CameraOverlayEditor, UnifiedOverlayPreview
from main import MainWindow


class _VisualHost:
    _captured_effects = MainWindow._captured_effects
    _replace_finalization_output = MainWindow._replace_finalization_output
    _rebind_manifest_after_owned_replace = MainWindow._rebind_manifest_after_owned_replace
    _clip_dimensions = staticmethod(MainWindow._clip_dimensions)


def test_overlay_geometry_uses_each_overlay_default():
    assert clamp_overlay_rect(None, DEFAULT_IMAGE_OVERLAY_RECT) == (
        DEFAULT_IMAGE_OVERLAY_RECT)


def test_camera_preview_keeps_camera_out_of_the_clip_background(qtbot):
    editor = CameraOverlayEditor()
    qtbot.addWidget(editor)
    editor.resize(640, 380)

    background = QPixmap(640, 360)
    background.fill(QColor('#cc2200'))
    camera = QPixmap(160, 120)
    camera.fill(QColor('#0044dd'))
    editor.set_background(background)
    editor.set_frame(camera)
    editor.set_enabled(True)

    rendered = QPixmap(editor.size())
    editor.render(rendered)
    image = rendered.toImage()
    canvas_center = editor._canvas_rect().center().toPoint()
    camera_center = editor._pixel_rect().center().toPoint()

    # The center is still the clip frame. The camera blue appears only inside
    # its placed box in the lower-right.
    assert image.pixelColor(canvas_center).red() > 100
    assert image.pixelColor(canvas_center).blue() < 80
    assert image.pixelColor(camera_center).blue() > 100


def test_composite_preview_keeps_multiple_image_layers(qtbot, tmp_path):
    first_path = tmp_path / 'first.png'
    second_path = tmp_path / 'second.png'
    first = QPixmap(80, 60)
    first.fill(QColor('#ee2200'))
    second = QPixmap(80, 60)
    second.fill(QColor('#00dd44'))
    assert first.save(str(first_path), 'PNG')
    assert second.save(str(second_path), 'PNG')

    editor = UnifiedOverlayPreview()
    qtbot.addWidget(editor)
    editor.resize(640, 380)
    editor.set_image_layers([
        {'path': str(first_path), 'enabled': True, 'opacity': 100,
         'fit': 'fill',
         'rect': {'x': .70, 'y': .05, 'w': .25, 'h': .30}},
        {'path': str(second_path), 'enabled': True, 'opacity': 100,
         'fit': 'fill',
         'rect': {'x': .05, 'y': .05, 'w': .25, 'h': .30}},
    ])

    assert set(editor.rects_data()) == {'camera', 'image:0', 'image:1'}
    rendered = QPixmap(editor.size())
    editor.render(rendered)
    image = rendered.toImage()
    assert image.pixelColor(editor._pixel_rect_for('image:0').center().toPoint()).red() > 150
    assert image.pixelColor(editor._pixel_rect_for('image:1').center().toPoint()).green() > 120


def test_new_overlay_settings_are_safe_and_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    settings = SettingsManager()

    assert settings.get('image_overlay_enabled') is False
    assert not any(key.startswith('keyboard_overlay_')
                   for key in settings.settings)
    assert settings.get('image_overlays') == []
    assert not any(key.startswith('mouse_overlay_')
                   for key in settings.settings)


def test_retired_input_overlay_settings_are_removed(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    config = tmp_path / '.fthr' / 'settings.json'
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"keyboard_overlay_enabled": true, "mouse_overlay_enabled": true, '
        '"mouse_overlay_show_left": true, "resolution": "720p"}',
        encoding='utf-8',
    )

    settings = SettingsManager()

    assert settings.get('resolution') == '720p'
    assert not any(key.startswith('keyboard_overlay_')
                   for key in settings.settings)
    assert not any(key.startswith('mouse_overlay_')
                   for key in settings.settings)
    assert 'keyboard_overlay_enabled' not in config.read_text(encoding='utf-8')


def test_legacy_single_image_setting_migrates_to_a_layer(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    config = tmp_path / '.fthr' / 'settings.json'
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"image_overlay_enabled": true, "image_overlay_path": "hud.png", '
        '"image_overlay_opacity": 72, "image_overlay_fit": "fill"}',
        encoding='utf-8')

    settings = SettingsManager()

    assert len(settings.get('image_overlays')) == 1
    layer = settings.get('image_overlays')[0]
    assert layer['path'] == 'hud.png'
    assert layer['opacity'] == 72
    assert layer['fit'] == 'fill'


def test_image_overlay_is_burned_into_a_real_video(tmp_path):
    from main import MainWindow

    clip = tmp_path / 'base.mp4'
    image_path = tmp_path / 'overlay.png'
    writer = cv2.VideoWriter(
        str(clip), cv2.VideoWriter_fourcc(*'mp4v'), 20, (320, 180))
    assert writer.isOpened()
    for _ in range(12):
        writer.write(np.full((180, 320, 3), (180, 20, 10), dtype=np.uint8))
    writer.release()

    overlay = QPixmap(120, 90)
    overlay.fill(QColor('#ef2200'))
    assert overlay.save(str(image_path), 'PNG')

    class _Host(_VisualHost):
        settings_manager = {
            'image_overlay_enabled': True,
            'image_overlay_path': str(image_path),
            'image_overlay_opacity': 100,
            'image_overlay_fit': 'fit',
            'image_overlay_rect': DEFAULT_IMAGE_OVERLAY_RECT,
        }
        warnings = []

        def _record_finalization_warning(self, _clip_path, message):
            self.warnings.append(message)

    host = _Host()
    MainWindow._apply_image_overlay(host, str(clip), get_ffmpeg_exe())

    capture = cv2.VideoCapture(str(clip))
    assert capture.isOpened()
    ok, frame = capture.read()
    capture.release()
    assert ok
    background = frame[150, 40]  # BGR
    image_pixel = frame[25, 260]
    assert background[0] > background[2]  # original blue background
    assert image_pixel[2] > image_pixel[0]  # red image overlay
    assert not host.warnings


def test_multiple_image_overlays_are_burned_in_one_pass(tmp_path):
    from main import MainWindow

    clip = tmp_path / 'input.mp4'
    writer = cv2.VideoWriter(
        str(clip), cv2.VideoWriter_fourcc(*'mp4v'), 20, (320, 180))
    assert writer.isOpened()
    for _ in range(12):
        writer.write(np.full((180, 320, 3), (180, 20, 10), dtype=np.uint8))
    writer.release()

    red_path = tmp_path / 'red.png'
    green_path = tmp_path / 'green.png'
    red = QPixmap(80, 60)
    red.fill(QColor('#ef2200'))
    green = QPixmap(80, 60)
    green.fill(QColor('#00dd44'))
    assert red.save(str(red_path), 'PNG')
    assert green.save(str(green_path), 'PNG')

    class _Host(_VisualHost):
        settings_manager = {
            'image_overlay_enabled': True,
            'image_overlays': [
                {'path': str(red_path), 'enabled': True, 'opacity': 100,
                 'fit': 'fill',
                 'rect': {'x': .72, 'y': .04, 'w': .24, 'h': .30}},
                {'path': str(green_path), 'enabled': True, 'opacity': 100,
                 'fit': 'fill',
                 'rect': {'x': .04, 'y': .04, 'w': .24, 'h': .30}},
            ],
        }
        warnings = []

        def _record_finalization_warning(self, _clip_path, message):
            self.warnings.append(message)

    host = _Host()
    MainWindow._apply_image_overlay(host, str(clip), get_ffmpeg_exe())

    capture = cv2.VideoCapture(str(clip))
    assert capture.isOpened()
    ok, frame = capture.read()
    capture.release()
    assert ok
    background = frame[150, 160]
    red_layer = frame[25, 250]
    green_layer = frame[25, 25]
    assert background[0] > background[2]
    assert red_layer[2] > red_layer[1]
    assert green_layer[1] > green_layer[2]
    assert not host.warnings


def test_keyboard_overlay_is_streamed_into_a_real_video(tmp_path):
    from main import MainWindow

    clip = tmp_path / 'keyboard-base.mp4'
    writer = cv2.VideoWriter(
        str(clip), cv2.VideoWriter_fourcc(*'mp4v'), 20, (320, 180))
    assert writer.isOpened()
    for _ in range(20):
        writer.write(np.full((180, 320, 3), (180, 20, 10), dtype=np.uint8))
    writer.release()

    class _KeyboardCapture:
        @staticmethod
        def iter_segment_rgba(
                _end, _duration, target_size, _color, _intensity, output_fps):
            width, height = target_size
            frame = np.zeros((height, width, 4), dtype=np.uint8)
            frame[:, :] = (255, 20, 10, 255)
            for _ in range(output_fps):
                yield frame

    class _Host(_VisualHost):
        settings_manager = {
            'third_party_keyboard': {
                'enabled': True,
                'hwnd': 42,
                'rect': {'x': .30, 'y': .65, 'w': .40, 'h': .25},
            },
        }
        _keyboard_overlay_capture = _KeyboardCapture()
        warnings = []

        def _record_finalization_warning(self, _clip_path, message):
            self.warnings.append(message)

    host = _Host()
    MainWindow._apply_keyboard_overlay(
        host, str(clip), get_ffmpeg_exe(), 100.0, 1)

    capture = cv2.VideoCapture(str(clip))
    assert capture.isOpened()
    ok, frame = capture.read()
    capture.release()
    assert ok
    background = frame[25, 25]
    keyboard_pixel = frame[135, 160]
    assert background[0] > background[2]
    assert keyboard_pixel[2] > keyboard_pixel[0]
    assert not host.warnings
