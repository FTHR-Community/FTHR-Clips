from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'FTHR_UI'))

from core.capture_bridge import (  # noqa: E402
    CaptureBridge, CaptureHealthFlag, CommandType, SharedMemoryLayout,
)
import ctypes  # noqa: E402

ROOT = Path(__file__).parents[1]


def _module(path: str):
    return ast.parse((ROOT / path).read_text(encoding='utf-8'))


def test_marker_action_is_appended_and_unset_by_default():
    tree = _module('FTHR_UI/core/hotkey_manager.py')
    actions = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name)
                           and target.id == '_HOTKEY_ACTIONS'
                           for target in node.targets))
    values = [item.value for item in actions.value.elts]
    assert values[-1] == 'mark_highlight'
    assert 'mark_highlight' in {
        key.value for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys if isinstance(key, ast.Constant)
    }


def test_bridge_writes_marker_payload_before_command_and_preserves_response(monkeypatch):
    monkeypatch.setattr(CaptureBridge, '_instance', None)
    buffer = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
    layout = SharedMemoryLayout.from_buffer(buffer)
    layout.is_initialized = True
    layout.capture_health_flags = int(
        CaptureHealthFlag.ACTIVE) | CaptureBridge.HIGHLIGHT_CAPABILITY
    bridge = CaptureBridge()
    bridge._layout = layout
    bridge._initialized = True
    marker = '00000000-0000-4000-8000-000000000001'

    assert bridge.mark_highlight(marker)
    assert layout.ui_command == CommandType.MARK_HIGHLIGHT
    stored = layout.ui_string
    assert stored == (marker if isinstance(stored, str) else marker.encode('utf-8'))


def test_main_highlight_handler_checks_known_exited_engine_before_bridge():
    tree = _module('FTHR_UI/main.py')
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == '_on_hotkey_mark_highlight')
    assert any(isinstance(node, ast.Attribute) and node.attr == 'poll'
               for node in ast.walk(method))
