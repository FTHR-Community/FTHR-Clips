"""Regressions for focus recovery, Unicode notifications and Share cleanup."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from core.capture_health import (
    CaptureHealthFlag, CaptureHealthMonitor, evaluate_save_admission,
)


def test_repeated_focus_generations_do_not_impose_ui_warmup_or_shorten_saves():
    monitor = CaptureHealthMonitor()
    frames = 600
    now = 10.0
    for generation in range(1, 11):
        paused = monitor.observe(
            connected=True, frame_count=frames, generation=generation,
            engine_flags=int(CaptureHealthFlag.ACTIVE | CaptureHealthFlag.PAUSED),
            now=now)
        assert not paused.request_recovery
        now += 20
        # Native capture has resumed before the UI's first poll of its new
        # generation. The UI estimate is zero; the native ring may be ready.
        frames += 60
        resumed = monitor.observe(
            connected=True, frame_count=frames, generation=generation + 1,
            engine_flags=int(CaptureHealthFlag.ACTIVE), now=now)
        assert resumed.fresh_buffer_seconds == 0
        admission = evaluate_save_admission(resumed, 30)
        assert admission.allowed
        assert admission.duration_seconds == 30
        now += 1


def test_capture_card_reader_decodes_utf8_even_under_legacy_windows_encoding(
        qtbot, monkeypatch):
    from ui import capture_card_process

    commands = ['capturing|KovaaK’s', 'background|Pokémon – 東京',
                'upload|Müller 🎯.mp4']
    stream = io.TextIOWrapper(
        io.BytesIO(('\n'.join(commands) + '\n').encode('utf-8')),
        encoding='cp1252')
    monkeypatch.setattr(capture_card_process.sys, 'stdin', stream)
    reader = capture_card_process._StdinReader()
    received = []
    reader.command.connect(received.append)
    reader.run()
    assert received == commands


@pytest.mark.parametrize('action', ['button', 'escape', 'close'])
def test_ready_share_dialog_closes_without_closing_editor(
        qtbot, tmp_path, monkeypatch, action):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget, QPushButton
    from ui.clip_viewer import ShareWindow
    from core.export_lifecycle import ExportState

    monkeypatch.setattr(ShareWindow, '_make_thumbnail', lambda self: None)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    output = tmp_path / 'shared.mp4'
    output.write_bytes(b'completed export')
    dialog = ShareWindow(str(output), 0, 1, None, {}, parent=parent)
    dialog._export_started = True  # Drive completion without launching FFmpeg.
    cancellations = []
    dialog._export_job = SimpleNamespace(
        state=ExportState.COMPLETED, cancel=lambda: cancellations.append(True))
    dialog.show()
    dialog._on_export_done(True, str(output))
    assert dialog._export_path == str(output)
    if action == 'button':
        button = next(b for b in dialog.findChildren(QPushButton) if b.accessibleName() == 'Close')
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    elif action == 'escape':
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        dialog.close()
    assert not dialog.isVisible()
    assert parent.isVisible()
    assert output.read_bytes() == b'completed export'
    assert dialog._export_cancel.is_set()
    assert len(cancellations) == 1


def test_retired_hotkey_is_removed_without_losing_other_bindings(tmp_path):
    from core.hotkey_manager import HotkeyManager

    manager = HotkeyManager.__new__(HotkeyManager)
    manager.config_file = tmp_path / 'hotkeys.json'
    manager.hotkeys = {'save_clip': 'F9', 'save_screenshot': 'F11'}
    manager.controller_hotkeys = {'save_clip': '', 'save_screenshot': ''}
    manager.config_file.write_text(json.dumps({
        'save_clip': {'keyboard': 'Ctrl+F8', 'controller': 'LB+A'},
        'save_extended_clip': {'keyboard': 'F10', 'controller': 'RB+A'},
        'save_screenshot': 'F11',
    }))
    manager._load_hotkeys()
    assert manager.hotkeys['save_clip'] == 'Ctrl+F8'
    assert 'save_extended_clip' not in manager.hotkeys
    saved = json.loads(manager.config_file.read_text())
    assert 'save_extended_clip' not in saved
    assert saved['save_clip']['controller'] == 'LB+A'


def test_retired_preset_duration_cannot_restore_extended_capture(tmp_path, monkeypatch):
    from core.presets_manager import PresetsManager
    from core.capture_settings import compute_buffer_seconds

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = PresetsManager()
    manager._path.parent.mkdir(parents=True)
    manager._path.write_text(json.dumps({
        'gaming': {'clip_length': 15, 'extended_clip_length': 300, 'framerate': 60},
    }))
    assert manager.load('gaming') == {'clip_length': 15, 'framerate': 60}
    assert 'extended_clip_length' not in manager._path.read_text()
    assert compute_buffer_seconds(15) == 17
