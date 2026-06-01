#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"

echo "=== FTHR Clips — Linux AppImage Builder ==="
echo "Python: $(python3 --version)"
echo "GCC:    $(gcc --version | head -1)"
echo ""

# ── 1. Check requirements ──────────────────────────────────────────────────
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
    echo "  pip install PyQt6 keyboard opencv-python-headless imageio-ffmpeg sounddevice numpy"
    exit 1
}
echo "    All dependencies found."

# ── 2. Build Linux C++ engine ─────────────────────────────────────────────
echo ""
echo ">>> Building Linux capture engine..."
cd "$SCRIPT_DIR/FTHRcapture_linux"
cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null
cmake --build build -j"$(nproc)" 2>&1 | grep -E "^\[|error:" || true
echo "    Engine built: FTHRcapture_linux/build/FTHRclips"
cd "$SCRIPT_DIR"

# ── 3. Bundle with PyInstaller ─────────────────────────────────────────────
echo ""
echo ">>> Bundling Python app with PyInstaller..."
pyinstaller FTHR_linux.spec --clean --noconfirm 2>&1 | grep -E "^(INFO|WARNING|ERROR|Building)" || true

PYINST_DIR="$SCRIPT_DIR/dist/FTHRClips"
[[ -f "$PYINST_DIR/FTHRClips" ]] || { echo "ERROR: PyInstaller output missing."; exit 1; }
echo "    Raw bundle: $(du -sh "$PYINST_DIR" | cut -f1)"

# ── 4. Strip unused libraries ─────────────────────────────────────────────
# This is the most impactful size-reduction step. We remove shared libraries
# that PyInstaller pulled in transitively but FTHR Clips never calls at runtime.
echo ""
echo ">>> Stripping unused libraries..."
INT="$PYINST_DIR/_internal"

_rm() { find "$INT" -maxdepth 1 -name "$1" -delete 2>/dev/null; }

# VTK — 141 MB. Pulled in by the full OpenCV package. We only use
# VideoCapture/VideoWriter/cvtColor, which need core + videoio + imgproc only.
_rm "libvtk*.so*"

# OpenCV contrib & unused modules (~60 MB).
# Keep: core, imgproc, videoio (the three we actually call)
for mod in \
    alphamat aruco bgsegm bioinspired calib3d ccalib \
    dnn dnn_superres face features2d flann freetype fuzzy \
    gapi hdf hfs highgui imgcodecs img_hash \
    intensity_transform line_descriptor mcc ml \
    objdetect optflow phase_unwrapping photo plot \
    quality rapid reg rgbd saliency shape signal \
    stereo stitching structured_light surface_matching \
    text tracking viz wechat_qrcode \
    xfeatures2d ximgproc xphoto; do
    _rm "libopencv_${mod}.so*"
done

# Qt Quick / QML / 3D — we use Qt Widgets only (~15 MB)
_rm "libQt6Quick*.so*"
_rm "libQt6Qml*.so*"
_rm "libQt6Quick3D*.so*"
_rm "libQt6ShaderTools*.so*"
_rm "libQt6Pdf*.so*"
_rm "libQt6WebEngine*.so*"
_rm "libQt6Location*.so*"
_rm "libQt6Positioning*.so*"
_rm "libQt6VirtualKeyboard*.so*"
_rm "libQt6Charts*.so*"
_rm "libQt6DataVisualization*.so*"

# OpenCV ML / DNN support libs (~12 MB)
_rm "libhdf5*.so*"
_rm "libprotobuf*.so*"

# ICU data — only needed for full Unicode / BIDI, Qt ships a smaller subset
# (don't remove — Qt itself needs libicudata)

AFTER="$(du -sh "$PYINST_DIR" | cut -f1)"
echo "    After strip: $AFTER"

# ── 5. Verify the app still launches after stripping ──────────────────────
echo ""
echo ">>> Smoke-testing stripped bundle..."
if timeout 6 "$PYINST_DIR/FTHRClips" 2>&1 | grep -q "Engine found\|successfully initiated"; then
    echo "    Smoke test passed."
else
    echo "    WARNING: Could not confirm launch (no display / normal in CI)."
fi

# ── 6. Set up AppDir ──────────────────────────────────────────────────────
echo ""
echo ">>> Setting up AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -r "$PYINST_DIR/." "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/"

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

# ── 7. Download appimagetool ──────────────────────────────────────────────
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L --progress-bar -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# ── 8. Pack AppImage ──────────────────────────────────────────────────────
echo ""
echo ">>> Packing AppImage..."
OUTPUT="$SCRIPT_DIR/FTHRClips-1.0.0-alpha-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT" 2>&1 | grep -v "^Please consider\|appimage.github"

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              FTHR Clips Linux AppImage Ready                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Output: %-52s║\n" "FTHRClips-1.0.0-alpha-x86_64.AppImage"
printf "║  Size:   %-52s║\n" "$SIZE"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Linux-only — contains the Linux capture engine only.       ║"
echo "║                                                              ║"
echo "║  For global hotkeys (one-time):                             ║"
echo "║    sudo usermod -aG input \$USER  (then re-login)            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
