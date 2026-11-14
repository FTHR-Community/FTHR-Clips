"""Load and save user preferences in ~/.fthr/settings.json."""
import json
import os
import tempfile
from pathlib import Path


class StoragePathError(OSError):
    """A storage location is invalid or cannot be prepared for use."""


_MISSING = object()
_CLIPS_SUBDIRECTORIES = (
    'Desktop', 'Recordings', 'Exported', 'Shared', 'Screenshots',
)


def _windows_videos_directory() -> str:
    """Return Qt's Windows Videos known-folder path without a home fallback."""
    from PySide6.QtCore import QStandardPaths

    return QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.MoviesLocation)


def _legacy_clips_directory() -> Path:
    """Return the historical library root used by existing installations."""
    return Path.home() / 'FTHR_Clips'


def default_clips_directory() -> Path:
    """Return the new-install default library root for the current platform."""

    try:
        known_folder = _windows_videos_directory()
        if not known_folder:
            raise ValueError('Windows returned an empty Videos known-folder path')
        raw_path = os.fspath(known_folder)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError('Windows returned an empty Videos known-folder path')
        videos = Path(os.path.expandvars(raw_path)).expanduser()
        if not videos.is_absolute():
            raise ValueError('Windows returned a non-absolute Videos known-folder path')
        return videos.resolve(strict=False) / 'FTHR Clips'
    except (OSError, TypeError, ValueError) as exc:
        raise StoragePathError(
            'FTHR could not locate the Windows Videos folder. Check that the '
            'Videos known folder is available, or choose a storage folder in '
            'Settings. No files were moved.') from exc


def _resolve_storage_path(value, label: str) -> Path:
    try:
        raw_path = os.fspath(value)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError('path is empty')
        resolved = Path(os.path.expandvars(raw_path)).expanduser().resolve(
            strict=False)
        if not resolved.is_absolute():
            raise ValueError('path is not absolute')
        return resolved
    except (OSError, TypeError, ValueError) as exc:
        raise StoragePathError(
            f'The configured {label} folder is invalid. Choose a valid folder '
            f'in FTHR Settings. No files were moved.') from exc


def clips_directory_from(settings_manager=None, *, missing_default=None) -> Path:
    """Resolve the selected library root or use the platform's new-install default."""
    if settings_manager is None:
        return default_clips_directory()
    try:
        configured = settings_manager.get('clips_directory', _MISSING)
        if configured is _MISSING:
            if missing_default is not None:
                return _resolve_storage_path(missing_default, 'clips storage')
            return default_clips_directory()
        return _resolve_storage_path(configured, 'clips storage')
    except StoragePathError:
        raise
    except (OSError, TypeError, ValueError):
        raise StoragePathError(
            'The configured clips storage folder is invalid. Choose a valid '
            'folder in FTHR Settings. No files were moved.') from None


def recording_directory_from(settings_manager=None) -> Path:
    """Use the same absolute, environment-expanded path in the UI and engine."""
    try:
        configured = (settings_manager.get('recording_directory', _MISSING)
                      if settings_manager is not None else _MISSING)
        if configured is _MISSING or configured is None:
            return clips_directory_from(settings_manager) / 'Recordings'
        return _resolve_storage_path(configured, 'manual recording')
    except StoragePathError:
        raise
    except (OSError, TypeError, ValueError):
        raise StoragePathError(
            'The configured manual recording folder is invalid. Choose a valid '
            'folder in FTHR Settings. No files were moved.') from None


def ensure_storage_directory(directory, *, probe_writable: bool = False) -> Path:
    """Create and optionally write-probe one storage directory."""
    resolved = _resolve_storage_path(directory, 'storage')
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))
        if probe_writable:
            fd, probe_name = tempfile.mkstemp(
                prefix='.fthr-storage-probe-', dir=str(resolved))
            os.close(fd)
            Path(probe_name).unlink()
        return resolved
    except OSError as exc:
        raise StoragePathError(
            f'FTHR cannot use the storage folder "{resolved}". Choose a '
            f'writable folder in Settings. No files were moved. ({exc})') from exc


def prepare_clips_storage(directory, *, probe_writable: bool = False) -> Path:
    """Create the library root and its standard application subfolders."""
    root = ensure_storage_directory(directory, probe_writable=probe_writable)
    for name in _CLIPS_SUBDIRECTORIES:
        ensure_storage_directory(root / name, probe_writable=probe_writable)
    return root


class SettingsManager:
    """Manages application settings persistence"""

    def __init__(self):
        # ~/.fthr is the one true home for all user state (settings, hotkeys,
        # themes). Lives outside the install dir so updates never wipe it.
        self.config_file = Path.home() / '.fthr' / 'settings.json'
        self._navigation_bindings_migrated = False
        self._retired_settings_removed = False
        self.settings = self._load_settings()
        if self._retired_settings_removed or self._navigation_bindings_migrated:
            self.save_settings()
    
    def _load_settings(self) -> dict:
        """Load settings from config file"""
        config_exists = self.config_file.exists()
        # Do not migrate an existing user's library merely because a new
        # version has a different first-run default. Only a genuinely fresh
        # install consults the redirected Windows Videos known folder.
        clips_root = (_legacy_clips_directory() if config_exists
                      else default_clips_directory())
        from core.navigation_bindings import DEFAULT_NAVIGATION_BINDINGS
        from core.ui_performance import DEFAULTS as UI_PERFORMANCE_DEFAULTS
        default_settings = {
            **UI_PERFORMANCE_DEFAULTS,
            "navigation_bindings": dict(DEFAULT_NAVIGATION_BINDINGS),
            "navigation_bindings_version": 5,
            'reduce_ui_animations': False,
            'shortcuts_enabled': True,
            'shortcut_categories_enabled': {},
            'custom_clip_lengths': [],
            'show_kofi_widget': True,
            'clip_length': 30,       # seconds
            'framerate': 60,         # FPS
            'resolution': 'source',  # 480p/720p/1080p/1440p/source
            'bitrate_level': 'medium',  # low/medium/high/custom
            # Used only when ``bitrate_level`` is ``custom``. The UI keeps it
            # within the encoder-safe 500–200,000 kbps range.
            'custom_bitrate_kbps': 25000,
            # Manual recordings temporarily switch the native encoder to this
            # profile, then return to the replay/clip profile above.  Keeping a
            # second profile is necessary because the crash-resilient recorder
            # writes the encoder's compressed packets directly.
            'recording_framerate': 60,
            'recording_resolution': 'source',
            'recording_bitrate_level': 'medium',
            'recording_custom_bitrate_kbps': 25000,
            'hotkeys': {
                'save_clip': 'F9',
                'save_screenshot': 'F12'
            },
            'quick_crop': None,      # dict {x,y,w,h,src_w,src_h} or None
            # Stable native endpoint ID on Windows. The friendly name remains
            # only for display and one-time migration of older settings files.
            'mic_device_id': None,
            'mic_device_name': None, # str display name, or None for system default
            'mic_volume': 100,       # 0–200 (scaled in callbacks)
            'mic_loopback': False,   # real-time monitor: hear your own mic
            # Gary Mode fades a calming image onto the active screen when the
            # microphone level moves through the configured response range.
            'gary_mode_enabled': False,
            'gary_min_level': 55,
            'gary_max_level': 85,
            'gary_image_path': None,
            # Windows only: X hides the main window to the notification area.
            # The dedicated title-bar power control remains the full-exit path.
            'close_to_tray': True,
            # 'stretch' = fill the target rect, distort if aspect differs.
            # 'fit'     = preserve aspect, add black bars (letterbox/pillarbox).
            'scaling_mode': 'stretch',
            # Editor audio mix — persisted between clips. Master controls the
            # QMediaPlayer playback volume directly. Per-source values are
            # applied at export time (ffmpeg amix); they only have audible
            # effect on clips recorded with multi-track audio capture.
            'master_volume': 80,     # 0–100, applied to QAudioOutput
            'source_volumes': {      # per-category 0–100, applied at export
                'game':    100,
                'browser': 100,
                'music':   100,
                'discord': 100,
            },
            'sound_volume_clip':        100,  # 0–100, FTHR notification sounds
            'sound_volume_screenshot':  100,
            'sound_volume_error':       100,
            'sound_volume_startup':     100,
            'sound_volume_upload_successful': 100,
            'sound_volume_upload_failed': 100,
            'notification_sounds_enabled': True,
            'notification_monitor': 'auto',  # 'auto' = highest refresh rate, or screen name e.g. 'DP-3'
            # Bottom-bar notifications are reserved for capture/save/upload
            # failures. Keep them enabled by default, but let users mute the
            # bar without disabling capture cards or sound cues.
            'error_notifications_enabled': True,
            'capture_monitor': '',
            'clips_directory': str(clips_root),
            'imported_clip_folders': [],  # additional folders from other clipping software
            'recording_directory': str(clips_root / 'Recordings'),
            'selected_export_preset': 'discord',
            'export_quality': 'quality',
            'parallel_exports': False,
            'parallel_export_limit': 2,
            # Encoder backend + codec + preset. ``auto`` keeps platform-native
            # selection; explicit keys are populated only after a runtime probe.
            'encoder_pref':   'auto',   # auto | nvenc | amf | qsv | software
            'codec_pref':     'auto',   # 'auto' | 'h264' | 'hevc' | 'av1'
            'encoder_preset': 4,        # 1–7
            # Suspend presentation-only polling and previews while the app is
            # hidden, minimized, or not the active desktop application. Core
            # capture/save services and notification cards/sounds stay live.
            'pause_ui_in_background': True,
            # Clip editor preview. When disabled, visual edits are retained
            # for export but the editor keeps showing the source frame.
            'clip_editor_live_preview': True,
            'timeline_preview_strip_enabled': True,
            'hide_deleted_timeline_segments': False,
            'grid_show_edited_duration': False,
            'game_detection_enabled':  False,
            # Foreground-game handoff. ``auto`` switches capture immediately;
            # ``prompt`` waits for the configured accept/dismiss hotkeys.
            'game_detection_mode':     'auto',
            # Manual rules can also carry legacy per-game crop overrides.
            'game_detection_custom_games': [],
            # If enabled, an automatically selected game is replaced by
            # desktop capture after the game window has actually closed.
            'game_detection_fallback_desktop': False,
            'capture_mode':           'desktop',
            'target_hwnd':            0,
            'target_window_name':     '',
            # Stable future-save folder identity; title remains separate UI text.
            'target_clip_group':      '',
            'audio_capture_enabled':   True,
            # Combined system + microphone audio is the simple, portable
            # default. Separated streams are an explicit opt-in because they
            # require a multi-track editor/export path.
            'audio_capture_mode':      'combined',
            # Extra playback devices recorded as own tracks: [{'id','name'}].
            'audio_device_tracks':     [],
            # None follows the separated mode; True/False is an explicit
            # choice for per-program tracks.
            'per_app_audio_tracks':    None,
            # Visual notification card. Sound cues remain active when this
            # is disabled because they are handled by the same sound-only
            # helper process.
            'capture_card_enabled':    True,
            'watermark_enabled':  False,
            'anticheat_detection_enabled': False,
            'camera_enabled':       False,
            'camera_device_index':  0,
            'camera_position':      'bottom-right',
            'camera_size':          'medium',
            'camera_overlay_rect': {
                'x': 0.72, 'y': 0.64, 'w': 0.25, 'h': 0.34,
            },
            'image_overlay_enabled': False,
            'image_overlay_path': '',
            'image_overlay_opacity': 100,
            'image_overlay_fit': 'fit',
            'image_overlay_rect': {
                'x': 0.76, 'y': 0.04, 'w': 0.20, 'h': 0.24,
            },
            # Ordered image layers. The singular keys above remain as a
            # compatibility mirror for older themes/settings builds.
            'image_overlays': [],
            # External keyboard-visualizer settings are separate from the retired
            # keyboard_overlay_* bitmap-overlay settings.
            'third_party_keyboard': {
                'enabled': False,
                'hwnd': 0,
                'window_name': '',
                'color': '#00ff00',
                'intensity': 58,
                'rect': {
                    'x': 0.30, 'y': 0.70, 'w': 0.40, 'h': 0.25,
                },
            },
            # Empty means the bundled desktop screenshot is used. A user
            # selected image is stored here so the preview remains portable
            # and can be reset to the standard background at any time.
            'input_overlay_preview_background': '',
        }
        
        if not config_exists:
            return default_settings

        try:
            with open(self.config_file, 'r') as f:
                loaded = json.load(f)
            # Merge persisted settings over defaults, including one level of nested
            # keys, so older files retain new defaults without losing user choices.
            merged = dict(default_settings)
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            if 'clips_directory' not in loaded:
                merged['clips_directory'] = str(_legacy_clips_directory())
            if 'recording_directory' not in loaded:
                library_root = _resolve_storage_path(
                    merged.get('clips_directory'), 'clips storage')
                merged['recording_directory'] = str(library_root / 'Recordings')
            if loaded.get('navigation_bindings_version', 1) < 4:
                bindings = merged.get('navigation_bindings', {})
                if isinstance(bindings, dict):
                    if loaded.get('navigation_bindings_version', 1) < 2:
                        for action, old, new in (
                                ('editor_next', 'Right', 'Tab+Right'),
                                ('editor_previous', 'Left', 'Tab+Left')):
                            if bindings.get(action) == old:
                                bindings[action] = new
                    # Move the old default pair together; customized close
                    # and minimize bindings remain the user's choice.
                    if (bindings.get('editor_minimize') == 'Escape'
                            and bindings.get('editor_close') == 'Shift+Escape'):
                        bindings['editor_minimize'] = 'Shift+Escape'
                        bindings['editor_close'] = 'Escape'
                    merged['navigation_bindings'] = {
                        key: value for key, value in bindings.items()
                        if key in DEFAULT_NAVIGATION_BINDINGS}
                merged['navigation_bindings_version'] = 4
                self._navigation_bindings_migrated = True
            if loaded.get('navigation_bindings_version', 1) < 5:
                bindings = merged.get('navigation_bindings', {})
                if isinstance(bindings, dict) and bindings.get('editor_split') == 'Ctrl+S':
                    bindings['editor_split'] = 'Ctrl+C'
                merged['navigation_bindings_version'] = 5
                self._navigation_bindings_migrated = True
            # Existing installations should not change recording quality just
            # because the profile feature was added.  Seed each missing
            # recording value from the user's established clip value.
            for recording_key, clip_key in (
                    ('recording_framerate', 'framerate'),
                    ('recording_resolution', 'resolution'),
                    ('recording_bitrate_level', 'bitrate_level'),
                    ('recording_custom_bitrate_kbps', 'custom_bitrate_kbps')):
                if recording_key not in loaded:
                    merged[recording_key] = merged[clip_key]
            retired_overlay_keys = [
                key for key in merged
                if (key.startswith('keyboard_overlay_')
                    or key.startswith('mouse_overlay_')
                    or key == 'input_overlay_preview_clip')
            ]
            retired_audio_keys = [
                key for key in ('multiband_audio_enabled', 'audio_categories')
                if key in merged
            ]
            retired_feature_keys = [
                key for key in ('auto_crop_enabled', 'extended_clip_length')
                if key in merged
            ]
            for key in (*retired_overlay_keys, *retired_audio_keys,
                        *retired_feature_keys):
                merged.pop(key, None)
            retired_hotkey = merged.get('hotkeys', {}).pop('save_extended_clip', None)
            self._retired_settings_removed = bool(
                retired_hotkey is not None or
                retired_overlay_keys or retired_audio_keys or retired_feature_keys)
            if ('image_overlays' not in loaded
                    and str(loaded.get('image_overlay_path', '') or '')):
                from core.camera_overlay import new_image_overlay_layer
                layer = new_image_overlay_layer(
                    str(loaded.get('image_overlay_path', '')), 0)
                layer.update({
                    'enabled': bool(loaded.get('image_overlay_enabled', False)),
                    'opacity': loaded.get('image_overlay_opacity', 100),
                    'fit': loaded.get('image_overlay_fit', 'fit'),
                    'rect': loaded.get('image_overlay_rect', layer['rect']),
                })
                merged['image_overlays'] = [layer]
                self._retired_settings_removed = True
            if 'camera_overlay_rect' not in loaded:
                from core.camera_overlay import legacy_overlay_rect
                merged['camera_overlay_rect'] = legacy_overlay_rect(
                    loaded.get('camera_position', 'bottom-right'),
                    loaded.get('camera_size', 'medium'),
                )
            return merged
        except StoragePathError:
            raise
        except Exception as e:
            print(f"Failed to load settings: {e}")
            # Keep invalid settings for diagnosis before falling back to defaults.
            try:
                self.config_file.replace(self.config_file.with_suffix('.json.corrupt'))
                print(f"Corrupt settings backed up to {self.config_file.with_suffix('.json.corrupt')}")
            except OSError:
                pass
            return default_settings
    
    def save_settings(self):
        """Save current settings to file"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            # Drop runtime-only scratch values before persisting user settings.
            persistable = {k: v for k, v in self.settings.items() if not k.startswith('_')}
            # Write to a temp file first, then atomically replace.
            # A crash or SIGKILL during a direct write truncates the JSON and
            # loses all settings on next launch.
            tmp = self.config_file.with_suffix('.json.tmp')
            with open(tmp, 'w') as f:
                json.dump(persistable, f, indent=2)
            os.replace(str(tmp), str(self.config_file))
        except Exception as e:
            print(f"Failed to save settings: {e}")
            try:
                if 'tmp' in locals():
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            print(f"[Settings] Saved to {self.config_file}")
        except Exception:
            # A console encoding failure cannot turn a committed write into a
            # reported settings failure.
            pass
        return True
    
    def get(self, key: str, default=None):
        """Get a setting value"""
        return self.settings.get(key, default)
    
    def set(self, key: str, value):
        """Set a setting value"""
        self.settings[key] = value
    
    def update(self, settings_dict: dict):
        """Update multiple settings at once"""
        self.settings.update(settings_dict)
