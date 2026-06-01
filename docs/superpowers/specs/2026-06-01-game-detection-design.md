# Game Detection — Design Spec

**Date:** 2026-06-01  
**Status:** Approved

## Summary

When a game window is detected, FTHR Clips shows a toast prompt with a hotkey to confirm. On confirmation the capture source switches to that game window. When the game closes, FTHR auto-switches back to Desktop.

---

## Settings

| Key | File | Type | Default | Description |
|-----|------|------|---------|-------------|
| `game_detection_enabled` | `settings.json` | bool | `false` | Master toggle |
| `confirm_game_detection` | `hotkeys.json` | str | `'F8'` | Hotkey to confirm game detection prompt |

`game_detection_enabled` lives in `~/.fthr/settings.json`. The confirmation hotkey key `confirm_game_detection` lives in `~/.fthr/hotkeys.json` alongside all other hotkeys.

---

## Detection — `FTHR_UI/core/game_detector.py` (new file)

`_enumerate_capturable_windows` is imported from `FTHR_UI/ui/capture_settings_widget.py` (already exported from there into `main.py`). `GameDetector` receives it as a constructor parameter (dependency injection) to keep the module testable without a display.

```
GameDetector(QObject)
  __init__(self, enumerate_fn=_enumerate_capturable_windows)

  Signals:
    game_appeared(dict)   — new is_game=True window; dict = {hwnd, display_name, ...}
    game_closed(int)      — hwnd of a previously-seen game window that disappeared

  Internals:
    _timer: QTimer (3s interval)
    _known_game_hwnds: set[int]
    _poll(): calls enumerate_fn(), diffs against _known_game_hwnds
             emits game_appeared for new entries, game_closed for removed entries
```

- Polling interval: 3 seconds
- Only emits `game_appeared` for windows where `is_game == True`
- Ignores windows already in `_known_game_hwnds` (no repeat prompts for the same session)
- `start()` / `stop()` methods — called when `game_detection_enabled` changes

---

## Toast Flow — `main.py`

### On `game_appeared(window)`

1. Store `window` in `self._pending_game_window`
2. Show toast: `"{game_name} erkannt — [{hotkey}] Aufnehmen  [Esc] Ablehnen"`
3. `QTimer.singleShot(15000, ...)` auto-dismisses toast and clears `_pending_game_window` if no input

### On confirm hotkey (`confirm_game_detection` signal)

1. If `_pending_game_window is None`: no-op
2. Call existing source-switch logic: set capture mode to `'window'`, hwnd to `_pending_game_window['hwnd']`, save settings
3. Call `_restart_capture_engine()` to apply new source
4. Set `_active_game_hwnd = _pending_game_window['hwnd']`
5. Clear `_pending_game_window = None`

### On `game_closed(hwnd)`

1. If `hwnd != _active_game_hwnd`: no-op
2. Switch capture mode back to `'desktop'`, hwnd = 0, save settings
3. Call `_restart_capture_engine()`
4. Show brief toast: `"Game geschlossen — zurück auf Desktop"`
5. Clear `_active_game_hwnd = None`

### On Esc / 15s timeout

- Clear `_pending_game_window = None`
- No source change

### Second game detected while prompt is open

- Replace `_pending_game_window` with new window, update toast text

---

## Hotkey Integration

- New action: `confirm_game_detection` in `hotkeys.json`, default `F8`
- New signal on `HotkeyManager`: `confirm_game_detection_triggered = pyqtSignal()`
- Socket dispatcher maps `'confirm_game_detection'` → signal
- Hyprland bind (shown in UI alongside other binds):
  `bind = , F8, exec, echo -n "confirm_game_detection" | nc -U /tmp/fthr_hotkey.sock`

---

## UI — General Settings Tab

- Toggle row: "GAME DETECTION" with on/off switch
- Below toggle (visible when enabled): hotkey selector for confirmation key
- On toggle change: save setting, start/stop `GameDetector`

---

## State Variables (FTHRApp)

| Variable | Type | Description |
|----------|------|-------------|
| `_pending_game_window` | `dict \| None` | Window waiting for user confirmation |
| `_active_game_hwnd` | `int \| None` | Currently captured game hwnd |

---

## Files Changed

| File | Change |
|------|--------|
| `FTHR_UI/core/game_detector.py` | New — `GameDetector` class |
| `FTHR_UI/core/settings_manager.py` | Add `game_detection_enabled: false` default |
| `FTHR_UI/core/hotkey_manager.py` | Add `confirm_game_detection_triggered` signal + socket dispatch |
| `FTHR_UI/main.py` | Wire `GameDetector`, handle signals, add state vars, General Settings toggle |
| `tests/test_game_detector.py` | New — unit tests for polling diff logic |
