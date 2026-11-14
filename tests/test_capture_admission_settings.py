"""SettingsManager persistence and same-manager CAS tests."""

from __future__ import annotations

import json
import threading

import pytest

from core.capture_admission import AdmissionDataError
from core.capture_admission import add_executable
from core.capture_admission import empty_policy
from core.capture_admission import policy_from_dict
from core.capture_admission import with_enabled
from core.settings_manager import SettingsManager


GAME = r'C:\Games\FTHR\Game.exe'


def _manager(tmp_path, settings=None):
    manager = object.__new__(SettingsManager)
    manager.config_file = tmp_path / 'settings.json'
    manager.settings = {} if settings is None else settings
    return manager


def test_missing_policy_is_disabled_without_inserting_a_settings_key(tmp_path):
    manager = _manager(tmp_path)
    policy = manager.read_capture_admission_policy()
    assert policy == empty_policy()
    assert 'capture_admission_policy' not in manager.settings


def test_policy_save_persists_only_after_atomic_success_and_updates_ram(tmp_path):
    manager = _manager(tmp_path, {'sentinel': {'value': 1}})
    toggled = with_enabled(empty_policy(), True)
    assert manager.save_capture_admission_policy(toggled, expected_revision=0)
    candidate = add_executable(toggled, GAME)
    assert manager.save_capture_admission_policy(
        candidate, expected_revision=toggled.revision)
    assert manager.read_capture_admission_policy() == candidate
    saved = json.loads(manager.config_file.read_text(encoding='utf-8'))
    assert saved['capture_admission_policy'] == candidate.to_dict()
    assert saved['sentinel'] == {'value': 1}


def test_stale_writer_is_rejected_without_mutation(tmp_path):
    manager = _manager(tmp_path)
    candidate = with_enabled(empty_policy(), True)
    assert manager.save_capture_admission_policy(candidate, expected_revision=0)
    other = add_executable(candidate, GAME)
    before = manager.settings['capture_admission_policy']
    assert not manager.save_capture_admission_policy(other, expected_revision=0)
    assert manager.settings['capture_admission_policy'] == before
    assert manager.read_capture_admission_policy() == candidate


def test_two_competing_candidates_have_one_deterministic_cas_winner(tmp_path):
    manager = _manager(tmp_path)
    base = empty_policy()
    candidates = (
        with_enabled(base, True),
        add_executable(base, GAME),
    )
    barrier = threading.Barrier(2)
    results = [None, None]

    def attempt(index):
        barrier.wait(timeout=3)
        results[index] = manager.save_capture_admission_policy(
            candidates[index], expected_revision=0)

    threads = [threading.Thread(target=attempt, args=(index,), daemon=True)
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)

    assert sorted(results) == [False, True]
    winner = candidates[results.index(True)]
    assert manager.read_capture_admission_policy() == winner
    persisted = json.loads(manager.config_file.read_text(encoding='utf-8'))
    assert persisted['capture_admission_policy'] == winner.to_dict()


def test_semantic_noop_does_not_call_writer(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    candidate = with_enabled(empty_policy(), True)
    assert manager.save_capture_admission_policy(candidate, expected_revision=0)
    calls = []
    monkeypatch.setattr(
        manager, '_persist_settings_candidate',
        lambda staged: calls.append(staged) or True)
    assert manager.save_capture_admission_policy(candidate, expected_revision=1)
    assert calls == []


def test_failed_persist_preserves_disk_and_ram(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    candidate = with_enabled(empty_policy(), True)
    assert manager.save_capture_admission_policy(candidate, expected_revision=0)
    previous_ram = dict(manager.settings['capture_admission_policy'])
    previous_disk = manager.config_file.read_bytes()
    changed = add_executable(candidate, GAME)
    monkeypatch.setattr(manager, '_persist_settings_candidate', lambda _: False)
    assert not manager.save_capture_admission_policy(
        changed, expected_revision=candidate.revision)
    assert manager.settings['capture_admission_policy'] == previous_ram
    assert manager.config_file.read_bytes() == previous_disk


def test_writer_exception_preserves_disk_and_ram(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    candidate = with_enabled(empty_policy(), True)
    assert manager.save_capture_admission_policy(candidate, expected_revision=0)
    previous_ram = dict(manager.settings['capture_admission_policy'])
    previous_disk = manager.config_file.read_bytes()
    changed = add_executable(candidate, GAME)

    def fail_writer(_candidate):
        raise OSError('write failed')

    monkeypatch.setattr(manager, '_persist_settings_candidate', fail_writer)
    with pytest.raises(OSError):
        manager.save_capture_admission_policy(
            changed, expected_revision=candidate.revision)
    assert manager.settings['capture_admission_policy'] == previous_ram
    assert manager.config_file.read_bytes() == previous_disk


@pytest.mark.parametrize('expected_revision', [None, True, -1, 1.5])
def test_save_requires_strict_expected_revision(tmp_path, expected_revision):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.save_capture_admission_policy(
            with_enabled(empty_policy(), True),
            expected_revision=expected_revision)
    assert 'capture_admission_policy' not in manager.settings


def test_save_rejects_int_subclass_expected_revision(tmp_path):
    class IntSubclass(int):
        pass

    manager = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.save_capture_admission_policy(
            with_enabled(empty_policy(), True),
            expected_revision=IntSubclass(0))


def test_invalid_future_policy_is_preserved_and_dedicated_save_refuses_it(tmp_path):
    future = {
        'version': 99,
        'revision': 8,
        'enabled': True,
        'executable_paths': [GAME],
        'future': {'value': 1},
    }
    raw_bytes = json.dumps({
        'capture_admission_policy': future,
        'sentinel': 'keep',
    }, separators=(',', ':')).encode('utf-8')
    manager = _manager(tmp_path, {
        'capture_admission_policy': future,
        'sentinel': 'keep',
    })
    manager.config_file.write_bytes(raw_bytes)
    before = manager.settings['capture_admission_policy']
    with pytest.raises(AdmissionDataError):
        manager.read_capture_admission_policy()
    with pytest.raises(AdmissionDataError):
        manager.save_capture_admission_policy(empty_policy(), expected_revision=8)
    assert manager.settings['capture_admission_policy'] is before
    assert manager.config_file.read_bytes() == raw_bytes


def test_raw_invalid_policy_is_not_repaired_or_overwritten(tmp_path):
    invalid = {
        'version': 1,
        'revision': 2,
        'enabled': True,
        'executable_paths': [GAME, r'c:/games/fthr/./game.exe'],
    }
    manager = _manager(tmp_path, {'capture_admission_policy': invalid})
    with pytest.raises(AdmissionDataError):
        manager.read_capture_admission_policy()
    with pytest.raises(AdmissionDataError):
        manager.save_capture_admission_policy(empty_policy(), expected_revision=2)
    assert manager.settings['capture_admission_policy'] is invalid


def test_candidate_is_detached_before_ram_publication(tmp_path):
    manager = _manager(tmp_path)
    raw = {
        'version': 1,
        'revision': 1,
        'enabled': True,
        'executable_paths': [GAME],
    }
    candidate = policy_from_dict(raw)
    assert manager.save_capture_admission_policy(candidate, expected_revision=0)
    raw['executable_paths'].append(r'C:\Games\Other.exe')
    assert manager.settings['capture_admission_policy'] == candidate.to_dict()
