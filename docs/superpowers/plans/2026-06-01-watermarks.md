# Watermarks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional text watermark (default OFF) burned into clips via ffmpeg drawtext.

**Architecture:** `_apply_watermark(clip_path, ffmpeg)` runs synchronously at the end of each clip-finalization path (mic mux worker, multiband mux worker, and a new no-mux finalizer thread). Re-encodes video with `ultrafast` preset + copies audio. UI toggle in General Settings.

**Tech Stack:** Python 3, PyQt6, ffmpeg (via imageio_ffmpeg)

---

## File Map

| File | Change |
|------|--------|
| `FTHR_UI/core/settings_manager.py` | Add `watermark_enabled: False`, `watermark_text: 'FTHR'` defaults |
| `FTHR_UI/main.py` | `_apply_watermark()` method; call in mic/multiband workers + no-mux path; General Settings toggle |

---

## Task 1: Settings defaults + `_apply_watermark` + mux wiring

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py`
- Modify: `FTHR_UI/main.py`

### Part A — Settings defaults

- [ ] **Step 1: Add defaults to settings_manager.py**

In `_load_settings()` default_settings dict, after `'audio_capture_enabled': True,`:

```python
            'watermark_enabled':  False,
            'watermark_text':     'FTHR',
```

### Part B — `_apply_watermark` method on FTHRApp

- [ ] **Step 2: Add `_apply_watermark` to FTHRApp**

Add after `_multiband_mux_worker`:

```python
    def _apply_watermark(self, clip_path: str, ffmpeg: str) -> None:
        if not self.settings_manager.get('watermark_enabled', False):
            return
        text = self.settings_manager.get('watermark_text', 'FTHR') or 'FTHR'
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(
            suffix='.mp4',
            dir=os.path.dirname(clip_path),
            delete=False,
        )
        tmp_path = tmp.name
        tmp.close()
        try:
            result = subprocess.run(
                [
                    ffmpeg, '-y',
                    '-i', clip_path,
                    '-vf', (
                        f"drawtext=text='{text}'"
                        ":fontsize=28:fontcolor=white@0.5"
                        ":x=w-tw-16:y=h-th-16"
                    ),
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
                    '-c:a', 'copy',
                    tmp_path,
                ],
                capture_output=True,
                **_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(tmp_path, clip_path)
                print(f'[Watermark] Applied to {os.path.basename(clip_path)}')
            else:
                err = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[Watermark] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
        except Exception as e:
            print(f'[Watermark] Error: {e}')
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
```

### Part C — Wire into mux workers

- [ ] **Step 3: Wire into `_mic_mux_worker`**

In `_mic_mux_worker`, find `os.replace(mixed_mp4, clip_path)` followed by the print and the `finally` block. Add the watermark call AFTER `os.replace` succeeds, still inside the `try` block (before the `finally`):

```python
                try:
                    os.replace(mixed_mp4, clip_path)
                    print(f'[Mic] Mixed mic into {os.path.basename(clip_path)}')
                    self._apply_watermark(clip_path, ffmpeg)   # ← add this line
                except OSError as e:
                    print(f'[Mic] Could not replace clip: {e}')
```

- [ ] **Step 4: Wire into `_multiband_mux_worker`**

In `_multiband_mux_worker`, find `ok = mix_multiband_clip(clip_path, category_wavs, volumes, ffmpeg)`. After it (and after the WAV cleanup loop), add:

```python
        ok = mix_multiband_clip(clip_path, category_wavs, volumes, ffmpeg)

        # Clean up WAV files regardless of mix result
        for wav in category_wavs.values():
            try:
                os.remove(wav)
            except OSError:
                pass

        if ok:
            self._apply_watermark(clip_path, ffmpeg)   # ← add this line
```

### Part D — No-mux finalizer path

When neither mic nor multiband mux runs, we need a background thread to wait for the clip to stabilize then apply the watermark.

- [ ] **Step 5: Add `_finalize_clip_worker` to FTHRApp**

Add after `_apply_watermark`:

```python
    def _finalize_clip(self, clip_path: str, duration_seconds: int):
        if not self.settings_manager.get('watermark_enabled', False):
            return
        threading.Thread(
            target=self._finalize_clip_worker,
            args=(clip_path, duration_seconds),
            daemon=True,
        ).start()

    def _finalize_clip_worker(self, clip_path: str, duration_seconds: int):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            return
        deadline = time.monotonic() + max(duration_seconds * 2, 15)
        last_size = -1
        while time.monotonic() < deadline:
            try:
                if os.path.exists(clip_path):
                    size = os.path.getsize(clip_path)
                    if size > 0 and size == last_size:
                        break
                    last_size = size
            except OSError:
                pass
            time.sleep(0.25)
        else:
            print(f'[Finalize] Clip did not stabilize — skipping watermark')
            return
        self._apply_watermark(clip_path, ffmpeg)
```

- [ ] **Step 6: Call `_finalize_clip` in `_save_clip` for no-mux case**

In `_save_clip`, after the mic/multiband mux calls (around line 2145–2149), the current code is:

```python
                self._mux_mic_into_clip(
                    str(output_path), duration_seconds, mic_end_time, clip_ready)
                if multiband_on:
                    self._mux_multiband_into_clip(
                        str(output_path), duration_seconds, mic_end_time)
```

Add after those lines:

```python
                if not mic_active and not multiband_on:
                    self._finalize_clip(str(output_path), duration_seconds)
```

### Part E — Verify + commit

- [ ] **Step 7: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 8: Run regression tests**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -10
```
Expected: all 23 tests PASS.

- [ ] **Step 9: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/core/settings_manager.py FTHR_UI/main.py
git commit -m "feat: watermark support — ffmpeg drawtext applied to clips when enabled"
```

---

## Task 2: General Settings UI toggle

**Files:**
- Modify: `FTHR_UI/main.py` — `SettingsPage._make_general_page` + handlers

- [ ] **Step 1: Add Watermark section to `_make_general_page()`**

Before the `layout.addStretch()` at the end of `_make_general_page`, add (before the Settings-Presets section):

```python
        # ── Wasserzeichen ─────────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Wasserzeichen'))
        layout.addSpacing(12)

        self.watermark_check = QCheckBox('Wasserzeichen in Clips einbrennen')
        self.watermark_check.setStyleSheet(CHECKBOX_QSS)
        self.watermark_check.setChecked(self.sm.get('watermark_enabled', False))
        self.watermark_check.toggled.connect(self._on_watermark_toggled)
        layout.addWidget(self.watermark_check)
        layout.addSpacing(8)

        wm_row = QHBoxLayout()
        wm_row.setSpacing(8)
        wm_lbl = QLabel('TEXT')
        wm_lbl.setStyleSheet(_LABEL_STYLE)
        wm_lbl.setFixedWidth(60)
        wm_row.addWidget(wm_lbl)
        self.watermark_text_edit = QLineEdit(self.sm.get('watermark_text', 'FTHR'))
        self.watermark_text_edit.setMaxLength(30)
        self.watermark_text_edit.setStyleSheet(_COMBO_STYLE)
        self.watermark_text_edit.textChanged.connect(self._on_watermark_text_changed)
        wm_row.addWidget(self.watermark_text_edit)
        layout.addLayout(wm_row)
        layout.addSpacing(4)

        _wm_hint = QLabel('Kleines Text-Overlay unten rechts. Standard: deaktiviert.')
        _wm_hint.setWordWrap(True)
        _wm_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_wm_hint)
```

- [ ] **Step 2: Add handlers to SettingsPage**

Add after `_on_audio_capture_toggled`:

```python
    def _on_watermark_toggled(self, checked: bool):
        self.sm.set('watermark_enabled', checked)
        self.sm.save_settings()

    def _on_watermark_text_changed(self, text: str):
        self.sm.set('watermark_text', text)
        self.sm.save_settings()
```

- [ ] **Step 3: Verify app starts cleanly**

```
cd ~/FTHR_Clips && timeout 5 python FTHR_UI/main.py 2>&1 | head -20
```
Expected: exit 143, no tracebacks.

- [ ] **Step 4: Run regression tests**

```
cd ~/FTHR_Clips && QT_QPA_PLATFORM=offscreen pytest tests/ -v 2>&1 | tail -5
```
Expected: all 23 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): Watermark toggle + text field in General Settings"
```
