#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"

echo "=== FTHR Clips Linux Self-Contained AppImage Builder ==="
echo "Python: $(python3 --version)"
echo "GCC:    $(gcc --version | head -1)"
echo ""

# ── 1. Check hard requirements ─────────────────────────────────────────────
echo ">>> Checking dependencies..."
for cmd in cmake gcc pkg-config python3 wayland-scanner curl pyinstaller; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: '$cmd' not found."
        [[ "$cmd" == "pyinstaller" ]] && echo "  Install: pip install pyinstaller --break-system-packages"
        exit 1
    }
done

pkg-config --exists libavcodec libpulse-simple wayland-client || {
    echo "ERROR: Missing C++ build deps (ffmpeg / pulseaudio / wayland)."
    echo "  Arch: sudo pacman -S ffmpeg libpulse wayland"
    exit 1
}

python3 -c "import PyQt6, keyboard, cv2, imageio_ffmpeg, sounddevice, numpy" 2>/dev/null || {
    echo "ERROR: Missing Python dependencies."
    echo "Install: pip install PyQt6 keyboard opencv-python-headless imageio-ffmpeg sounddevice numpy"
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

# ── 3. Bundle with PyInstaller ─────────────────────────────────────────────
echo ""
echo ">>> Bundling with PyInstaller (self-contained)..."
pyinstaller FTHR_linux.spec --clean --noconfirm 2>&1 | grep -E "^(INFO|WARNING|ERROR|Building)" || true

PYINST_DIR="$SCRIPT_DIR/dist/FTHRClips"
if [ ! -f "$PYINST_DIR/FTHRClips" ]; then
    echo "ERROR: PyInstaller output not found at $PYINST_DIR/FTHRClips"
    exit 1
fi
echo "    PyInstaller bundle ready: $(du -sh "$PYINST_DIR" | cut -f1)"

# ── 4. Strip unused libraries from PyInstaller bundle ─────────────────────
echo ""
echo ">>> Stripping unused libraries..."
STRIP_DIR="$PYINST_DIR/_internal"

_strip_glob() {
    find "$STRIP_DIR" -maxdepth 1 -name "$1" -delete 2>/dev/null && true
}

# VTK — 141 MB, pulled in by full OpenCV, completely unused
_strip_glob "libvtk*.so*"

# OpenCV modules we don't use (we only need core, imgproc, videoio)
for mod in dnn dnn_superres gapi ml objdetect stitching calib3d \
           features2d flann photo video highgui imgcodecs \
           alphamat aruco bgsegm bioinspired ccalib \
           face freetype fuzzy hdf hfs hfs img_hash \
           intensity_transform line_descriptor mcc optflow \
           phase_unwrapping plot quality rapid reg rgbd \
           saliency shape signal stereo structured_light \
           surface_matching text tracking viz wechat_qrcode \
           xfeatures2d ximgproc xphoto; do
    _strip_glob "libopencv_${mod}.so*"
done

# Qt Quick / QML — unused (we use Qt Widgets only)
_strip_glob "libQt6Quick*.so*"
_strip_glob "libQt6Qml*.so*"
_strip_glob "libQt6Quick3D*.so*"
_strip_glob "libQt6Pdf*.so*"
_strip_glob "libQt6WebEngine*.so*"
_strip_glob "libQt6Location*.so*"
_strip_glob "libQt6Positioning*.so*"
_strip_glob "libQt6VirtualKeyboard*.so*"

# HDF5 + protobuf (OpenCV ML / DNN deps, no longer needed)
_strip_glob "libhdf5*.so*"
_strip_glob "libprotobuf*.so*"

# Print savings
echo "    Done. Bundle size: $(du -sh "$PYINST_DIR" | cut -f1)"

# ── 5. Set up fresh AppDir ─────────────────────────────────────────────────
echo ""
echo ">>> Setting up AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR"

# Copy the entire PyInstaller onedir bundle into AppDir
cp -r "$PYINST_DIR/." "$APPDIR/"

# AppImage metadata
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/"

# Minimal AppRun — just exec the bundled binary
cat > "$APPDIR/AppRun" << 'APPRUN_EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"

if [ -n "$WAYLAND_DISPLAY" ]; then
    export QT_QPA_PLATFORM="wayland"
elif [ -n "$DISPLAY" ]; then
    export QT_QPA_PLATFORM="xcb"
fi

export PYTHONUNBUFFERED=1
exec "$HERE/FTHRClips" "$@"
APPRUN_EOF
chmod +x "$APPDIR/AppRun"

# ── 6. Download appimagetool ───────────────────────────────────────────────
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L --progress-bar -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# ── 7. Build AppImage ──────────────────────────────────────────────────────
echo ""
echo ">>> Building AppImage..."
OUTPUT="$SCRIPT_DIR/FTHRClips-1.0.0-alpha-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT" 2>&1 | grep -v "^Please consider\|appimage.github"

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         FTHR Clips Self-Contained AppImage Ready!           ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Output: %-52s║\n" "FTHRClips-1.0.0-alpha-x86_64.AppImage"
printf "║  Size:   %-52s║\n" "$SIZE"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Fully self-contained — no system Python deps required!     ║"
echo "║                                                              ║"
echo "║  For global hotkeys (one-time setup):                       ║"
echo "║    sudo usermod -aG input \$USER  (then re-login)            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
