# Anticheat Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the target game window loses focus, pause the ring-buffer capture loop so no non-game frames are recorded. Resume when the game regains focus. Prevents recording content from other windows and helps avoid anti-cheat detection.

**Architecture:** C++ engine gains a real `SetPaused(bool)` method (STOP/START_RECORDING no longer no-ops). Python `FocusMonitor` polls `hyprctl activewindow -j` every 2s, compares active window title to capture target, sends PAUSE/RESUME via bridge. Only active in window-capture mode when `anticheat_detection_enabled` is True.

**Tech Stack:** C++17, Python 3, PyQt6, Hyprland IPC (hyprctl subprocess)

---

## File Map

| File | Change |
|------|--------|
| `FTHRcapture_linux/src/capture_engine.h` | Add `paused_` atomic + `SetPaused()` / `IsPaused()` |
| `FTHRcapture_linux/src/capture_engine.cpp` | Check `paused_` at top of CaptureLoop main loop |
| `FTHRcapture_linux/src/main.cpp` | STOP_RECORDING → SetPaused(true), START_RECORDING → SetPaused(false) |
| `FTHR_UI/core/capture_bridge.py` | Add `pause_recording()` + `resume_recording()` |
| `FTHR_UI/core/focus_monitor.py` | New — `FocusMonitor` QObject polling `hyprctl activewindow` |
| `FTHR_UI/core/settings_manager.py` | Add `anticheat_detection_enabled: False` default |
| `FTHR_UI/main.py` | Wire `FocusMonitor`, handlers, General Settings toggle |
| `tests/test_focus_monitor.py` | New — unit tests for focus match logic |

---

## Task 1: C++ pause + rebuild

**Files:**
- Modify: `FTHRcapture_linux/src/capture_engine.h`
- Modify: `FTHRcapture_linux/src/capture_engine.cpp`
- Modify: `FTHRcapture_linux/src/main.cpp`

- [ ] **Step 1: Add `paused_` to `capture_engine.h`**

In `CaptureEngine` class (private section), after `std::atomic<bool> running_{false};`:
```cpp
    std::atomic<bool>       paused_{false};
```

In the public section, after `IsNvencActive()`:
```cpp
    void SetPaused(bool p) { paused_.store(p); }
    bool IsPaused()  const { return paused_.load(); }
```

- [ ] **Step 2: Add pause check to `CaptureLoop` in `capture_engine.cpp`**

Find the start of the main loop body (line ~462, just after `while (running_.load()) {`):
```cpp
    while (running_.load()) {
        // Request a new frame
```

Replace with:
```cpp
    while (running_.load()) {
        if (paused_.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }
        // Request a new frame
```

- [ ] **Step 3: Update `main.cpp` command handlers**

Find `case fthr::CommandType::STOP_RECORDING:` (around line 235) and replace the two command handlers:

```cpp
            case fthr::CommandType::STOP_RECORDING:
                engine.SetPaused(true);
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STOPPED);
                break;

            case fthr::CommandType::START_RECORDING:
                engine.SetPaused(false);
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::RECORDING_STARTED);
                break;
```

- [ ] **Step 4: Rebuild**

```bash
cd ~/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -5
```
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/capture_engine.cpp FTHRcapture_linux/src/main.cpp
git commit -m "feat(engine): STOP/START_RECORDING pause/resume CaptureLoop via paused_ atomic"
```

---

## Task 2: FocusMonitor + bridge + settings + UI

**Files:**
- Create: `FTHR_UI/core/focus_monitor.py`
- Modify: `FTHR_UI/core/capture_bridge.py`
- Modify: `FTHR_UI/core/settings_manager.py`
- Modify: `FTHR_UI/main.py`
- Create: `tests/test_focus_monitor.py`

### Part A — `focus_monitor.py`

The monitor polls `hyprctl activewindow -j` every 2s and emits `focus_lost` / `focus_regained` based on whether the active window title matches the configured target name.

- [ ] **Step 1: Write failing tests**

Create `tests/test_focus_monitor.py`:

```python
import sys
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.focus_monitor import FocusMonitor


def test_title_matches_exact(qtbot):
    m = FocusMonitor(target_name='Counter-Strike 2')
    assert m._title_matches('Counter-Strike 2') is True


def test_title_matches_partial(qtbot):
    m = FocusMonitor(target_name='CS2')
    assert m._title_matches('CS2 - Valve') is True


def test_title_no_match(qtbot):
    m = FocusMonitor(target_name='Counter-Strike 2')
    assert m._title_matches('Discord') is False


def test_title_case_insensitive(qtbot):
    m = FocusMonitor(target_name='counter-strike 2')
    assert m._title_matches('Counter-Strike 2') is True
```

- [ ] **Step 2: Run to verify they fail**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_focus_monitor.py -v
```

- [ ] **Step 3: Create `FTHR_UI/core/focus_monitor.py`**

```python
import json
import subprocess
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class FocusMonitor(QObject):
    focus_lost     = pyqtSignal()
    focus_regained = pyqtSignal()

    def __init__(self, target_name: str = '', parent=None):
        super().__init__(parent)
        self._target = target_name
        self._focused = True
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)

    def set_target(self, name: str):
        self._target = name
        self._focused = True

    def start(self):
        self._focused = True
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _title_matches(self, title: str) -> bool:
        if not self._target:
            return True
        return self._target.lower() in title.lower()

    def _poll(self):
        if not self._target:
            return
        try:
            result = subprocess.run(
                ['hyprctl', 'activewindow', '-j'],
                capture_output=True, timeout=1,
            )
            data = json.loads(result.stdout.decode(errors='replace'))
            title = data.get('title', '') or data.get('class', '')
            focused = self._title_matches(title)
        except Exception:
            return
        if focused and not self._focused:
            self._focused = True
            self.focus_regained.emit()
        elif not focused and self._focused:
            self._focused = False
            self.focus_lost.emit()
```

- [ ] **Step 4: Run tests to verify 4 pass**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_focus_monitor.py -v
```

### Part B — Bridge methods

- [ ] **Step 5: Add `pause_recording` + `resume_recording` to `capture_bridge.py`**

Add after `save_clip`:

```python
    def pause_recording(self) -> bool:
        if not self.is_connected():
            return False
        self._layout.ui_command = CommandType.STOP_RECORDING
        return True

    def resume_recording(self) -> bool:
        if not self.is_connected():
            return False
        self._layout.ui_command = CommandType.START_RECORDING
        return True
```

### Part C — Settings default + FTHRApp wiring + UI

- [ ] **Step 6: Add setting to `settings_manager.py`**

After `'auto_crop_enabled': False,`:
```python
            'anticheat_detection_enabled': False,
```

- [ ] **Step 7: Wire in `FTHRApp.__init__`**

Add import near other core imports:
```python
from core.focus_monitor import FocusMonitor
```

After `self._game_detector = GameDetector()` block, add:
```python
        self._focus_monitor = FocusMonitor()
        self._focus_monitor.focus_lost.connect(self._on_focus_lost)
        self._focus_monitor.focus_regained.connect(self._on_focus_regained)
```

- [ ] **Step 8: Add handlers to FTHRApp**

After `_on_game_prompt_timeout`:

```python
    def _on_focus_lost(self):
        if self.bridge.is_connected():
            self.bridge.pause_recording()
            self._set_status('PAUSED — GAME UNFOCUSED', STATUS_WARNING)

    def _on_focus_regained(self):
        if self.bridge.is_connected():
            self.bridge.resume_recording()
            self._set_status('CAPTURING', STATUS_ACTIVE)
```

- [ ] **Step 9: Start/stop focus monitor based on settings + capture mode**

In `FTHRApp.__init__`, after connecting focus signals, add:

```python
        if (self.settings_manager.get('anticheat_detection_enabled', False)
                and self.settings_manager.get('capture_mode', 'desktop') == 'window'):
            target = self.settings_manager.get('target_window_name', '')
            self._focus_monitor.set_target(target)
            self._focus_monitor.start()
```

- [ ] **Step 10: Add toggle to General Settings in SettingsPage**

In `_make_general_page()`, add before Settings-Presets section:

```python
        # ── Anticheat Detection ───────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Anticheat Detection'))
        layout.addSpacing(12)

        self.anticheat_check = QCheckBox('Aufnahme pausieren wenn Game unfokussiert')
        self.anticheat_check.setStyleSheet(CHECKBOX_QSS)
        self.anticheat_check.setChecked(
            self.sm.get('anticheat_detection_enabled', False))
        self.anticheat_check.toggled.connect(self._on_anticheat_toggled)
        layout.addWidget(self.anticheat_check)
        layout.addSpacing(4)

        _at_hint = QLabel(
            'Nur aktiv bei Fenster-Aufnahme. Pausiert den Buffer wenn das Game '
            'nicht im Vordergrund ist. Standard: deaktiviert.'
        )
        _at_hint.setWordWrap(True)
        _at_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_at_hint)
```

- [ ] **Step 11: Add `_on_anticheat_toggled` to SettingsPage**

After `_on_auto_crop_toggled`:

```python
    def _on_anticheat_toggled(self, checked: bool):
        self.sm.set('anticheat_detection_enabled', checked)
        self.sm.save_settings()
        main_win = self.window()
        if not hasattr(main_win, '_focus_monitor'):
            return
        if checked and self.sm.get('capture_mode', 'desktop') == 'window':
            target = self.sm.get('target_window_name', '')
            main_win._focus_monitor.set_target(target)
            main_win._focus_monitor.start()
        else:
            main_win._focus_monitor.stop()
            if hasattr(main_win, 'bridge') and main_win.bridge.is_connected():
                main_win.bridge.resume_recording()
```

- [ ] **Step 12: Verify + run tests**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -5
```
Expected: exit 143, all 27 tests PASS (23 + 4 new).

- [ ] **Step 13: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/focus_monitor.py FTHR_UI/core/capture_bridge.py \
        FTHR_UI/core/settings_manager.py FTHR_UI/main.py \
        tests/test_focus_monitor.py
git commit -m "feat: anticheat detection — FocusMonitor pauses ring buffer when game unfocused"
```
