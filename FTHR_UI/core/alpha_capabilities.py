"""Single source for platform feature policy."""

from __future__ import annotations


def multiband_audio_supported() -> bool:
    # Per-application stems are retired. Clips contain only the system mix and
    # microphone so capture, playback and export all share one stable model.
    return False


def effective_multiband_audio_enabled(requested: object = False) -> bool:
    del requested
    return False


def focus_pause_supported(platform: str) -> bool:
    return not platform.startswith('win')


def encoder_preset_supported(platform: str, encoder: str = 'nvenc') -> bool:
    return encoder == 'nvenc'


def filter_alpha_preset(data: dict[str, object]) -> dict[str, object]:
    """Discard settings that an old preset must not reactivate in alpha."""
    filtered = dict(data)
    filtered.pop('multiband_audio_enabled', None)
    filtered.pop('audio_categories', None)
    filtered.pop('auto_crop_enabled', None)
    return filtered
