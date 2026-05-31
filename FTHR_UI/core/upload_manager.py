"""
upload_manager.py — background clip-upload coordinator.

Modes
-----
    immediate  Upload as soon as the clip file is ready (after mic mux finishes)
    interval   QTimer scans ~/FTHR_Clips periodically and uploads any pending clips
    manual     Only uploads when the user right-clicks → Upload

Threading model
---------------
    Main thread (Qt)  starts/stops timers, emits signals, calls notify_clip_saved
    Worker thread     blocking queue.get() → upload one clip at a time (daemon)
    Mic mux thread    sets a threading.Event to signal the clip is fully written

Upload history
--------------
    ~/.fthr/upload_history.json  — entries pruned after 90 days on startup,
    written atomically via temp-file + os.replace.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


_HISTORY_FILE = Path.home() / '.fthr' / 'upload_history.json'
_CLIPS_DIR    = Path.home() / 'FTHR_Clips'
_VIDEO_EXTS   = ('.mp4', '.mkv', '.avi')
_PRUNE_DAYS   = 90
_RETRY_DELAYS = (5, 15, 45)          # seconds between upload attempts


class UploadManager(QObject):
    upload_started  = pyqtSignal(str)              # clip_path
    upload_finished = pyqtSignal(str, bool, str)   # path, success, message

    def __init__(self, settings_manager):
        super().__init__()
        self._sm = settings_manager
        self._history: dict = self._load_history()
        self._queue: queue.Queue = queue.Queue()
        self._interval_timer = QTimer(self)
        self._interval_timer.timeout.connect(self._interval_scan)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name='fthr-upload-worker')
        self._worker_thread.start()
        self._apply_interval_timer()

    def stop(self):
        self._stop_event.set()
        self._interval_timer.stop()
        self._queue.put(None)   # unblock the worker so it can see the stop flag
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)

    def refresh_settings(self):
        """Re-apply interval timer after settings change."""
        self._apply_interval_timer()

    def _apply_interval_timer(self):
        self._interval_timer.stop()
        if not self._sm.get('upload_enabled', False):
            return
        if self._sm.get('upload_mode', 'manual') != 'interval':
            return
        value = self._sm.get('upload_interval_value', 5)
        unit  = self._sm.get('upload_interval_unit', 'minutes')
        ms = value * {'minutes': 60_000, 'hours': 3_600_000, 'days': 86_400_000}.get(unit, 60_000)
        self._interval_timer.start(int(ms))

    # ── Public API ────────────────────────────────────────────────────────

    def notify_clip_saved(self, path: str, has_mic_mux: bool = False) -> threading.Event:
        """
        Called from _save_clip() immediately after the engine confirms the save.

        Returns a threading.Event the mic-mux worker must set when the final
        file is fully written (os.replace complete).  If has_mic_mux=False the
        event is pre-set so the upload worker starts without waiting.
        """
        event = threading.Event()
        if not has_mic_mux:
            event.set()

        if not self._sm.get('upload_enabled', False):
            event.set()
            return event

        mode = self._sm.get('upload_mode', 'manual')
        if mode == 'immediate':
            self._queue.put(('upload', path, event))

        return event

    def enqueue_upload(self, path: str):
        """Manual or interval-triggered upload. Assumes file is already complete."""
        if not self._sm.get('upload_enabled', False):
            return
        event = threading.Event()
        event.set()
        self._queue.put(('upload', path, event))

    def is_uploaded(self, path: str) -> bool:
        return self._history.get(path, {}).get('status') == 'ok'

    # ── Worker loop ───────────────────────────────────────────────────────

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task is None:
                break
            _, path, event = task
            # Wait up to 60 s for the mic-mux thread to finish writing the file
            event.wait(timeout=60.0)
            self.upload_started.emit(path)
            success, msg = self._do_single_upload(path)
            self.upload_finished.emit(path, success, msg)

    # ── Upload logic ──────────────────────────────────────────────────────

    def _do_single_upload(self, path: str) -> tuple[bool, str]:
        url = self._sm.get('upload_server_url', '').strip()
        if not url:
            return False, 'No server URL configured'

        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                status = self._http_post(
                    path, url,
                    self._sm.get('upload_auth_header', ''))

                if 200 <= status < 300:
                    self._record_success(path)
                    if self._sm.get('upload_auto_delete', False):
                        try:
                            os.remove(path)
                        except OSError as e:
                            print(f'[Upload] Auto-delete failed for {os.path.basename(path)}: {e}')
                    return True, f'OK ({status})'

                msg = f'HTTP {status}'
                if delay is None:
                    return False, msg
                print(f'[Upload] {msg} — retry in {delay}s ({os.path.basename(path)})')
                time.sleep(delay)

            except Exception as e:
                msg = str(e)
                if delay is None:
                    return False, msg
                print(f'[Upload] Error: {e} — retry in {delay}s ({os.path.basename(path)})')
                time.sleep(delay)

        return False, 'All retries exhausted'

    def _http_post(self, file_path: str, url: str, auth_header: str) -> int:
        """Multipart/form-data POST via stdlib. Streams the file to avoid loading
        the entire clip into memory (large clips can exceed 200 MB)."""
        boundary = b'FTHRUploadBoundary7a3f9c'
        filename  = os.path.basename(file_path).encode('utf-8', errors='replace')
        file_size = os.path.getsize(file_path)

        part_header = (
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="clip"; filename="' + filename + b'"\r\n'
            b'Content-Type: video/mp4\r\n\r\n'
        )
        part_footer = b'\r\n--' + boundary + b'--\r\n'
        content_length = len(part_header) + file_size + len(part_footer)

        import http.client
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        host   = parsed.netloc
        path   = parsed.path or '/'
        use_ssl = parsed.scheme == 'https'

        conn = (http.client.HTTPSConnection(host, timeout=120)
                if use_ssl else
                http.client.HTTPConnection(host, timeout=120))
        try:
            conn.putrequest('POST', path)
            conn.putheader('Content-Type',
                           f'multipart/form-data; boundary={boundary.decode()}')
            conn.putheader('Content-Length', str(content_length))
            if auth_header:
                conn.putheader('Authorization', auth_header)
            conn.endheaders()

            conn.send(part_header)
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(part_footer)

            resp = conn.getresponse()
            return resp.status
        finally:
            conn.close()

    # ── Interval scan ─────────────────────────────────────────────────────

    def _interval_scan(self):
        """Called on QTimer in the main thread; enqueues all un-uploaded clips."""
        if not self._sm.get('upload_enabled', False):
            return
        try:
            paths = [
                p for p in _CLIPS_DIR.rglob('*')
                if p.suffix.lower() in _VIDEO_EXTS and p.is_file()
            ]
        except OSError:
            return
        for p in paths:
            if not self.is_uploaded(str(p)):
                self.enqueue_upload(str(p))


    # ── History ───────────────────────────────────────────────────────────

    def _record_success(self, path: str):
        self._history[path] = {
            'uploaded_at': datetime.now().isoformat(timespec='seconds'),
            'status': 'ok',
        }
        self._save_history()

    def _load_history(self) -> dict:
        if not _HISTORY_FILE.exists():
            return {}
        try:
            with open(_HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cutoff = datetime.now() - timedelta(days=_PRUNE_DAYS)
            return {
                k: v for k, v in data.items()
                if _safe_isoparse(v.get('uploaded_at', '')) > cutoff
            }
        except Exception:
            return {}

    def _save_history(self):
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HISTORY_FILE.with_suffix('.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._history, f, indent=2)
            os.replace(str(tmp), str(_HISTORY_FILE))
        except Exception as e:
            print(f'[Upload] Could not save history: {e}')


def test_server_connection(url: str, auth_header: str) -> tuple[bool, str]:
    """
    Fire a HEAD request to check the server is reachable.
    Returns (ok, status_string).  Runs synchronously — call from a background thread.
    """
    if not url.strip():
        return False, 'No URL'
    try:
        req = urllib.request.Request(url.strip(), method='HEAD')
        if auth_header:
            req.add_header('Authorization', auth_header)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, f'OK ({resp.status})'
    except urllib.error.HTTPError as e:
        # Any HTTP response means the server is up; treat 4xx as "reachable"
        return True, f'Reachable — HTTP {e.code}'
    except Exception as e:
        return False, f'Failed: {e}'


def _safe_isoparse(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.min
