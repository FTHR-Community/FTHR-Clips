# Linux Engine Hardening, Phase 2: Implementation Plan (engine-facing Python)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Linux-specific defects in the Python layer that the engine depends on: the bridge-replacement race during engine connect, compositor misdetection, incomplete and noisy Hyprland bind generation, uploader secrets written world-readable, and the hotkey path (PR #16).

**Architecture:** PySide6 UI in `FTHR_UI/main.py` (12k lines, touched minimally), engine IPC in `FTHR_UI/core/capture_bridge.py` (singleton `CaptureBridge`), compositor detection in `FTHR_UI/core/compositor.py`, hotkeys in `FTHR_UI/core/hotkey_manager.py` (Unix socket + generated Hyprland binds; `keyboard` library fallback until #16). Tests are pytest under `tests/` with `QT_QPA_PLATFORM=offscreen` set by `tests/conftest.py`; tests that need `main` import it as a module (see `tests/test_background_ui_pause.py` for the pattern).

**Tech Stack:** Python 3.14 venv at `.venv-linux` (pinned `requirements-alpha.txt`), pytest 9, ruff. Run: `QT_QPA_PLATFORM=offscreen .venv-linux/bin/python -m pytest <files> -q -p no:cacheprovider` and `.venv-linux/bin/python -m ruff check .`.

**Spec:** `docs/superpowers/specs/2026-10-12-linux-engine-hardening-design.md`, section "Phase 2".

## Global Constraints

- Branch from `origin/linux`; one PR per task group; merge after the Linux CI checks are green. Author is `kalopsia-space`; no AI attribution anywhere.
- Do not change the shared-memory layout, command IDs or `FTHR_SharedMemory_v4`.
- `FTHR_UI/main.py` edits are surgical: the UI overhaul will replace widgets, so touch only the engine-connect path and the hotkey message strings named below.
- Python style: match surrounding code; comments give reasons, not narration; `ruff check .` clean; `python -m compileall -q FTHR_UI tests tools` clean.
- Tests that import `main` must not launch the engine or write under `~/.fthr`; follow the fixtures in `tests/test_background_ui_pause.py` (monkeypatched settings dir) exactly.

## Review Focus

1. Engine restart while a previous connect worker is still polling: the generation check must keep the old worker from killing the new engine, and the new rule must not block the new worker's own connect. Pinned in Task 1's test with two generations.
2. A machine with Plasma packages installed but a Sway or GNOME session: must not be classified as `kwin`. Pinned in Task 2.
3. A user whose `hyprland.conf` already has hand-written binds using the current socket path: the source line must not be appended a second time. Pinned in Task 3.
4. Uploader settings file that already exists with 0644 from an older build: the next write must leave it 0600. Pinned in Task 4.
5. Hyprland with `xdg-desktop-portal-hyprland` answering GlobalShortcuts: the portal binds nothing unless the user adds `global` dispatcher lines, so the socket path must stay the Hyprland default. Pinned in Task 6's decision and test.

---

## PR D: engine-facing core fixes (Tasks 1, 2, 4; branch `fix/linux-ui-core-hardening`)

### Task 1: The status poll must not replace the bridge while a connect worker owns it

**Files:**
- Modify: `FTHR_UI/main.py` around line 5342 (`start_engine`, where `_poll_connect` is started) and around line 7884 (`_update_status` reconnect block)
- Test: `tests/test_engine_connect_race.py` (new)

**Interfaces:**
- Produces: `MainWindow._engine_connect_thread: threading.Thread | None`, set in `start_engine` to the `fthr-engine-connect` thread, and `MainWindow._connect_worker_active() -> bool` returning `thread is not None and thread.is_alive()`.

Background: `CaptureBridge` is a process-wide singleton. `_update_status` runs on the Qt thread every 500 ms; while the engine is not yet connected, every third tick it calls `self.bridge.shutdown()`, replaces `self.bridge` and calls `initialize()`. The `fthr-engine-connect` worker thread is calling `self.bridge.initialize()` on the same singleton at the same time. Both run `_initialize_linux`, which closes and recreates the mmap; the 2026-10-11 log shows the worker dying with `BufferError` and the status poll "reconnecting" a moment later.

- [ ] **Step 1: Write the failing test.** Model on `tests/test_background_ui_pause.py` for how to build a `MainWindow` offscreen without an engine. Then:

```python
import threading
import time


def test_status_poll_leaves_bridge_alone_while_connect_worker_runs(window, monkeypatch):
    # A bridge that records every shutdown/initialize call.
    calls = []

    class FakeBridge:
        def shutdown(self): calls.append('shutdown')
        def initialize(self): calls.append('initialize'); return False
        def is_connected(self): return False

    monkeypatch.setattr('main.CaptureBridge', FakeBridge)
    window.bridge = FakeBridge()
    window.engine_process = type('P', (), {'poll': lambda self: None})()
    window._reconnect_counter = 0

    gate = threading.Event()
    worker = threading.Thread(target=gate.wait, name='fthr-engine-connect', daemon=True)
    worker.start()
    window._engine_connect_thread = worker
    try:
        for _ in range(4):           # covers the "% 3 == 1" reconnect tick
            window._update_status()
        assert calls == []            # the worker owns the bridge
    finally:
        gate.set(); worker.join(1)

    window._update_status()           # worker finished: reconnect resumes
    assert 'initialize' in calls


def test_worker_generation_check_still_protects_a_newer_engine(window):
    # Pre-existing behaviour, pinned so Task 1 cannot regress it: a worker
    # from generation N returns without touching a generation N+1 engine.
    window._engine_gen = 2
    assert window._connect_worker_active() is False
```

Adjust the fixture name to whatever `tests/test_background_ui_pause.py` provides (it constructs the window; reuse that fixture or copy it).

- [ ] **Step 2: Run, verify failure** (`_connect_worker_active` missing / `calls` non-empty).
- [ ] **Step 3: Implement.** In `start_engine`, replace `threading.Thread(target=_poll_connect, daemon=True, name='fthr-engine-connect').start()` with:

```python
            self._engine_connect_thread = threading.Thread(
                target=_poll_connect, daemon=True, name='fthr-engine-connect')
            self._engine_connect_thread.start()
```

Add the helper near `_update_status`:

```python
    def _connect_worker_active(self) -> bool:
        # The connect worker calls initialize() on the singleton bridge; the
        # status poll must not shut that bridge down underneath it.
        thread = getattr(self, '_engine_connect_thread', None)
        return thread is not None and thread.is_alive()
```

In `_update_status`, change the reconnect condition to:

```python
            if (self.engine_process is not None
                    and not self._connect_worker_active()
                    and self._reconnect_counter % 3 == 1):
```

Initialise `self._engine_connect_thread = None` wherever `_engine_gen` is first initialised in `__init__` (grep `_engine_gen = ` in `__init__`; if it is only created lazily with `getattr`, add the attribute in `__init__` next to `self.bridge = CaptureBridge()` at ~line 5536's constructor equivalent).

- [ ] **Step 4: Run the new test and `tests/test_background_ui_pause.py tests/test_capture_bridge.py tests/test_save_state.py`.** Expected: pass.
- [ ] **Step 5: Commit** `fix(linux): status poll leaves the bridge alone while the connect worker owns it`.

### Task 2: Compositor detection order

**Files:**
- Modify: `FTHR_UI/core/compositor.py`
- Test: `tests/test_compositor_detection.py` (new)

**Interfaces:**
- Produces: `detect_compositor()` unchanged signature; `_reset_cache_for_tests()` helper that sets the module cache to `None`.

- [ ] **Step 1: Failing test:**

```python
import shutil
from core import compositor


def _detect(monkeypatch, env, which=lambda name: None):
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP',
                'XDG_SESSION_DESKTOP', 'HYPRLAND_INSTANCE_SIGNATURE', 'GNOME_DESKTOP_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(shutil, 'which', which)
    compositor._reset_cache_for_tests()
    return compositor.detect_compositor()


def test_gnome_session_with_plasma_installed_is_gnome(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'GNOME'},
                   which=lambda name: '/usr/bin/kwin_wayland') == 'gnome'


def test_sway_session_with_plasma_installed_is_unknown_not_kwin(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-1', 'XDG_CURRENT_DESKTOP': 'sway'},
                   which=lambda name: '/usr/bin/kwin_wayland') == 'wayland-unknown'


def test_kde_env_wins_before_any_binary_probe(monkeypatch):
    assert _detect(monkeypatch, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CURRENT_DESKTOP': 'KDE'}) == 'kwin'


def test_xdg_session_type_wayland_counts_as_wayland_even_without_display_var(monkeypatch):
    assert _detect(monkeypatch, {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'wayland',
                                 'XDG_CURRENT_DESKTOP': 'Hyprland',
                                 'HYPRLAND_INSTANCE_SIGNATURE': 'x'}) == 'hyprland'


def test_pure_x11(monkeypatch):
    assert _detect(monkeypatch, {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11'}) == 'x11'
```

- [ ] **Step 2: fails. Step 3: implement:** `has_wayland = bool(WAYLAND_DISPLAY) or XDG_SESSION_TYPE == 'wayland'`; move the `shutil.which('kwin_wayland')` probe after the GNOME check (keep it as the last resort before `wayland-unknown`); add `def _reset_cache_for_tests(): global _COMPOSITOR; _COMPOSITOR = None`. Keep `x11_monitor.is_native_x11_session` as is (it already uses `XDG_SESSION_TYPE`); the two now agree.
- [ ] **Step 4: pass; also run `tests/test_x11_monitor.py tests/test_linux_alpha_runtime_policy.py`. Step 5: commit** `fix(linux): detect the compositor from the session before probing for kwin_wayland`.

### Task 4: Uploader secrets file is owner-only

**Files:**
- Modify: `FTHR_UI/core/upload_manager.py:176-183` (`_atomic_json`)
- Test: `tests/test_upload_manager_retry.py` (append)

- [ ] **Step 1: Failing test:**

```python
import os, stat
from core import upload_manager


def test_atomic_json_writes_owner_only(tmp_path):
    target = tmp_path / 'settings.json'
    target.write_text('{}')
    os.chmod(target, 0o644)                       # an older build's file
    upload_manager._atomic_json(target, {'catbox_userhash': 'secret'})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (tmp_path / 'settings.json.tmp').exists()
```

- [ ] **Step 2: fails. Step 3: implement:** open the temp file with `os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`, write through `os.fdopen(fd, 'w', encoding='utf-8')`, then `os.chmod(temporary, 0o600)` (umask may have widened it on create) and `os.replace`. Comment: the file holds `catbox_userhash` and the auth header. On Windows `chmod` is a no-op for these bits; fine.
- [ ] **Step 4: pass. Step 5: commit** `fix(linux): write uploader settings and receipts owner-only`.

After Tasks 1, 2, 4: push `fix/linux-ui-core-hardening`, open PR D to `linux`.

## PR E: Hyprland binds (Task 3; branch `fix/linux-hyprland-binds`)

### Task 3: Binds for every socket action, no redundant reload, current socket name

**Files:**
- Modify: `FTHR_UI/core/hotkey_manager.py:1319-1440`
- Test: `tests/test_hyprland_binds.py` (new; reuse the stand-in pattern from `tests/test_hotkey_config_migration.py`, which already exercises `_write_hyprland_lua_config` with a temp path)

**Interfaces:**
- Produces: `HotkeyManager._hyprland_bind_actions() -> list[str]`: the socket actions that have a non-empty key, in the fixed order `save_clip, save_screenshot, start_recording, stop_recording, confirm_game_detection, dismiss_game_detection`. `_write_hyprland_config()` and `_write_hyprland_lua_config()` return `bool`: `True` when the file content changed. `_apply_compositor_config()` runs `hyprctl reload` only when a write returned `True`.

- [ ] **Step 1: Failing tests:**

```python
def test_conf_includes_every_bound_socket_action_and_skips_empty(manager, tmp_path, monkeypatch):
    manager.hotkeys.update({'save_clip': 'F9', 'save_screenshot': '',
                            'confirm_game_detection': 'F7', 'dismiss_game_detection': 'F8'})
    monkeypatch.setattr(hotkey_manager, '_FTHR_HYPR_CONF', tmp_path / 'fthr-hotkeys.conf')
    assert manager._write_hyprland_config() is True
    text = (tmp_path / 'fthr-hotkeys.conf').read_text()
    assert 'confirm_game_detection' in text and 'dismiss_game_detection' in text
    assert 'save_screenshot' not in text          # empty key → no bind line
    assert 'bind = , ,' not in text
    assert manager._write_hyprland_config() is False   # unchanged → no reload


def test_source_line_not_duplicated_when_user_bound_current_socket(manager, tmp_path, monkeypatch):
    conf = tmp_path / 'hyprland.conf'
    conf.write_text(f'bind = , F9, exec, echo -n save_clip | nc -U {manager.socket_path}\n')
    monkeypatch.setattr(hotkey_manager, '_HYPR_CONF', conf)
    manager._ensure_hyprland_source()
    assert 'source =' not in conf.read_text()
```

(Use whatever attribute exposes the socket path; grep `sock_path`/`socket_path` in `hotkey_manager.py` and use the real one.)

- [ ] **Step 2: fail. Step 3: implement:**
  - `_hyprland_bind_actions()` as specified; `_build_bind_line` unchanged.
  - `_write_hyprland_config`: build `lines` from `_hyprland_bind_actions()`; read the existing file if present; if identical, return `False` without writing; else write atomically as now and return `True`.
  - `_write_hyprland_lua_config`: same change (compare the new full text with `existing` before writing; return `bool`).
  - `_ensure_hyprland_source`: the skip condition becomes `'fthr-hotkeys.conf' in content or 'fthr_hotkey.sock' in content or 'hotkey.sock' in content`.
  - `_apply_compositor_config`: `changed = self._write_hyprland_lua_config() if ... else (self._write_hyprland_config() or False); self._ensure_hyprland_source()` and only start the reload thread when `changed`. Fix the comment that claims "without a full reload": it is a full reload, run only when the file changed.
- [ ] **Step 4: pass; run `tests/test_hotkey_config_migration.py tests/test_linux_tools.py tests/test_linux_runtime.py` too. Step 5: commit** `fix(linux): Hyprland binds cover every socket action and reload only when they change`. Push, open PR E.

## PR F: GlobalShortcuts portal hotkeys (Tasks 5 and 6; branch `feat/linux-portal-hotkeys-rebased`)

### Task 6: Rebase #16 and decide the Hyprland path

Facts from the trial rebase: skip the contributor's commit `70254fa` (duplicate of the BufferError fix already on `linux`); cherry-pick `8109a57` then `6d8ead3`. Conflicts: `README.md` (keep linux's ScreenCast bullets, take the PR's netcat wording), `RELEASE_NOTES.md` (keep both new sections), `build_linux.sh` (keep linux's 69-column banner, replace only the hotkey lines; the AppRun systemd-run block auto-merges but see below).

- [ ] **Step 1:** `git fetch origin pull/16/head:pr16`; branch from `origin/linux`; cherry-pick the two commits, resolving as above. Keep the contributor's authorship.
- [ ] **Step 2:** Drop the AppRun `systemd-run` scope block if it is in the diff: it has no test, the docs that mention it are being removed in Phase 3, and a scope re-exec changes how the engine process is reaped. Note the removal in the PR body.
- [ ] **Step 3: Hyprland decision.** On this machine run `hyprctl globalshortcuts` and start the app; observe whether `portal_shortcuts.is_available()` returns true. `xdg-desktop-portal-hyprland` implements GlobalShortcuts but binds no keys by itself: the user must add `bind = , F9, global, <appid>:<shortcut_id>` lines. Therefore in `hotkey_manager._ensure_portal()` (the PR's lazy portal start), return early when `detect_compositor() == 'hyprland'` so the socket path and the generated binds remain the Hyprland default. Add a test in `tests/test_portal_shortcuts.py` that `_ensure_portal` does not construct `PortalShortcuts` when the compositor is `hyprland`.
- [ ] **Step 4: Messaging (Task 5).** With #16 in, the portal is the default off-Hyprland. Make `_warn_if_linux_hotkeys_dead` say: "Global hotkeys use the desktop's shortcut portal; no root access or input group is needed. If your desktop has no GlobalShortcuts portal, bind a key in <where> to this command: ..." Remove `_warn_input_group` and its `QTimer.singleShot(1500, ...)` call in `main.py` (the PR already does this; confirm). Change the `build_linux.sh` success banner's two hotkey lines to "Global hotkeys: desktop portal, or Hyprland binds (auto)". `docs/linux.md` Hotkeys section: describe portal first, Hyprland binds second, manual socket binds last.
- [ ] **Step 5:** Run `tests/test_portal_shortcuts.py tests/test_hotkey_config_migration.py tests/test_alpha_ui_truth.py tests/test_capture_bridge.py tests/test_hyprland_binds.py`, `tools/verify_exception_handling.py`, `ruff`. Physical on Hyprland: launch the app, confirm the socket path is used (log line `[Hotkey] Socket server listening`), F9 saves a clip, F12 saves a screenshot. Then force the portal path once (`FTHR_FORCE_PORTAL_SHORTCUTS=1` env read in `_ensure_portal`, test-only) to confirm it binds through `xdg-desktop-portal-hyprland` without error.
- [ ] **Step 6:** Push, open PR F with the contributor credited, merge after CI. Close #16 with a comment pointing at the merged PR.

## Execution

PR D and PR E are independent of each other and of Phase 1; implement in parallel worktrees. PR F after D and E merge (it touches `hotkey_manager.py` like E). Fresh reviewer per PR before merge.
