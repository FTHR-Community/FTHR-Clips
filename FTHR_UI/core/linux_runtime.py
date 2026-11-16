"""Private per-user runtime paths for Linux hotkey IPC.

Use $XDG_RUNTIME_DIR/fthr, falling back to ~/.fthr/run. Directories must be
owned by the current user, have mode 0700, and not be symlinks. A private
parent prevents another user from replacing or pre-creating the socket.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import sys
from pathlib import Path

SOCKET_FILENAME = 'hotkey.sock'

# Historical path. Only used to clean up after an older build; never bound.
LEGACY_SOCKET_PATH = '/tmp/fthr_hotkey.sock'


class RuntimeDirError(RuntimeError):
    """The runtime directory is unusable and it is not safe to continue."""


def _validate_dir(d: Path) -> None:
    """Reject anything that is not a private directory owned by this user."""
    st = os.lstat(d)   # lstat: a symlink here is a finding, not a path to follow
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeDirError(f'{d} exists but is not a directory')
    if st.st_uid != os.getuid():
        raise RuntimeDirError(
            f'{d} is owned by uid {st.st_uid}, not by you ({os.getuid()}) — '
            f'refusing to use it')
    if st.st_mode & 0o077:
        raise RuntimeDirError(
            f'{d} has mode {stat.filemode(st.st_mode)}; it must not be '
            f'readable or writable by group or others')


def _candidate_bases() -> list[Path]:
    bases: list[Path] = []
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg:
        bases.append(Path(xdg) / 'fthr')
    bases.append(Path.home() / '.fthr' / 'run')
    return bases


def runtime_dir(create: bool = True) -> Path:
    """Resolve $XDG_RUNTIME_DIR/fthr, falling back to ~/.fthr/run.

    Create the directory if requested. Raise RuntimeDirError if both are unusable.
    """
    if not create:
        # Pure path query — used to *display* the socket location (generated
        # binds, setup instructions). Validating here would raise merely
        # because nothing has started yet.
        return _candidate_bases()[0]

    problems: list[str] = []
    for base in _candidate_bases():
        try:
            if create:
                # mode=0o700 is applied before anything can be placed inside,
                # and mkdir is atomic — there is no window with looser bits.
                base.mkdir(mode=0o700, parents=True, exist_ok=True)
                # exist_ok=True does NOT reapply the mode to a directory that
                # already existed, so assert it.
                if base.exists():
                    os.chmod(base, 0o700)
            _validate_dir(base)
            return base
        except (OSError, RuntimeDirError) as exc:
            problems.append(f'{base}: {exc}')
    raise RuntimeDirError(
        'no usable private runtime directory. Tried:\n  ' + '\n  '.join(problems))


def hotkey_socket_path(create_dir: bool = True) -> str:
    """Absolute path of the hotkey socket, inside the private runtime dir."""
    return str(runtime_dir(create=create_dir) / SOCKET_FILENAME)


def _is_live_socket(path: str) -> bool:
    """True if something is actually listening on *path*."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(path)
        return True
    except OSError:
        # ECONNREFUSED / ENOENT — nobody is listening. Anything else (EACCES on
        # a socket we do not own) also means "not ours to reuse", and the
        # ownership check in prepare_socket_path() is what decides that.
        return False
    finally:
        s.close()


def prepare_socket_path(path: str) -> None:
    """Prepare a socket path for binding, or raise.

    Remove only a socket owned by this user with no listener. Leave other
    files, symlinks, foreign sockets, and live sockets untouched.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return                      # nothing there — the normal case
    except OSError as exc:
        raise RuntimeDirError(f'cannot inspect {path}: {exc}') from exc

    if stat.S_ISLNK(st.st_mode):
        raise RuntimeDirError(
            f'{path} is a symlink. Refusing to touch it — remove it by hand '
            f'after checking where it points.')
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeDirError(
            f'{path} exists and is not a socket ({stat.filemode(st.st_mode)}). '
            f'Refusing to delete a file FTHR did not create.')
    if st.st_uid != os.getuid():
        raise RuntimeDirError(
            f'{path} is owned by uid {st.st_uid}, not by you. Refusing to '
            f'remove another user\'s socket.')
    if _is_live_socket(path):
        raise RuntimeDirError(
            f'{path} is already in use by a running FTHRClips instance.')

    # Stale socket, ours, nobody home.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass                        # someone else cleaned up; fine
    except OSError as exc:
        raise RuntimeDirError(f'cannot remove stale socket {path}: {exc}') from exc


def cleanup_legacy_socket() -> str | None:
    """Remove the old /tmp socket iff it is ours and dead.

    Users upgrading from a build that used /tmp would otherwise leave a stray
    socket behind forever. The same ownership rules apply — if the path is a
    squatted file belonging to someone else, it is reported and left alone.
    """
    if sys.platform == 'win32':
        return None
    try:
        st = os.lstat(LEGACY_SOCKET_PATH)
    except OSError:
        return None
    if (stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()
            and not _is_live_socket(LEGACY_SOCKET_PATH)):
        try:
            os.unlink(LEGACY_SOCKET_PATH)
            return f'removed stale legacy socket {LEGACY_SOCKET_PATH}'
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                return f'could not remove {LEGACY_SOCKET_PATH}: {exc}'
    else:
        return (f'{LEGACY_SOCKET_PATH} exists but is not a stale socket of '
                f'yours — left untouched')
    return None
