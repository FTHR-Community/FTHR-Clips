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
    tracker.fail('encoder unavailable')

    assert tracker.status is ApplyStatus.FAILED
    assert tracker.active == old
    assert tracker.requested == requested
    assert tracker.error == 'encoder unavailable'


def test_engine_exit_clears_the_active_process_snapshot():
    old = _config(codec='h264')
    tracker = CaptureConfigTracker(active=old)

    tracker.deactivate('engine exited')

    assert tracker.status is ApplyStatus.FAILED
    assert tracker.active is None
    assert tracker.requested == old
    assert tracker.error == 'engine exited'


def test_linux_ring_budget_follows_bitrate_and_buffer():
    from core.capture_settings import (
        LINUX_RING_BUDGET_MAX_MB, LINUX_RING_BUDGET_MIN_MB, linux_ring_budget_mb)

    big_machine = 256 * 1024
    # 15 minutes at 50 Mbps needs ~5.4 GiB; the budget must cover it.
    expected_mb = 50_000 * 1000 // 8 * 905 / (1024 * 1024)
    budget = linux_ring_budget_mb(50_000, 900, total_memory_mb=big_machine)
    assert budget >= expected_mb > 2048
    # Short buffers stay small, never below the engine's minimum.
    assert linux_ring_budget_mb(500, 5, total_memory_mb=big_machine) == LINUX_RING_BUDGET_MIN_MB
    # 900 s at 200 Mbps exceeds the engine's maximum.
    assert linux_ring_budget_mb(200_000, 900, total_memory_mb=big_machine) == (
        LINUX_RING_BUDGET_MAX_MB)
    # Never more than half of physical memory.
    assert linux_ring_budget_mb(200_000, 900, total_memory_mb=8192) == 4096
    # An unknown memory size does not cap the budget.
    assert linux_ring_budget_mb(50_000, 900, total_memory_mb=0) == budget
