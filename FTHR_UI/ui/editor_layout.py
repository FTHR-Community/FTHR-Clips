"""Aspect-aware Studio geometry in logical pixels, independent of monitor DPI."""


def studio_layout_metrics(width: int, height: int, aspect: float):
    """Keep inspectors beside the fitted image instead of beside empty gutters.

    Preview height is precious for narrow clips. Keep the timeline compact,
    then give space the image cannot use to the inspectors. Very broad video
    inspectors reflow their tools into two columns in ClipViewer.
    """
    scale = max(1.0, min(1.75, width / 1440, height / 900))
    text_scale = round(min(1.25, 1 + (scale - 1) / 3), 2)
    strip = round(116 * text_scale)
    left, right = round(238 * scale), round(300 * scale)
    preview_height = max(160, height - 130 * text_scale - 82 - strip)
    fitted_width = max(360, preview_height * max(0.1, aspect))
    spare = max(0, width - left - right - fitted_width - 24)
    left += round(spare * 0.46)
    right += round(spare * 0.54)
    budget = max(538, width - 360)
    if left + right > budget:
        ratio = budget / (left + right)
        left, right = round(left * ratio), round(right * ratio)
    return left, right, strip, text_scale
