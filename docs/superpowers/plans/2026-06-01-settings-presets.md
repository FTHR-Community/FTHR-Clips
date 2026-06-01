# Settings Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users save named snapshots of their capture settings and load them back instantly.

**Architecture:** New `PresetsManager` module handles JSON persistence. `CaptureSettingsPopup` gains `reload_from_settings()` to refresh UI combos without emitting change signals. General Settings tab gets a Presets section with Dropdown + Save + Load + Delete. Loading a preset saves to sm, reloads the popup UI, and restarts the engine.

**Tech Stack:** Python 3, PyQt6

---

## Saved Keys per Preset
`clip_length`, `extended_clip_length`, `framerate`, `resolution`, `bitrate_level`, `codec_pref`, `encoder_preset`, `audio_capture_enabled`, `multiband_audio_enabled`

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/core/presets_manager.py` | New — `PresetsManager`: load/save/delete presets from `~/.fthr/presets.json` |
| `FTHR_UI/main.py` | `CaptureSettingsPopup.reload_from_settings()`; General Settings presets section; `_on_preset_save`, `_on_preset_load`, `_on_preset_delete` handlers |
| `tests/test_presets_manager.py` | New — unit tests for save/load/delete |

---

## Task 1: PresetsManager

**Files:**
- Create: `FTHR_UI/core/presets_manager.py`
- Create: `tests/test_presets_manager.py`

**Saved keys constant** (used by both PresetsManager and the load handler):
```python
PRESET_KEYS = [
    'clip_length', 'extended_clip_length', 'framerate',
    'resolution', 'bitrate_level', 'codec_pref', 'encoder_preset',
    'audio_capture_enabled', 'multiband_audio_enabled',
]
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_presets_manager.py`:

```python
import sys, json
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from pathlib import Path
from core.presets_manager import PresetsManager


def test_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('Gaming', {'clip_length': 60, 'framerate': 144})
    data = pm.load('Gaming')
    assert data == {'clip_length': 60, 'framerate': 144}


def test_list_names(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('A', {'clip_length': 30})
    pm.save('B', {'clip_length': 60})
    assert 'A' in pm.names()
    assert 'B' in pm.names()


def test_delete(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    pm.save('ToDelete', {'clip_length': 30})
    pm.delete('ToDelete')
    assert 'ToDelete' not in pm.names()


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    pm = PresetsManager()
    assert pm.load('NonExistent') is None
```

- [ ] **Step 2: Run to verify they fail**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_presets_manager.py -v
```
Expected: `ModuleNotFoundError: No module named 'core.presets_manager'`

- [ ] **Step 3: Create `FTHR_UI/core/presets_manager.py`**

```python
import json
from pathlib import Path


PRESET_KEYS = [
    'clip_length', 'extended_clip_length', 'framerate',
    'resolution', 'bitrate_level', 'codec_pref', 'encoder_preset',
    'audio_capture_enabled', 'multiband_audio_enabled',
]


class PresetsManager:
    def __init__(self):
        self._path = Path.home() / '.fthr' / 'presets.json'

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: dict):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, 'w') as f:
            json.dump(data, f, indent=2)

    def names(self) -> list[str]:
        return sorted(self._read().keys())

    def load(self, name: str) -> dict | None:
        return self._read().get(name)

    def save(self, name: str, data: dict):
        presets = self._read()
        presets[name] = data
        self._write(presets)

    def delete(self, name: str):
        presets = self._read()
        presets.pop(name, None)
        self._write(presets)
```

- [ ] **Step 4: Run tests to verify 4 pass**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/test_presets_manager.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/presets_manager.py tests/test_presets_manager.py
git commit -m "feat(core): PresetsManager — save/load/delete named settings snapshots"
```

---

## Task 2: CaptureSettingsPopup.reload_from_settings + Presets UI

**Files:**
- Modify: `FTHR_UI/main.py`

### Part A: Add `reload_from_settings()` to CaptureSettingsPopup

This method re-reads values from `sm` and updates combo boxes silently (blocking signals so no engine restart is triggered). Call this after loading a preset.

- [ ] **Step 1: Add `reload_from_settings` to CaptureSettingsPopup**

Add after `_on_ext_clip_changed` in `CaptureSettingsPopup`:

```python
    def reload_from_settings(self):
        """Re-read sm and update all combo boxes silently (no signals emitted)."""
        self.cur_clip = self.sm.get('clip_length', 30)
        self.cur_ext  = self.sm.get('extended_clip_length', 60)
        self.cur_fps  = self.sm.get('framerate', 60)
        self.cur_res  = self.sm.get('resolution', 'source')
        self.cur_qual = self.sm.get('bitrate_level', 'high')

        for combo, values, val in [
            (self.clip_combo, self._CLIP_VALUES, self.cur_clip),
            (self.ext_combo,  self._EXT_VALUES,  self.cur_ext),
            (self.fps_combo,  self._FPS_VALUES,   self.cur_fps),
        ]:
            idx = values.index(val) if val in values else 0
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

        res_idx = self._RES_KEYS.index(self.cur_res.lower()) \
            if self.cur_res.lower() in self._RES_KEYS else 4
        self.res_combo.blockSignals(True)
        self.res_combo.setCurrentIndex(res_idx)
        self.res_combo.blockSignals(False)

        qual_idx = self._QUAL_KEYS.index(self.cur_qual) \
            if self.cur_qual in self._QUAL_KEYS else 2
        self.qual_combo.blockSignals(True)
        self.qual_combo.setCurrentIndex(qual_idx)
        self.qual_combo.blockSignals(False)

        self._update_summary()
```

### Part B: Add Presets section to General Settings tab

- [ ] **Step 2: Add import at top of SettingsPage or at top of file**

Near the other core imports (search for `from core.game_detector import GameDetector`), add:

```python
from core.presets_manager import PresetsManager, PRESET_KEYS
```

- [ ] **Step 3: Instantiate PresetsManager in SettingsPage.__init__**

In `SettingsPage.__init__` (find it — search for `def __init__` in the SettingsPage class, which has `self.sm = ...`), add:

```python
        self._presets_mgr = PresetsManager()
```

- [ ] **Step 4: Add Presets section to `_make_general_page()`**

In `_make_general_page()`, before `layout.addStretch()` at the end, add:

```python
        # ── Settings Presets ──────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Settings-Presets'))
        layout.addSpacing(12)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)

        self.preset_combo = _DropdownCombo()
        self.preset_combo.setStyleSheet(_COMBO_STYLE)
        self.preset_combo.setMinimumWidth(160)
        self._refresh_preset_combo()
        preset_row.addWidget(self.preset_combo, 1)

        load_btn = QPushButton('LADEN')
        load_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        load_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        load_btn.clicked.connect(self._on_preset_load)
        preset_row.addWidget(load_btn)

        save_btn = QPushButton('SPEICHERN')
        save_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.clicked.connect(self._on_preset_save)
        preset_row.addWidget(save_btn)

        del_btn = QPushButton('LÖSCHEN')
        del_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        del_btn.clicked.connect(self._on_preset_delete)
        preset_row.addWidget(del_btn)

        layout.addLayout(preset_row)
```

- [ ] **Step 5: Add three handlers + `_refresh_preset_combo` to SettingsPage**

Add after `_on_audio_capture_toggled`:

```python
    def _refresh_preset_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        names = self._presets_mgr.names()
        if names:
            self.preset_combo.addItems(names)
        else:
            self.preset_combo.addItem('— kein Preset —')
        self.preset_combo.blockSignals(False)

    def _on_preset_save(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, 'Preset speichern', 'Name:',
            text=self.preset_combo.currentText() if self._presets_mgr.names() else '')
        if not ok or not name.strip():
            return
        name = name.strip()
        data = {k: self.sm.get(k) for k in PRESET_KEYS}
        self._presets_mgr.save(name, data)
        self._refresh_preset_combo()
        # Select the just-saved preset
        idx = self.preset_combo.findText(name)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)

    def _on_preset_load(self):
        name = self.preset_combo.currentText()
        data = self._presets_mgr.load(name)
        if data is None:
            return
        for k, v in data.items():
            self.sm.set(k, v)
        self.sm.save_settings()
        # Refresh the capture settings popup
        main_win = self.window()
        if hasattr(main_win, 'cap_settings_popup'):
            main_win.cap_settings_popup.reload_from_settings()
        # Restart engine to apply new settings
        if hasattr(main_win, '_restart_capture_engine'):
            main_win._restart_capture_engine()

    def _on_preset_delete(self):
        name = self.preset_combo.currentText()
        if not self._presets_mgr.names():
            return
        self._presets_mgr.delete(name)
        self._refresh_preset_combo()
```

- [ ] **Step 6: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 7: Run regression tests**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -10
```
Expected: all 23 tests PASS (19 existing + 4 new).

- [ ] **Step 8: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): Settings-Presets — save/load/delete named capture setting snapshots"
```
