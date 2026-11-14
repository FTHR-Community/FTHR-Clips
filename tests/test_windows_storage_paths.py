"""Regression coverage for redirected Windows storage and safe root changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FTHR_UI'))
pytest.importorskip('PySide6.QtCore')

from core import settings_manager
from core.settings_manager import (
    SettingsManager,
    StoragePathError,
    clips_directory_from,
    default_clips_directory,
    prepare_clips_storage,
    recording_directory_from,
)


def _windows_home(monkeypatch, tmp_path, *, videos=None):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    redirected = (tmp_path / 'OneDrive - Example' / 'Videos'
                  if videos is None else videos)
    monkeypatch.setattr(
        settings_manager, '_windows_videos_directory', lambda: redirected)
    return redirected


def _write_settings(home: Path, settings: dict) -> Path:
    config = home / '.fthr' / 'settings.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(settings), encoding='utf-8')
    return config


def test_fresh_windows_install_uses_redirected_videos_known_folder(
        tmp_path, monkeypatch):
    videos = _windows_home(monkeypatch, tmp_path)

    manager = SettingsManager()

    assert default_clips_directory() == videos / 'FTHR Clips'
    assert clips_directory_from(manager) == videos / 'FTHR Clips'
    assert recording_directory_from(manager) == videos / 'FTHR Clips' / 'Recordings'


def test_windows_known_folder_failure_is_actionable_and_has_no_home_fallback(
        tmp_path, monkeypatch):
    _windows_home(monkeypatch, tmp_path, videos='')

    with pytest.raises(StoragePathError, match='Videos'):
        default_clips_directory()


@pytest.mark.parametrize('saved', [
    {},
    {'clips_directory': str(Path('custom-library').resolve())},
])
def test_existing_settings_keep_legacy_or_configured_root_without_known_folder_lookup(
        tmp_path, monkeypatch, saved):
    _windows_home(monkeypatch, tmp_path)
    config = _write_settings(tmp_path, saved)
    legacy_clip = tmp_path / 'FTHR_Clips' / 'existing.mp4'
    legacy_clip.parent.mkdir(parents=True)
    legacy_clip.write_bytes(b'user media')
    calls = []
    monkeypatch.setattr(settings_manager, '_windows_videos_directory',
                        lambda: calls.append('called'))

    manager = SettingsManager()

    if 'clips_directory' in saved:
        expected = Path(saved['clips_directory']).resolve()
        assert clips_directory_from(manager) == expected
    else:
        expected = tmp_path / 'FTHR_Clips'
        assert clips_directory_from(manager) == expected
        assert recording_directory_from(manager) == expected / 'Recordings'
    assert manager.config_file == config
    assert calls == []
    assert legacy_clip.read_bytes() == b'user media'


def test_missing_recording_path_derives_from_configured_library_and_restart(
        tmp_path, monkeypatch):
    _windows_home(monkeypatch, tmp_path)
    custom = tmp_path / 'Archive' / 'clips'
    config = _write_settings(tmp_path, {'clips_directory': str(custom)})
    monkeypatch.setattr(settings_manager, '_windows_videos_directory',
                        lambda: pytest.fail('existing settings queried Videos'))

    manager = SettingsManager()
    assert manager.get('recording_directory') == str(custom / 'Recordings')
    assert recording_directory_from(manager) == custom / 'Recordings'
    assert manager.save_settings() is True

    restarted = SettingsManager()
    assert restarted.config_file == config
    assert clips_directory_from(restarted) == custom
    assert recording_directory_from(restarted) == custom / 'Recordings'


def test_explicit_recording_path_survives_library_root_change_in_settings(
        tmp_path, monkeypatch):
    _windows_home(monkeypatch, tmp_path)
    custom_recordings = tmp_path / 'Manual Capture'
    manager = SettingsManager()
    manager.set('recording_directory', str(custom_recordings))
    manager.set('clips_directory', str(tmp_path / 'Library'))

    assert recording_directory_from(manager) == custom_recordings


@pytest.mark.parametrize('key', ['clips_directory', 'recording_directory'])
def test_explicit_malformed_directory_raises_typed_actionable_error(
        tmp_path, monkeypatch, key):
    _windows_home(monkeypatch, tmp_path)
    values = {'clips_directory': str(tmp_path / 'Library'),
              'recording_directory': str(tmp_path / 'Library' / 'Recordings')}
    values[key] = ''
    settings = SimpleNamespace(get=lambda name, default=None: values.get(name, default))

    with pytest.raises(StoragePathError, match='storage|recording'):
        (clips_directory_from(settings) if key == 'clips_directory'
         else recording_directory_from(settings))


def test_configured_library_is_resolved_before_any_default_known_folder_lookup(
        tmp_path, monkeypatch):
    _windows_home(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(settings_manager, '_windows_videos_directory',
                        lambda: calls.append('called'))
    manager = SimpleNamespace(
        get=lambda key, default=None: str(tmp_path / 'Configured')
        if key == 'clips_directory' else default)

    assert clips_directory_from(manager) == tmp_path / 'Configured'
    assert calls == []


def test_prepare_clips_storage_creates_required_folders_and_probes_writable(
        tmp_path):
    root = tmp_path / 'Redirected' / 'FTHR Clips'

    prepared = prepare_clips_storage(root, probe_writable=True)

    assert prepared == root
    assert all((root / name).is_dir() for name in (
        'Desktop', 'Recordings', 'Exported', 'Shared', 'Screenshots'))
    assert not list(root.rglob('.fthr-storage-probe-*'))


def test_prepare_clips_storage_reports_directory_creation_failure(
        tmp_path, monkeypatch):
    root = tmp_path / 'blocked'
    original_mkdir = Path.mkdir

    def denied_mkdir(path, *args, **kwargs):
        if path == root:
            raise PermissionError('access denied')
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', denied_mkdir)

    with pytest.raises(StoragePathError, match='Choose a writable folder'):
        prepare_clips_storage(root, probe_writable=True)


def test_settings_save_returns_false_when_parent_creation_fails(
        tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    manager = SettingsManager()
    original_mkdir = Path.mkdir

    def denied_mkdir(path, *args, **kwargs):
        if path == manager.config_file.parent:
            raise PermissionError('access denied')
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', denied_mkdir)

    assert manager.save_settings() is False


def test_clip_folder_browse_cancel_is_a_noop(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    videos = _windows_home(monkeypatch, tmp_path)
    manager = SettingsManager()

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('unchanged')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    signals = []
    page.clips_directory_changed.connect(signals.append)
    monkeypatch.setattr(
        'main.QFileDialog.getExistingDirectory', lambda *args: '')

    _SettingsPage._on_clips_folder_browse(page)

    assert clips_directory_from(manager) == videos / 'FTHR Clips'
    assert page.clips_directory_edit.text() == 'unchanged'
    assert signals == []


@pytest.mark.parametrize('chooser', ['browse', 'default'])
def test_clip_folder_change_save_failure_rolls_back_without_ui_or_grid_change(
        qapp, tmp_path, monkeypatch, chooser):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    videos = _windows_home(monkeypatch, tmp_path)
    old_root = tmp_path / 'Old Library'
    old_recording = old_root / 'Recordings'
    manager = SettingsManager()
    manager.set('clips_directory', str(old_root))
    manager.set('recording_directory', str(old_recording))
    manager.save_settings()

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('before')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    signals = []
    page.clips_directory_changed.connect(signals.append)
    monkeypatch.setattr(
        'main.FthrMessageDialog.warning', lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, 'save_settings', lambda: False)
    if chooser == 'browse':
        monkeypatch.setattr(
            'main.QFileDialog.getExistingDirectory',
            lambda *args: str(tmp_path / 'Next Library'))
        _SettingsPage._on_clips_folder_browse(page)
    else:
        _SettingsPage._on_clips_folder_use_default(page)

    assert manager.get('clips_directory') == str(old_root)
    assert manager.get('recording_directory') == str(old_recording)
    assert page.clips_directory_edit.text() == 'before'
    assert signals == []
    expected_new_root = (tmp_path / 'Next Library' if chooser == 'browse'
                         else videos / 'FTHR Clips')
    assert expected_new_root.is_dir()


def test_browse_can_repair_invalid_clips_root_without_saving_chooser_start_path(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    _windows_home(monkeypatch, tmp_path)
    manager = SettingsManager()
    manager.set('clips_directory', '')
    manager.set('recording_directory', None)

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('invalid')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    selected = tmp_path / 'Repaired Library'
    calls = []
    monkeypatch.setattr(
        'main.FthrMessageDialog.warning', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        'main.QFileDialog.getExistingDirectory',
        lambda *args: calls.append(args) or str(selected))

    _SettingsPage._on_clips_folder_browse(page)

    assert calls and calls[0][2] == str(tmp_path)
    assert manager.get('clips_directory') == str(selected)
    assert manager.get('recording_directory') == str(selected / 'Recordings')
    assert page.clips_directory_edit.text() == str(selected)


def test_recording_browse_can_repair_invalid_custom_path(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    _windows_home(monkeypatch, tmp_path)
    root = tmp_path / 'Library'
    manager = SettingsManager()
    manager.set('clips_directory', str(root))
    manager.set('recording_directory', '')

    class Page(QObject):
        def __init__(self):
            super().__init__()
            self.sm = manager
            self.recording_directory_edit = QLineEdit('invalid')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

    page = Page()
    selected = root / 'Recordings'
    calls = []
    monkeypatch.setattr(
        'main.FthrMessageDialog.warning', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        'main.QFileDialog.getExistingDirectory',
        lambda *args: calls.append(args) or str(selected))

    _SettingsPage._on_recording_folder_browse(page)

    assert calls and calls[0][2] == str(tmp_path)
    assert manager.get('recording_directory') == str(selected)
    assert page.recording_directory_edit.text() == str(selected)


def test_use_default_moves_only_default_derived_recording_path(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    videos = _windows_home(monkeypatch, tmp_path)
    old_root = tmp_path / 'Old Library'
    old_manual = old_root / 'Recordings'
    old_root.mkdir()
    (old_root / 'existing.mp4').write_bytes(b'leave here')
    manager = SettingsManager()
    manager.set('clips_directory', str(old_root))
    manager.set('recording_directory', str(old_manual))

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('before')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    signals = []
    page.clips_directory_changed.connect(signals.append)
    _SettingsPage._on_clips_folder_use_default(page)

    assert manager.get('clips_directory') == str(videos / 'FTHR Clips')
    assert manager.get('recording_directory') == str(videos / 'FTHR Clips' / 'Recordings')
    assert page.clips_directory_edit.text() == str(videos / 'FTHR Clips')
    assert signals == [str(videos / 'FTHR Clips')]
    assert (videos / 'FTHR Clips' / 'Screenshots').is_dir()
    assert (old_root / 'existing.mp4').read_bytes() == b'leave here'


def test_use_default_preserves_custom_manual_recording_folder(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    videos = _windows_home(monkeypatch, tmp_path)
    old_root = tmp_path / 'Old Library'
    custom_recordings = tmp_path / 'Manual Capture'
    manager = SettingsManager()
    manager.set('clips_directory', str(old_root))
    manager.set('recording_directory', str(custom_recordings))

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('before')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    _SettingsPage._on_clips_folder_use_default(page)

    assert manager.get('clips_directory') == str(videos / 'FTHR Clips')
    assert manager.get('recording_directory') == str(custom_recordings)


def test_missing_storage_root_does_not_switch_to_a_fallback_root(
        qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ui import clip_grid

    missing = tmp_path / 'unavailable' / 'FTHR Clips'
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(missing), 'imported_clip_folders': [],
        'hotkeys': {},
    }.get(key, default))
    grid = clip_grid.ClipGrid(settings)

    assert Path(grid.clips_dir) == missing
    assert missing.is_dir()


def test_clip_grid_rejects_root_before_mutating_watchers(
        qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ui import clip_grid

    current = tmp_path / 'Current Library'
    current.mkdir()
    target = tmp_path / 'Unavailable Library'
    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(current), 'imported_clip_folders': [],
        'hotkeys': {},
    }.get(key, default))
    grid = clip_grid.ClipGrid(settings)
    watched_before = set(grid._watcher.directories())

    def unavailable(*_args, **_kwargs):
        raise StoragePathError('Choose a writable folder')

    monkeypatch.setattr(clip_grid, 'ensure_storage_directory', unavailable)
    monkeypatch.setattr(
        clip_grid.FthrMessageDialog, 'warning', lambda *args: None)

    grid.set_clips_directory(str(target))

    assert Path(grid.clips_dir) == current
    assert set(grid._watcher.directories()) == watched_before
    assert not target.exists()


def test_prepared_settings_signal_handoff_keeps_selected_root_on_late_failure(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage
    from ui import clip_grid

    old_root = tmp_path / 'Current Library'
    old_root.mkdir()
    target = tmp_path / 'Selected Library'
    manager = SettingsManager()
    manager.set('clips_directory', str(old_root))
    manager.set('recording_directory', None)
    manager.save_settings = lambda: True

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('before')
            self.recording_directory_edit = QLineEdit('before')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    grid = clip_grid.ClipGrid(manager)
    # The settings preflight succeeded, but the selected mount disappears
    # before the signal reaches the grid.
    monkeypatch.setattr('main.prepare_clips_storage', lambda *_a, **_k: target)
    warnings = []
    monkeypatch.setattr(
        clip_grid.FthrMessageDialog, 'warning',
        lambda _parent, title, detail: warnings.append((title, detail)))
    real_ensure = clip_grid.ensure_storage_directory

    def unavailable_selected_root(directory, **kwargs):
        if Path(directory) == target:
            raise StoragePathError(
                f'FTHR cannot use the storage folder "{target}"')
        return real_ensure(directory, **kwargs)

    monkeypatch.setattr(
        clip_grid, 'ensure_storage_directory', unavailable_selected_root)
    page.clips_directory_changed.connect(
        lambda path: grid.set_clips_directory(path, prepared=True))

    assert page._commit_clips_directory(target)

    assert manager.get('clips_directory') == str(target)
    assert manager.get('recording_directory') == str(target / 'Recordings')
    assert page.clips_directory_edit.text() == str(target)
    assert Path(grid.clips_dir) == target
    assert str(old_root) not in grid._watcher.directories()
    assert warnings and str(target) in warnings[0][1]
    grid.shutdown()


def test_root_change_preserves_raw_invalid_custom_recording_value(
        qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QLineEdit
    from main import _SettingsPage

    _windows_home(monkeypatch, tmp_path)
    old_root = tmp_path / 'Old Library'
    invalid_manual_value = {'bad': ['saved', 'value']}
    manager = SettingsManager()
    manager.set('clips_directory', str(old_root))
    manager.set('recording_directory', invalid_manual_value)
    manager.save_settings = lambda: True
    target = tmp_path / 'New Library'

    class Page(QObject):
        clips_directory_changed = Signal(str)

        def __init__(self):
            super().__init__()
            self.sm = manager
            self.clips_directory_edit = QLineEdit('before')
            self.recording_directory_edit = QLineEdit('unchanged')

        def _report_storage_change_failure(self, error):
            _SettingsPage._report_storage_change_failure(self, error)

        def _commit_clips_directory(self, directory):
            return _SettingsPage._commit_clips_directory(self, directory)

    page = Page()
    monkeypatch.setattr('main.prepare_clips_storage', lambda *_a, **_k: target)

    assert page._commit_clips_directory(target)

    assert manager.get('clips_directory') == str(target)
    assert manager.get('recording_directory') == invalid_manual_value
    assert page.recording_directory_edit.text() == 'unchanged'


def test_upload_manager_uses_central_root_resolver_and_propagates_bad_path(
        tmp_path):
    from types import SimpleNamespace
    from core.upload_manager import UploadManager

    manager = UploadManager.__new__(UploadManager)
    manager._core_settings = SimpleNamespace(
        get=lambda key, default=None: str(tmp_path / 'Chosen Library')
        if key == 'clips_directory' else default)
    assert manager.clips_directory() == tmp_path / 'Chosen Library'

    manager._core_settings = SimpleNamespace(
        get=lambda key, default=None: None if key == 'clips_directory' else default)
    with pytest.raises(StoragePathError):
        manager.clips_directory()


def test_settings_path_display_uses_resolver_and_keeps_invalid_value_repairable(
        tmp_path, monkeypatch):
    from types import SimpleNamespace
    from main import _SettingsPage

    manager = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path / 'Library'),
        'recording_directory': None,
    }.get(key, default))
    page = SimpleNamespace(sm=manager)
    assert _SettingsPage._storage_path_for_display(
        page, 'recording_directory', recording_directory_from
    ) == str(tmp_path / 'Library' / 'Recordings')

    manager.get = lambda key, default=None: {
        'clips_directory': str(tmp_path / 'Library'),
        'recording_directory': '',
    }.get(key, default)
    assert _SettingsPage._storage_path_for_display(
        page, 'recording_directory', recording_directory_from
    ) == ''


def test_upload_interval_scan_reports_unavailable_storage_without_fallback(
        monkeypatch):
    import threading
    from core import upload_manager
    from core.upload_manager import UploadManager

    manager = UploadManager.__new__(UploadManager)
    manager._stop_event = threading.Event()
    manager._interval_scan_cancel = threading.Event()
    manager._interval_scan_lock = threading.Lock()
    manager._interval_scan_thread = None
    manager.clips_directory = lambda: (_ for _ in ()).throw(
        StoragePathError('storage root unavailable'))
    events = []
    monkeypatch.setattr(
        upload_manager, 'emit_event',
        lambda *args, **kwargs: events.append((args, kwargs)), raising=False)

    manager._interval_scan_worker(manager._interval_scan_cancel)

    assert len(events) == 1
    args, fields = events[0]
    assert args[:2] == ('upload', 'library_scan_unavailable')
    assert fields['state'] == 'RETRYING'
    assert fields['reason'] == 'storage_unavailable'
    assert len(fields['detail']) <= 200
