"""Load and save user preferences in ~/.fthr/settings.json."""
import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from core.replay_storage import REPLAY_STORAGE_KEY, ReplayStorageSettings


class StoragePathError(OSError):
    """A storage location is invalid or cannot be prepared for use."""


_MISSING = object()
_SETTINGS_WRITER_LOCK = RLock()
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


def clips_directory_lexical_from(settings_manager=None, *, missing_default=None) -> Path:
    """Read the configured clips path without resolving filesystem links.

    GUI admission compares the configured spelling only.  The storage worker
    performs the authoritative non-reparse root identity check before walking.
    """
    if settings_manager is None:
        return clips_directory_from(None, missing_default=missing_default)
    try:
        configured = settings_manager.get('clips_directory', _MISSING)
        if configured is _MISSING:
            if missing_default is None:
                return clips_directory_from(settings_manager)
            configured = missing_default
        raw_path = os.fspath(configured)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError('path is empty')
        path = Path(os.path.expandvars(raw_path)).expanduser()
        path = Path(os.path.abspath(os.fspath(path)))
        if not path.is_absolute():
            raise ValueError('path is not absolute')
        return path
    except (OSError, TypeError, ValueError) as exc:
        raise StoragePathError(
            'The configured clips storage folder is invalid. Choose a valid '
            'folder in FTHR Settings. No files were moved.') from exc


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
        self._persistence_blocked = False
        self._corrupt_backup_path = None
        self._corrupt_backup_verified = False
        with _SETTINGS_WRITER_LOCK:
            self.settings = self._load_settings()
            if self._retired_settings_removed or self._navigation_bindings_migrated:
                self.save_settings()

    def _load_settings(self) -> dict:
        """Load settings from config file"""
        existence_error = None
        try:
            self.config_file.stat()
            config_exists = True
        except FileNotFoundError:
            config_exists = False
        except OSError as error:
            existence_error = error
            config_exists = True  # Unreadable is not a new installation.
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
            'shortcuts_enabled': True,
            'shortcut_categories_enabled': {},
            'custom_clip_lengths': [],
            REPLAY_STORAGE_KEY: ReplayStorageSettings().to_dict(),
            'allow_replay_gaps': True,
            'allow_low_fps_clips': True,
            'capture_cursor': True,
            'show_kofi_widget': True,
            'clip_length': 30,       # seconds
            'clip_post_roll_enabled': False,
            'clip_post_roll_seconds': 5,
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
            # Windows only: X hides the main window to the notification area.
            # The dedicated title-bar power control remains the full-exit path.
            'close_to_tray': True,
            # Packaged Windows builds check the website; downloads are opt-in.
            'app_updates_auto_check': False,
            'app_updates_privacy_consent': False,
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
            # Per-root storage limits are intentionally opt-in.  Keep this
            # mapping separate from the selected root so switching libraries
            # never silently transfers a quota or an automation toggle.
            'storage_policies': {},
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
            # Foreground-game handoff. ``auto`` switches capture immediately;
            # ``prompt`` waits for the configured accept/dismiss hotkeys.
            'game_detection_mode':     'auto',
            'game_detection_enabled':  True,
            # Manual rules can also carry legacy per-game crop overrides.
            'game_detection_custom_games': [],
            # If enabled, an automatically selected game is replaced by
            # desktop capture after the game window has actually closed.
            'game_detection_fallback_desktop': False,
            'capture_mode':           'desktop',
            # Track whether the selected source was picked manually or
            # selected by game detection for startup and game-close handling.
            'capture_source_authority': 'manual',
            'target_hwnd':            0,
            'target_window_name':     '',
            # Stable manual HWND identity; a reused HWND is not the same target.
            'target_window_identity': None,
            # Stable future-save folder identity; title remains separate UI text.
            'target_clip_group':      '',
            'audio_capture_enabled':   True,
            # Combined system + microphone audio is the simple, portable
            # default. Separated streams are an explicit opt-in because they
            # require a multi-track editor/export path.
            'audio_capture_mode':      'combined',
            'audio_source_mode':       None,
            'audio_device_tracks':     [],
            'per_app_audio_tracks':    None,
            'application_audio_exclusions': [],
            'keep_system_audio_backup': True,
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

        raw = None
        try:
            if existence_error is not None:
                raise existence_error
            raw = self.config_file.read_bytes()
            loaded = json.loads(raw)
            # Merge persisted settings over defaults, including one level of nested
            # keys, so older files retain new defaults without losing user choices.
            merged = dict(default_settings)
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            mode = str(merged['game_detection_mode']).lower()
            if (merged['game_detection_mode'] != mode
                    or mode not in {'auto', 'prompt'}):
                merged['game_detection_mode'] = (
                    'prompt' if mode == 'prompt' else 'auto')
                if mode == 'paused':
                    merged['game_detection_enabled'] = False
                self._retired_settings_removed = True
            merged['game_detection_enabled'] = merged['game_detection_enabled'] is not False
            merged['allow_replay_gaps'] = merged['allow_replay_gaps'] is True
            merged['allow_low_fps_clips'] = merged['allow_low_fps_clips'] is True
            merged['capture_cursor'] = merged['capture_cursor'] is not False
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
                self._retired_settings_removed or retired_hotkey is not None or
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
            merged[REPLAY_STORAGE_KEY] = ReplayStorageSettings.from_value(
                merged.get(REPLAY_STORAGE_KEY)).to_dict()
            return merged
        except StoragePathError:
            raise
        except Exception as e:
            # A partial migration is not a reason to overwrite a corrupt input
            # automatically. Defaults remain RAM-only until an explicit save.
            self._navigation_bindings_migrated = False
            self._retired_settings_removed = False
            preservation_error = None
            try:
                if raw is None:
                    raise OSError('Original settings bytes could not be read')
                # Exclusive creation preserves previous quarantines. Preserve
                # precisely the bytes passed to JSON, not a later reread.
                with tempfile.NamedTemporaryFile(
                        mode='wb', dir=self.config_file.parent,
                        prefix=self.config_file.name + '.corrupt-', delete=False) as backup:
                    self._corrupt_backup_path = Path(backup.name)
                    if backup.write(raw) != len(raw):
                        raise OSError('Incomplete settings preservation write')
                    backup.flush()
                    os.fsync(backup.fileno())
                self._corrupt_backup_verified = True
            except Exception as backup_error:
                self._persistence_blocked = True
                preservation_error = backup_error
            try:
                print(f'Failed to load settings: {e}. Using defaults in memory.')
                if preservation_error is not None:
                    print('Settings persistence blocked: original settings could not '
                          f'be safely preserved ({preservation_error}). '
                          f'Unverified backup, if created: {self._corrupt_backup_path}')
                else:
                    print(f'Original settings preserved at {self._corrupt_backup_path}; '
                          'source file left unchanged.')
            except Exception:
                # Console failures cannot lift the persistence safety latch.
                pass
            return default_settings

    def _persist_settings_candidate(self, candidate: dict) -> bool:
        """Atomically persist one detached settings candidate.

        The candidate writer is deliberately narrow so the game-profile API
        can stage a document and publish it to RAM only after the replace
        succeeds.  Generic settings persistence continues to use this same
        atomic path and retains its historical return/error behavior.
        """
        if getattr(self, '_persistence_blocked', False):
            return False
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            # Drop runtime-only scratch values before persisting user settings.
            persistable = {k: v for k, v in candidate.items()
                           if not k.startswith('_')}
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
        return True

    def save_settings(self):
        """Save current settings to file."""
        with _SETTINGS_WRITER_LOCK:
            saved = self._persist_settings_candidate(self.settings)
        if not saved:
            return False
        try:
            print(f"[Settings] Saved to {self.config_file}")
        except Exception:
            # A console encoding failure cannot turn a committed write into a
            # reported settings failure.
            pass
        return True

    def read_game_capture_profiles(self):
        """Read the strict per-game catalog without inserting defaults."""
        from core.game_profiles import STORAGE_KEY, catalog_from_raw
        from core.game_profiles import empty_catalog

        with _SETTINGS_WRITER_LOCK:
            if STORAGE_KEY not in self.settings:
                return empty_catalog()
            raw = self.settings[STORAGE_KEY]
            return catalog_from_raw(raw)

    def save_game_capture_profiles(self, catalog, *, expected_revision) -> bool:
        """Persist a versioned per-game catalog with same-manager CAS.

        The compare, transition validation, stage, atomic replace and RAM
        publication are one serialized operation for this manager.  The
        contract intentionally does not claim coordination with another
        process or an external editor of the same JSON file.
        """
        from core.game_profiles import STORAGE_KEY
        from core.game_profiles import catalog_from_dict
        from core.game_profiles import validate_catalog_transition

        with _SETTINGS_WRITER_LOCK:
            current = self.read_game_capture_profiles()
            if (isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                    or expected_revision < 0):
                raise ValueError('expected_revision must be a non-negative integer')
            if expected_revision != current.revision:
                return False
            candidate = catalog_from_dict(catalog.to_dict()
                                          if hasattr(catalog, 'to_dict')
                                          else catalog)
            validate_catalog_transition(current, candidate)
            if candidate.to_dict() == current.to_dict():
                return True

            # Keep the old in-memory value visible until the disk commit is
            # successful.  A shallow copy is sufficient because no nested
            # value is mutated while the staged candidate is serialized.
            staged = dict(self.settings)
            staged[STORAGE_KEY] = candidate.to_dict()
            if not self._persist_settings_candidate(staged):
                return False
            self.settings[STORAGE_KEY] = candidate.to_dict()
            return True

    def read_capture_admission_policy(self):
        """Read the executable-admission policy without inserting defaults."""
        from core.capture_admission import STORAGE_KEY, empty_policy
        from core.capture_admission import policy_from_raw

        with _SETTINGS_WRITER_LOCK:
            if STORAGE_KEY not in self.settings:
                return empty_policy()
            return policy_from_raw(self.settings[STORAGE_KEY])

    def save_capture_admission_policy(self, policy, *, expected_revision) -> bool:
        """Persist an admission policy with same-manager revision CAS.

        The candidate is fully validated and staged before the successful
        atomic write is published to RAM.  This intentionally provides the
        same-manager guarantee as the game-profile API and makes no claim
        about another process editing the settings file concurrently.
        """
        from core.capture_admission import STORAGE_KEY
        from core.capture_admission import policy_from_dict
        from core.capture_admission import validate_policy_transition

        with _SETTINGS_WRITER_LOCK:
            current = self.read_capture_admission_policy()
            if (type(expected_revision) is not int
                    or expected_revision < 0):
                raise ValueError(
                    'expected_revision must be a non-negative integer')
            if expected_revision != current.revision:
                return False
            candidate = policy_from_dict(
                policy.to_dict() if hasattr(policy, 'to_dict') else policy)
            validate_policy_transition(current, candidate)
            if candidate.to_dict() == current.to_dict():
                return True

            staged = dict(self.settings)
            staged[STORAGE_KEY] = candidate.to_dict()
            if not self._persist_settings_candidate(staged):
                return False
            self.settings[STORAGE_KEY] = candidate.to_dict()
            return True

    def storage_policy_for(self, root, *, strict=False):
        """Return the validated policy for one canonical clips root.

        Invalid or future entries are fail-closed.  They are not repaired here:
        a later settings save must not erase a policy the current build cannot
        interpret.
        """
        from core.library_storage import policy_for_root
        return policy_for_root(self, root, strict=strict)

    def save_storage_policy(self, root, policy) -> bool:
        """Persist one validated root policy with RAM rollback on failure."""
        from core.library_storage import StoragePolicy, policy_map_with
        if not isinstance(policy, StoragePolicy):
            raise TypeError('policy must be a StoragePolicy')
        # Revalidate at the persistence boundary so a future caller cannot
        # bypass the strict JSON contract with a mutable/subclassed object.
        policy = StoragePolicy.from_mapping(policy.to_dict(), strict=True)
        previous = self.settings.get('storage_policies', _MISSING)
        updated = policy_map_with(self, root, policy)
        self.settings['storage_policies'] = updated
        try:
            saved = bool(self.save_settings())
        except Exception:
            if previous is _MISSING:
                self.settings.pop('storage_policies', None)
            else:
                self.settings['storage_policies'] = previous
            raise
        if saved:
            return True
        if previous is _MISSING:
            self.settings.pop('storage_policies', None)
        else:
            self.settings['storage_policies'] = previous
        return False

    def get(self, key: str, default=None):
        """Get a setting value"""
        return self.settings.get(key, default)

    def set(self, key: str, value):
        """Set a setting value"""
        self.settings[key] = value

    def update(self, settings_dict: dict):
        """Update multiple settings at once"""
        self.settings.update(settings_dict)
