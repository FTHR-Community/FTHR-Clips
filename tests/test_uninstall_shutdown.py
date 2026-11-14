import ctypes
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from core import uninstall_shutdown


def test_zero_message_is_never_a_shutdown_request(monkeypatch):
    monkeypatch.setattr(uninstall_shutdown, 'shutdown_message_id', lambda: 0)
    assert not uninstall_shutdown.is_shutdown_message(0)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows uninstall protocol')
def test_native_uninstall_message_queues_full_exit_not_close_to_tray(monkeypatch):
    from ctypes import wintypes
    import main
    message_id = uninstall_shutdown.shutdown_message_id()
    assert 0xC000 <= message_id <= 0xFFFF
    msg = wintypes.MSG()
    msg.message = message_id
    window = SimpleNamespace(request_full_exit=Mock())
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda delay, callback: callbacks.append(callback))
    handled = main.MainWindow.nativeEvent(window, b'windows_generic_MSG', ctypes.addressof(msg))
    assert handled == (True, 0)
    window.request_full_exit.assert_not_called()
    assert len(callbacks) == 1
    callbacks[0]()
    window.request_full_exit.assert_called_once()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native event')
def test_unrelated_native_message_does_not_exit():
    from ctypes import wintypes
    import main
    msg = wintypes.MSG()
    msg.message = 0x000F  # WM_PAINT
    window = SimpleNamespace(request_full_exit=Mock())
    assert main.MainWindow.nativeEvent(window, b'windows_generic_MSG', ctypes.addressof(msg)) == (False, 0)
    window.request_full_exit.assert_not_called()
