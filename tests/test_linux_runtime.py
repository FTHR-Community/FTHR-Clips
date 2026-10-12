"""Verify private Linux runtime paths and safe socket reuse.

Require user-owned 0700 directories; preserve foreign files, symlinks,
and sockets with active listeners. POSIX-only fixtures use tmp_path.
"""

import os
import socket
import stat
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == 'win32', reason='POSIX runtime directory semantics')

from core import linux_runtime  # noqa: E402


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Point XDG_RUNTIME_DIR at a private tmp dir, like a real login session."""
    base = tmp_path / 'run-user'
    base.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(base))
    return base


# Path selection

def test_socket_lives_under_xdg_runtime_dir(xdg):
    p = linux_runtime.hotkey_socket_path()
    assert p == str(xdg / 'fthr' / 'hotkey.sock')


def test_socket_dir_is_private_not_world_writable(xdg):
    """The property that matters is the directory's mode, not its prefix.

    Asserting `not p.startswith("/tmp")` would be wrong here — pytest's own
    tmp_path lives under /tmp. What made the old location unsafe was that /tmp
    itself is world-writable, so the path could be squatted before we bound it.
    """
    p = linux_runtime.hotkey_socket_path()
    assert p != linux_runtime.LEGACY_SOCKET_PATH
    st = os.lstat(os.path.dirname(p))
    assert st.st_uid == os.getuid()
    assert st.st_mode & 0o077 == 0, stat.filemode(st.st_mode)


def test_falls_back_to_home_when_xdg_runtime_dir_missing(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    p = linux_runtime.hotkey_socket_path()
    assert p == str(tmp_path / '.fthr' / 'run' / 'hotkey.sock')
    assert os.lstat(os.path.dirname(p)).st_mode & 0o077 == 0, \
        'the fallback must also be private'


def test_falls_back_when_xdg_runtime_dir_is_unusable(tmp_path, monkeypatch):
    """A broken XDG_RUNTIME_DIR must degrade, not kill hotkeys outright."""
    bogus = tmp_path / 'not-a-dir'
    bogus.write_text('i am a file')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(bogus))
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr('pathlib.Path.home', lambda: home)
    p = linux_runtime.hotkey_socket_path()
    assert p.startswith(str(home))


# Directory permissions and ownership

def test_runtime_dir_is_created_0700(xdg):
    d = linux_runtime.runtime_dir()
    st = os.lstat(d)
    assert stat.S_ISDIR(st.st_mode)
    assert st.st_mode & 0o777 == 0o700, stat.filemode(st.st_mode)
    assert st.st_uid == os.getuid()


def test_loose_permissions_on_existing_dir_are_tightened(xdg):
    d = xdg / 'fthr'
    d.mkdir(mode=0o755)
    assert os.lstat(d).st_mode & 0o077   # precondition: group/other bits set
    linux_runtime.runtime_dir()
    assert os.lstat(d).st_mode & 0o777 == 0o700


def test_symlinked_runtime_dir_is_rejected(tmp_path, monkeypatch):
    """A symlink where the directory should be is a finding, not a path."""
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir(mode=0o700)
    base = tmp_path / 'run-user'
    base.mkdir(mode=0o700)
    (base / 'fthr').symlink_to(elsewhere)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(base))
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setattr('pathlib.Path.home', lambda: home)
    # Must not use the symlinked path; falls back to the private home dir.
    d = linux_runtime.runtime_dir()
    assert str(d).startswith(str(home))


def test_runtime_dir_error_when_nothing_is_usable(tmp_path, monkeypatch):
    blocked = tmp_path / 'blocked'
    blocked.write_text('file, not a dir')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(blocked))
    monkeypatch.setattr('pathlib.Path.home', lambda: blocked)
    with pytest.raises(linux_runtime.RuntimeDirError):
        linux_runtime.runtime_dir()


# prepare_socket_path — what may and may not be deleted

def test_missing_path_is_fine(xdg):
    linux_runtime.prepare_socket_path(linux_runtime.hotkey_socket_path())


def test_stale_socket_of_ours_is_removed(xdg):
    p = linux_runtime.hotkey_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(p)
    s.close()                      # bound then closed = stale, nobody listening
    assert os.path.exists(p)
    linux_runtime.prepare_socket_path(p)
    assert not os.path.exists(p), 'a dead socket of ours should be reclaimed'


def test_regular_file_is_never_deleted(xdg):
    """The core of the /tmp squatting bug: do not unlink what we did not make."""
    p = linux_runtime.hotkey_socket_path()
    with open(p, 'w') as fh:
        fh.write('not a socket')
    with pytest.raises(linux_runtime.RuntimeDirError, match='not a socket'):
        linux_runtime.prepare_socket_path(p)
    assert os.path.exists(p), 'the file must still be there'
    assert open(p).read() == 'not a socket'


def test_symlink_is_never_followed_or_deleted(xdg, tmp_path):
    victim = tmp_path / 'precious.txt'
    victim.write_text('do not delete me')
    p = linux_runtime.hotkey_socket_path()
    os.symlink(victim, p)
    with pytest.raises(linux_runtime.RuntimeDirError, match='symlink'):
        linux_runtime.prepare_socket_path(p)
    assert victim.exists() and victim.read_text() == 'do not delete me'
    assert os.path.islink(p), 'the symlink itself must be left for the user'


def test_live_socket_is_not_stolen(xdg):
    """A second instance must not yank the socket out from under the first."""
    p = linux_runtime.hotkey_socket_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(p)
    srv.listen(1)
    try:
        with pytest.raises(linux_runtime.RuntimeDirError, match='already in use'):
            linux_runtime.prepare_socket_path(p)
        assert os.path.exists(p)
    finally:
        srv.close()
        os.unlink(p)


def test_directory_at_socket_path_is_refused(xdg):
    p = linux_runtime.hotkey_socket_path()
    os.mkdir(p)
    with pytest.raises(linux_runtime.RuntimeDirError):
        linux_runtime.prepare_socket_path(p)
    assert os.path.isdir(p)


# Bind end to end — mode 0600 (AUDIT-003)

def test_bound_socket_is_owner_only(xdg):
    """The AUDIT-003 property, re-asserted at the new path."""
    p = linux_runtime.hotkey_socket_path()
    linux_runtime.prepare_socket_path(p)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        srv.bind(p)
    finally:
        os.umask(old)
    try:
        st = os.lstat(p)
        assert stat.S_ISSOCK(st.st_mode)
        assert st.st_uid == os.getuid()
        assert st.st_mode & 0o777 == 0o600, stat.filemode(st.st_mode)
        # And the directory above it is private too, which is the part the
        # mode alone never gave us.
        assert os.lstat(os.path.dirname(p)).st_mode & 0o077 == 0
    finally:
        srv.close()
        os.unlink(p)


def test_cleanup_after_clean_shutdown(xdg):
    p = linux_runtime.hotkey_socket_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(p)
    srv.listen(1)
    srv.close()
    os.unlink(p)                                  # what _serve() does on exit
    assert not os.path.exists(p)
    linux_runtime.prepare_socket_path(p)          # next start is clean


def test_cleanup_after_crash(xdg):
    """A crash leaves the node behind; the next start must reclaim it."""
    p = linux_runtime.hotkey_socket_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(p)
    srv.listen(1)
    srv.close()                                   # no unlink — simulates a crash
    assert os.path.exists(p)
    linux_runtime.prepare_socket_path(p)
    assert not os.path.exists(p)


# Legacy /tmp socket migration

def test_legacy_cleanup_leaves_foreign_paths_alone(monkeypatch, tmp_path):
    legacy = tmp_path / 'fthr_hotkey.sock'
    legacy.write_text('someone else made this')
    monkeypatch.setattr(linux_runtime, 'LEGACY_SOCKET_PATH', str(legacy))
    msg = linux_runtime.cleanup_legacy_socket()
    assert legacy.exists(), 'a non-socket at the legacy path must survive'
    assert msg and 'left untouched' in msg


def test_legacy_cleanup_removes_our_dead_socket(monkeypatch, tmp_path):
    legacy = tmp_path / 'fthr_hotkey.sock'
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(legacy))
    s.close()
    monkeypatch.setattr(linux_runtime, 'LEGACY_SOCKET_PATH', str(legacy))
    msg = linux_runtime.cleanup_legacy_socket()
    assert not legacy.exists()
    assert msg and 'removed stale legacy socket' in msg


def test_child_environment_restores_the_host_library_path():
    frozen = {'LD_LIBRARY_PATH': '/mnt/app/_internal', 'LD_LIBRARY_PATH_ORIG': '/opt/lib',
              'PATH': '/usr/bin'}
    env = linux_runtime.child_process_environment(frozen)
    assert env['LD_LIBRARY_PATH'] == '/opt/lib'
    assert 'LD_LIBRARY_PATH_ORIG' not in env
    assert env['PATH'] == '/usr/bin'
    assert frozen['LD_LIBRARY_PATH'] == '/mnt/app/_internal'   # input untouched


def test_child_environment_drops_the_bundle_path_when_host_had_none():
    env = linux_runtime.child_process_environment(
        {'LD_LIBRARY_PATH': '/mnt/app/_internal', 'LD_LIBRARY_PATH_ORIG': ''})
    assert 'LD_LIBRARY_PATH' not in env


def test_child_environment_is_identity_outside_pyinstaller():
    env = linux_runtime.child_process_environment({'LD_LIBRARY_PATH': '/custom'})
    assert env == {'LD_LIBRARY_PATH': '/custom'}


def test_restore_is_a_no_op_when_not_frozen(monkeypatch):
    monkeypatch.setenv('LD_LIBRARY_PATH', '/mnt/app/_internal')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', '/opt/lib')
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    linux_runtime.restore_library_search_path_for_children()
    assert os.environ['LD_LIBRARY_PATH'] == '/mnt/app/_internal'


def test_restore_rewrites_the_process_environment_when_frozen(monkeypatch):
    monkeypatch.setenv('LD_LIBRARY_PATH', '/mnt/app/_internal')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', '')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    linux_runtime.restore_library_search_path_for_children()
    assert 'LD_LIBRARY_PATH' not in os.environ
    assert 'LD_LIBRARY_PATH_ORIG' not in os.environ

