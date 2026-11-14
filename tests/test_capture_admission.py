"""Pure contract tests for exact executable capture admission."""

from __future__ import annotations

import json

import pytest

from core.capture_admission import AdmissionDataError
from core.capture_admission import AdmissionDecision
from core.capture_admission import AdmissionPolicy
from core.capture_admission import AdmissionValidationError
from core.capture_admission import MAX_DOCUMENT_BYTES
from core.capture_admission import MAX_EXECUTABLES
from core.capture_admission import add_executable
from core.capture_admission import empty_policy
from core.capture_admission import evaluate_admission
from core.capture_admission import policy_from_dict
from core.capture_admission import policy_from_raw
from core.capture_admission import remove_executable
from core.capture_admission import validate_policy_transition
from core.capture_admission import with_enabled


GAME = r'C:\Games\FTHR\Game.exe'
OTHER = r'C:\Games\Other\Other.exe'


def _enabled(*paths: str) -> AdmissionPolicy:
    policy = with_enabled(empty_policy(), True)
    for path in paths:
        policy = add_executable(policy, path)
    return policy


def test_empty_policy_is_immutable_and_roundtrips_with_detached_list():
    policy = empty_policy()
    assert policy.to_dict() == {
        'version': 1,
        'revision': 0,
        'enabled': False,
        'executable_paths': [],
    }
    exported = policy.to_dict()
    exported['executable_paths'].append(GAME)
    assert policy.executable_paths == ()
    assert policy_from_dict(exported).executable_paths == (
        r'c:\games\fthr\game.exe',)
    with pytest.raises((AttributeError, TypeError)):
        policy.enabled = True


@pytest.mark.parametrize('value', [True, 1.0, -1, None, '1'])
def test_policy_rejects_non_strict_revisions(value):
    with pytest.raises(AdmissionValidationError):
        AdmissionPolicy(revision=value)


@pytest.mark.parametrize('value', [0, 1, 'true', None])
def test_policy_requires_an_exact_boolean_enabled_value(value):
    with pytest.raises(AdmissionValidationError):
        AdmissionPolicy(enabled=value)


def test_policy_rejects_int_subclasses_for_version_and_revision():
    class IntSubclass(int):
        pass

    with pytest.raises(AdmissionDataError):
        AdmissionPolicy(version=IntSubclass(1))
    with pytest.raises(AdmissionValidationError):
        AdmissionPolicy(revision=IntSubclass(1))


def test_policy_rejects_unknown_future_and_missing_document_fields():
    raw = empty_policy().to_dict()
    for mutation in (
            {'unexpected': True},
            {'version': 2},
            {'version': True},
    ):
        candidate = dict(raw)
        candidate.update(mutation)
        with pytest.raises(AdmissionDataError):
            policy_from_dict(candidate)
    missing = dict(raw)
    del missing['enabled']
    with pytest.raises(AdmissionDataError):
        policy_from_dict(missing)
    with pytest.raises(AdmissionDataError):
        policy_from_raw(None)
    tuple_paths = dict(raw)
    tuple_paths['executable_paths'] = (GAME,)
    with pytest.raises(AdmissionValidationError):
        policy_from_raw(tuple_paths)


@pytest.mark.parametrize('path', [
    r'Game.exe', r'C:Games\Game.exe', r'C:\Games\Game.txt',
    r'\\?\C:\Games\Game.exe', r'C:\Games\Game.exe:stream',
    r'C:\Games.\Game.exe', r'C:\Games\Game?.exe',
])
def test_policy_uses_exact_canonical_windows_executable_paths(path):
    with pytest.raises(AdmissionValidationError):
        add_executable(empty_policy(), path)


def test_duplicate_canonical_paths_are_rejected_but_mutator_noop_is_stable():
    with pytest.raises(AdmissionValidationError):
        policy_from_dict({
            'version': 1,
            'revision': 1,
            'enabled': False,
            'executable_paths': [GAME, r'c:/games/fthr/./game.exe'],
        })
    policy = add_executable(empty_policy(), GAME)
    assert add_executable(policy, r'c:/games/fthr/./game.exe') is policy
    assert remove_executable(policy, OTHER) is policy
    assert with_enabled(policy, False) is policy


def test_mutations_increment_once_and_preserve_path_order():
    policy = empty_policy()
    enabled = with_enabled(policy, True)
    assert enabled.revision == 1
    first = add_executable(enabled, GAME)
    second = add_executable(first, OTHER)
    assert second.revision == 3
    assert second.executable_paths == (
        r'c:\games\fthr\game.exe', r'c:\games\other\other.exe')
    removed = remove_executable(second, GAME)
    assert removed.revision == 4
    assert removed.executable_paths == (r'c:\games\other\other.exe',)


def test_policy_count_and_utf8_size_limits_are_enforced():
    paths = tuple(f'C:\\Games\\g{index}.exe' for index in range(MAX_EXECUTABLES))
    policy = AdmissionPolicy(executable_paths=paths)
    assert len(policy.executable_paths) == MAX_EXECUTABLES
    with pytest.raises(AdmissionValidationError):
        add_executable(policy, r'C:\Games\one-more.exe')

    oversized = {
        'version': 1,
        'revision': 0,
        'enabled': False,
        'executable_paths': [
            'C:\\Games\\' + ('é' * 8000) + str(index) + '.exe'
            for index in range(MAX_EXECUTABLES)
        ],
    }
    encoded_size = len(json.dumps(
        oversized, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    assert encoded_size > MAX_DOCUMENT_BYTES
    with pytest.raises(AdmissionValidationError):
        policy_from_dict(oversized)


def test_transition_requires_exactly_one_revision_for_real_changes():
    current = empty_policy()
    candidate = with_enabled(current, True)
    validate_policy_transition(current, candidate)
    validate_policy_transition(current, current)
    with pytest.raises(AdmissionValidationError):
        validate_policy_transition(current, AdmissionPolicy(
            revision=2, enabled=True))
    with pytest.raises(AdmissionValidationError):
        validate_policy_transition(current, AdmissionPolicy(revision=1))


@pytest.mark.parametrize('source_mode', ['desktop', 'unknown'])
def test_enabled_policy_denies_non_window_sources(source_mode):
    decision = evaluate_admission(_enabled(GAME), source_mode, GAME, True)
    assert not decision.allowed
    assert decision.code in {'desktop-not-approved', 'source-unavailable'}


def test_enabled_policy_requires_live_identity_and_exact_approved_exe():
    policy = _enabled(GAME)
    assert evaluate_admission(policy, 'window', GAME, True).allowed
    denied_identity = evaluate_admission(policy, 'window', GAME, False)
    assert (not denied_identity.allowed
            and denied_identity.code == 'identity-incomplete')
    denied_path = evaluate_admission(policy, 'window', OTHER, True)
    assert (not denied_path.allowed
            and denied_path.code == 'source-not-approved')
    denied_basename = evaluate_admission(
        policy, 'window', r'C:\Other\Game.exe', True)
    assert not denied_basename.allowed


@pytest.mark.parametrize('source_mode', ['desktop', 'window', 'unknown'])
@pytest.mark.parametrize('exe_path,identity_valid', [
    (None, False),
    (GAME, True),
])
def test_disabled_policy_allows_legacy_source_without_exe_admission(
        source_mode, exe_path, identity_valid):
    decision = evaluate_admission(
        empty_policy(), source_mode, exe_path, identity_valid)
    assert decision.allowed
    assert decision.code == 'policy-disabled'
    assert decision.exe_path in (None, r'c:\games\fthr\game.exe')


def test_disabled_policy_still_rejects_invalid_source_and_identity_types():
    invalid_mode = evaluate_admission(empty_policy(), object(), GAME, True)
    assert not invalid_mode.allowed
    invalid_identity = evaluate_admission(empty_policy(), 'window', GAME, 1)
    assert not invalid_identity.allowed
    invalid_source = evaluate_admission(empty_policy(), 'capture', None, False)
    assert not invalid_source.allowed


def test_evaluate_never_looks_up_the_host_and_bad_policy_fails_closed():
    decision = evaluate_admission({'version': 99}, 'window', GAME, True)
    assert (not decision.allowed
            and decision.code == 'policy-unavailable'
            and decision.policy_revision == 0)


def test_decision_is_validated_and_detached():
    decision = AdmissionDecision(True, 'admitted', 'ok', 4, GAME)
    assert decision.to_dict()['exe_path'] == r'c:\games\fthr\game.exe'
    with pytest.raises(AdmissionValidationError):
        AdmissionDecision(1, 'admitted', 'ok', 4, GAME)
    with pytest.raises(AdmissionValidationError):
        AdmissionDecision(True, '', 'ok', 4, GAME)
    with pytest.raises(AdmissionValidationError):
        AdmissionDecision(True, 'admitted', 'ok', True, GAME)
