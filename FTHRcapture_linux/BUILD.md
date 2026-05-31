# FTHR Clips Linux Engine — Build Guide

## Requirements

```bash
# Arch Linux
sudo pacman -S cmake gcc ffmpeg wayland wayland-protocols \
               libpulse pkg-config

# Ubuntu 22.04+
sudo apt install cmake g++ libavcodec-dev libavformat-dev \
                 libavutil-dev libavdevice-dev libswscale-dev \
                 libswresample-dev libwayland-dev \
                 wayland-protocols libpulse-dev pkg-config
```

## Build

```bash
cd FTHRcapture_linux
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

The binary is at `build/FTHRclips`.

## Wayland Permissions

No special permissions needed — wlr-screencopy works without root on
wlroots-based compositors (Hyprland, Sway, etc.).

For audio loopback, ensure your user is in the `audio` group:

```bash
sudo usermod -aG audio $USER
```

PipeWire's PulseAudio compatibility layer works out of the box on Arch Linux.
The engine captures from the default monitor source (system audio loopback).
To select a specific monitor source, set the `PULSE_SOURCE` environment variable
before launching:

```bash
PULSE_SOURCE=alsa_output.pci-0000_00_1f.3.analog-stereo.monitor ./FTHRclips ...
```

## Running

The Python UI (`FTHR_UI/main.py`) automatically detects the OS and launches
the correct engine binary. On Linux it looks for:

```
FTHRcapture_linux/build/FTHRclips
```

You can also run the engine manually for testing:

```bash
./build/FTHRclips 60 30 0 0 16000 0 0 0 0
#                 fps buf w  h  kbps  mb mode hwnd scaling
```

## Notes

- Screen capture: wlr-screencopy protocol (Wayland, Hyprland/Sway/wlroots)
- Encoding: auto-detects h264_nvenc -> h264_amf -> h264_qsv -> libx264
- Audio: PulseAudio simple API monitor source (PipeWire compat works)
- Shared memory: POSIX shm_open, visible at `/dev/shm/FTHR_SharedMemory_v1`
- X11 / GNOME (Mutter) are NOT supported — wlr-screencopy is wlroots-specific
