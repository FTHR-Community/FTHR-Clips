# FTHR Clips on Linux

This page documents the Linux path in the upstream `linux` branch. It describes
what is supported, how to install the AppImage, how capture/audio/encoding work,
and how to diagnose the common setup problems.

## Supported path

The currently qualified path is:

- x86_64 Linux
- native Wayland desktop
- Hyprland, tested with `wlr-screencopy`
- PipeWire with PulseAudio compatibility, or PulseAudio
- AMD Radeon hardware using VA-API when the Mesa driver exposes the encoder

Other compositors, distributions, GPUs, X11 sessions, and ARM systems may work,
but are not covered by this qualification. Do not treat a headless, WSL, or ARM
build as proof that graphical capture works on a native x86_64 desktop.

## Install the AppImage

Download the latest Linux pre-release from the [GitHub release page](https://github.com/FTHR-Community/FTHR-Clips/releases/tag/v1.1.0-alpha):

- [FTHRClips-1.1.0-alpha-x86_64.AppImage](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.0-alpha/FTHRClips-1.1.0-alpha-x86_64.AppImage)
- [SHA-256 checksum](https://github.com/FTHR-Community/FTHR-Clips/releases/download/v1.1.0-alpha/FTHRClips-1.1.0-alpha-x86_64.AppImage.sha256)

Then run:

```bash
chmod +x FTHRClips-1.1.0-alpha-x86_64.AppImage
./FTHRClips-1.1.0-alpha-x86_64.AppImage
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
- `grim` for screenshots
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
screen, and screenshots are a separate path (`grim`) that the portal backend
does not provide.

On `wlr-screencopy` and `ext-image-copy-capture` compositors the selected
output and scaling settings are controlled from the application. The KDE
portal path uses the desktop's source chooser instead.
For a compositor or output that is not detected correctly, run the diagnostic
commands below and include their redacted output in a support report.

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
