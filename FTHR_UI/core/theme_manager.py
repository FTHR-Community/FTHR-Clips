"""
Theme Manager — lets users repaint the whole app and not have it look like ours.

Handles three kinds of customization: colors (hex tokens that feed the QSS),
icons (PNG/SVG swaps), and sounds (the little blip when you grab a clip). All of
it persists under ~/.fthr/theme/, and the whole thing can be zipped up and shared
so people can trade themes like Pokemon cards.

Loaded once at startup; QSS is only regenerated when someone hits Apply, because
restyling the entire widget tree on every color pick is how you get a slideshow.
The merge-with-defaults dance everywhere is so that adding a new color token in a
future version doesn't crash on someone's old theme.json. forward-compat or bust.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Optional


# Default color tokens — mirrors style.py Colors class at its defaults.
# Keys match the attribute names on Colors exactly.
DEFAULT_COLORS: dict[str, str] = {
    'BG':           '#000000',
    'SURFACE_1':    '#0a0a0a',
    'SURFACE_2':    '#111111',
    'SURFACE_3':    '#1c1c1c',
    'SHELL_BG':     '#000000',
    'SHELL_BG_2':   '#0a0a0a',
    'SHELL_DIVIDER': '#222222',
    'CARD_BG':      '#0a0a0a',
    'CARD_BG_HI':   '#111111',
    'CARD_BORDER':  '#222222',
    'HAIRLINE':     '#111111',
    'BORDER':       '#222222',
    'BORDER_HI':    '#333333',
    'TEXT':         '#ffffff',
    'TEXT_DIM':     '#888888',
    'TEXT_MUTED':   '#555555',
    'TEXT_GHOST':   '#222222',
    'ACCENT':       '#00ffaa',
    'ACCENT_DIM':   '#00aa72',
    'ACCENT_SOFT':  '#0a2218',
    'ERROR':        '#cc0000',
    'SUCCESS':      '#00aa00',
    'DELETE':       '#cc0000',
}

# Icons that can be customized (filename -> display label)
CUSTOMIZABLE_ICONS: dict[str, str] = {
    'fthr_logo.png':        'App Logo',
    'clip.png':             'Clip Tab',
    'sound.png':            'Audio Tab',
    'visuals.png':          'Visuals Tab',
    'settings(general).png': 'General Tab',
    'updates.png':          'Updates Tab',
    'personalize.png':      'Customize Tab',
    'home.png':             'Home Button',
    'refresh.png':          'Refresh Button',
    'play.png':             'Play Button',
    'pause.png':            'Pause Button',
    'close.png':            'Close Button',
    'minimize.png':         'Minimize Button',
    'maximize.png':         'Maximize Button',
    'dropdown.png':         'Dropdown Arrow',
}

# Sounds that can be customized (key -> display label)
CUSTOMIZABLE_SOUNDS: dict[str, str] = {
    'clip_captured':       'Clip Captured',
    'screenshot_captured': 'Screenshot Captured',
    'error':               'Error',
    'startup':             'Startup',
}

# Supported formats
SUPPORTED_IMAGE_FORMATS = ('.png', '.jpg', '.jpeg', '.bmp', '.ico', '.svg')
SUPPORTED_SOUND_FORMATS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.wma', '.aac')

# Default icon tint — signature teal, applied to all default (non-imported) icons
DEFAULT_ICON_TINT = '#00ffaa'

# Default capture card colors
DEFAULT_CAPTURE_CARD_COLORS: dict[str, str] = {
    'CAPTURE_CARD_BG':             '#000000',
    'CAPTURE_CARD_ACCENT':         '#ffffff',
    'CAPTURE_CARD_TEXT':            '#ffffff',
    'CAPTURE_CARD_DIVIDER':        '#222222',
    'CAPTURE_CARD_STATS_DIM':      '#666666',
    'CAPTURE_CARD_PROGRESS_TRACK': '#111111',
    'CAPTURE_CARD_PROGRESS_FILL':  '#ffffff',
}


class ThemeManager:
    """Singleton manager for UI theme customization."""

    _instance: Optional['ThemeManager'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._theme_dir = Path.home() / '.fthr' / 'theme'
        self._icons_dir = self._theme_dir / 'icons'
        self._sounds_dir = self._theme_dir / 'sounds'
        self._config_file = self._theme_dir / 'theme.json'

        self._theme_dir.mkdir(parents=True, exist_ok=True)
        self._icons_dir.mkdir(exist_ok=True)
        self._sounds_dir.mkdir(exist_ok=True)

        self._data = self._load()

    # ─── Persistence ──────��───────────────────────────────────────────────

    def _load(self) -> dict:
        default = {
            'colors': dict(DEFAULT_COLORS),
            'icons': {},   # filename -> custom path (relative to icons_dir)
            'sounds': {},  # key -> custom path (relative to sounds_dir)
            'icon_tints': {'_global': DEFAULT_ICON_TINT},
            'capture_card': dict(DEFAULT_CAPTURE_CARD_COLORS),
        }
        if not self._config_file.exists():
            return default
        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            # Merge with defaults for forward-compatibility
            merged = dict(default)
            if 'colors' in loaded:
                merged['colors'] = {**DEFAULT_COLORS, **loaded['colors']}
            if 'icons' in loaded:
                merged['icons'] = loaded['icons']
            if 'sounds' in loaded:
                merged['sounds'] = loaded['sounds']
            if 'icon_tints' in loaded:
                merged['icon_tints'] = loaded['icon_tints']
            if 'capture_card' in loaded:
                merged['capture_card'] = {**DEFAULT_CAPTURE_CARD_COLORS,
                                          **loaded['capture_card']}
            return merged
        except Exception as e:
            print(f'[Theme] Load failed: {e}')
            return default

    def save(self):
        self._theme_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            print(f'[Theme] Save failed: {e}')

    # ─── Color access ─────────────────────────────────────────────────────

    def get_color(self, token: str) -> str:
        # The '#ff00ff' fallback is deliberate: if you ever see screaming magenta
        # in the UI, it means a token name got typo'd somewhere. Loud on purpose.
        return self._data['colors'].get(token, DEFAULT_COLORS.get(token, '#ff00ff'))

    def set_color(self, token: str, hex_value: str):
        self._data['colors'][token] = hex_value

    def get_all_colors(self) -> dict[str, str]:
        return dict(self._data['colors'])

    def reset_color(self, token: str):
        if token in DEFAULT_COLORS:
            self._data['colors'][token] = DEFAULT_COLORS[token]

    def reset_all_colors(self):
        self._data['colors'] = dict(DEFAULT_COLORS)

    def is_color_default(self, token: str) -> bool:
        return self._data['colors'].get(token) == DEFAULT_COLORS.get(token)

    # ─── Icon access ──────────────────────────────────────────────────────

    def get_custom_icon_path(self, filename: str) -> Optional[Path]:
        rel = self._data['icons'].get(filename)
        if rel:
            full = self._icons_dir / rel
            if full.exists():
                return full
        return None

    def set_custom_icon(self, filename: str, source_path: Path) -> Path:
        """Copy an icon file into the theme directory. Returns the stored path."""
        ext = source_path.suffix.lower()
        dest_name = Path(filename).stem + ext
        dest = self._icons_dir / dest_name
        shutil.copy2(source_path, dest)
        self._data['icons'][filename] = dest_name
        return dest

    def remove_custom_icon(self, filename: str):
        rel = self._data['icons'].pop(filename, None)
        if rel:
            full = self._icons_dir / rel
            if full.exists():
                full.unlink(missing_ok=True)

    # ─── Sound access ─────────────────────────────────────────────────────

    def get_custom_sound_path(self, key: str) -> Optional[Path]:
        rel = self._data['sounds'].get(key)
        if rel:
            full = self._sounds_dir / rel
            if full.exists():
                return full
        return None

    def set_custom_sound(self, key: str, source_path: Path) -> Path:
        """Copy a sound file into the theme directory. Returns the stored path."""
        dest_name = f'{key}{source_path.suffix.lower()}'
        dest = self._sounds_dir / dest_name
        shutil.copy2(source_path, dest)
        self._data['sounds'][key] = dest_name
        return dest

    def remove_custom_sound(self, key: str):
        rel = self._data['sounds'].pop(key, None)
        if rel:
            full = self._sounds_dir / rel
            if full.exists():
                full.unlink(missing_ok=True)

    # ─── Icon tint access ─────────────────────────────────────────────

    def get_icon_tint(self, filename: str) -> str:
        tints = self._data.get('icon_tints', {})
        return tints.get(filename, tints.get('_global', DEFAULT_ICON_TINT))

    def set_icon_tint(self, filename: str, hex_color: str):
        self._data.setdefault('icon_tints', {'_global': DEFAULT_ICON_TINT})
        self._data['icon_tints'][filename] = hex_color

    def remove_icon_tint(self, filename: str):
        self._data.get('icon_tints', {}).pop(filename, None)

    def get_global_icon_tint(self) -> str:
        return self._data.get('icon_tints', {}).get('_global', DEFAULT_ICON_TINT)

    def set_global_icon_tint(self, hex_color: str):
        self._data.setdefault('icon_tints', {})
        self._data['icon_tints']['_global'] = hex_color

    def has_icon_tint_override(self, filename: str) -> bool:
        return filename in self._data.get('icon_tints', {}) and filename != '_global'

    def reset_all_icon_tints(self):
        self._data['icon_tints'] = {'_global': DEFAULT_ICON_TINT}

    # ─── Capture card color access ────────────────────────────────────

    def get_capture_card_color(self, key: str) -> str:
        return self._data.get('capture_card', {}).get(
            key, DEFAULT_CAPTURE_CARD_COLORS.get(key, '#ffffff'))

    def set_capture_card_color(self, key: str, hex_color: str):
        self._data.setdefault('capture_card', dict(DEFAULT_CAPTURE_CARD_COLORS))
        self._data['capture_card'][key] = hex_color

    def get_all_capture_card_colors(self) -> dict[str, str]:
        return {**DEFAULT_CAPTURE_CARD_COLORS, **self._data.get('capture_card', {})}

    def reset_capture_card_colors(self):
        self._data['capture_card'] = dict(DEFAULT_CAPTURE_CARD_COLORS)

    # ─── Export / Import ──────────────────────────────────────────────────

    def export_theme(self, dest_zip: Path) -> bool:
        """Bundle the entire theme (colors + icons + sounds) into a ZIP."""
        try:
            with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Write config
                zf.writestr('theme.json', json.dumps(self._data, indent=2))
                # Write custom icons
                for rel_name in self._data['icons'].values():
                    icon_path = self._icons_dir / rel_name
                    if icon_path.exists():
                        zf.write(icon_path, f'icons/{rel_name}')
                # Write custom sounds
                for rel_name in self._data['sounds'].values():
                    sound_path = self._sounds_dir / rel_name
                    if sound_path.exists():
                        zf.write(sound_path, f'sounds/{rel_name}')
            return True
        except Exception as e:
            print(f'[Theme] Export failed: {e}')
            return False

    def import_theme(self, zip_path: Path) -> bool:
        """Full override — extract ZIP into theme directory, replacing everything.

        Note "replacing everything": we wipe the existing custom icons/sounds
        first. Importing a theme is a clean slate, not a merge — otherwise you'd
        accumulate orphaned files from every theme you ever tried. This is fine.
        """
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Gatekeep: no theme.json, no dice. Stops someone importing a
                # random zip of cat photos and wondering why nothing happened.
                names = zf.namelist()
                if 'theme.json' not in names:
                    print('[Theme] Invalid theme ZIP: missing theme.json')
                    return False

                # Clear existing custom assets
                for f in self._icons_dir.iterdir():
                    f.unlink(missing_ok=True)
                for f in self._sounds_dir.iterdir():
                    f.unlink(missing_ok=True)

                # Extract icons and sounds
                for name in names:
                    if name.startswith('icons/') and len(name) > 6:
                        data = zf.read(name)
                        dest = self._icons_dir / Path(name).name
                        dest.write_bytes(data)
                    elif name.startswith('sounds/') and len(name) > 7:
                        data = zf.read(name)
                        dest = self._sounds_dir / Path(name).name
                        dest.write_bytes(data)

                # Load config
                config_data = json.loads(zf.read('theme.json'))
                self._data = {
                    'colors': {**DEFAULT_COLORS, **config_data.get('colors', {})},
                    'icons': config_data.get('icons', {}),
                    'sounds': config_data.get('sounds', {}),
                    'icon_tints': config_data.get('icon_tints',
                                                  {'_global': DEFAULT_ICON_TINT}),
                    'capture_card': {**DEFAULT_CAPTURE_CARD_COLORS,
                                     **config_data.get('capture_card', {})},
                }
                self.save()
            return True
        except Exception as e:
            print(f'[Theme] Import failed: {e}')
            return False

    # ─── Utility ──────────────────────────────────────────────────────────

    @property
    def icons_dir(self) -> Path:
        return self._icons_dir

    @property
    def sounds_dir(self) -> Path:
        return self._sounds_dir

    def has_any_customization(self) -> bool:
        if self._data['icons'] or self._data['sounds']:
            return True
        if self._data['colors'] != DEFAULT_COLORS:
            return True
        tints = self._data.get('icon_tints', {})
        if tints != {'_global': DEFAULT_ICON_TINT}:
            return True
        if self._data.get('capture_card', {}) != DEFAULT_CAPTURE_CARD_COLORS:
            return True
        return False
