"""Small pure policy helpers for honest clip-save reporting."""

from __future__ import annotations

import math


MINIMUM_PARTIAL_SECONDS = 0.5


def classify_saved_clip_duration(
        *, requested_seconds: float, actual_video_seconds: float | None,
        fps: float | None, minimum_partial_seconds: float = MINIMUM_PARTIAL_SECONDS,
) -> str:
    """Classify a committed clip from measured video presentation duration.

    Unknown or materially overlong measurements are never claimed as full.
    A full save may be short by at most one configured frame. Clips shorter
    than the native partial-save floor are rejected, not presented as saves.
    """
    try:
        requested = float(requested_seconds)
        actual = float(actual_video_seconds)
        rate = float(fps)
        minimum = float(minimum_partial_seconds)
    except (TypeError, ValueError, OverflowError):
        # Invalid/missing measurements remain unknown rather than being guessed.
        return 'unknown'
    if (not all(math.isfinite(value) for value in
                (requested, actual, rate, minimum))
            or requested <= 0 or actual <= 0 or rate <= 0 or minimum < 0):
        return 'unknown'
    tolerance = 1.0 / rate
    if actual > requested + tolerance:
        return 'unknown'
    if actual + tolerance >= requested:
        return 'full'
    if actual < minimum:
        return 'rejected'
    return 'partial'
