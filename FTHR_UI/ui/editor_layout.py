"""Aspect-aware Studio geometry in logical pixels, independent of monitor DPI."""

AUDIO_LANE_HEIGHT = 34


def audio_lane_capacity(height: int) -> int:
    return max(1, min(4, 1 + (height - 440) // 145))


def studio_layout_metrics(width: int, height: int, aspect: float, *, audio_rows: int = 1):
    """Keep inspectors beside the fitted image instead of beside empty gutters.

    Preview height is precious for narrow clips. Keep the timeline compact,
    then give space the image cannot use to the inspectors. Very broad video
    inspectors reflow their tools into two columns in ClipViewer.
    """
    scale = max(1.0, min(1.75, width / 1440, height / 900))
    text_scale = round(min(1.25, 1 + (scale - 1) / 3), 2)
    # Use the existing full-width filmstrip space for both lanes. Keep more
    # picture height on laptop screens while retaining the audio hit target.
    filmstrip = 80 if height < 750 else 98
    strip = round(filmstrip * text_scale) + AUDIO_LANE_HEIGHT * max(1, audio_rows)
    right_base = 225 if width <= 1050 else 300
    left, right = round(238 * scale), round(right_base * scale)
    # Header, fixed export controls, transport, and trim controls surround
    # the preview. Account for all four before distributing unused gutters.
    preview_height = max(160, height - 150 * text_scale - 88 - strip)
    fitted_width = max(360, preview_height * max(0.1, aspect))
    spare = max(0, width - left - right - fitted_width - 24)
    left += round(spare * 0.46)
    right += round(spare * 0.54)
    budget = max(238 + right_base, width - 360)
    if left + right > budget:
        ratio = budget / (left + right)
        left, right = round(left * ratio), round(right * ratio)
    if width <= 1050:
        right = min(right, 230)
    return left, right, strip, text_scale
