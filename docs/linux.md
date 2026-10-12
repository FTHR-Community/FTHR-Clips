# FTHR Clips on Linux

This page documents the Linux path in the upstream `linux` branch. It describes
what is supported, how to install the AppImage, how capture/audio/encoding work,
and how to diagnose the common setup problems.

## Supported path

The currently qualified path is:

- x86_64 Linux AppImage
- native Wayland desktop
- Hyprland, tested with `wlr-screencopy`
- KDE Plasma/KWin, tested through the ScreenCast portal and PipeWire
- PipeWire with PulseAudio compatibility, or PulseAudio
- AMD Radeon hardware using VA-API when the Mesa driver exposes the encoder

This is a narrow qualification, not a promise of universal Linux support:

- GNOME, X11 sessions, NVIDIA and Intel hardware encoding, ARM, and other
  distributions remain unqualified unless separately tested.
- KDE/KWin video capture requires an available ScreenCast portal backend,
  PipeWire, and D-Bus; the portal chooses the desktop output.
- A headless, WSL, or ARM build does not qualify graphical capture on native
  x86_64 hardware.

## Install the AppImage

Download the current Linux pre-release from the [GitHub release page](https://github.com/FTHR-Community/FTHR-Clips/releases/tag/v1.1.2-alpha):

- [FTHRClips-1.1.2-alpha-x86_64.AppImage](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.2-alpha/FTHRClips-1.1.2-alpha-x86_64.AppImage)
- [SHA-256 checksum](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.2-alpha/FTHRClips-1.1.2-alpha-x86_64.AppImage.sha256)

Then verify and run:

```bash
sha256sum -c FTHRClips-1.1.2-alpha-x86_64.AppImage.sha256
chmod +x FTHRClips-1.1.2-alpha-x86_64.AppImage
./FTHRClips-1.1.2-alpha-x86_64.AppImage
```

If FUSE is unavailable:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./FTHRClips-<version>-x86_64.AppImage
```

The Linux package is x86_64-only. Keep the AppImage and its published SHA-256
file together when verifying a download.

## Runtime requirements

The AppImage carries the application and its pinned FFmpeg runtime. The desktop
session still needs these system services and helpers:

- Wayland compositor exposing `wlr-screencopy` or `ext-image-copy-capture`,
  or an `org.freedesktop.portal.ScreenCast` backend (KDE Plasma/KWin:
  `xdg-desktop-portal-kde`) with PipeWire and D-Bus
- PipeWire with PulseAudio compatibility, or PulseAudio
- `grim` for direct, selected-output screenshots on supported compositors,
  or an `org.freedesktop.portal.Screenshot` backend for compositor-selected
  screenshots (see below)
- `nc`/netcat for compositor hotkey commands
- Mesa VA-API drivers and access to `/dev/dri/renderD*` for AMD hardware encoding

For Arch Linux, a useful starting point is:

```bash
sudo pacman -S wayland wayland-protocols pipewire pipewire-pulse \
  grim openbsd-netcat libva-mesa-driver mesa-utils
```

Package names differ on Debian, Ubuntu, Fedora, and other distributions. Install
the equivalent Wayland, PipeWire/PulseAudio, screenshot, netcat, and VA-API
packages from that distribution.

## Capture

On Wayland, FTHR tries `wlr-screencopy` first and
`ext-image-copy-capture` second. Compositors that advertise neither (KWin) are
captured through the `org.freedesktop.portal.ScreenCast` portal: the desktop
shows its screen picker once, FTHR keeps the returned restore token in
`~/.fthr/portal_screencast_token` (owner-only) so later starts need no dialog,
and the frames arrive over PipeWire as memfd buffers. On KDE Plasma, pressing
`SOURCE` removes that token and restarts the engine so the KDE monitor picker
opens again; the selected monitor becomes the capture source. The picker, not
the app's monitor setting, decides the source. Declining it stops the engine
and the app shows that reason. Linux portal capture is desktop-output only.

The portal path needs, at runtime, `xdg-desktop-portal` with a ScreenCast
backend for the desktop (`xdg-desktop-portal-kde` on Plasma), PipeWire
(`libpipewire-0.3.so.0`) and D-Bus (`libdbus-1.so.3`). None of them is
bundled: the engine loads the two libraries only when it takes that path and
otherwise reports why it could not. Its limits: the picker appears once per
app identity (a source build started from a terminal is not the packaged
app), the app's monitor selection is advisory because the portal picks the
screen. Screenshots are a separate operation, not a frame read from this
ScreenCast session.

On `wlr-screencopy` and `ext-image-copy-capture` compositors the selected
output and scaling settings are controlled from the application. The KDE
portal path uses the desktop's source chooser instead.
For a compositor or output that is not detected correctly, run the diagnostic
commands below and include their redacted output in a support report.

### Screenshots

On supported Wayland compositors, FTHR runs `grim -o <selected-output>` to capture
only the configured replay monitor. If `grim` is missing, fails, or times out,
FTHR does **not** fall back to a Qt screen grab that can be empty on Wayland.
Instead the error bar offers **CHOOSE SCREENSHOT**. This opens the desktop's
Screenshot portal dialog; its source selection is independent of the replay
monitor. Check the selected screen/area in that dialog before approving it.

KDE and GNOME need a Screenshot portal backend for this path. FTHR imports only
the returned local PNG and publishes it through the normal screenshot save
path; it does not delete the portal's original image. Canceling or an
unavailable/timed-out portal reports a failure, not a successful black image.
The offer expires after 60 seconds, the portal method has a 5-second deadline,
and the desktop dialog has a 60-second deadline. X11 and Windows retain their
existing selected-screen capture paths.

These changes need native compositor testing before claiming KDE/GNOME
screenshot qualification. An isolated D-Bus test checks protocol handling, not
screen capture on a real desktop.

## Audio

FTHR records desktop audio from the default PulseAudio sink's monitor source.
This works with PipeWire's PulseAudio compatibility layer. The default
microphone/source is not used as desktop audio.

If the audio service cannot be opened, video capture may continue without audio.
Check the service and default sink with:

```bash
pactl info
pactl list short sinks
pactl list short sources
```

Microphone recording uses a separate input stream. When changing **Settings →
Audio → Microphone**, FTHR stops the old level meter and monitoring stream,
opens the recorder on the selected input, and then lets the meter share that
recorder. This avoids opening a second input before the recorder on exclusive
ALSA devices. The selected input name is saved, rather than a transient device
index; after enumeration changes FTHR resolves a unique input with that name.
A missing or ambiguous saved name falls back to System Default with a log
message. This does not change desktop-audio selection, gain, or timing.

### Camera refresh

A running Linux camera is retained in the device list without opening it again
for a probe. Refreshing the list does not emit intermediate selection changes
that could restart the recorder. Failed readers release their capture and
cannot start using a later recorder generation. Other camera indices still
require synchronous OpenCV probes; a slow or stuck driver can still delay the
refresh. These fixes do not establish camera/audio synchronization.

## AMD VA-API encoding

Linux `auto` mode prefers these VA-API encoders when they are available:

- `h264_vaapi`
- `hevc_vaapi`
- `av1_vaapi`

The default render node is `/dev/dri/renderD128`. On systems with another node:

```bash
FTHR_VAAPI_DEVICE=/dev/dri/renderD129 ./FTHRClips-<version>-x86_64.AppImage
```

Use `vainfo` to inspect the driver and encode profiles:

```bash
vainfo
ls -l /dev/dri/renderD*
```

If VA-API initialization fails, the application falls back to software encoding.
That fallback keeps recording available but does not qualify hardware encoding.
The current Linux qualification does not claim NVIDIA NVENC or Intel hardware
encoding.

## Hotkeys

The defaults are:

- **F9**: save clip
- **F12**: save screenshot

Linux hotkeys use a private owner-only Unix socket in the user's runtime
 directory. The application generates Hyprland bindings when its hotkeys are
configured.

### Traditional Hyprland configuration

For a normal `hyprland.conf` setup, FTHR writes its generated
`fthr-hotkeys.conf` and adds a source entry when needed. Change the keys in
**Settings → Hotkeys**, then reload Hyprland if the new bindings are not active.

If a source entry is not already present, the relevant line is:

```ini
source = ~/.config/hypr/fthr-hotkeys.conf
```

Use the absolute path printed by FTHR when your home directory differs.

### Lua-based Hyprland configuration

Lua-based setups use the FTHR-managed block in the loaded Lua keybind file.
Updates replace only the explicit FTHR begin/end block and preserve all other
user configuration. An incomplete managed block is left untouched rather than
truncating the file.

Confirm the compositor has loaded the bindings:

```bash
hyprctl binds | grep -i fthr
```

### Other compositors

KDE, GNOME, and other compositors do not automatically import Hyprland binds.
Use the exact command displayed in **Settings → Hotkeys** with that compositor's
global shortcut facility. Root access and the `input` group are not required.

## Clip locations

The default library is:

```text
~/FTHR_Clips/
├── <game-or-window-name>/   # saved replay clips
├── Desktop/                 # desktop captures
└── Recordings/              # manual recordings
```

The library and recording directories can be changed in Settings. Application
settings and runtime state are stored under `~/.fthr/`, separate from the
AppImage, so updating the application does not remove the user's clips or
settings.

## Uploading

The Linux AppImage now contains the optional Linux uploader bundle. Uploading is
not automatic: enable it in Settings and complete the required consent and
provider terms first. The uploader remains a separate subprocess from the main
application and only receives approved clip paths through the existing boundary.

Catbox and custom-server providers do not require the separate hardware-identity
component. Lustful hardware-bound account and upload flows remain unavailable on
Linux until a Linux hardware-identity component is implemented and qualified.
Never put credentials in bug reports, Discord, logs, or documentation.

## Troubleshooting

### AppImage does not launch

Try the extraction fallback and verify the executable bit:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./FTHRClips-<version>-x86_64.AppImage
file ./FTHRClips-<version>-x86_64.AppImage
```

### No video

Check that the process is running inside the graphical user's session, not a
headless SSH shell. Confirm `WAYLAND_DISPLAY`, the compositor, and the selected
output. WSL and remote shells do not provide the same capture environment as the
desktop session.

### No audio

Check PipeWire/PulseAudio and the default sink monitor with `pactl info` and
`pactl list short sources`. FTHR needs the output monitor source for desktop
audio; a working microphone does not prove desktop audio is configured.

### Hotkeys do nothing

Check `hyprctl binds`, reload/restart Hyprland, and confirm the generated file is
part of the active configuration. Do not assume that a generated file is loaded
just because it exists on disk.

### Upload extension unavailable

Use a current Linux AppImage containing `FTHR-Uploader-linux.fthrplugin`. Then
enable uploading and complete consent in Settings. If Catbox or custom-server
uploading still fails, check the uploader status and provider connection without
sharing credentials. Lustful hardware-bound uploading is intentionally not
available on Linux.

## Diagnostics

`FTHR_CAPTURE_BACKEND` forces one capture backend and disables the usual
fallback order, so a problem can be reproduced on a specific path:

```bash
FTHR_CAPTURE_BACKEND=ext ./FTHRClips-<version>-x86_64.AppImage
```

Accepted values are `wlr` (wlr-screencopy), `ext` (ext-image-copy-capture),
`portal` (ScreenCast portal over PipeWire) and `x11` (native x11grab). A forced
backend that cannot start fails capture with that backend's reason instead of
trying another; an unknown value is logged and ignored.

When capture is interrupted, the engine retries the backend that last
delivered frames rather than running the first-time selection again, so a
compositor stall never reaches the portal's screen picker. A compositor that
stops answering, or a selected output that disappears, is waited for every two
seconds for as long as the app runs, and the replay history is kept whenever
only the capture backend had to restart. The status bar shows what is being
waited for.

## Redacted support reports

Run these commands as the desktop user and do not use `sudo`:

```bash
bash tools/linux_system_report.sh > linux-system-report.txt
bash tools/linux_appimage_report.sh ./FTHRClips-<version>-x86_64.AppImage \
  > appimage-report.txt
```

These reports are designed to omit private configuration values. Review them
before posting and remove any paths or metadata you do not want to share.

For source builds, pinned FFmpeg setup, native tests, and AppImage packaging,
see [`../BUILDING.md`](../BUILDING.md).
