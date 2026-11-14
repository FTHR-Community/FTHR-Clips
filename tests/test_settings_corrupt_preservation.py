"""Corrupt configuration bytes survive fallback and every failed preservation stage."""
import builtins
import json
from pathlib import Path

import pytest

from core import settings_manager


def _config(tmp_path, monkeypatch, raw=b'{"path":"C:\\invalid\\escape"}'):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.fthr' / 'settings.json'
    config.parent.mkdir(exist_ok=True)
    config.write_bytes(raw)
    return config


def test_prior_quarantine_is_never_overwritten(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    original = config.read_bytes()
    prior = config.with_suffix('.json.corrupt')
    prior.write_bytes(b'previous irreplaceable diagnostics')
    settings_manager.SettingsManager()
    assert prior.read_bytes() == b'previous irreplaceable diagnostics'
    assert config.read_bytes() == original
    backups = tuple(config.parent.glob('settings.json.corrupt-*'))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_failed_preservation_blocks_save_before_any_write(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    original = config.read_bytes()
    prior = config.with_suffix('.json.corrupt')
    prior.write_bytes(b'old quarantine')
    def fail(*args, **kwargs):
        raise OSError('preservation unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(settings_manager.tempfile, 'NamedTemporaryFile', fail)
        patch.setattr(Path, 'replace', fail)
        manager = settings_manager.SettingsManager()
    attempts = []
    original_open = builtins.open
    def track_open(*args, **kwargs):
        attempts.append(args)
        return original_open(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', track_open)
        assert manager.save_settings() is False
    assert attempts == []
    assert config.read_bytes() == original
    assert prior.read_bytes() == b'old quarantine'


def test_repeated_corrupt_loads_create_distinct_exact_backups(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, b'\xef\xbb\xbf{"bad":"C:\\invalid\\escape"}\r\n')
    original = config.read_bytes()
    first = settings_manager.SettingsManager()
    second = settings_manager.SettingsManager()
    assert first._corrupt_backup_path != second._corrupt_backup_path
    assert first._corrupt_backup_verified and second._corrupt_backup_verified
    assert first._corrupt_backup_path.read_bytes() == original
    assert second._corrupt_backup_path.read_bytes() == original
    assert config.read_bytes() == original


@pytest.mark.parametrize('stage', ['read', 'create', 'write', 'short_write', 'flush', 'fsync', 'close'])
def test_preservation_io_failure_latches_all_writers_before_target_writes(
        tmp_path, monkeypatch, stage, capsys):
    from core.game_profiles import create_profile
    from core.capture_admission import with_enabled

    config = _config(tmp_path, monkeypatch)
    original = config.read_bytes()
    prior = config.with_suffix('.json.corrupt')
    prior.write_bytes(b'keep prior quarantine')
    factory = settings_manager.tempfile.NamedTemporaryFile
    read_bytes = Path.read_bytes
    def fail(*args, **kwargs):
        raise OSError(f'injected {stage} failure')
    class Backup:
        def __init__(self, *args, **kwargs):
            self.real = factory(*args, **kwargs)
            self.name = self.real.name
        def __enter__(self):
            self.real.__enter__()
            return self
        def __exit__(self, *args):
            result = self.real.__exit__(*args)
            if stage == 'close':
                fail()
            return result
        def write(self, raw):
            if stage == 'write':
                fail()
            if stage == 'short_write':
                return self.real.write(raw[:3])
            return self.real.write(raw)
        def flush(self):
            if stage == 'flush':
                fail()
            return self.real.flush()
        def fileno(self):
            return self.real.fileno()
    with monkeypatch.context() as patch:
        if stage == 'read':
            def read(path):
                if path == config:
                    fail()
                return read_bytes(path)
            patch.setattr(Path, 'read_bytes', read)
        elif stage == 'create':
            patch.setattr(settings_manager.tempfile, 'NamedTemporaryFile', fail)
        else:
            patch.setattr(settings_manager.tempfile, 'NamedTemporaryFile', Backup)
        if stage == 'fsync':
            patch.setattr(settings_manager.os, 'fsync', fail)
        manager = settings_manager.SettingsManager()
    assert manager._persistence_blocked
    assert manager._corrupt_backup_verified is False
    assert manager.get('clip_length') is not None
    assert config.read_bytes() == original
    assert prior.read_bytes() == b'keep prior quarantine'
    assert 'persistence blocked' in capsys.readouterr().out.lower()
    if stage not in ('read', 'create'):
        assert manager._corrupt_backup_path.is_file()
    catalog = manager.read_game_capture_profiles()
    profile_candidate = create_profile(catalog, 'Game', r'C:\Games\Game.exe')
    policy = manager.read_capture_admission_policy()
    policy_candidate = with_enabled(policy, True)
    def forbidden(*args, **kwargs):
        pytest.fail('Blocked persistence must not create temporary files or write targets')
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(Path, 'mkdir', forbidden)
        patch.setattr(settings_manager.os, 'replace', forbidden)
        patch.setattr(settings_manager.tempfile, 'NamedTemporaryFile', forbidden)
        assert manager._persist_settings_candidate({'safe': False}) is False
        assert manager.save_settings() is False
        assert manager.save_game_capture_profiles(profile_candidate, expected_revision=0) is False
        assert manager.save_capture_admission_policy(policy_candidate, expected_revision=0) is False
    assert manager.read_game_capture_profiles() == catalog
    assert manager.read_capture_admission_policy() == policy
    assert config.read_bytes() == original
    assert prior.read_bytes() == b'keep prior quarantine'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16'])
def test_valid_unicode_and_secondary_path_are_not_repaired_or_rewritten(tmp_path, monkeypatch, encoding):
    values = {'navigation_bindings_version': 5, 'clips_directory': r'D:\Clips\東京',
              'recording_directory': r'E:\Recordings\Café', 'custom_text': 'Café\t東京'}
    raw = json.dumps(values, ensure_ascii=False).encode(encoding)
    config = _config(tmp_path, monkeypatch, raw)
    manager = settings_manager.SettingsManager()
    assert all(manager.get(key) == value for key, value in values.items())
    assert config.read_bytes() == raw
    assert manager._corrupt_backup_path is None
    assert not manager._persistence_blocked


def test_backup_uses_exact_bytes_that_were_parsed_not_a_second_read(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    parsed = config.read_bytes()
    loads = settings_manager.json.loads
    replacement = b'{"external_editor":"newer data"}'
    def concurrent_edit(raw):
        assert raw == parsed
        config.write_bytes(replacement)
        return loads(raw)
    monkeypatch.setattr(settings_manager.json, 'loads', concurrent_edit)
    manager = settings_manager.SettingsManager()
    assert manager._corrupt_backup_path.read_bytes() == parsed
    assert config.read_bytes() == replacement


def test_constructor_load_and_automatic_migration_use_existing_writer_lock(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch, b'{"clip_length":42}')
    load = settings_manager.SettingsManager._load_settings
    writer = settings_manager.SettingsManager._persist_settings_candidate
    observed = []
    def locked_load(manager):
        assert settings_manager._SETTINGS_WRITER_LOCK._is_owned()
        observed.append('load')
        return load(manager)
    def locked_write(manager, candidate):
        assert settings_manager._SETTINGS_WRITER_LOCK._is_owned()
        observed.append('write')
        return writer(manager, candidate)
    monkeypatch.setattr(settings_manager.SettingsManager, '_load_settings', locked_load)
    monkeypatch.setattr(settings_manager.SettingsManager, '_persist_settings_candidate', locked_write)
    manager = settings_manager.SettingsManager()
    assert observed == ['load', 'write']
    assert manager.get('clip_length') == 42
    assert json.loads(config.read_bytes())['clip_length'] == 42


def test_partial_migration_failure_does_not_automatically_reset_original(tmp_path, monkeypatch):
    # Migration flags are set before the malformed hotkeys object is encountered.
    raw = b'{"navigation_bindings_version":1,"hotkeys":42,"sentinel":"keep me"}'
    config = _config(tmp_path, monkeypatch, raw)
    manager = settings_manager.SettingsManager()
    assert config.read_bytes() == raw
    assert manager._corrupt_backup_path.read_bytes() == raw


def test_unreadable_existence_probe_is_not_treated_as_a_fresh_install(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    raw = config.read_bytes()
    exists = Path.exists
    stat = settings_manager.os.stat
    def inaccessible(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) == config:
            raise PermissionError('inaccessible settings, not absent settings')
        return stat(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        # exists() may suppress an inaccessible-path error into False; unlike
        # stat(), that result cannot distinguish a new install from data loss.
        patch.setattr(Path, 'exists', lambda path: False if path == config else exists(path))
        patch.setattr(settings_manager.os, 'stat', inaccessible)
        manager = settings_manager.SettingsManager()
    assert manager._persistence_blocked
    assert manager.save_settings() is False
    assert config.read_bytes() == raw
