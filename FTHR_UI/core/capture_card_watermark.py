"""Compose the animated FTHR export watermark using the bundled brand assets."""
from __future__ import annotations

from pathlib import Path

from core.theme_manager import ThemeManager


_ASSETS = Path(__file__).resolve().parents[1] / 'assets'
CARD_EXIT_S = 2.90


def _filter_path(path: Path) -> str:
    # A filter option is parsed twice: once as a graph, then as an option list.
    # Escape each level separately so drive letters, apostrophes and brackets in
    # installation directories survive both parsers. No shell is involved.
    value = path.as_posix()
    for special in ("\\", "'", ":"):
        value = value.replace(special, "\\" + special)
    return ''.join('\\' + ch if ch in "\\'[],;" else ch for ch in value)


def _ffmpeg_color(value: str) -> str:
    clean = str(value).strip().lstrip('#')
    if len(clean) == 3:
        clean = ''.join(char * 2 for char in clean)
    if len(clean) != 6 or any(ch not in '0123456789abcdefABCDEF' for ch in clean):
        clean = '000000'
    return f'0x{clean}'


def capture_card_watermark_filters(
        base_label: str, output_label: str = 'vout',
        card_label: str = 'fthr_watermark') -> list[str]:
    """Brand zero-based video without changing its frame count or duration."""
    colors = ThemeManager().get_all_capture_card_colors()
    bg = _ffmpeg_color(colors['CAPTURE_CARD_BG'])
    accent = _ffmpeg_color(colors['CAPTURE_CARD_ACCENT'])
    title = _ffmpeg_color(colors['CAPTURE_CARD_TEXT'])
    subtitle = _ffmpeg_color(colors['CAPTURE_CARD_STATS_DIM'])
    bold = _filter_path(_ASSETS / 'fonts' / 'Oswald-Bold.ttf')
    regular = _filter_path(_ASSETS / 'fonts' / 'Oswald-Regular.ttf')
    logo = _filter_path(_ASSETS / 'fthr_logo.png')

    # Render one high-resolution card. Only its position is animated on the
    # output clock; text and logo do not need to be redrawn with every frame.
    card = (
        f'color=c={bg}@0.94:s=416x128:r=1:d=1,format=rgba,'
        f'drawbox=x=0:y=0:w=iw:h=ih:color={title}@0.24:t=2,'
        f'drawbox=x=124:y=25:w=3:h=78:color={accent}:t=fill,'
        f"drawtext=fontfile={regular}:text='CAPTURED WITH':"
        f'fontcolor={subtitle}:fontsize=20:x=152:y=16,'
        f"drawtext=fontfile={bold}:text='FTHRClips':"
        f'fontcolor={title}:fontsize=52:x=150:y=45[{card_label}]'
    )
    icon = (
        f'movie=filename={logo},format=rgba,scale=84:84:flags=lanczos'
        f'[{card_label}_icon]'
    )
    composed = (
        f'[{card_label}][{card_label}_icon]overlay=x=19:y=22:format=rgb:shortest=1,'
        f'format=rgba[{card_label}_face]'
    )
    # Size against the finished video, including portrait and cropped exports.
    # scale2ref also passes the unchanged base through on its second output.
    scale = (
        f'[{card_label}_face][{base_label}]scale2ref='
        "w='max(2,trunc(min(416,min(iw*0.34,min(ih*0.5,max(156,ih*0.27))))/2)*2)':"
        f'h=ow*128/416:flags=lanczos[{card_label}_sized][{card_label}_video]'
    )
    margin = 'max(2,min(24,min(W,H)*0.025))'
    slide = (
        f'W-w-{margin}+(w+{margin})*'
        '(pow(max(0,1-t/0.24),3)+pow(min(1,max(0,(t-2.55)/0.35)),3))'
    )
    overlay = (
        f'[{card_label}_video][{card_label}_sized]overlay='
        f"x='{slide}':y='H-h-{margin}':format=yuv420:"
        f"enable='between(t,0,{CARD_EXIT_S})':eof_action=repeat[{output_label}]"
    )
    return [card, icon, composed, scale, overlay]
