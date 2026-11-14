from __future__ import annotations

import pytest

from core.capture_settings import (
    FPS_VALUES,
    NORMAL_CLIP_VALUES,
    MAX_CLIP_SECONDS,
    ApplyStatus,
    CaptureConfig,
    CaptureConfigTracker,
    compute_buffer_seconds,
    validate_fps,
    validate_normal_clip_length,
)


def _config(**overrides) -> CaptureConfig:
    values = dict(
        fps=60,
        buffer_seconds=62,
        width=1920,
        height=1080,
        bitrate_kbps=25_000,
        codec='h264',
        preset=4,
        monitor='',
        scaling='stretch',
        audio_enabled=True,
    )
    values.update(overrides)
    return CaptureConfig(**values)


def test_product_limits_have_one_authoritative_policy():
    assert max(NORMAL_CLIP_VALUES) == 300
    assert max(FPS_VALUES) == 240

    assert validate_normal_clip_length(300) == 300
    assert validate_normal_clip_length(900) == 900
    assert validate_fps(240) == 240


def test_every_exposed_duration_fits_the_native_ring():
    for normal in (*NORMAL_CLIP_VALUES, 301, 600, 899, 900):
        ring = compute_buffer_seconds(normal)
        assert normal <= ring <= MAX_CLIP_SECONDS


def test_python_microphone_history_matches_maximum_replay():
    from core.mic_recorder import KEEP_SECONDS

    assert KEEP_SECONDS >= MAX_CLIP_SECONDS


@pytest.mark.parametrize(
    ('validator', 'invalid'),
    [
        (validate_normal_clip_length, 901),
        (validate_fps, 241),
    ],
)
def test_invalid_values_are_rejected_not_silently_clamped(validator, invalid):
    with pytest.raises(ValueError):
        validator(invalid)


def test_requested_config_is_not_active_until_restart_succeeds():
    old = _config(codec='h264', bitrate_kbps=25_000)
    requested = _config(codec='av1', bitrate_kbps=50_000)
    tracker = CaptureConfigTracker(active=old)

    tracker.request(requested)
    assert tracker.status is ApplyStatus.REQUESTED
    assert tracker.active == old
    assert tracker.requested == requested

    tracker.begin_apply()
    assert tracker.status is ApplyStatus.APPLYING
    assert tracker.active == old

    tracker.succeed()
    assert tracker.status is ApplyStatus.ACTIVE
    assert tracker.active == requested


def test_failed_restart_preserves_previous_active_config():
    old = _config(codec='h264')
    requested = _config(codec='hevc')
    tracker = CaptureConfigTracker(active=old)

    tracker.request(requested)
    tracker.begin_apply()
    assert tracker.fail('encoder unavailable') is True

    assert tracker.status is ApplyStatus.FAILED
    assert tracker.active == old
    assert tracker.requested == requested
    assert tracker.error == 'encoder unavailable'


@pytest.mark.parametrize('use_token', [False, True])
def test_request_during_apply_keeps_frozen_config_until_matching_success(
        use_token):
    old = _config(codec='h264')
    applying = _config(codec='hevc')
    requested_next = _config(codec='av1')
    tracker = CaptureConfigTracker(active=old)

    tracker.request(applying)
    token = tracker.begin_apply()
    tracker.request(requested_next)

    assert tracker.applying == applying
    assert tracker.applying_token == token
    assert tracker.status is ApplyStatus.APPLYING
    if use_token:
        assert tracker.succeed(token) is True
    else:
        assert tracker.succeed() is True
    assert tracker.active == applying
    assert tracker.requested == requested_next
    assert tracker.status is ApplyStatus.REQUESTED
    assert tracker.accepted_token == token


def test_stale_completion_is_false_and_does_not_mutate_tracker_state():
    first = _config(codec='h264')
    second = _config(codec='hevc')
    tracker = CaptureConfigTracker(active=first)

    tracker.request(first)
    first_token = tracker.begin_apply()
    tracker.request(second)
    second_token = tracker.begin_apply()
    before = (
        tracker.active, tracker.requested, tracker.status, tracker.error,
        tracker.applying, tracker.applying_token, tracker.accepted_token,
        tracker._token_counter, tracker.transition_revision)

    assert tracker.succeed(first_token) is False
    assert tracker.fail('stale failure', first_token) is False
    after = (
        tracker.active, tracker.requested, tracker.status, tracker.error,
        tracker.applying, tracker.applying_token, tracker.accepted_token,
        tracker._token_counter, tracker.transition_revision)
    assert after == before
    assert tracker.applying_token == second_token


def test_cancel_and_deactivate_invalidate_old_completion_receipts():
    old = _config(codec='h264')
    requested = _config(codec='hevc')
    tracker = CaptureConfigTracker(active=old)

    tracker.request(requested)
    token = tracker.begin_apply()
    tracker.cancel_apply()
    assert tracker.status is ApplyStatus.REQUESTED
    assert tracker.succeed(token) is False
    assert tracker.active == old

    tracker.request(requested)
    token = tracker.begin_apply()
    tracker.deactivate('engine exited')
    assert tracker.active is None
    assert tracker.requested == requested
    assert tracker.status is ApplyStatus.FAILED
    assert tracker.succeed(token) is False


def test_no_arg_success_requires_an_actual_applying_config():
    tracker = CaptureConfigTracker(active=_config())

    with pytest.raises(RuntimeError):
        tracker.succeed()
    assert tracker.fail('preflight failure') is True
    assert tracker.status is ApplyStatus.FAILED


def test_no_arg_failure_invalidates_an_accepted_receipt():
    config = _config(codec='h264')
    tracker = CaptureConfigTracker(active=config)

    tracker.request(config)
    token = tracker.begin_apply()
    assert tracker.succeed(token) is True
    revision = tracker.transition_revision

    assert tracker.fail('late preflight failure') is True
    assert tracker.accepted_token is None
    assert tracker.active == config
    assert tracker.status is ApplyStatus.FAILED
    assert tracker.transition_revision > revision
    assert tracker.succeed(token) is False


def test_duplicate_explicit_completion_is_mutation_free_including_revision():
    config = _config(codec='h264')
    requested = _config(codec='hevc')
    tracker = CaptureConfigTracker(active=config)

    tracker.request(requested)
    token = tracker.begin_apply()
    assert tracker.succeed(token) is True
    before = (
        tracker.active, tracker.requested, tracker.status, tracker.error,
        tracker.applying, tracker.applying_token, tracker.accepted_token,
        tracker._token_counter, tracker.transition_revision)

    assert tracker.succeed(token) is False
    assert tracker.fail('duplicate completion', token) is False
    after = (
        tracker.active, tracker.requested, tracker.status, tracker.error,
        tracker.applying, tracker.applying_token, tracker.accepted_token,
        tracker._token_counter, tracker.transition_revision)
    assert after == before


def test_cancel_without_applying_invalidates_accepted_receipt_only():
    config = _config(codec='h264')
    tracker = CaptureConfigTracker(active=config)

    tracker.request(config)
    token = tracker.begin_apply()
    assert tracker.succeed(token) is True
    before_revision = tracker.transition_revision

    tracker.cancel_apply()

    assert tracker.accepted_token is None
    assert tracker.active == config
    assert tracker.requested == config
    assert tracker.status is ApplyStatus.ACTIVE
    assert tracker.transition_revision == before_revision + 1


def test_engine_exit_clears_the_active_process_snapshot():
    old = _config(codec='h264')
    tracker = CaptureConfigTracker(active=old)

    tracker.deactivate('engine exited')

    assert tracker.status is ApplyStatus.FAILED
    assert tracker.active is None
    assert tracker.requested == old
    assert tracker.error == 'engine exited'
