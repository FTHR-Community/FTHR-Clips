"""Coordinate optional upload packages through local subprocess IPC.

Core owns consent, extraction, settings, and queueing. Network requests
run in the uploader; hardware identity requires its separate capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from core.clip_files import is_completed_video_path, iter_safe_tree
from core.clip_readiness import (
    ClipReadinessRegistry,
    ClipReadinessState,
    get_clip_readiness_registry,
)
from core.export_profiles import (
    MIB,
    compress_media,
    provider_compression_preset,
    provider_limit_mb,
)
from core.uploader_bundle_manifest import (
    EXPECTED_HARDWARE_BUNDLE_SHA256,
    EXPECTED_UPLOADER_BUNDLE_SHA256,
    EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256,
    HARDWARE_PLUGIN_ID,
    HARDWARE_PLUGIN_VERSION,
    HARDWARE_POLICY_VERSION,
    UPLOADER_PLUGIN_ID,
    UPLOADER_PLUGIN_VERSION,
    UPLOADER_PRIVACY_VERSION,
    UPLOADER_TERMS_VERSION,
)


_LOCAL_APP_DATA = Path(os.environ.get(
    'LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
_PLUGIN_DATA_ROOT = _LOCAL_APP_DATA / 'FTHR Clips' / 'plugins'
_UPLOADER_ROOT = _PLUGIN_DATA_ROOT / 'uploader'
_HARDWARE_ROOT = _PLUGIN_DATA_ROOT / 'hardware-identity'
_UPLOADER_ACTIVATION_FILE = _UPLOADER_ROOT / 'activation.json'
_HARDWARE_ACTIVATION_FILE = _HARDWARE_ROOT / 'activation.json'

_SETTINGS_FILE = Path.home() / '.fthr' / 'uploader' / 'settings.json'
_HISTORY_FILE = Path.home() / '.fthr' / 'uploader' / 'upload_history.json'
_CREDENTIALS_FILE = Path.home() / '.fthr' / 'uploader' / 'credentials.json'
_LEGACY_HISTORY_FILE = Path.home() / '.fthr' / 'upload_history.json'
_CLIPS_DIR = Path.home() / 'FTHR_Clips'
_WRITE_SETTLE_S = 30

CATBOX_LEGAL_VERSION = 'catbox-legal-2021-03-06'
LUSTFUL_LEGAL_VERSION = 'lustful-legal-2026-07-27'

_DEFAULT_SETTINGS: dict[str, Any] = {
    'upload_enabled': False,
    'upload_provider': 'catbox',
    'catbox_userhash': '',
    'upload_server_url': '',
    'upload_auth_header': '',
    'upload_mode': 'manual',
    'upload_interval_value': 5,
    'upload_interval_unit': 'minutes',
    'upload_auto_compress': False,
    'upload_auto_delete': False,
    'uploader_terms_accepted_version': '',
    'uploader_privacy_accepted_version': '',
    'uploader_accepted_at': '',
    'catbox_legal_accepted_version': '',
    'catbox_legal_accepted_at': '',
    'lustful_legal_accepted_version': '',
    'lustful_legal_accepted_at': '',
    'lustful_hardware_policy_accepted_version': '',
    'lustful_hardware_policy_accepted_at': '',
}

_LEGACY_UPLOAD_KEYS = (
    'upload_enabled',
    'upload_server_url',
    'upload_auth_header',
    'upload_mode',
    'upload_interval_value',
    'upload_interval_unit',
    'upload_auto_delete',
)


@dataclass(frozen=True)
class _BundleSpec:
    label: str
    filename: str
    plugin_id: str
    plugin_version: str
    expected_sha256: str
    entrypoint: str
    install_root: Path
    receipt_path: Path


def _uploader_spec() -> _BundleSpec:
    if sys.platform == 'win32':
        filename = 'FTHR-Uploader.fthrplugin'
        entrypoint = 'FTHR Uploader.exe'
    else:
        filename = 'FTHR-Uploader-linux.fthrplugin'
        entrypoint = 'FTHR-Uploader'
    return _BundleSpec(
        label='FTHR Upload Extension',
        filename=filename,
        plugin_id=UPLOADER_PLUGIN_ID,
        plugin_version=UPLOADER_PLUGIN_VERSION,
        expected_sha256=(
            EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256
            if sys.platform != 'win32' else EXPECTED_UPLOADER_BUNDLE_SHA256),
        entrypoint=entrypoint,
        install_root=_UPLOADER_ROOT,
        receipt_path=_UPLOADER_ACTIVATION_FILE,
    )


def _hardware_spec() -> _BundleSpec:
    return _BundleSpec(
        label='FTHR Lustful Hardware Identity',
        filename='FTHR-Hardware-Identity.fthrplugin',
        plugin_id=HARDWARE_PLUGIN_ID,
        plugin_version=HARDWARE_PLUGIN_VERSION,
        expected_sha256=EXPECTED_HARDWARE_BUNDLE_SHA256,
        entrypoint='FTHR Hardware Identity.exe',
        install_root=_HARDWARE_ROOT,
        receipt_path=_HARDWARE_ACTIVATION_FILE,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object in {path}')
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    os.replace(str(temporary), str(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        # ``relative_to`` uses ValueError to report the normal outside-root case.
        return False


class UploadManager(QObject):
    """Core-side facade retaining the app's existing upload-facing API."""

    upload_started = Signal(str)
    upload_finished = Signal(str, bool, str)
    upload_error = Signal(str, str, str, str)
    compression_required = Signal(str, str, int)  # path, provider, hard limit MB
    compression_started = Signal(str, str)
    compression_progress = Signal(str, int, str)
    plugin_state_changed = Signal(bool, bool)

    def __init__(
            self,
            settings_manager,
            readiness: ClipReadinessRegistry | None = None):
        super().__init__()
        self._core_settings = settings_manager
        self._readiness = readiness or get_clip_readiness_registry()
        self._settings = self._load_settings()
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._interval_timer = QTimer(self)
        self._interval_timer.timeout.connect(self._interval_scan)
        self._interval_scan_lock = threading.Lock()
        self._interval_scan_thread: threading.Thread | None = None
        self._interval_scan_cancel = threading.Event()
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._history: dict[str, Any] = {}
        self._history_mtime_ns = -1
        self._bundle_cache: dict[str, tuple[str, dict[str, Any], str, str]] = {}

    # Settings adapter

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._settings[key] = value

    def save_settings(self) -> bool:
        try:
            _atomic_json(_SETTINGS_FILE, self._settings)
            self.refresh_settings()
            return True
        except OSError as exc:
            print(f'[Uploader] Could not save optional-extension settings: {exc}')
            return False

    def clips_directory(self) -> Path:
        configured = self._core_settings.get('clips_directory', '')
        if configured:
            try:
                return Path(configured).expanduser().resolve(strict=False)
            except (OSError, TypeError, ValueError):
                # A malformed core setting must not break the optional uploader;
                # retain its historical fallback root for this scan.
                pass
        return _CLIPS_DIR

    def _load_settings(self) -> dict[str, Any]:
        if not _SETTINGS_FILE.is_file():
            return dict(_DEFAULT_SETTINGS)
        try:
            return {**_DEFAULT_SETTINGS, **_read_object(_SETTINGS_FILE)}
        except (OSError, ValueError, TypeError):
            return dict(_DEFAULT_SETTINGS)

    # Dormant bundles and activation

    def bundle_path(self, filename: str = 'FTHR-Uploader.fthrplugin') -> Path:
        candidates: list[Path] = []
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            candidates.append(Path(meipass) / 'plugin-packages' / filename)
        if getattr(sys, 'frozen', False):
            candidates.append(
                Path(sys.executable).resolve().parent / 'plugin-packages' / filename)
        candidates.append(
            Path(__file__).resolve().parents[2] / 'plugin-packages' / filename)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0] if candidates else Path(filename)

    def _inspect_bundle(
            self,
            spec: _BundleSpec) -> tuple[dict[str, Any], str, str]:
        bundle = self.bundle_path(spec.filename)
        if not bundle.is_file():
            raise FileNotFoundError(
                f'The dormant {spec.label} bundle is missing. Reinstall FTHR Clips.')
        digest = _sha256(bundle).lower()
        expected = spec.expected_sha256.strip().lower()
        if not expected:
            raise RuntimeError(
                f'This development build is not bound to a verified {spec.label} bundle.')
        if digest != expected:
            raise PermissionError(f'The {spec.label} bundle failed its integrity check.')
        cached = self._bundle_cache.get(spec.plugin_id)
        if cached and cached[0] == digest:
            return dict(cached[1]), cached[2], cached[3]

        with zipfile.ZipFile(bundle, 'r') as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
            terms = archive.read('TERMS_OF_SERVICE.txt').decode('utf-8')
            privacy = archive.read('PRIVACY_POLICY.txt').decode('utf-8')
        if not isinstance(manifest, dict):
            raise ValueError(f'{spec.label} manifest is invalid.')
        if manifest.get('plugin_id') != spec.plugin_id:
            raise ValueError(f'{spec.label} plugin ID does not match.')
        if manifest.get('plugin_version') != spec.plugin_version:
            raise ValueError(f'{spec.label} version does not match Core.')
        if manifest.get('entrypoint') != spec.entrypoint:
            raise ValueError(f'{spec.label} entrypoint does not match Core.')
        self._bundle_cache[spec.plugin_id] = (digest, dict(manifest), terms, privacy)
        return manifest, terms, privacy

    def uploader_legal_text(self) -> tuple[str, str]:
        _, terms, privacy = self._inspect_bundle(_uploader_spec())
        return terms, privacy

    def hardware_legal_text(self) -> tuple[str, str]:
        _, terms, privacy = self._inspect_bundle(_hardware_spec())
        return terms, privacy

    def _is_installed(self, spec: _BundleSpec) -> bool:
        try:
            receipt = _read_object(spec.receipt_path)
            if receipt.get('plugin_id') != spec.plugin_id:
                return False
            if receipt.get('plugin_version') != spec.plugin_version:
                return False
            if receipt.get('bundle_sha256', '').lower() != spec.expected_sha256.lower():
                return False
            executable = Path(str(receipt.get('executable', '')))
            expected_exe = str(receipt.get('executable_sha256', '')).lower()
            return (
                executable.is_file()
                and bool(expected_exe)
                and _sha256(executable).lower() == expected_exe
            )
        except (OSError, ValueError, TypeError):
            # Missing, stale, or malformed receipts mean "not installed".
            return False

    def is_plugin_installed(self) -> bool:
        if not self._is_installed(_uploader_spec()):
            return False
        try:
            receipt = _read_object(_UPLOADER_ACTIVATION_FILE)
            return (
                receipt.get('terms_version') == UPLOADER_TERMS_VERSION
                and receipt.get('privacy_version') == UPLOADER_PRIVACY_VERSION
            )
        except (OSError, ValueError, TypeError):
            # A malformed legal receipt is never treated as current consent.
            return False

    def is_hardware_identity_installed(self) -> bool:
        if not self._is_installed(_hardware_spec()):
            return False
        try:
            receipt = _read_object(_HARDWARE_ACTIVATION_FILE)
            return receipt.get('policy_version') == HARDWARE_POLICY_VERSION
        except (OSError, ValueError, TypeError):
            # A malformed capability receipt is never treated as current consent.
            return False

    def _activate_bundle(
            self,
            spec: _BundleSpec,
            accepted_versions: dict[str, str]) -> tuple[bool, str]:
        try:
            manifest, _, _ = self._inspect_bundle(spec)
            for field, accepted in accepted_versions.items():
                if str(manifest.get(field, '')) != accepted:
                    return False, f'The {spec.label} legal notice was not accepted.'

            entries = manifest.get('files')
            if not isinstance(entries, list) or not entries:
                raise ValueError(f'{spec.label} has no verified payload list.')
            bundle = self.bundle_path(spec.filename)
            bundle_digest = _sha256(bundle).lower()
            spec.install_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='.install-', dir=str(spec.install_root)))
            try:
                allowed = {
                    'manifest.json',
                    'TERMS_OF_SERVICE.txt',
                    'PRIVACY_POLICY.txt',
                }
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError(f'{spec.label} payload entry is invalid.')
                    name = str(entry.get('path', '')).replace('\\', '/')
                    if not name or name.startswith('/') or '..' in Path(name).parts:
                        raise ValueError(f'{spec.label} contains an unsafe path.')
                    allowed.add(name)

                with zipfile.ZipFile(bundle, 'r') as archive:
                    names = {
                        item.filename.replace('\\', '/')
                        for item in archive.infolist()
                        if not item.is_dir()
                    }
                    if names != allowed:
                        raise ValueError(f'{spec.label} contains undeclared payload files.')
                    root = temporary.resolve(strict=False)
                    for item in archive.infolist():
                        if item.is_dir():
                            continue
                        name = item.filename.replace('\\', '/')
                        target = (temporary / name).resolve(strict=False)
                        if target != root and not _is_within(target, root):
                            raise ValueError(f'{spec.label} path escaped staging.')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item, 'r') as source, target.open('wb') as output:
                            shutil.copyfileobj(source, output)

                for entry in entries:
                    target = temporary / str(entry['path'])
                    if _sha256(target).lower() != str(entry.get('sha256', '')).lower():
                        raise PermissionError(
                            f'{spec.label} payload integrity failed: {entry["path"]}')

                version_dir = (spec.install_root / spec.plugin_version).resolve(strict=False)
                install_root = spec.install_root.resolve(strict=False)
                if not _is_within(version_dir, install_root):
                    raise PermissionError(f'{spec.label} install path is invalid.')
                if version_dir.exists():
                    shutil.rmtree(version_dir)
                payload_dir = temporary / 'payload'
                if not payload_dir.is_dir():
                    raise ValueError(f'{spec.label} payload directory is missing.')
                os.replace(str(payload_dir), str(version_dir))
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

            executable = version_dir / spec.entrypoint
            if not executable.is_file():
                raise FileNotFoundError(f'Installed {spec.label} executable is missing.')
            accepted_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            receipt = {
                'plugin_id': spec.plugin_id,
                'plugin_version': spec.plugin_version,
                **accepted_versions,
                'accepted_at': accepted_at,
                'activation_id': secrets.token_urlsafe(32),
                'bundle_sha256': bundle_digest,
                'executable': str(executable),
                'executable_sha256': _sha256(executable),
                'settings_file': str(_SETTINGS_FILE),
            }
            _atomic_json(spec.receipt_path, receipt)
            return True, f'{spec.label} installed.'
        except Exception as exc:
            return False, str(exc)

    def activate_plugin(
            self,
            accepted_terms_version: str,
            accepted_privacy_version: str) -> tuple[bool, str]:
        if accepted_terms_version != UPLOADER_TERMS_VERSION:
            return False, 'The uploader Terms of Service were not accepted.'
        if accepted_privacy_version != UPLOADER_PRIVACY_VERSION:
            return False, 'The uploader Privacy Policy was not accepted.'
        ok, message = self._activate_bundle(
            _uploader_spec(),
            {
                'terms_version': accepted_terms_version,
                'privacy_version': accepted_privacy_version,
            },
        )
        if not ok:
            self._settings['upload_enabled'] = False
            self.save_settings()
            return False, message

        self._migrate_legacy_state()
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self._settings.update({
            'upload_enabled': True,
            'uploader_terms_accepted_version': accepted_terms_version,
            'uploader_privacy_accepted_version': accepted_privacy_version,
            'uploader_accepted_at': now,
        })
        if not self.save_settings():
            self._settings['upload_enabled'] = False
            return False, 'The uploader was installed, but its settings could not be saved.'
        self.plugin_state_changed.emit(True, True)
        return True, message

    def activate_hardware_identity(
            self,
            accepted_policy_version: str) -> tuple[bool, str]:
        if accepted_policy_version != HARDWARE_POLICY_VERSION:
            return False, 'The Lustful hardware-identity notice was not accepted.'
        ok, message = self._activate_bundle(
            _hardware_spec(),
            {'policy_version': accepted_policy_version},
        )
        if not ok:
            return False, message
        self._settings['lustful_hardware_policy_accepted_version'] = accepted_policy_version
        self._settings['lustful_hardware_policy_accepted_at'] = (
            datetime.now(timezone.utc).isoformat(timespec='seconds'))
        if not self.save_settings():
            return False, 'Hardware Identity was installed, but consent could not be saved.'
        return True, message

    def set_plugin_enabled(self, enabled: bool) -> tuple[bool, str]:
        if enabled and not self.is_plugin_installed():
            return False, 'Accept the uploader terms and privacy policy before enabling it.'
        previous = bool(self._settings.get('upload_enabled', False))
        requested = bool(enabled)
        self._settings['upload_enabled'] = requested
        if not self.save_settings():
            self._settings['upload_enabled'] = previous
            return False, 'The uploader setting could not be saved. Please try again.'
        self.plugin_state_changed.emit(self.is_plugin_installed(), requested)
        return True, 'Upload Extension enabled.' if enabled else 'Upload Extension disabled.'

    def record_provider_consent(self, provider: str, version: str) -> bool:
        if provider not in {'catbox', 'lustful'}:
            return False
        expected = CATBOX_LEGAL_VERSION if provider == 'catbox' else LUSTFUL_LEGAL_VERSION
        if version != expected:
            return False
        self._settings[f'{provider}_legal_accepted_version'] = version
        self._settings[f'{provider}_legal_accepted_at'] = (
            datetime.now(timezone.utc).isoformat(timespec='seconds'))
        return self.save_settings()

    def provider_consent_current(self, provider: str) -> bool:
        expected = CATBOX_LEGAL_VERSION if provider == 'catbox' else LUSTFUL_LEGAL_VERSION
        return self.get(f'{provider}_legal_accepted_version', '') == expected

    def _migrate_legacy_state(self) -> None:
        legacy = getattr(self._core_settings, 'settings', {})
        changed = False
        if isinstance(legacy, dict):
            for key in _LEGACY_UPLOAD_KEYS:
                if key not in legacy:
                    continue
                value = legacy.pop(key)
                if key in _DEFAULT_SETTINGS:
                    self._settings[key] = value
                changed = True
        if changed:
            # The pre-extension uploader used these fields for a generic
            # endpoint. Keep an existing custom setup selected after migration
            # instead of silently falling back to Catbox.
            self._settings['upload_provider'] = (
                'custom' if self._settings.get('upload_server_url') else 'catbox')
            self._core_settings.save_settings()
        if _LEGACY_HISTORY_FILE.is_file() and not _HISTORY_FILE.exists():
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_LEGACY_HISTORY_FILE, _HISTORY_FILE)

    # Process invocation

    def _activation(self, spec: _BundleSpec) -> dict[str, Any]:
        if not self._is_installed(spec):
            raise PermissionError(f'{spec.label} is not installed.')
        return _read_object(spec.receipt_path)

    def _approved_roots(self) -> list[str]:
        roots = [self.clips_directory()]
        for value in self._core_settings.get('imported_clip_folders', []) or []:
            try:
                roots.append(Path(str(value)).expanduser())
            except (OSError, TypeError):
                # An invalid optional import folder is not an approved root.
                continue
        result = []
        for root in roots:
            try:
                result.append(str(root.resolve(strict=False)))
            except OSError:
                # Unresolvable folders cannot safely be authorized for reads.
                continue
        return result

    def _invoke_plugin(
            self,
            action: str,
            payload: dict[str, Any] | None = None,
            timeout: int = 600) -> dict[str, Any]:
        receipt = self._activation(_uploader_spec())
        request: dict[str, Any] = {
            'action': action,
            'activation_id': receipt['activation_id'],
            **(payload or {}),
        }
        if self.is_hardware_identity_installed():
            hardware = self._activation(_hardware_spec())
            request['hardware_identity'] = {
                'receipt': str(_HARDWARE_ACTIVATION_FILE),
                'activation_id': hardware['activation_id'],
            }
        creation_flags = (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0)
        completed = subprocess.run(
            [str(receipt['executable']), '--activation-receipt',
             str(_UPLOADER_ACTIVATION_FILE)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=creation_flags,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            detail = completed.stderr.strip() or (
                f'Uploader exited with code {completed.returncode}')
            return {'ok': False, 'message': detail}
        try:
            response = json.loads(lines[-1])
        except ValueError:
            return {'ok': False, 'message': 'Uploader returned an invalid response.'}
        if not isinstance(response, dict):
            return {'ok': False, 'message': 'Uploader returned an invalid response.'}
        return response

    # Lifecycle and queue

    def start(self) -> None:
        self._stop_event.clear()
        self._interval_scan_cancel.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name='fthr-uploader-plugin-worker',
        )
        self._worker_thread.start()
        self._apply_interval_timer()

    def stop(self) -> None:
        self._stop_event.set()
        self._interval_timer.stop()
        self._interval_scan_cancel.set()
        self._queue.put(None)
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
        scan_thread = self._interval_scan_thread
        if scan_thread is not None and scan_thread.is_alive():
            scan_thread.join(timeout=1.0)

    def refresh_settings(self) -> None:
        self._apply_interval_timer()

    def _apply_interval_timer(self) -> None:
        self._interval_timer.stop()
        if not self.is_enabled() or self.get('upload_mode', 'manual') != 'interval':
            return
        value = int(self.get('upload_interval_value', 5) or 5)
        unit = self.get('upload_interval_unit', 'minutes')
        milliseconds = value * {
            'minutes': 60_000,
            'hours': 3_600_000,
            'days': 86_400_000,
        }.get(unit, 60_000)
        self._interval_timer.start(int(milliseconds))

    def is_enabled(self) -> bool:
        return bool(self.get('upload_enabled', False) and self.is_plugin_installed())

    def notify_clip_saved(
            self,
            path: str,
            has_mic_mux: bool = False) -> threading.Event:
        if not is_completed_video_path(path):
            event = threading.Event()
            event.set()
            print(f'[Uploader] Refused incomplete clip path: {os.path.basename(path)}')
            return event
        handle = self._readiness.engine_committed(path, needs_finalization=has_mic_mux)
        event = handle.event
        if self.is_enabled() and self.get('upload_mode', 'manual') == 'immediate':
            # File size may still change during finalization. Preflight in the
            # worker only after the readiness event opens.
            self._enqueue(path, event, task_kind='preflight_upload')
        return event

    def enqueue_upload(self, path: str) -> None:
        if not is_completed_video_path(path):
            print(f'[Uploader] Refused incomplete clip path: {os.path.basename(path)}')
            return
        if not self.is_enabled():
            return
        oversized, provider, limit_mb = self._compression_preflight(path)
        if oversized:
            if self.get('upload_auto_compress', False):
                self.enqueue_compressed_upload(path)
                return
            self.compression_required.emit(path, provider, int(limit_mb))
            return
        self._enqueue(path, self._readiness.event_for(path))

    def enqueue_compressed_upload(self, path: str) -> None:
        """Compress an oversized original below its provider limit, then upload.

        The queue continues to key in-flight work by the original path so a
        repeated click cannot launch duplicate compression jobs. The produced
        copy lives under ``Shared`` and the original is never modified.
        """
        if not is_completed_video_path(path) or not self.is_enabled():
            return
        with self._in_flight_lock:
            if path in self._in_flight:
                return
            self._in_flight.add(path)
        self._queue.put(('compress_upload', path, self._readiness.event_for(path)))

    def _enqueue(self, path: str, event: threading.Event,
                 *, task_kind: str = 'upload') -> None:
        with self._in_flight_lock:
            if self.is_uploaded(path) or path in self._in_flight:
                return
            self._in_flight.add(path)
        self._queue.put((task_kind, path, event))

    def _compression_preflight(self, path: str) -> tuple[bool, str, int | None]:
        provider = str(self.get('upload_provider', 'catbox') or 'catbox').lower()
        limit_mb = provider_limit_mb(provider)
        try:
            oversized = bool(limit_mb and os.path.getsize(path) > limit_mb * MIB)
        except OSError:
            oversized = False
        return oversized, provider, limit_mb

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                # A timeout is the worker's regular opportunity to see shutdown.
                continue
            if task is None:
                break
            task_kind, path, event = task
            while not self._stop_event.is_set():
                event.wait(timeout=0.25)
                state = self._readiness.state(path)
                if state in {
                        ClipReadinessState.READY,
                        ClipReadinessState.READY_WITH_WARNING,
                        ClipReadinessState.FINALIZATION_FAILED}:
                    break
            if self._stop_event.is_set():
                with self._in_flight_lock:
                    self._in_flight.discard(path)
                continue
            publish_finished = True
            if not self._readiness.can_access(path):
                success, message = False, 'Clip finalization failed; upload was not started'
            elif task_kind == 'preflight_upload':
                oversized, provider, limit_mb = self._compression_preflight(path)
                if oversized:
                    if self.get('upload_auto_compress', False):
                        self.upload_started.emit(path)
                        success, message = self._compress_then_upload(path)
                    else:
                        self.compression_required.emit(
                            path, provider, int(limit_mb or 0))
                        success, message = False, 'Compression confirmation required'
                        publish_finished = False
                else:
                    self.upload_started.emit(path)
                    success, message = self._do_single_upload(path)
            elif task_kind == 'compress_upload':
                self.upload_started.emit(path)
                success, message = self._compress_then_upload(path)
            else:
                self.upload_started.emit(path)
                success, message = self._do_single_upload(path)
            with self._in_flight_lock:
                self._in_flight.discard(path)
            if publish_finished:
                self.upload_finished.emit(path, success, message)

    def _compress_then_upload(self, original_path: str) -> tuple[bool, str]:
        provider = str(self.get('upload_provider', 'catbox') or 'catbox').lower()
        try:
            preset = provider_compression_preset(provider)
        except ValueError as exc:
            return False, str(exc)

        source = Path(original_path)
        shared = self.clips_directory() / 'Shared'
        try:
            shared.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f'Could not create compression folder: {exc}'
        stamp = datetime.now().strftime('%H-%M-%S')
        output = shared / f'{source.stem}_{provider}_upload_{stamp}.mp4'
        number = 2
        while output.exists():
            output = shared / f'{source.stem}_{provider}_upload_{stamp}_{number}.mp4'
            number += 1

        self.compression_started.emit(original_path, provider)

        def _progress(percent: int, detail: str) -> None:
            self.compression_progress.emit(original_path, percent, detail)

        try:
            compressed, plan = compress_media(
                original_path, output, preset, progress=_progress,
                cancel=self._stop_event.is_set)
        except Exception as exc:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                # Failed compression may never publish an output to remove.
                pass
            message = f'Automatic compression failed: {exc}'
            self.upload_error.emit('COMPRESSION FAILED', message, 'warning', original_path)
            return False, message

        try:
            actual_size = compressed.stat().st_size
        except OSError as exc:
            return False, f'Compressed file could not be verified: {exc}'
        hard_limit = provider_limit_mb(provider)
        if hard_limit and actual_size > hard_limit * MIB:
            try:
                compressed.unlink(missing_ok=True)
            except OSError:
                # Refusal is still safe if an external process retains the copy.
                pass
            return False, 'Compressed file still exceeds the provider limit; upload was not started'

        success, message = self._do_single_upload(str(compressed))
        if success:
            self._alias_compressed_upload_history(
                original_path, str(compressed), actual_size)
            return True, (
                f'Uploaded compressed copy ({actual_size / MIB:.1f} MB); '
                f'original preserved. {plan.summary}')
        return False, message

    def _alias_compressed_upload_history(
            self, original_path: str, compressed_path: str,
            compressed_size: int) -> None:
        """Keep the original card's uploaded state across app restarts."""
        try:
            history = _read_object(_HISTORY_FILE)
            uploaded = history.get(compressed_path)
            if not isinstance(uploaded, dict) or uploaded.get('status') != 'ok':
                return
            history[original_path] = {
                **uploaded,
                'uploaded_copy': 'compressed',
                'uploaded_path': compressed_path,
                'uploaded_size_bytes': int(compressed_size),
                'original_preserved': True,
            }
            _atomic_json(_HISTORY_FILE, history)
            self._invalidate_history()
        except (OSError, ValueError, TypeError):
            # Upload already succeeded; a badge-history failure must not retry
            # the network request or misreport the provider result.
            return

    def _do_single_upload(self, path: str) -> tuple[bool, str]:
        if not is_completed_video_path(path):
            return False, 'Incomplete clip files cannot be uploaded'
        if not os.path.isfile(path):
            return False, 'File no longer exists (deleted before upload)'
        if not self.is_enabled():
            return False, 'Upload Extension is disabled or not installed'
        try:
            response = self._invoke_plugin(
                'upload',
                {
                    'path': path,
                    'allowed_roots': self._approved_roots(),
                    'clips_root': str(self.clips_directory()),
                },
            )
        except Exception as exc:
            response = {'ok': False, 'message': str(exc)}
        self._invalidate_history()
        success = bool(response.get('ok'))
        message = str(response.get('message', '') or 'Upload failed')
        if not success:
            self.upload_error.emit('UPLOAD FAILED', message, 'warning', path)
        return success, message

    def _interval_scan(self) -> None:
        if not self.is_enabled():
            return
        with self._interval_scan_lock:
            if (self._interval_scan_thread is not None
                    and self._interval_scan_thread.is_alive()):
                return
            cancel_event = self._interval_scan_cancel
            worker = threading.Thread(
                target=self._interval_scan_worker,
                args=(cancel_event,), daemon=True,
                name='fthr-upload-library-scan')
            self._interval_scan_thread = worker
            worker.start()

    def _interval_scan_worker(self, cancel_event: threading.Event) -> None:
        """Enumerate settled clips without occupying the Qt event loop."""
        try:
            now = time.time()
            for item in iter_safe_tree(
                    self.clips_directory(), cancel_event=cancel_event):
                if (cancel_event.is_set() or self._stop_event.is_set()):
                    break
                if not item.is_file() or not is_completed_video_path(item):
                    continue
                try:
                    if now - item.stat().st_mtime < _WRITE_SETTLE_S:
                        continue
                except OSError:
                    # The file changed during the scan; retry it next interval.
                    continue
                path = str(item)
                if not self.is_uploaded(path):
                    self.enqueue_upload(path)
        except OSError:
            # A temporarily unavailable clips directory is retried next interval.
            return
        finally:
            with self._interval_scan_lock:
                if self._interval_scan_thread is threading.current_thread():
                    self._interval_scan_thread = None

    # Local state consumed by Core UI

    def _invalidate_history(self) -> None:
        self._history_mtime_ns = -1

    def _load_history(self) -> dict[str, Any]:
        try:
            stat = _HISTORY_FILE.stat()
        except OSError:
            self._history = {}
            self._history_mtime_ns = -1
            return self._history
        if stat.st_mtime_ns == self._history_mtime_ns:
            return self._history
        try:
            value = _read_object(_HISTORY_FILE)
        except (OSError, ValueError, TypeError):
            value = {}
        self._history = value
        self._history_mtime_ns = stat.st_mtime_ns
        return self._history

    def get_upload_info(self, path: str) -> dict[str, Any] | None:
        entry = dict(self._load_history().get(path, {}))
        return entry if entry.get('status') == 'ok' else None

    def is_uploaded(self, path: str) -> bool:
        info = self.get_upload_info(path)
        if not info:
            return False
        expires_at = str(info.get('expires_at', '')).strip()
        if expires_at and not info.get('favorite', False):
            try:
                expiration = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                now = datetime.now(expiration.tzinfo) if expiration.tzinfo else datetime.now()
                return expiration > now
            except ValueError:
                # Provider timestamps are advisory; an unknown format is not expiry.
                pass
        return True

    def local_account(self) -> dict[str, Any] | None:
        try:
            value = _read_object(_CREDENTIALS_FILE)
        except (OSError, ValueError, TypeError):
            # No valid local credentials simply means the account is disconnected.
            return None
        return value if value.get('account_id') and value.get('hwid') else None

    # Settings UI actions

    def account_action(
            self,
            mode: str,
            account_id: str) -> tuple[bool, dict[str, Any], str]:
        response = self._invoke_plugin(mode, {'account_id': account_id}, timeout=60)
        data = response.get('data')
        return (
            bool(response.get('ok')),
            dict(data) if isinstance(data, dict) else {},
            str(response.get('message', '')),
        )

    def logout_account(self) -> tuple[bool, str]:
        response = self._invoke_plugin('logout', timeout=30)
        return bool(response.get('ok')), str(response.get('message', ''))

    def test_connection(self, config: dict[str, Any]) -> tuple[bool, str]:
        response = self._invoke_plugin(
            'test_connection', {'config': config}, timeout=30)
        return bool(response.get('ok')), str(response.get('message', ''))
