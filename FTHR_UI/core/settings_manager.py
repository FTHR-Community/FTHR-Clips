"""Load and save user preferences in ~/.fthr/settings.json."""
import json
import os
from pathlib import Path


def default_clips_directory() -> Path:
    """Return the default root used for FTHR's local clip library."""
    return Path.home() / 'FTHR_Clips'


def clips_directory_from(settings_manager=None) -> Path:
    """Resolve the user-selected clip library root, with a safe fallback."""
    default = default_clips_directory()
    if settings_manager is None:
        return default
    try:
        configured = settings_manager.get('clips_directory', default)
        if not configured:
            return default
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        # User-editable settings may contain a malformed path; keep the app on
        # its known local library root instead of failing during startup.
        return default


def recording_directory_from(settings_manager=None) -> Path:
    """Use the same absolute, environment-expanded path in the UI and engine."""
    default = clips_directory_from(settings_manager) / 'Recordings'
    try:
        configured = (settings_manager.get('recording_directory', default)
                      if settings_manager is not None else default)
        return Path(os.path.expandvars(configured or default)).expanduser().resolve(
            strict=False)
    except (OSError, TypeError, ValueError):
        return default


class SettingsManager:
    """Manages application settings persistence"""

    def __init__(self):
        # ~/.fthr is the one true home for all user state (settings, hotkeys,
        # themes). Lives outside the install dir so updates never wipe it.
        self.config_file = Path.home() / '.fthr' / 'settings.json'
        self._retired_settings_removed = False
        self.settings = self._load_settings()
        if self._retired_settings_removed:
            self.save_settings()
    
    def _load_settings(self) -> dict:
        """Load settings from config file"""
        default_settings = {
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
            # Windows: stable monitor device path. Linux: wl_output name.
            'capture_monitor': '',
            'clips_directory': str(default_clips_directory()),
            'imported_clip_folders': [],  # additional folders from other clipping software
            'recording_directory': str(default_clips_directory() / 'Recordings'),
            'deduplication_count': 0,
            'deduplication_saved_bytes': 0,
            'selected_export_preset': 'discord',
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
            'audio_capture_enabled':   True,
            # Combined system + microphone audio is the simple, portable
            # default. Separated streams are an explicit opt-in because they
            # require a multi-track editor/export path.
            'audio_capture_mode':      'combined',
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
            'merge_overlapping_clips': False,
            'replay_storage_mode': 'auto',  # 'auto' | 'memory' | 'disk'
            # Empty means the bundled desktop screenshot is used. A user
            # selected image is stored here so the preview remains portable
            # and can be reset to the standard background at any time.
            'input_overlay_preview_background': '',
        }
        
        if not self.config_file.exists():
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
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        # Drop runtime-only scratch values before persisting user settings.
        persistable = {k: v for k, v in self.settings.items() if not k.startswith('_')}
        # Write to a temp file first, then atomically replace.
        # A crash or SIGKILL during a direct write truncates the JSON and
        # loses all settings on next launch.
        tmp = self.config_file.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w') as f:
                json.dump(persistable, f, indent=2)
            os.replace(str(tmp), str(self.config_file))
        except Exception as e:
            print(f"Failed to save settings: {e}")
            try:
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
