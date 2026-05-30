#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"

echo "=== FTHR Clips Linux AppImage Builder ==="
echo "Python: $(python3 --version)"
echo "GCC:    $(gcc --version | head -1)"
echo ""

# ── 1. Check hard requirements ─────────────────────────────────────────────
echo ">>> Checking dependencies..."
for cmd in cmake gcc pkg-config python3 wayland-scanner curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: '$cmd' not found."
        echo "  Arch:   sudo pacman -S cmake gcc wayland wayland-protocols ffmpeg libpulse pkg-config curl"
        echo "  Ubuntu: sudo apt install cmake g++ wayland-protocols libwayland-dev libavcodec-dev libavformat-dev libavutil-dev libavdevice-dev libswscale-dev libswresample-dev libpulse-dev pkg-config curl"
        exit 1
    }
done

pkg-config --exists libavcodec libpulse-simple wayland-client || {
    echo "ERROR: Missing C++ build deps (ffmpeg / pulseaudio / wayland)."
    echo "  Arch:   sudo pacman -S ffmpeg libpulse wayland"
    echo "  Ubuntu: sudo apt install libavcodec-dev libpulse-dev libwayland-dev"
    exit 1
}

python3 -c "import PyQt6, keyboard, cv2, imageio_ffmpeg, sounddevice, numpy" 2>/dev/null || {
    echo "ERROR: Missing Python dependencies."
    echo "Install: pip install -r requirements.txt"
    exit 1
}
echo "    All dependencies found."

# ── 2. Build C++ engine ────────────────────────────────────────────────────
echo ""
echo ">>> Building C++ capture engine..."
cd "$SCRIPT_DIR/FTHRcapture_linux"
cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null
cmake --build build -j"$(nproc)" 2>&1 | grep -E "^\[|error:" || true
echo "    Engine built: FTHRcapture_linux/build/FTHRclips"
cd "$SCRIPT_DIR"

# ── 3. Set up fresh AppDir ─────────────────────────────────────────────────
echo ""
echo ">>> Setting up AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/fthr-clips"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons"

# C++ engine
cp "$SCRIPT_DIR/FTHRcapture_linux/build/FTHRclips" "$APPDIR/usr/bin/FTHRclips"
chmod +x "$APPDIR/usr/bin/FTHRclips"

# Python app source
cp -r "$SCRIPT_DIR/FTHR_UI" "$APPDIR/usr/share/fthr-clips/FTHR_UI"

# AppImage metadata
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/usr/share/applications/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/usr/share/icons/"
cp "$SCRIPT_DIR/AppDir/AppRun"             "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# ── 4. Download appimagetool ───────────────────────────────────────────────
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L --progress-bar -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# ── 5. Build AppImage ──────────────────────────────────────────────────────
echo ""
echo ">>> Building AppImage..."
OUTPUT="$SCRIPT_DIR/FTHRClips-1.0.0-alpha-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT" 2>&1 | grep -v "^Please consider\|appimage.github"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║           FTHR Clips AppImage Ready!             ║"
echo "╠══════════════════════════════════════════════════╣"
printf "║  Output: %-40s║\n" "FTHRClips-1.0.0-alpha-x86_64.AppImage"
printf "║  Size:   %-40s║\n" "$(du -sh "$OUTPUT" | cut -f1)"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Requirements on target system:                  ║"
echo "║    python3, PyQt6, keyboard, cv2, sounddevice    ║"
echo "║                                                  ║"
echo "║  For global hotkeys (one-time setup):            ║"
echo "║    sudo usermod -aG input \$USER                  ║"
echo "║    (then log out and back in)                    ║"
echo "╚══════════════════════════════════════════════════╝"
