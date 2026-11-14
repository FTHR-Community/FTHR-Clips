# Minecraft source-switch recovery — 2026-11-07

The live session 521cafb2 began with healthy desktop capture and completed three
clip saves. The requested Minecraft HWND 0x1970ac6 matched Better Minecraft
[FORGE] 1.20.1 (GLFW30, the configured Java runtime). Native window capture
refused startup because the window was minimized and its client area was 0x0.
This is startup readiness, not a failed custom-game match.

The Python heartbeat could process an exited startup process while its handshake
worker still owned failure reporting. Admission teardown then invalidated the
worker's cleanup receipt, leaving capture blocked instead of allowing a later
Desktop request. Regression coverage must exercise the heartbeat both before the
worker and before its queued callback, then connect a replacement Desktop engine.

Window identity must survive minimize/pause. A separate startup-readiness probe
checks visibility, minimized state and nonempty client area; it must not replace
identity lookup with a renderability test. Source switching should defer before
stopping a working engine, then retry when the selected game is restored.

Automatic game detection and manual source ownership are separate settings.
Adding a new custom game while Auto-Switch is enabled and Desktop is selected
now authorizes automatic selection as part of game setup. The rule update is
applied before stale or dismissed foreground observations are invalidated; a
fresh stable observation can then select the configured executable. Editing or
removing rules does not authorize switching, and adding a game preserves an
explicit manually selected window. Selecting Auto-Switch, Prompt First, or
enabling detection explicitly resumes automatic selection from that manual pin.

The header now says `Game detection: Paused` while manual source ownership blocks
switching. The popup explains the pause and offers the existing mode controls to
resume; paused mode controls do not appear active. No native monitor fallback
silently replaces a requested window target.

## Validation

198 startup/recovery, profile, shell, admission, lifecycle and save-state tests
passed; 25 detector tests passed. Live UI validation preserved Desktop engine
PID 25628 when minimized Minecraft was selected. Restoring the same game window
started WGC_WINDOW PID 26336 and reached HEALTHY at 15:23:16 local time. Selecting
Desktop then started DXGI PID 12820 and reached HEALTHY at 15:23:51. No test clips
were saved. Normal app shutdown completed in 0.513 seconds; the app was relaunched
to load the final disconnected-status refinement.

## Automatic setup follow-up

209 detector, popup, source-authority, admission and engine-recovery tests passed
with an isolated temporary home and log directory. New integration cases cover
adding the configured Minecraft-style Java executable from Desktop, invalidating
an old suppressed observation, requiring fresh stable foreground observations,
and dispatching the real source-switch coordinator. They also cover preserving a
manual window pin until Auto-Switch is selected, metadata/remove/disabled/prompt
changes retaining authority, and capture admission continuing to block an
unapproved executable. Popup renders were inspected in both paused and active
states at `ui-preview/detection-auto-recovery/detection-paused.png` and
`ui-preview/detection-auto-recovery/detection-active.png`.

These follow-up changes do not alter live preferences or restart the running
application. A fresh live auto-switch smoke check is still needed after loading
the updated UI.

A second live smoke exposed an additional recovery case: a restored game can
have the same HWND/process identity as the persisted selection even though its
startup failed and no capture process remains. Same-target metadata handling
now submits the ordinary capture restart coordinator when Auto-Switch is
authorized and the engine is absent or exited. It leaves a running process,
startup handshake, pending launch/apply/restart, manual pin and prompt-first mode
alone. Adopting the restored automatic identity also restores game-close
fallback bookkeeping. Nine regression cases cover missing/exited ownership and
all of those guards.

Live follow-up: Auto startup reset to Desktop; bringing Better Minecraft forward selected WGC_WINDOW automatically and reached HEALTHY at 15:36:49. Tabbing away paused the minimized fullscreen source; returning resumed frames and reached HEALTHY at 15:37:11. No manual source selection or new clip save was needed.

Final deployed verification: automatic Minecraft capture reached HEALTHY at 15:39:00. Fresh UI inspection confirmed the Minecraft source, Game detection: On, and Capturing after the committed-authority status refresh. Returned focus to Minecraft.
