# Cursor capture and scaling validation — 2026-11-07

## Failure and correction

The supplied diagnostic session 0b473fbe recorded a correctly resolved physical
ASUS monitor on NVIDIA, followed by E_INVALIDARG from
CreateVideoProcessorInputView while scaling 2560x1440 to 1920x1080.
The owned DXGI background had only SHADER_RESOURCE binding, unlike the cursor
compositor output, which also had RENDER_TARGET binding. Hidden/off-screen cursor
frames could consequently fail in the scaler. The background now supports both
uses, with regression checks for the cursor-dependent texture paths.

## Scope

Capture cursor defaults on and is persisted through UI configuration, restart
snapshots, diagnostics and native arguments. Off uses WGC on Windows to avoid
DXGI's potentially embedded system pointer. Linux X11, wlr and ext capture had
hardcoded cursor exclusion; these now honor the preference, as does the portal.

## Minecraft qualification still required

There is no Minecraft recording or exact game/version/backend reproduction in
the supplied bundle. Do not claim a verified Minecraft fullscreen fix from the
scaling regression or synthetic pointer checks alone. Verify Java and Bedrock
as applicable, windowed/borderless/fullscreen, inventory/menu pointer visibility,
mouse movement between the captured and secondary display, and hidden-pointer
camera control. Repeat with Capture cursor enabled and disabled. Game-rendered
crosshairs should remain; no synthetic OS pointer should appear during mouse lock.

## Linux local validation

Development engine built with X11, wlr and ext backends. Five executable CLI
checks passed: legacy default, off after replay flags, on before replay flags,
invalid value, missing value. CTest: 16 passed, one VAAPI hardware skip, one
encoder timestamp failure because this WSL FFmpeg lacks a usable libopenh264
encoder. Portal compile validation was unavailable because PipeWire/D-Bus
headers are absent. This development build is not a distribution artifact.

## Windows and UI validation

Windows Release solution built successfully. Focused native execution passed
110 DXGI cursor checks and 17 backend-policy checks, including rejection of the
original SRV-only texture and successful hardware 1440p-to-1080p video processing
for visible, hidden, off-screen and bypassed cursor states. Production executable
CLI checks accepted both values after replay flags and rejected invalid input.
Python UI/config/session coverage passed 103 tests. Settings were rendered and
inspected at widths 1100 and 1500. The Impeccable mechanical check returned no
findings. Shared-memory and engine response-publication contracts passed.

Live Windows Auto + cursor disabled smoke also passed: WGC_MONITOR on Intel
with h264_qsv captured 180 frames in three seconds and shut down cleanly.
Cursor property setter/readback succeeded before capture started. This checks
startup/frame delivery; it is not visual proof from a Minecraft recording.
No recording was saved. Evidence: sibling `cursor-native-smoke/run.log`.
