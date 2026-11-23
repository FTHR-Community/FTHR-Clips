from __future__ import annotations

import inspect

import numpy as np
import cv2

import core.third_party_keyboard as keyboard
from core.third_party_keyboard import (
    DEFAULT_KEYBOARD_COLOR,
    DEFAULT_KEYBOARD_INTENSITY,
    ThirdPartyKeyboardCapture,
    chroma_key_rgba,
    chroma_similarity_for_intensity,
    third_party_keyboard_settings,
)


def test_keyboard_settings_are_nested_and_safe():
    class _Settings:
        def get(self, key, default=None):
            return {
                'third_party_keyboard': {
                    'enabled': True,
                    'hwnd': '17',
                    'color': '00FF00',
                    'intensity': 140,
                    'rect': {'x': 2, 'y': -1, 'w': 0.3, 'h': 0.2},
                },
            }.get(key, default)

    config = third_party_keyboard_settings(_Settings())

    assert config['enabled'] is True
    assert config['hwnd'] == 17
    assert config['color'] == DEFAULT_KEYBOARD_COLOR
    assert config['intensity'] == 100
    assert config['rect']['x'] == 0.7
    assert config['rect']['y'] == 0.0


def test_browser_keyboard_source_accepts_https_and_lan_companion_urls():
    urls = (
        'https://overlays.example/keyboard?theme=blue',
        'http://localhost:5001/default-keyboard.html',
        'http://127.0.0.1:9876/',
        'http://192.168.1.23:5001/default-keyboard.html',
        'http://overlay.local:5001/',
        'http://[::1]:5001/',
    )
    for url in urls:
        config = third_party_keyboard_settings({'third_party_keyboard': {
            'source_type': 'browser', 'browser_url': url, 'enabled': True,
        }})
        assert config['enabled']
        assert config['browser_url'] == url
        assert config['hwnd'] == 0
    for url in ('http://overlays.example/keyboard', 'javascript:alert(1)',
                'file:///C:/overlay.html', 'http://localhost@overlays.example/',
                'https://user:secret@overlays.example/'):
        assert keyboard.normalize_keyboard_browser_url(url) == ''


def test_browser_source_alpha_survives_recording_and_ignores_chroma_key(monkeypatch):
    monkeypatch.setattr(keyboard.sys, 'platform', 'win32')
    capture = ThirdPartyKeyboardCapture(capture_fps=2)
    idle = np.zeros((16, 16, 4), dtype=np.uint8)
    active = idle.copy()
    active[2:8, 2:8] = [0, 255, 0, 255]
    capture._store_ring_frame_if_changed(9.0, idle)
    capture._store_ring_frame_if_changed(9.5, active)

    rendered = list(capture.iter_segment_rgba(10.0, 1, (16, 16), output_fps=2))

    assert np.all(rendered[0] == 0)
    assert np.all(rendered[1][4, 4] == [0, 255, 0, 255])
    assert np.all(rendered[1][0, 0] == 0)


def test_chroma_key_makes_selected_color_transparent():
    # BGR input: green is the keyed background, red is the visible keycap.
    frame = np.array([
        [[0, 255, 0], [0, 0, 255]],
    ], dtype=np.uint8)

    result = chroma_key_rgba(frame, '#00ff00', DEFAULT_KEYBOARD_INTENSITY)

    assert result.shape == (1, 2, 4)
    assert result[0, 0, 3] == 0
    assert result[0, 1, 3] == 255
    assert chroma_similarity_for_intensity(0) < chroma_similarity_for_intensity(100)


def test_keyboard_capture_configuration_can_be_stopped():
    capture = ThirdPartyKeyboardCapture()
    try:
        capture.configure({'enabled': True, 'hwnd': 123})
        if __import__('sys').platform != 'win32':
            assert capture.is_running() is False
    finally:
        capture.stop()


def test_window_capture_excludes_native_non_client_chrome():
    body = inspect.getsource(keyboard.capture_window)

    assert 'GetClientRect' in body
    assert 'GetWindowRect' not in body
    assert 'GetDC' in body
    assert 'GetWindowDC' not in body
    assert '0x00000001 | 0x00000002' in body


def test_duplicate_keyboard_states_are_not_continuously_jpeg_encoded(monkeypatch):
    encoded = []
    monkeypatch.setattr(
        keyboard, '_encode_jpeg',
        lambda frame: encoded.append(frame.copy()) or b'jpeg')
    capture = ThirdPartyKeyboardCapture(capture_fps=15, buffer_seconds=2)
    first = np.zeros((12, 18, 3), dtype=np.uint8)
    changed = first.copy()
    changed[2:5, 3:7] = 255

    assert capture._store_ring_frame_if_changed(1.0, first) is True
    assert capture._store_ring_frame_if_changed(1.1, first.copy()) is False
    assert capture._store_ring_frame_if_changed(1.2, changed) is True

    assert len(encoded) == 2
    assert len(capture._frames) == 2


def test_segment_stream_reuses_preview_keying(monkeypatch):
    """Finalization receives the same keyed RGBA pixels as the preview."""
    monkeypatch.setattr(keyboard.sys, 'platform', 'win32')
    capture = ThirdPartyKeyboardCapture(capture_fps=2, buffer_seconds=1)
    bgr = np.zeros((16, 16, 3), dtype=np.uint8)
    bgr[:, :8] = [0, 255, 0]
    bgr[:, 8:] = [0, 0, 255]
    encoded_ok, encoded = cv2.imencode('.jpg', bgr)
    assert encoded_ok
    capture._frames.extend([(9.0, encoded.tobytes()), (9.5, encoded.tobytes())])

    frames = list(capture.iter_segment_rgba(
        10.0, 1, (16, 16), '#00ff00', DEFAULT_KEYBOARD_INTENSITY, 2))

    assert len(frames) == 2
    assert all(frame.shape == (16, 16, 4) for frame in frames)
    assert all(frame[0, 0, 3] == 0 for frame in frames)
    assert all(frame[0, 15, 3] > 240 for frame in frames)


def test_changed_state_ring_keeps_the_last_frame_before_clip_start(monkeypatch):
    monkeypatch.setattr(keyboard.sys, 'platform', 'win32')
    capture = ThirdPartyKeyboardCapture(capture_fps=2, buffer_seconds=10)
    before = np.full((8, 8, 3), [0, 0, 255], dtype=np.uint8)
    after = np.full((8, 8, 3), [255, 0, 0], dtype=np.uint8)
    ok_before, jpeg_before = cv2.imencode('.jpg', before)
    ok_after, jpeg_after = cv2.imencode('.jpg', after)
    assert ok_before and ok_after
    capture._frames.extend([
        (5.0, jpeg_before.tobytes()),
        (11.0, jpeg_after.tobytes()),
    ])

    frames = list(capture.iter_segment_frames(12.0, 2, output_fps=2))

    assert len(frames) == 4
    assert frames[0][0, 0, 2] > 200
    assert frames[-1][0, 0, 0] > 200
