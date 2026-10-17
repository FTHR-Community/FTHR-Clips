"""Small, UI-independent helpers for Gary Mode's microphone response."""
from __future__ import annotations


DEFAULT_MIN_LEVEL = 55
DEFAULT_MAX_LEVEL = 85
MIN_THRESHOLD_GAP = 5


def clamp_thresholds(min_level, max_level) -> tuple[int, int]:
    """Return valid percentage thresholds with a useful minimum gap."""
    try:
        low = int(min_level)
    except (TypeError, ValueError):
        low = DEFAULT_MIN_LEVEL
    try:
        high = int(max_level)
    except (TypeError, ValueError):
        high = DEFAULT_MAX_LEVEL

    low = max(1, min(low, 100 - MIN_THRESHOLD_GAP))
    high = max(low + MIN_THRESHOLD_GAP, min(high, 100))
    return low, high


def intensity_for_level(level: float, min_level, max_level) -> float:
    """Map a normalized mic level to a normalized Gary image intensity."""
    low, high = clamp_thresholds(min_level, max_level)
    try:
        percent = max(0.0, min(float(level), 1.0)) * 100.0
    except (TypeError, ValueError):
        # Invalid meter input leaves the decorative response idle, not active.
        return 0.0
    if percent <= low + 1e-9:
        return 0.0
    if percent >= high - 1e-9:
        return 1.0
    return (percent - low) / (high - low)


def step_intensity(current: float, target: float,
                   rise_step: float = 0.18,
                   fall_step: float = 0.065) -> float:
    """Advance one frame and land exactly on the requested intensity."""
    current = max(0.0, min(float(current), 1.0))
    target = max(0.0, min(float(target), 1.0))
    delta = target - current
    step = max(0.001, rise_step if delta > 0.0 else fall_step)
    if abs(delta) <= step:
        return target
    return current + (step if delta > 0.0 else -step)
