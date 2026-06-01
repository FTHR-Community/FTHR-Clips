# Extended Clips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable "EXT. CLIP" duration setting so F10 saves a user-defined length from the ring buffer, replacing the current broken `clip_duration * 2` stub.

**Architecture:** Two files change. `settings_manager.py` gets the new default key. `main.py` gets the combo row in `CaptureSettingsPopup`, a new signal, updated buffer formula, updated hotkey handler, and a new settings-change handler. `capture_settings_widget.py` is NOT touched — it is not used for the main settings popup.

**Tech Stack:** Python 3, PyQt6, pytest

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/core/settings_manager.py` | Add `'extended_clip_length': 60` to defaults dict |
| `FTHR_UI/main.py` | `CaptureSettingsPopup`: new signal + combo row + handler; `FTHRApp.__init__`: load extended duration + fix buffer; `_on_hotkey_save_extended_clip`: use new duration; `_on_clip_length_changed`: recalc buffer with max(); new `_on_extended_clip_length_changed` |
| `tests/test_settings_manager.py` | Two new tests for the new default |

---

## Task 1: Add `extended_clip_length` default to SettingsManager

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py:28-78`
- Test: `tests/test_settings_manager.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_settings_manager.py`:

```python
def test_extended_clip_length_default(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') == 60


def test_old_config_gets_extended_clip_length_default(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 30}, f)
    sm = SettingsManager()
    assert sm.get('extended_clip_length') == 60
    assert sm.get('clip_length') == 30  # existing value unaffected
```

- [ ] **Step 2: Run to verify they fail**

```
cd ~/FTHR_Clips && pytest tests/test_settings_manager.py -v
```
Expected: `test_extended_clip_length_default` and `test_old_config_gets_extended_clip_length_default` FAIL with `AssertionError`.

- [ ] **Step 3: Add the default to settings_manager.py**

In `FTHR_UI/core/settings_manager.py`, in the `default_settings` dict after `'clip_length': 30,` (line 28), add:

```python
            'clip_length': 30,       # seconds
            'extended_clip_length': 60,  # seconds — used by F10 / EXT. CLIP hotkey
```

- [ ] **Step 4: Run tests to verify they pass**

```
cd ~/FTHR_Clips && pytest tests/test_settings_manager.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/settings_manager.py tests/test_settings_manager.py
git commit -m "feat(settings): add extended_clip_length default (60s)"
```

---

## Task 2: Add EXT. CLIP row to CaptureSettingsPopup

**Files:**
- Modify: `FTHR_UI/main.py` — `CaptureSettingsPopup` class (lines 641–789)

The `CaptureSettingsPopup` class lives in `main.py`. It has its own combo rows for clip/fps/res/quality. We add an "EXT. CLIP" row after the CLIP LENGTH row.

- [ ] **Step 1: Add class-level constants and signal**

In `CaptureSettingsPopup` (around line 651), add the ext-clip values alongside the existing clip values:

```python
    clip_length_changed     = pyqtSignal(int)
    extended_clip_changed   = pyqtSignal(int)   # ← add this line
    framerate_changed       = pyqtSignal(int)
    resolution_changed      = pyqtSignal(int, int)
    bitrate_changed         = pyqtSignal(int)
    restart_needed          = pyqtSignal()
    summary_changed         = pyqtSignal(str)

    _CLIP_VALUES  = [5, 10, 15, 30, 45, 60, 120, 180, 300, 600, 900]
    _CLIP_LABELS  = ['5s','10s','15s','30s','45s','1m','2m','3m','5m','10m','15m']
    _EXT_VALUES   = [30, 45, 60, 120, 180, 300, 600, 900]          # ← add
    _EXT_LABELS   = ['30s','45s','1m','2m','3m','5m','10m','15m']  # ← add
    _FPS_VALUES   = [30, 60, 120, 144, 165, 240, 360]
```

- [ ] **Step 2: Load extended_clip_length in `__init__`**

In `CaptureSettingsPopup.__init__` (around line 664), add:

```python
        self.cur_clip   = self.sm.get('clip_length',          30)
        self.cur_ext    = self.sm.get('extended_clip_length',  60)  # ← add
        self.cur_fps    = self.sm.get('framerate',             60)
        self.cur_res    = self.sm.get('resolution',         'source')
        self.cur_qual   = self.sm.get('bitrate_level',       'high')
```

- [ ] **Step 3: Add EXT. CLIP combo row in `_setup_ui`**

In `_setup_ui` (around line 691), add the ext-clip row directly after the clip-length row:

```python
        # Clip length
        clip_idx = self._CLIP_VALUES.index(self.cur_clip) \
            if self.cur_clip in self._CLIP_VALUES else 3
        self.clip_combo = self._make_combo(self._CLIP_LABELS, clip_idx,
                                           self._on_clip_changed)
        layout.addLayout(_row('CLIP LENGTH', self.clip_combo))

        # Extended clip length
        ext_idx = self._EXT_VALUES.index(self.cur_ext) \
            if self.cur_ext in self._EXT_VALUES else 2  # default index for '1m'
        self.ext_combo = self._make_combo(self._EXT_LABELS, ext_idx,
                                          self._on_ext_clip_changed)
        layout.addLayout(_row('EXT. CLIP', self.ext_combo))
```

- [ ] **Step 4: Add `_on_ext_clip_changed` handler**

After `_on_clip_changed` (around line 733), add:

```python
    def _on_ext_clip_changed(self, idx):
        self.cur_ext = self._EXT_VALUES[idx]
        self.sm.set('extended_clip_length', self.cur_ext)
        self.sm.save_settings()
        self.extended_clip_changed.emit(self.cur_ext)
```

- [ ] **Step 5: Run the app and verify the EXT. CLIP row appears**

```
cd ~/FTHR_Clips && python FTHR_UI/main.py
```
Open the capture settings popup (click the top-bar settings button). Confirm a new "EXT. CLIP" row with a dropdown appears between CLIP LENGTH and FRAMERATE. Changing it should not crash.

- [ ] **Step 6: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): add EXT. CLIP row to capture settings popup"
```

---

## Task 3: Wire extended clip duration into buffer and hotkey

**Files:**
- Modify: `FTHR_UI/main.py` — `FTHRApp.__init__`, `_on_hotkey_save_extended_clip`, `_on_clip_length_changed`, new `_on_extended_clip_length_changed`, signal connection at line 1332

- [ ] **Step 1: Load extended_clip_duration on startup**

In `FTHRApp.__init__` (around line 1209), add extended duration and fix buffer:

```python
        self.clip_duration          = self.settings_manager.get('clip_length',         30)
        self.extended_clip_duration = self.settings_manager.get('extended_clip_length', 60)  # ← add
        self.capture_fps            = self.settings_manager.get('framerate',            60)
        self.capture_bitrate        = self.settings_manager.get('bitrate_kbps',      16000)

        saved_res = self.settings_manager.get('resolution', 'source')
        self.capture_width, self.capture_height = _resolution_to_dims(saved_res)
        self.buffer_seconds = max(self.clip_duration, self.extended_clip_duration) + 2  # ← fix
```

- [ ] **Step 2: Fix `_on_hotkey_save_extended_clip`**

Replace the current stub (line 1783):

```python
    def _on_hotkey_save_extended_clip(self): self._save_clip(self.extended_clip_duration)
```

- [ ] **Step 3: Fix `_on_clip_length_changed` to recalculate buffer correctly**

Replace (around line 1792):

```python
    def _on_clip_length_changed(self, duration: int):
        self.clip_duration  = duration
        self.buffer_seconds = max(duration, self.extended_clip_duration) + 2
```

- [ ] **Step 4: Add `_on_extended_clip_length_changed` handler**

Add directly after `_on_clip_length_changed`:

```python
    def _on_extended_clip_length_changed(self, duration: int):
        self.extended_clip_duration = duration
        self.buffer_seconds = max(self.clip_duration, duration) + 2
```

- [ ] **Step 5: Connect the new signal**

In the popup setup block (around line 1332):

```python
        self.cap_settings_popup = CaptureSettingsPopup(self.settings_manager, self)
        self.cap_settings_popup.clip_length_changed.connect(self._on_clip_length_changed)
        self.cap_settings_popup.extended_clip_changed.connect(self._on_extended_clip_length_changed)  # ← add
        self.cap_settings_popup.framerate_changed.connect(self._on_framerate_changed)
        self.cap_settings_popup.resolution_changed.connect(self._on_resolution_changed)
        self.cap_settings_popup.bitrate_changed.connect(self._on_bitrate_changed)
        self.cap_settings_popup.restart_needed.connect(self._restart_capture_engine)
        self.cap_settings_popup.summary_changed.connect(self._on_cap_summary_changed)
```

- [ ] **Step 6: Run the app and test F10**

```
cd ~/FTHR_Clips && python FTHR_UI/main.py
```
1. Open Settings popup → change EXT. CLIP to `2m`
2. Close popup
3. Press F10 — verify the saved clip is ~2 minutes (or as long as the buffer allows)
4. Check `~/.fthr/settings.json` — `extended_clip_length` should be `120`

- [ ] **Step 7: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat: extended clip duration wired — F10 uses configurable EXT. CLIP length"
```

---

## Task 4: Full regression test run

- [ ] **Step 1: Run full test suite**

```
cd ~/FTHR_Clips && pytest tests/ -v
```
Expected: all 15 tests PASS (13 existing + 2 new).

- [ ] **Step 2: Commit if any test fixes needed, otherwise done**

If all green — no commit needed. If anything broke, fix and commit with `fix:` prefix.
