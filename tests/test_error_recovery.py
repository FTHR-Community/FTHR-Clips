from __future__ import annotations

from core.error_recovery import RecoveryTracker


def test_recovery_requires_three_progress_polls_spanning_three_seconds():
    tracker = RecoveryTracker()
    common = dict(
        incident_key='capture:4:7', connected=True,
        process_generation=4, backend_generation=7,
        health_state='healthy')

    assert not tracker.observe(**common, frame_count=10, now=0.0)
    assert not tracker.observe(**common, frame_count=11, now=0.5)
    assert not tracker.observe(**common, frame_count=12, now=2.0)
    assert tracker.observe(**common, frame_count=13, now=3.6)


def test_nonhealthy_poll_resets_stable_window():
    tracker = RecoveryTracker()
    common = dict(
        incident_key='capture:4:7', connected=True,
        process_generation=4, backend_generation=7)

    assert not tracker.observe(
        **common, health_state='healthy', frame_count=1, now=0.0)
    assert not tracker.observe(
        **common, health_state='healthy', frame_count=2, now=1.0)
    assert not tracker.observe(
        **common, health_state='recovering', frame_count=3, now=1.5)
    assert not tracker.observe(
        **common, health_state='healthy', frame_count=4, now=2.0)
    assert not tracker.observe(
        **common, health_state='healthy', frame_count=5, now=4.0)
    assert not tracker.observe(
        **common, health_state='healthy', frame_count=6, now=5.0)
    assert not tracker.observe(
        **common, health_state='healthy', frame_count=7, now=5.1)
    assert tracker.observe(
        **common, health_state='healthy', frame_count=8, now=7.1)


def test_generation_change_counter_decrease_and_disconnect_reset_window():
    tracker = RecoveryTracker()
    common = dict(incident_key='launch:2', connected=True,
                  health_state='healthy')

    assert not tracker.observe(
        **common, process_generation=3, backend_generation=1,
        frame_count=1, now=0.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=1,
        frame_count=2, now=1.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=2, now=2.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=3, now=3.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=1, now=3.5)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=2, now=4.0)
    assert not tracker.observe(
        **{**common, 'connected': False}, process_generation=3,
        backend_generation=2, frame_count=3, now=5.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=4, now=5.5)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=5, now=6.0)
    assert not tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=6, now=9.0)
    assert tracker.observe(
        **common, process_generation=3, backend_generation=2,
        frame_count=7, now=9.1)


def test_content_suspicion_is_not_encoder_or_black_content_recovery_evidence():
    tracker = RecoveryTracker()
    common = dict(
        incident_key='encoder-init', connected=True,
        process_generation=8, backend_generation=2,
        health_state='content_suspect')

    assert not tracker.observe(**common, frame_count=1, now=0.0)
    assert not tracker.observe(**common, frame_count=2, now=1.0)
    assert not tracker.observe(**common, frame_count=3, now=4.0)


def test_capture_backend_may_recover_with_progress_while_content_is_suspect():
    tracker = RecoveryTracker()
    common = dict(
        incident_key='capture-backend', connected=True,
        process_generation=8, backend_generation=3,
        health_state='content_suspect', allow_content_suspect=True)

    assert not tracker.observe(**common, frame_count=1, now=0.0)
    assert not tracker.observe(**common, frame_count=2, now=1.0)
    assert not tracker.observe(**common, frame_count=3, now=2.0)
    assert not tracker.observe(**common, frame_count=4, now=3.1)
    assert tracker.observe(**common, frame_count=5, now=4.1)
