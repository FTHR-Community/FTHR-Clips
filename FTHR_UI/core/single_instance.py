"""Prevent concurrent UIs from sharing the single-writer capture IPC channel.

Windows uses a named mutex; Linux uses nonblocking flock on ~/.fthr/fthr.lock.
The OS releases either lock on process exit, so a leftover lock file is safe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Global kernel namespace would need extra privileges and would also collide
# across user sessions on a shared machine; Local\ is per-session, which is
# exactly the scope we want (one instance per logged-in user).
_WIN_MUTEX_NAME = 'Local\\FTHR_Clips_SingleInstance_v1'
_LOCK_FILE = Path.home() / '.fthr' / 'fthr.lock'

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Acquire the instance slot at startup and release it on exit.

    acquire() returns True on ownership or if the platform lock is unavailable
    (fail-open). Optional primitive names isolate tests from a running app.
    """

    def __init__(
        self,
        *,
        win_mutex_name: str = _WIN_MUTEX_NAME,
        lock_file: Path = _LOCK_FILE,
    ) -> None:
        self._acquired = False
        self._handle: Optional[int] = None      # Windows mutex HANDLE
        self._fd: Optional[int] = None          # Linux lock-file fd
        self._win_mutex_name = win_mutex_name
        self._lock_file = Path(lock_file)

    # public API

    def acquire(self) -> bool:
        if self._acquired:
            return True
        if sys.platform == 'win32':
            ok = self._acquire_windows()
        else:
            ok = self._acquire_posix()
        self._acquired = ok
        return ok

    def release(self) -> None:
        """Drop the lock. Safe to call when never acquired, and safe to call
        twice — exit paths are messy and this must never be the thing that
        raises on shutdown."""
        if sys.platform == 'win32':
            if self._handle:
                try:
                    import ctypes
                    from ctypes import wintypes
                    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                    # Same x64 truncation trap as in _acquire_windows: without
                    # argtypes the handle is passed as a 32-bit int.
                    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                    kernel32.ReleaseMutex.restype = wintypes.BOOL
                    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel32.CloseHandle.restype = wintypes.BOOL
                    kernel32.ReleaseMutex(self._handle)
                    kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
                self._handle = None
        else:
            if self._fd is not None:
                try:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except Exception:
                    pass  # Closing the descriptor below drops the lock anyway.
                try:
                    os.close(self._fd)
                except OSError:
                    pass  # Already closed; release must never raise on exit.
                self._fd = None
        self._acquired = False

    # Context-manager support so callers can't forget to release.
    def __enter__(self) -> 'SingleInstance':
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    # platform implementations

    def _acquire_windows(self) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            # Use ctypes.get_last_error() with use_last_error=True. ctypes preserves
            # a private last-error copy around foreign calls; calling GetLastError
            # through ctypes can read the restored, unrelated value.
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

            # Explicit signatures matter on x64: the default restype is c_int,
            # which truncates a 64-bit HANDLE to 32 bits. The truncated value
            # is still truthy, so the bug would only surface later as a failing
            # ReleaseMutex/CloseHandle on shutdown.
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            # CreateMutexW returns a valid handle even when the mutex already
            # exists, so the only signal is the last error.
            handle = kernel32.CreateMutexW(None, False, self._win_mutex_name)
            last_error = ctypes.get_last_error()
            if not handle:
                return True   # fail open: cannot create the primitive at all
            if last_error == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except Exception:
            return True   # fail open

    def _acquire_posix(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return True   # fail open (no flock on this platform)
        try:
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            # 0o600: the lock lives in the user's own state dir; no reason for
            # any other account to read or write it.
            fd = os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True   # fail open: read-only home shouldn't block startup
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by another instance — this is the one real "no" case.
            try:
                os.close(fd)
            except OSError:
                pass  # The answer is already "no"; a close error changes nothing.
            return False
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass  # Failing open below; a close error changes nothing.
            return True   # fail open on anything unexpected

        # Record the PID purely as a diagnostic aid for bug reports. The lock
        # itself is the flock, never this number — so a torn or stale write
        # here cannot cause a false "already running".
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass  # Diagnostic only; the flock above is the lock.

        self._fd = fd
        return True
