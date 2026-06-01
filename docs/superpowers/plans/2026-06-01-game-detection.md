# Game Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect game windows and prompt the user via toast + hotkey to switch capture source to the detected game; auto-switch back to Desktop when the game closes.

**Architecture:** A new `GameDetector` QObject polls `_enumerate_capturable_windows()` every 3s and emits signals on game appearance/disappearance. `FTHRApp` listens to these signals and shows a toast prompt via the existing `CaptureCardClient` subprocess. A new `confirm_game_detection` hotkey wires through the existing socket server. The toggle lives in the General Settings page.

**Tech Stack:** Python 3, PyQt6, pytest

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/core/game_detector.py` | New — `GameDetector` QObject class |
| `FTHR_UI/core/settings_manager.py` | Add `game_detection_enabled: False` default |
| `FTHR_UI/core/hotkey_manager.py` | Add `confirm_game_detection_triggered` + `dismiss_game_detection_triggered` signals, hotkeys defaults, socket dispatch, register methods |
| `FTHR_UI/ui/capture_card.py` | Add `show_prompt(text)` method |
| `FTHR_UI/ui/capture_card_process.py` | Add `prompt|` command dispatch |
| `FTHR_UI/ui/capture_card_client.py` | Add `show_prompt(text)` method |
| `FTHR_UI/main.py` | Wire `GameDetector`, state vars, signal handlers, `_on_game_detection_toggled`, General Settings toggle UI |
| `tests/test_game_detector.py` | New — unit tests for polling diff logic |

---

## Task 1: GameDetector class

**Files:**
- Create: `FTHR_UI/core/game_detector.py`
- Create: `tests/test_game_detector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_game_detector.py`:

```python
import sys
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from unittest.mock import MagicMock
from core.game_detector import GameDetector


def _make_window(hwnd, is_game=True):
    return {'hwnd': hwnd, 'display_name': f'Game{hwnd}', 'is_game': is_game}


def test_game_appeared_emitted_for_new_game(qtbot):
    windows = []
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    windows.append(_make_window(1001))
    detector._poll()

    assert len(appeared) == 1
    assert appeared[0]['hwnd'] == 1001


def test_game_appeared_not_repeated(qtbot):
    windows = [_make_window(1001)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    detector._poll()
    detector._poll()

    assert len(appeared) == 1  # second poll: already known, no repeat


def test_non_game_window_ignored(qtbot):
    windows = [_make_window(2001, is_game=False)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    detector._poll()

    assert appeared == []


def test_game_closed_emitted_when_window_disappears(qtbot):
    windows = [_make_window(1001)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    closed = []
    detector.game_closed.connect(lambda hwnd: closed.append(hwnd))

    detector._poll()       # registers 1001
    windows.clear()
    detector._poll()       # 1001 gone

    assert closed == [1001]
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd ~/FTHR_Clips && pytest tests/test_game_detector.py -v
```
Expected: `ModuleNotFoundError: No module named 'core.game_detector'`

- [ ] **Step 3: Create `FTHR_UI/core/game_detector.py`**

```python
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class GameDetector(QObject):
    game_appeared = pyqtSignal(dict)   # new is_game=True window
    game_closed   = pyqtSignal(int)    # hwnd of a game that disappeared

    def __init__(self, enumerate_fn=None, parent=None):
        super().__init__(parent)
        if enumerate_fn is None:
            from ui.capture_settings_widget import _enumerate_capturable_windows
            enumerate_fn = _enumerate_capturable_windows
        self._enumerate = enumerate_fn
        self._known: dict[int, dict] = {}   # hwnd → window dict
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._known.clear()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        current = {w['hwnd']: w for w in self._enumerate() if w.get('is_game')}
        for hwnd, window in current.items():
            if hwnd not in self._known:
                self._known[hwnd] = window
                self.game_appeared.emit(window)
        for hwnd in list(self._known):
            if hwnd not in current:
                del self._known[hwnd]
                self.game_closed.emit(hwnd)
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd ~/FTHR_Clips && pytest tests/test_game_detector.py -v
```
Expected: 4 tests PASS.

Note: if `pytest-qt` is not installed, run `pip install pytest-qt` first. If `qtbot` fixture causes issues without a display, add `@pytest.mark.parametrize` or use `pytest-qt` with `QT_QPA_PLATFORM=offscreen`.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/game_detector.py tests/test_game_detector.py
git commit -m "feat(core): GameDetector — polls for game windows, emits game_appeared/game_closed"
```

---

## Task 2: Settings default + Hotkey signals

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py:27-78`
- Modify: `FTHR_UI/core/hotkey_manager.py`

- [ ] **Step 1: Add `game_detection_enabled` default to settings_manager.py**

In `_load_settings` default_settings dict, after `'multiband_audio_enabled': False,`:

```python
            'multiband_audio_enabled': False,
            'game_detection_enabled':  False,
```

- [ ] **Step 2: Add signals and hotkeys to HotkeyManager**

In `hotkey_manager.py`, in the `HotkeyManager` class signals block, add two signals after `save_screenshot_triggered`:

```python
    save_clip_triggered               = pyqtSignal()
    save_extended_clip_triggered      = pyqtSignal()
    save_screenshot_triggered         = pyqtSignal()
    confirm_game_detection_triggered  = pyqtSignal()
    dismiss_game_detection_triggered  = pyqtSignal()
```

In `__init__`, add entries to `self.hotkeys` dict:

```python
        self.hotkeys = {
            'save_clip':               'F9',
            'save_extended_clip':      'F10',
            'save_screenshot':         'F11',
            'confirm_game_detection':  'F8',
            'dismiss_game_detection':  'Escape',
        }
```

- [ ] **Step 3: Add register methods**

After `_register_save_screenshot`, add:

```python
    def _register_confirm_game_detection(self):
        self._register_keyboard_hotkey(
            self.hotkeys['confirm_game_detection'],
            self.confirm_game_detection_triggered)

    def _register_dismiss_game_detection(self):
        self._register_keyboard_hotkey(
            self.hotkeys['dismiss_game_detection'],
            self.dismiss_game_detection_triggered)
```

In `register_all`, add the two new registrations:

```python
    def register_all(self):
        self._register_save_clip()
        self._register_save_extended_clip()
        self._register_save_screenshot()
        self._register_confirm_game_detection()
        self._register_dismiss_game_detection()
        self._start_socket_server()
```

In `set_hotkey`, add the two new branches:

```python
        if action == 'save_clip':
            self._register_save_clip()
        elif action == 'save_extended_clip':
            self._register_save_extended_clip()
        elif action == 'save_screenshot':
            self._register_save_screenshot()
        elif action == 'confirm_game_detection':
            self._register_confirm_game_detection()
        elif action == 'dismiss_game_detection':
            self._register_dismiss_game_detection()
```

- [ ] **Step 4: Add socket dispatch entries**

In `_start_socket_server`, extend the `_dispatch` dict:

```python
        _dispatch = {
            'save_clip':               self.save_clip_triggered,
            'save_extended_clip':      self.save_extended_clip_triggered,
            'save_screenshot':         self.save_screenshot_triggered,
            'confirm_game_detection':  self.confirm_game_detection_triggered,
            'dismiss_game_detection':  self.dismiss_game_detection_triggered,
        }
```

- [ ] **Step 5: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143 (SIGTERM), no tracebacks.

- [ ] **Step 6: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/settings_manager.py FTHR_UI/core/hotkey_manager.py
git commit -m "feat(hotkeys): add confirm/dismiss_game_detection signals and socket dispatch"
```

---

## Task 3: Capture card prompt support

**Files:**
- Modify: `FTHR_UI/ui/capture_card.py`
- Modify: `FTHR_UI/ui/capture_card_process.py`
- Modify: `FTHR_UI/ui/capture_card_client.py`

The capture card subprocess receives one-line commands on stdin. We add a `prompt|{text}` command for game detection toasts.

- [ ] **Step 1: Add `show_prompt` to `capture_card.py`**

In `capture_card.py`, after `show_upload`:

```python
    def show_prompt(self, text: str) -> None:
        self._headline = text
        self._stats = []
        self._show(_SND_ERROR)
```

- [ ] **Step 2: Add `prompt|` dispatch to `capture_card_process.py`**

In `capture_card_process.py`, in the command dispatch block (after the `upload` branch), add:

```python
        elif cmd.startswith('prompt|'):
            text = cmd[7:]
            card.show_prompt(text)
```

Also add `prompt|{text}` to the protocol docstring at the top.

- [ ] **Step 3: Add `show_prompt` to `capture_card_client.py`**

In `capture_card_client.py`, after `show_upload`:

```python
    def show_prompt(self, text: str) -> None:
        self._send(f'prompt|{text}')
```

- [ ] **Step 4: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/ui/capture_card.py FTHR_UI/ui/capture_card_process.py FTHR_UI/ui/capture_card_client.py
git commit -m "feat(card): add show_prompt command for game detection toast"
```

---

## Task 4: Wire GameDetector in FTHRApp

**Files:**
- Modify: `FTHR_UI/main.py` — `FTHRApp` class

- [ ] **Step 1: Add state variables and detector in `__init__`**

In `FTHRApp.__init__`, after `self.hotkey_manager = HotkeyManager()`, add:

```python
        self.hotkey_manager  = HotkeyManager()
        self._pending_game_window: dict | None = None
        self._active_game_hwnd:    int  | None = None
        self._game_dismiss_timer = QTimer(self)
        self._game_dismiss_timer.setSingleShot(True)
        self._game_dismiss_timer.timeout.connect(self._on_game_prompt_timeout)
```

At the top of the file, add the import (near other core imports):

```python
from core.game_detector import GameDetector
```

After building `self.hotkey_manager`, instantiate the detector (but don't start it yet):

```python
        self._game_detector = GameDetector()
        self._game_detector.game_appeared.connect(self._on_game_appeared)
        self._game_detector.game_closed.connect(self._on_game_closed)
```

- [ ] **Step 2: Connect confirm/dismiss hotkey signals**

In `_setup_hotkeys` (around line 1773), add:

```python
        self.hotkey_manager.confirm_game_detection_triggered.connect(
            self._on_confirm_game_detection)
        self.hotkey_manager.dismiss_game_detection_triggered.connect(
            self._on_dismiss_game_detection)
```

- [ ] **Step 3: Add the four handler methods**

Add these methods after `_on_hotkey_save_screenshot` (around line 1786):

```python
    def _on_game_appeared(self, window: dict):
        self._pending_game_window = window
        game_name = window.get('display_name', 'Game')
        hotkey = self.hotkey_manager.hotkeys.get('confirm_game_detection', 'F8')
        self.capture_card.show_prompt(
            f'{game_name} erkannt — [{hotkey}] Aufnehmen  [Esc] Ablehnen')
        self._game_dismiss_timer.start(15000)

    def _on_game_closed(self, hwnd: int):
        if hwnd != self._active_game_hwnd:
            return
        self._active_game_hwnd = None
        self.settings_manager.set('capture_mode', 'desktop')
        self.settings_manager.set('target_hwnd', 0)
        self.settings_manager.save_settings()
        self._restart_capture_engine()
        self.capture_card.show_prompt('Game geschlossen — zurück auf Desktop')

    def _on_confirm_game_detection(self):
        if self._pending_game_window is None:
            return
        hwnd = self._pending_game_window['hwnd']
        self._active_game_hwnd = hwnd
        self.settings_manager.set('capture_mode', 'window')
        self.settings_manager.set('target_hwnd', hwnd)
        self.settings_manager.save_settings()
        self._pending_game_window = None
        self._game_dismiss_timer.stop()
        self._restart_capture_engine()

    def _on_dismiss_game_detection(self):
        self._pending_game_window = None
        self._game_dismiss_timer.stop()

    def _on_game_prompt_timeout(self):
        self._pending_game_window = None
```

- [ ] **Step 4: Start/stop detector based on saved setting at startup**

In `FTHRApp.__init__`, after creating `_game_detector` and connecting its signals, add:

```python
        if self.settings_manager.get('game_detection_enabled', False):
            self._game_detector.start()
```

- [ ] **Step 5: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 6: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat: wire GameDetector into FTHRApp — game_appeared/closed handlers + confirm/dismiss"
```

---

## Task 5: General Settings toggle UI

**Files:**
- Modify: `FTHR_UI/main.py` — `SettingsPage._make_general_page` + `_on_game_detection_toggled`

The `SettingsPage` class is defined inside `main.py`. Its `_make_general_page` method builds the General tab. We add a "Game Detection" section following the same `QCheckBox` + section header pattern as the existing System section.

- [ ] **Step 1: Add the toggle to `_make_general_page`**

In `_make_general_page` (around line 2847), after the System section (`self.autostart_check` block) and before the Import Clips section, add:

```python
        # ── Game Detection ────────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Game Detection'))
        layout.addSpacing(12)

        self.game_detection_check = QCheckBox('Games erkennen und zum Aufnehmen vorschlagen')
        self.game_detection_check.setStyleSheet(CHECKBOX_QSS)
        self.game_detection_check.setChecked(self.sm.get('game_detection_enabled', False))
        self.game_detection_check.toggled.connect(self._on_game_detection_toggled)
        layout.addWidget(self.game_detection_check)
        layout.addSpacing(4)

        _gd_hint = QLabel('Drücke F8 um aufzunehmen wenn ein Game erkannt wird.')
        _gd_hint.setWordWrap(True)
        _gd_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_gd_hint)
```

- [ ] **Step 2: Add `_on_game_detection_toggled` to SettingsPage**

In `SettingsPage`, add after `_on_multiband_toggled`:

```python
    def _on_game_detection_toggled(self, checked: bool):
        self.sm.set('game_detection_enabled', checked)
        self.sm.save_settings()
        # Notify main window to start/stop detector
        main_win = self.window()
        if hasattr(main_win, '_game_detector'):
            if checked:
                main_win._game_detector.start()
            else:
                main_win._game_detector.stop()
```

- [ ] **Step 3: Verify app starts and toggle is visible**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks. Open Settings → General tab, confirm "Game Detection" section with checkbox appears.

- [ ] **Step 4: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): add Game Detection toggle to General Settings"
```

---

## Task 6: Full regression test run

**Files:**
- Test: `tests/`

- [ ] **Step 1: Check if pytest-qt is available**

```
cd ~/FTHR_Clips && python -m pytest tests/test_game_detector.py -v 2>&1 | head -20
```

If `pytest-qt` missing: `pip install pytest-qt` then re-run.
If display issues: `QT_QPA_PLATFORM=offscreen pytest tests/test_game_detector.py -v`

- [ ] **Step 2: Run full test suite**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v
```
Expected: all 19 tests PASS (15 existing + 4 new in `test_game_detector.py`).

- [ ] **Step 3: Commit if any fixes needed**

If all green — no commit needed. If anything broke, fix and commit with `fix:` prefix.
