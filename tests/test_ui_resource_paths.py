"""GUI status work must stay cheap without becoming an execution authority."""
from collections import Counter
import ctypes
import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from core import clip_readiness, game_detector, library_index, upload_manager


def _uploader(monkeypatch, tmp_path):
    receipt_path = tmp_path / 'activation.json'
    monkeypatch.setattr(upload_manager, '_UPLOADER_ROOT', tmp_path)
    monkeypatch.setattr(upload_manager, '_UPLOADER_ACTIVATION_FILE', receipt_path)
    monkeypatch.setattr(upload_manager, '_SETTINGS_FILE', tmp_path / 'settings.json')
    monkeypatch.setattr(upload_manager, 'EXPECTED_UPLOADER_BUNDLE_SHA256', 'b' * 64)
    executable = tmp_path / 'uploader.exe'
    executable.write_bytes(b'test-executable')
    receipt = dict(
        plugin_id=upload_manager.UPLOADER_PLUGIN_ID,
        plugin_version=upload_manager.UPLOADER_PLUGIN_VERSION,
        bundle_sha256='b' * 64, executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        terms_version=upload_manager.UPLOADER_TERMS_VERSION,
        privacy_version=upload_manager.UPLOADER_PRIVACY_VERSION)
    receipt_path.write_text(json.dumps(receipt))
    manager = upload_manager.UploadManager(SimpleNamespace(get=lambda _key, default=None: default))
    manager.set('upload_enabled', True)
    return manager, executable, receipt_path, receipt


def test_card_status_hashes_unchanged_plugin_once(monkeypatch, tmp_path, qtbot):
    manager, _executable, _receipt_path, _receipt = _uploader(monkeypatch, tmp_path)
    hashes = []
    original = upload_manager._sha256

    def digest(path):
        hashes.append(path)
        return original(path)

    monkeypatch.setattr(upload_manager, '_sha256', digest)
    assert all(manager.is_enabled_for_display() for _ in range(64))
    assert len(hashes) == 1
    manager.set('upload_enabled', False)
    assert not manager.is_enabled_for_display()
    assert len(hashes) == 1


@pytest.mark.parametrize('change', ['executable', 'receipt', 'delete'])
def test_card_status_invalidates_changed_installation(monkeypatch, tmp_path, qtbot, change):
    manager, executable, receipt_path, receipt = _uploader(monkeypatch, tmp_path)
    assert manager.is_enabled_for_display()
    if change == 'executable':
        executable.write_bytes(b'modified-executable-with-a-different-size')
    elif change == 'receipt':
        receipt['privacy_version'] = 'outdated'
        receipt_path.write_text(json.dumps(receipt))
    else:
        executable.unlink()
    assert not manager.is_enabled_for_display()


def test_display_cache_never_authorizes_plugin_execution(monkeypatch, tmp_path, qtbot):
    manager, _executable, _receipt_path, _receipt = _uploader(monkeypatch, tmp_path)
    assert manager.is_enabled_for_display()
    monkeypatch.setattr(upload_manager, '_sha256', lambda _path: 'invalid')
    assert manager.is_enabled_for_display()
    assert not manager.is_enabled()
    with pytest.raises(PermissionError, match='not installed'):
        manager._activation(upload_manager._uploader_spec())


def test_card_entry_exits_before_large_imports_and_keeps_startup_guard(monkeypatch):
    import builtins
    from core import windows_startup

    calls = []
    policy = SimpleNamespace(status='enabled', error_code=0)

    def apply_policy():
        calls.append('policy')
        return policy

    def logging(*, startup_policy_result):
        assert startup_policy_result is policy
        calls.append('logging')

    monkeypatch.setattr(windows_startup, 'apply_startup_policy', apply_policy)
    monkeypatch.setitem(sys.modules, 'core.startup_logging', SimpleNamespace(setup_file_logging=logging))
    monkeypatch.setitem(sys.modules, 'ui.capture_card_process', SimpleNamespace(main=lambda: calls.append('card')))
    monkeypatch.setattr(sys, 'argv', ['FTHR.exe', '--card-process'])
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {'ui.icons', 'ui.motion', 'numpy', 'PySide6.QtQuickWidgets'}:
            pytest.fail(f'card entry reached unnecessary import: {name}')
        return original(name, *args, **kwargs)

    with monkeypatch.context() as imports:
        imports.setattr(builtins, '__import__', guarded_import)
        with pytest.raises(SystemExit) as result:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'main.py'), run_name='__main__')
    assert result.value.code is None
    assert calls == ['policy', 'logging', 'card']


def test_library_scan_canonicalizes_each_media_path_once(monkeypatch, tmp_path):
    for index in range(6):
        (tmp_path / f'clip-{index}.mp4').write_bytes(b'')
    calls = Counter()
    original = library_index.canonical_media_path

    def canonical(path):
        if str(path).endswith('.mp4'):
            calls[str(path)] += 1
        return original(path)

    monkeypatch.setattr(library_index, 'canonical_media_path', canonical)
    result = library_index.scan_library(tmp_path)
    assert len(result.records) == 6
    assert set(calls.values()) == {1}


def test_readiness_status_resolves_one_key_and_preserves_blocks(monkeypatch, tmp_path):
    path = tmp_path / 'ready.mp4'
    path.write_bytes(b'video')
    registry = clip_readiness.ClipReadinessRegistry()
    registry.engine_committed(path, needs_finalization=False)
    calls = []
    original = clip_readiness._key

    def key(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(clip_readiness, '_key', key)
    assert registry.can_access(path)
    assert len(calls) == 1
    registry.block_paths([path])
    calls.clear()
    assert registry.state(path) == clip_readiness.ClipReadinessState.RECOVERY_REQUIRED
    assert len(calls) == 1


@pytest.mark.parametrize('path_error', [False, True])
def test_game_metadata_uses_one_handle_even_with_partial_failure(monkeypatch, path_error):
    calls = []

    def query_path(handle, _flags, buffer, _size):
        calls.append(('path', handle))
        if path_error:
            raise OSError('access denied')
        buffer.value = r'C:\Games\game.exe'
        return True

    def query_times(handle, created, *_args):
        calls.append(('times', handle))
        created._obj.dwLowDateTime = 123
        return True

    def open_process(*_args):
        calls.append(('open', 41))
        return 41

    monkeypatch.setattr(game_detector, '_KERNEL32', SimpleNamespace(
        OpenProcess=open_process, QueryFullProcessImageNameW=query_path,
        GetProcessTimes=query_times, CloseHandle=lambda handle: calls.append(('close', handle))))
    assert game_detector._process_metadata(202) == ('' if path_error else r'C:\Games\game.exe', 123)
    assert calls == [('open', 41), ('path', 41), ('times', 41), ('close', 41)]


def test_text_window_metadata_preserves_name_without_loading_icon(monkeypatch):
    from ui import capture_settings_widget as widget

    def shell_info(_path, _attributes, pointer, _size, _flags):
        pointer._obj.szDisplayName = 'Example.exe'
        return 1

    monkeypatch.setattr(widget, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(
        shell32=SimpleNamespace(SHGetFileInfoW=shell_info)), raising=False)
    monkeypatch.setattr(widget, 'QFileIconProvider', lambda: pytest.fail('text picker loaded an icon'))
    assert widget._get_exe_info('example.exe', include_icon=False) == ('Example', None)
