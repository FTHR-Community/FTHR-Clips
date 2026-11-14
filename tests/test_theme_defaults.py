"""Regressions for the shipped palette and preservation of saved custom themes."""
import json

import pytest

from core import theme_manager as themes


@pytest.fixture(autouse=True)
def isolated_theme(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    themes.ThemeManager._instance = None
    yield
    themes.ThemeManager._instance = None


def write_theme(tmp_path, data):
    path = tmp_path / '.fthr' / 'theme' / 'theme.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


@pytest.mark.parametrize(('version', 'colors', 'capture', 'tint'), [
    (1, themes._LEGACY_COLORS, themes._LEGACY_CAPTURE_CARD_COLORS, '#00ffaa'),
    (2, themes._V2_COLORS, themes._V2_CAPTURE_CARD_COLORS, '#00ffaa'),
    (3, themes._V3_COLORS, themes._V3_CAPTURE_CARD_COLORS, themes._V3_COLORS['TEXT_DIM']),
    (4, themes._V4_COLORS, themes._V4_CAPTURE_CARD_COLORS, themes._V4_COLORS['TEXT_DIM']),
])
def test_previous_stock_themes_upgrade_and_persist(tmp_path, version, colors, capture, tint):
    write_theme(tmp_path, {
        'palette_version': version, 'colors': colors, 'capture_card': capture,
        'icon_tints': {'_global': tint},
    })
    theme = themes.ThemeManager()
    assert theme.get_all_colors() == themes.DEFAULT_COLORS
    assert theme.get_all_capture_card_colors() == themes.DEFAULT_CAPTURE_CARD_COLORS
    assert theme.get_global_icon_tint() == themes.DEFAULT_ICON_TINT
    assert not theme.has_any_customization()
    assert theme.save()
    themes.ThemeManager._instance = None
    reloaded = themes.ThemeManager()
    assert reloaded.get_all_colors() == themes.DEFAULT_COLORS
    assert reloaded._data['palette_version'] == 5


def test_upgrade_preserves_custom_colors_assets_and_individual_icon_tints(tmp_path):
    colors = {**themes._V4_COLORS, 'BG': '#454545', 'ACCENT': '#db98fc'}
    capture = {**themes._V4_CAPTURE_CARD_COLORS, 'CAPTURE_CARD_TEXT': '#f0debc'}
    write_theme(tmp_path, {
        'palette_version': 4, 'colors': colors, 'capture_card': capture,
        'icon_tints': {'_global': '#d4c098', 'play.png': '#ecb0ec'},
        'icons': {'play.png': 'custom.png'}, 'sounds': {'startup': 'custom.wav'},
        'fonts': {'body': 'Imported Font'}, 'font_files': {'Imported Font': 'custom.ttf'},
    })
    theme = themes.ThemeManager()
    assert theme.get_color('BG') == '#454545'
    assert theme.get_color('ACCENT') == '#db98fc'
    assert theme.get_color('SURFACE_1') == themes.DEFAULT_COLORS['SURFACE_1']
    assert theme.get_capture_card_color('CAPTURE_CARD_TEXT') == '#f0debc'
    assert theme.get_capture_card_color('CAPTURE_CARD_BG') == themes.DEFAULT_CAPTURE_CARD_COLORS['CAPTURE_CARD_BG']
    assert theme.get_global_icon_tint() == '#d4c098'
    assert theme.get_icon_tint('play.png') == '#ecb0ec'
    assert theme._data['icons'] == {'play.png': 'custom.png'}
    assert theme._data['sounds'] == {'startup': 'custom.wav'}
    assert theme.get_font('body') == 'Imported Font'


def test_upgrade_does_not_mistake_other_version_colors_for_stock(tmp_path):
    # A v3 user deliberately picked the later v4 green-black background.
    custom_bg = themes._V4_COLORS['BG']
    custom_capture = themes._V4_CAPTURE_CARD_COLORS['CAPTURE_CARD_BG']
    write_theme(tmp_path, {
        'palette_version': 3, 'colors': {**themes._V3_COLORS, 'BG': custom_bg},
        'capture_card': {**themes._V3_CAPTURE_CARD_COLORS, 'CAPTURE_CARD_BG': custom_capture},
        'icon_tints': {'_global': themes._V4_COLORS['TEXT_DIM']},
    })
    theme = themes.ThemeManager()
    assert theme.get_color('BG') == custom_bg
    assert theme.get_capture_card_color('CAPTURE_CARD_BG') == custom_capture
    assert theme.get_global_icon_tint() == themes._V4_COLORS['TEXT_DIM']


def test_current_version_customizations_are_not_migrated(tmp_path):
    write_theme(tmp_path, {
        'palette_version': 5, 'colors': themes._V4_COLORS,
        'capture_card': themes._V4_CAPTURE_CARD_COLORS,
        'icon_tints': {'_global': themes._V4_COLORS['TEXT_DIM']},
    })
    theme = themes.ThemeManager()
    assert theme.get_all_colors() == themes._V4_COLORS
    assert theme.get_all_capture_card_colors() == themes._V4_CAPTURE_CARD_COLORS
    assert theme.get_global_icon_tint() == themes._V4_COLORS['TEXT_DIM']
