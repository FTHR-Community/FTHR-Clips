# Extended Clips — Design Spec

**Date:** 2026-06-01  
**Status:** Approved

## Summary

Extended Clips adds a separately configurable clip duration for the F10 hotkey, mirroring Medal.tv's approach: the user sets an "Extended Clip" length independent of the regular clip length, and pressing F10 saves that many seconds from the ring buffer.

The current implementation (`clip_duration * 2`) is replaced with a user-configurable `extended_clip_length` setting.

---

## Settings

- **Key:** `extended_clip_length` in `~/.fthr/settings.json`
- **Default:** `60` (seconds)
- **Valid values:** any integer > 0; UI exposes presets 30s / 45s / 1m / 2m / 3m / 5m / 10m / 15m

---

## UI — capture_settings_widget.py

- Add `SettingBlock("EXT. CLIP", _ext_labels, _ext_cur)` as the 5th block in the settings row, to the right of Quality
- Presets: `['30s', '45s', '1m', '2m', '3m', '5m', '10m', '15m']` → `[30, 45, 60, 120, 180, 300, 600, 900]`
- New signal: `extended_clip_length_changed = pyqtSignal(int)`
- On change: save to settings, emit signal, show APPLY+RESTART button (same flow as `clip_block`)
- New getter: `get_extended_clip_length() -> int`

---

## Buffer Size — main.py

Buffer must accommodate whichever clip is longer:

```python
self.buffer_seconds = max(self.clip_duration, self.extended_clip_duration) + 2
```

Recalculate on both `clip_length_changed` and `extended_clip_length_changed`. The engine is restarted via APPLY+RESTART when either changes, so the new buffer size is passed on next engine launch.

---

## Hotkey Logic — main.py

- Load `extended_clip_duration` from settings on startup alongside `clip_duration`
- Connect `extended_clip_length_changed` signal to update `self.extended_clip_duration` and recalculate `buffer_seconds`
- `_on_hotkey_save_extended_clip` calls `_save_clip(self.extended_clip_duration)` instead of `_save_clip(self.clip_duration * 2)`

---

## Edge Cases

| Case | Behavior |
|------|----------|
| Extended < Clip Length | Allowed — user just gets a shorter clip on F10 |
| Engine buffer shorter than requested | Engine returns whatever is buffered — no crash, shorter clip |
| Extended == Clip Length | Works fine, F9 and F10 behave identically |

---

## Files Changed

| File | Change |
|------|--------|
| `FTHR_UI/core/settings_manager.py` | Add `extended_clip_length: 60` default |
| `FTHR_UI/ui/capture_settings_widget.py` | New EXT. CLIP SettingBlock + signal + getter |
| `FTHR_UI/main.py` | Load `extended_clip_duration`, fix `_on_hotkey_save_extended_clip`, recalculate buffer |
