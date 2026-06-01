# Performance Settings — Audio Capture Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a toggle in Performance Settings to fully disable audio capture, reducing CPU/memory usage.

**Architecture:** New `audio_enabled` field in `CaptureConfig` (C++), read from argv[14] (default 1). C++ engine skips audio init when 0; `save_clip_to_file` already handles empty audio_pcm gracefully (has_audio check on line 94 of save_clip.cpp). Python adds setting, passes arg to engine, skips mic/multiband mux when disabled, adds toggle to Performance tab.

**Tech Stack:** C++17, Python 3, PyQt6, CMake

---

## File Map

| File | Change |
|------|--------|
| `FTHRcapture_linux/src/capture_engine.h` | Add `bool audio_enabled = true;` to `CaptureConfig` |
| `FTHRcapture_linux/src/main.cpp` | Read argv[14] as audio_enabled |
| `FTHRcapture_linux/src/capture_engine.cpp` | Guard audio Start/Stop with `cfg_.audio_enabled` |
| `FTHR_UI/core/settings_manager.py` | Add `audio_capture_enabled: True` default |
| `FTHR_UI/main.py` | Pass audio_arg as argv[14]; skip mic+mux when disabled; Performance tab toggle |

---

## Task 1: C++ audio_enabled flag

**Files:**
- Modify: `FTHRcapture_linux/src/capture_engine.h`
- Modify: `FTHRcapture_linux/src/main.cpp`
- Modify: `FTHRcapture_linux/src/capture_engine.cpp`

- [ ] **Step 1: Add field to `CaptureConfig` in `capture_engine.h`**

In `struct CaptureConfig`, add after `bool multiband_enabled = false;`:

```cpp
    bool      audio_enabled    = true;
```

- [ ] **Step 2: Read argv[14] in `main.cpp`**

After `cfg.multiband_enabled = (argc > 13) && (arg_u32(argv, 13, 0) == 1);` (line 59), add:

```cpp
    cfg.audio_enabled = !((argc > 14) && (arg_u32(argv, 14, 1) == 0));
```

Also add to the argv contract comment block at the top of `main()`:

```
//   [14] audio_enabled (1=on default, 0=off)
```

- [ ] **Step 3: Guard audio Start/Stop in `capture_engine.cpp`**

Find the audio start block (around line 252–257):
```cpp
    if (cfg.multiband_enabled && !cfg.audio_categories.empty()) {
        multi_audio_.Start(cfg.audio_categories);
    } else {
        audio_.Start("");
    }
```

Replace with:
```cpp
    if (cfg.audio_enabled) {
        if (cfg.multiband_enabled && !cfg.audio_categories.empty()) {
            multi_audio_.Start(cfg.audio_categories);
        } else {
            audio_.Start("");
        }
    }
```

Find the audio stop block in `Shutdown()` (around lines 274–275):
```cpp
    audio_.Stop();
    multi_audio_.Stop();
```

Replace with:
```cpp
    if (cfg_.audio_enabled) {
        audio_.Stop();
        multi_audio_.Stop();
    }
```

Also find the Reconfigure audio start block (around line 669–672):
```cpp
    if (cfg_.multiband_enabled && !cfg_.audio_categories.empty()) {
        multi_audio_.Start(cfg_.audio_categories);
    } else {
        audio_.Start("");
    }
```

Replace with:
```cpp
    if (cfg_.audio_enabled) {
        if (cfg_.multiband_enabled && !cfg_.audio_categories.empty()) {
            multi_audio_.Start(cfg_.audio_categories);
        } else {
            audio_.Start("");
        }
    }
```

- [ ] **Step 4: Rebuild the engine**

```bash
cd ~/FTHR_Clips/FTHRcapture_linux/build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) 2>&1 | tail -10
```
Expected: build succeeds, `FTHRclips` binary updated.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/main.cpp FTHRcapture_linux/src/capture_engine.cpp
git commit -m "feat(engine): audio_enabled argv[14] — skip audio init/stop when 0"
```

---

## Task 2: Python settings + engine arg + UI toggle

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py`
- Modify: `FTHR_UI/main.py`

- [ ] **Step 1: Add `audio_capture_enabled` default to settings_manager.py**

In `_load_settings` default_settings dict, after `'game_detection_enabled': False,`:

```python
            'audio_capture_enabled':   True,
```

- [ ] **Step 2: Pass audio_arg as argv[14] in `start_engine`**

Find the `subprocess.Popen` call (around line 1944). Currently ends with:
```python
                 multiband_arg],
```

Add `audio_arg` before the Popen call:
```python
        audio_enabled = self.settings_manager.get('audio_capture_enabled', True)
        audio_arg = '1' if audio_enabled else '0'
```

And add it to the args list:
```python
                 multiband_arg, audio_arg],
```

- [ ] **Step 3: Skip mic and multiband mux in `_save_clip` when audio disabled**

Find `_save_clip` (around line 1972). It currently checks `mic_active` and calls `_mux_multiband_into_clip`. Add an audio guard:

Find the block (around line 2100–2120):
```python
                multiband_on = self.settings_manager.get('multiband_audio_enabled', False)
                mic_active = (not multiband_on and
                              MicRecorder.is_available() and MicRecorder().is_running())
```

Replace with:
```python
                audio_on = self.settings_manager.get('audio_capture_enabled', True)
                multiband_on = audio_on and self.settings_manager.get('multiband_audio_enabled', False)
                mic_active = (audio_on and not multiband_on and
                              MicRecorder.is_available() and MicRecorder().is_running())
```

- [ ] **Step 4: Add "Audio Capture" section to Performance tab**

In `SettingsPage._make_performance_page()`, after the existing `layout.addStretch()` line, add BEFORE it:

```python
        layout.addSpacing(28)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addWidget(_flat_section_header('Audio Capture'))
        layout.addSpacing(12)

        self.audio_capture_check = QCheckBox('Audio aufnehmen')
        self.audio_capture_check.setStyleSheet(CHECKBOX_QSS)
        self.audio_capture_check.setChecked(self.sm.get('audio_capture_enabled', True))
        self.audio_capture_check.toggled.connect(self._on_audio_capture_toggled)
        layout.addWidget(self.audio_capture_check)
        layout.addSpacing(4)

        _ac_hint = QLabel('Deaktivieren spart CPU. Änderung gilt beim nächsten Engine-Neustart.')
        _ac_hint.setWordWrap(True)
        _ac_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_ac_hint)
```

- [ ] **Step 5: Add `_on_audio_capture_toggled` to SettingsPage**

Add after `_on_game_detection_toggled`:

```python
    def _on_audio_capture_toggled(self, checked: bool):
        self.sm.set('audio_capture_enabled', checked)
        self.sm.save_settings()
```

No live start/stop needed — change takes effect on next engine restart (APPLY + RESTART or app restart).

- [ ] **Step 6: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 7: Run regression tests**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v
```
Expected: all 19 tests PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/settings_manager.py FTHR_UI/main.py
git commit -m "feat: audio capture toggle in Performance Settings — passes argv[14] to engine"
```
