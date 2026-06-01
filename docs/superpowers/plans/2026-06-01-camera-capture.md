# Camera Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture webcam frames in a rolling ring buffer; burn the camera feed as a corner overlay into saved clips via ffmpeg; show live preview in Settings.

**Architecture:** `CameraRecorder` singleton (modeled after `MicRecorder`) runs a background thread with `cv2.VideoCapture`, stores (timestamp, BGR-frame) pairs in a deque. At save time, `_apply_camera_overlay(clip_path, ffmpeg)` extracts the matching segment, writes it to a temp MP4 via `cv2.VideoWriter`, then ffmpeg overlays it onto the clip. Settings page has a Camera section with device selector, position/size dropdowns, and a live preview label.

**Tech Stack:** Python 3, PyQt6, OpenCV (`cv2`), ffmpeg (imageio_ffmpeg)

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/core/camera_recorder.py` | New — `CameraRecorder` singleton |
| `FTHR_UI/core/settings_manager.py` | Add camera defaults |
| `FTHR_UI/main.py` | `_apply_camera_overlay()`; wire into finalize workers; Camera section in General Settings |
| `tests/test_camera_recorder.py` | New — unit tests for extract_segment logic |

---

## Task 1: CameraRecorder

**Files:**
- Create: `FTHR_UI/core/camera_recorder.py`
- Create: `tests/test_camera_recorder.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_camera_recorder.py`:

```python
import sys, time
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

import numpy as np
from core.camera_recorder import CameraRecorder

# Reset singleton state between tests
def _fresh():
    CameraRecorder._instance = None
    return CameraRecorder()


def test_no_frames_returns_none():
    r = _fresh()
    assert r.extract_segment(time.monotonic(), 5) is None


def test_extract_returns_frames_in_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    r._lock.acquire()
    r._chunks.append((now - 2.0, frame.copy()))
    r._chunks.append((now - 1.0, frame.copy()))
    r._chunks.append((now - 0.0, frame.copy()))
    r._lock.release()
    result = r.extract_segment(now, 3)
    assert result is not None
    assert len(result) == 3


def test_extract_respects_time_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    r._lock.acquire()
    r._chunks.append((now - 10.0, frame.copy()))  # outside 5s window
    r._chunks.append((now - 2.0,  frame.copy()))  # inside
    r._chunks.append((now - 1.0,  frame.copy()))  # inside
    r._lock.release()
    result = r.extract_segment(now, 5)
    assert result is not None
    assert len(result) == 2
```

- [ ] **Step 2: Run to verify they fail**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_camera_recorder.py -v
```

- [ ] **Step 3: Create `FTHR_UI/core/camera_recorder.py`**

```python
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

try:
    import cv2 as _cv2
    import numpy as _np
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

KEEP_SECONDS = 90
DEFAULT_FPS   = 30


class CameraRecorder:
    """Continuous webcam capture singleton. Always-on when a device is selected."""

    _instance: Optional['CameraRecorder'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self._cap = None
        self._device_index = 0
        self._lock = threading.Lock()
        self._chunks: deque = deque()  # (monotonic_ts, BGR frame)
        self._running = False
        self._thread: threading.Thread | None = None
        self._latest_frame = None

    @classmethod
    def is_available(cls) -> bool:
        return _AVAILABLE

    def is_running(self) -> bool:
        return self._running

    def start(self, device_index: int = 0) -> bool:
        if not _AVAILABLE:
            return False
        self.stop()
        self._device_index = device_index
        self._cap = _cv2.VideoCapture(device_index)
        if not self._cap.isOpened():
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name='fthr-camera')
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        with self._lock:
            self._chunks.clear()

    @property
    def latest_frame(self):
        return self._latest_frame

    def _capture_loop(self):
        while self._running:
            if not self._cap or not self._cap.isOpened():
                break
            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.033)
                continue
            ts = time.monotonic()
            self._latest_frame = frame
            cutoff = ts - KEEP_SECONDS
            with self._lock:
                self._chunks.append((ts, frame))
                while self._chunks and self._chunks[0][0] < cutoff:
                    self._chunks.popleft()

    def extract_segment(
        self, end_time: float, duration_sec: float
    ) -> list | None:
        start_time = end_time - duration_sec
        with self._lock:
            frames = [f for ts, f in self._chunks
                      if start_time <= ts <= end_time]
        return frames if frames else None

    def write_segment(
        self, path: str, end_time: float, duration_sec: float, fps: float = DEFAULT_FPS
    ) -> bool:
        if not _AVAILABLE:
            return False
        frames = self.extract_segment(end_time, duration_sec)
        if not frames:
            return False
        h, w = frames[0].shape[:2]
        fourcc = _cv2.VideoWriter_fourcc(*'mp4v')
        writer = _cv2.VideoWriter(path, fourcc, fps, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()
        return True
```

- [ ] **Step 4: Run tests to verify 3 pass**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_camera_recorder.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/camera_recorder.py tests/test_camera_recorder.py
git commit -m "feat(core): CameraRecorder — continuous webcam ring buffer + extract_segment"
```

---

## Task 2: Settings defaults + `_apply_camera_overlay` + finalize wiring

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py`
- Modify: `FTHR_UI/main.py`

- [ ] **Step 1: Add camera settings defaults**

In `settings_manager.py` default_settings, after `'anticheat_detection_enabled': False,`:

```python
            'camera_enabled':       False,
            'camera_device_index':  0,
            'camera_position':      'bottom-right',  # top-left/top-right/bottom-left/bottom-right
            'camera_size':          'medium',          # small/medium/large
```

- [ ] **Step 2: Add `_apply_camera_overlay` to FTHRApp**

Add after `_apply_crop`:

```python
    def _apply_camera_overlay(self, clip_path: str, ffmpeg: str,
                               clip_end_time: float, duration_sec: int) -> None:
        if not self.settings_manager.get('camera_enabled', False):
            return
        from core.camera_recorder import CameraRecorder
        if not CameraRecorder.is_available() or not CameraRecorder().is_running():
            return
        import re as _re
        import tempfile as _tf

        cam_tmp = _tf.NamedTemporaryFile(
            suffix='.mp4', dir=os.path.dirname(clip_path), delete=False)
        cam_path = cam_tmp.name
        cam_tmp.close()

        device_fps = 30.0
        if not CameraRecorder().write_segment(cam_path, clip_end_time, duration_sec, device_fps):
            try:
                os.remove(cam_path)
            except FileNotFoundError:
                pass
            return

        # Get clip dimensions
        info = subprocess.run([ffmpeg, '-i', clip_path],
                              capture_output=True, **_NO_WINDOW)
        dim = _re.search(r'(\d{3,5})x(\d{3,5})', info.stderr.decode(errors='replace'))
        clip_w = int(dim.group(1)) if dim else 1920
        clip_h = int(dim.group(2)) if dim else 1080

        size_map  = {'small': 0.20, 'medium': 0.25, 'large': 0.33}
        factor    = size_map.get(self.settings_manager.get('camera_size', 'medium'), 0.25)
        cam_w     = int(clip_w * factor)

        pos = self.settings_manager.get('camera_position', 'bottom-right')
        pos_map = {
            'top-left':     '10:10',
            'top-right':    'W-w-10:10',
            'bottom-left':  '10:H-h-10',
            'bottom-right': 'W-w-10:H-h-10',
        }
        overlay_pos = pos_map.get(pos, 'W-w-10:H-h-10')

        out_tmp = _tf.NamedTemporaryFile(
            suffix='.mp4', dir=os.path.dirname(clip_path), delete=False)
        out_path = out_tmp.name
        out_tmp.close()

        try:
            result = subprocess.run(
                [ffmpeg, '-y',
                 '-i', clip_path,
                 '-i', cam_path,
                 '-filter_complex',
                 f'[1:v]scale={cam_w}:-1[cam];[0:v][cam]overlay={overlay_pos}',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
                 '-c:a', 'copy',
                 out_path],
                capture_output=True, **_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(out_path, clip_path)
                print(f'[Camera] Overlay applied to {os.path.basename(clip_path)}')
            else:
                err = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[Camera] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
        except Exception as e:
            print(f'[Camera] Error: {e}')
        finally:
            for p in (cam_path, out_path):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
```

- [ ] **Step 3: Wire into `_mic_mux_worker` — BEFORE `_apply_crop`**

The `_mic_mux_worker` doesn't have access to `clip_end_time` or `duration_sec`. We need to pass these. This worker is called from `_mux_mic_into_clip` which is called from `_save_clip`.

Instead of threading these through the call chain, use a different approach: camera overlay is handled in `_finalize_clip_worker` (which already handles the no-mux case) and inside the mux workers.

For `_mic_mux_worker`: add `clip_end_time` and `duration_sec` parameters.

Find the `_mux_mic_into_clip` call in `_save_clip`:
```python
                self._mux_mic_into_clip(
                    str(output_path), duration_seconds, mic_end_time, clip_ready)
```

This already passes `mic_end_time` (which is `time.monotonic() - 0.5`). We'll reuse this as `clip_end_time`.

In `_mic_mux_worker`, the signature is `(self, clip_path, duration_seconds, mic_end_time, clip_ready)`.
After `self._apply_watermark(clip_path, ffmpeg)`, add:
```python
                    self._apply_camera_overlay(
                        clip_path, ffmpeg, mic_end_time, duration_seconds)
```

- [ ] **Step 4: Wire into `_multiband_mux_worker`**

The multiband worker receives `audio_end_time` (which is `mic_end_time`). After `self._apply_watermark`, add:
```python
        if ok:
            self._apply_crop(clip_path, ffmpeg)
            self._apply_watermark(clip_path, ffmpeg)
            self._apply_camera_overlay(clip_path, ffmpeg, audio_end_time, duration_seconds)
```

- [ ] **Step 5: Wire into `_finalize_clip_worker`**

`_finalize_clip_worker` doesn't have `clip_end_time` either. Add it.

Change `_finalize_clip` signature:
```python
    def _finalize_clip(self, clip_path: str, duration_seconds: int,
                       clip_end_time: float = 0.0):
        if not (self.settings_manager.get('watermark_enabled', False)
                or self.settings_manager.get('auto_crop_enabled', False)
                or self.settings_manager.get('camera_enabled', False)):
            return
        threading.Thread(
            target=self._finalize_clip_worker,
            args=(clip_path, duration_seconds, clip_end_time),
            daemon=True,
        ).start()
```

Change `_finalize_clip_worker` to accept `clip_end_time`:
```python
    def _finalize_clip_worker(self, clip_path: str, duration_seconds: int,
                               clip_end_time: float = 0.0):
        ...
        self._apply_crop(clip_path, ffmpeg)
        self._apply_watermark(clip_path, ffmpeg)
        self._apply_camera_overlay(clip_path, ffmpeg, clip_end_time, duration_seconds)
```

Update the call in `_save_clip`:
```python
                if not mic_active and not multiband_on:
                    self._finalize_clip(str(output_path), duration_seconds,
                                        mic_end_time)
```

- [ ] **Step 6: Start CameraRecorder on app startup if enabled**

In `FTHRApp.__init__`, after `_focus_monitor` block:
```python
        from core.camera_recorder import CameraRecorder
        if (CameraRecorder.is_available()
                and self.settings_manager.get('camera_enabled', False)):
            device_idx = self.settings_manager.get('camera_device_index', 0)
            CameraRecorder().start(device_idx)
```

- [ ] **Step 7: Verify + run tests**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -8
```
Expected: exit 143, all 30 tests PASS (27 + 3 new).

- [ ] **Step 8: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/settings_manager.py FTHR_UI/main.py
git commit -m "feat: camera overlay — burn webcam into clips at save time"
```

---

## Task 3: Camera Settings UI

**Files:**
- Modify: `FTHR_UI/main.py` — `SettingsPage`

The camera settings live in the General Settings page under a "Kamera" section with: toggle, device selector, position dropdown, size dropdown, and a live preview label updated by a QTimer.

- [ ] **Step 1: Add Camera section to `_make_general_page()`**

Add BEFORE the "Anticheat Detection" section:

```python
        # ── Kamera ────────────────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Kamera'))
        layout.addSpacing(12)

        self.camera_check = QCheckBox('Kamera-Overlay in Clips einbrennen')
        self.camera_check.setStyleSheet(CHECKBOX_QSS)
        self.camera_check.setChecked(self.sm.get('camera_enabled', False))
        self.camera_check.toggled.connect(self._on_camera_toggled)
        layout.addWidget(self.camera_check)
        layout.addSpacing(8)

        # Device selector row
        cam_dev_row = QHBoxLayout()
        cam_dev_row.setSpacing(8)
        _dev_lbl = QLabel('GERÄT')
        _dev_lbl.setStyleSheet(_LABEL_STYLE)
        _dev_lbl.setFixedWidth(80)
        cam_dev_row.addWidget(_dev_lbl)
        self.camera_device_combo = _DropdownCombo()
        self.camera_device_combo.setStyleSheet(_COMBO_STYLE)
        self._populate_camera_devices()
        self.camera_device_combo.currentIndexChanged.connect(
            self._on_camera_device_changed)
        cam_dev_row.addWidget(self.camera_device_combo, 1)
        layout.addLayout(cam_dev_row)
        layout.addSpacing(6)

        # Position + size row
        cam_pos_row = QHBoxLayout()
        cam_pos_row.setSpacing(8)
        _pos_lbl = QLabel('POSITION')
        _pos_lbl.setStyleSheet(_LABEL_STYLE)
        _pos_lbl.setFixedWidth(80)
        cam_pos_row.addWidget(_pos_lbl)
        self.camera_pos_combo = _DropdownCombo()
        self.camera_pos_combo.addItems(
            ['Unten rechts', 'Unten links', 'Oben rechts', 'Oben links'])
        self.camera_pos_combo.setStyleSheet(_COMBO_STYLE)
        _pos_keys = ['bottom-right', 'bottom-left', 'top-right', 'top-left']
        saved_pos = self.sm.get('camera_position', 'bottom-right')
        self.camera_pos_combo.setCurrentIndex(
            _pos_keys.index(saved_pos) if saved_pos in _pos_keys else 0)
        self.camera_pos_combo.currentIndexChanged.connect(
            self._on_camera_pos_changed)
        cam_pos_row.addWidget(self.camera_pos_combo)

        _sz_lbl = QLabel('GRÖSSE')
        _sz_lbl.setStyleSheet(_LABEL_STYLE)
        _sz_lbl.setFixedWidth(64)
        cam_pos_row.addWidget(_sz_lbl)
        self.camera_size_combo = _DropdownCombo()
        self.camera_size_combo.addItems(['Klein', 'Mittel', 'Groß'])
        self.camera_size_combo.setStyleSheet(_COMBO_STYLE)
        _sz_keys = ['small', 'medium', 'large']
        saved_sz = self.sm.get('camera_size', 'medium')
        self.camera_size_combo.setCurrentIndex(
            _sz_keys.index(saved_sz) if saved_sz in _sz_keys else 1)
        self.camera_size_combo.currentIndexChanged.connect(
            self._on_camera_size_changed)
        cam_pos_row.addWidget(self.camera_size_combo)
        layout.addLayout(cam_pos_row)
        layout.addSpacing(8)

        # Preview label
        self.camera_preview_lbl = QLabel('Kamera deaktiviert')
        self.camera_preview_lbl.setFixedSize(160, 90)
        self.camera_preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera_preview_lbl.setStyleSheet(
            f'background: {Colors.BG_CARD}; color: {Colors.TEXT_DIM}; '
            f'border: 1px solid {Colors.BORDER};')
        layout.addWidget(self.camera_preview_lbl)
        layout.addSpacing(4)

        # Preview timer
        self._camera_preview_timer = QTimer(self)
        self._camera_preview_timer.setInterval(100)
        self._camera_preview_timer.timeout.connect(self._update_camera_preview)
        if self.sm.get('camera_enabled', False):
            self._camera_preview_timer.start()
```

- [ ] **Step 2: Add 6 handlers to SettingsPage**

Add after `_on_anticheat_toggled`:

```python
    def _populate_camera_devices(self):
        from core.camera_recorder import CameraRecorder
        self.camera_device_combo.clear()
        if not CameraRecorder.is_available():
            self.camera_device_combo.addItem('cv2 nicht verfügbar')
            return
        import cv2 as _cv2
        found = []
        for i in range(5):
            cap = _cv2.VideoCapture(i)
            if cap.isOpened():
                found.append(f'Kamera {i}')
                cap.release()
        if not found:
            self.camera_device_combo.addItem('Keine Kamera gefunden')
        else:
            self.camera_device_combo.addItems(found)
            saved = self.sm.get('camera_device_index', 0)
            if saved < len(found):
                self.camera_device_combo.setCurrentIndex(saved)

    def _on_camera_toggled(self, checked: bool):
        self.sm.set('camera_enabled', checked)
        self.sm.save_settings()
        from core.camera_recorder import CameraRecorder
        if checked and CameraRecorder.is_available():
            idx = self.sm.get('camera_device_index', 0)
            CameraRecorder().start(idx)
            self._camera_preview_timer.start()
        else:
            CameraRecorder().stop()
            self._camera_preview_timer.stop()
            self.camera_preview_lbl.setText('Kamera deaktiviert')
            self.camera_preview_lbl.setPixmap(QPixmap())

    def _on_camera_device_changed(self, idx: int):
        self.sm.set('camera_device_index', idx)
        self.sm.save_settings()
        if self.sm.get('camera_enabled', False):
            from core.camera_recorder import CameraRecorder
            CameraRecorder().start(idx)

    def _on_camera_pos_changed(self, idx: int):
        keys = ['bottom-right', 'bottom-left', 'top-right', 'top-left']
        self.sm.set('camera_position', keys[idx] if idx < len(keys) else 'bottom-right')
        self.sm.save_settings()

    def _on_camera_size_changed(self, idx: int):
        keys = ['small', 'medium', 'large']
        self.sm.set('camera_size', keys[idx] if idx < len(keys) else 'medium')
        self.sm.save_settings()

    def _update_camera_preview(self):
        from core.camera_recorder import CameraRecorder
        frame = CameraRecorder().latest_frame
        if frame is None:
            return
        import cv2 as _cv2
        from PyQt6.QtGui import QImage, QPixmap as _QPixmap
        rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qi = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = _QPixmap.fromImage(qi).scaled(
            160, 90,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.camera_preview_lbl.setPixmap(pix)
        self.camera_preview_lbl.setText('')
```

- [ ] **Step 3: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 4: Run regression tests**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -8
```
Expected: all 30 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): Camera Settings — device selector, position/size, live preview"
```
