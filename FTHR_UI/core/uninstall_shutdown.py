"""Windows uninstall requests enter the app's normal recording-safe exit path."""

from functools import lru_cache
import sys


MESSAGE_NAME = 'FTHRClips.UninstallShutdown.v1'


@lru_cache(maxsize=1)
def shutdown_message_id() -> int:
    if sys.platform != 'win32':
        return 0
    import ctypes
    from ctypes import wintypes
    register = ctypes.WinDLL('user32', use_last_error=True).RegisterWindowMessageW
    register.argtypes = [wintypes.LPCWSTR]
    register.restype = wintypes.UINT
    return int(register(MESSAGE_NAME))


def is_shutdown_message(message: int) -> bool:
    registered = shutdown_message_id()
    return bool(registered and message == registered)
