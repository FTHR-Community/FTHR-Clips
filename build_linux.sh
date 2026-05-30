#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"
PYTHON_VERSION="$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)"
PYTHON_BIN="$(which python3)"

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
echo "    All dependencies found."

# ── 2. Build C++ engine ────────────────────────────────────────────────────
echo ""
echo ">>> Building C++ capture engine..."
cd "$SCRIPT_DIR/FTHRcapture_linux"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$APPDIR/usr" > /dev/null
cmake --build build -j"$(nproc)" 2>&1 | grep -E "^\[|error:|warning:.*error" || true
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
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/plugins/platforms"

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

# ── 4. Bundle Python interpreter ───────────────────────────────────────────
echo ""
echo ">>> Bundling Python $PYTHON_VERSION interpreter..."
cp "$PYTHON_BIN" "$APPDIR/usr/bin/python3"

# Python stdlib
PY_STDLIB="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
PY_PLATSTDLIB="$(python3 -c 'import sysconfig; print(sysconfig.get_path("platstdlib"))')"
mkdir -p "$APPDIR/usr/lib/python${PYTHON_VERSION}"
cp -r "$PY_STDLIB/." "$APPDIR/usr/lib/python${PYTHON_VERSION}/"
# lib-dynload (C extensions like _ssl, _hashlib)
DYNLOAD="$(python3 -c 'import sysconfig; print(sysconfig.get_path("platlib") + "/../lib-dynload")' 2>/dev/null || echo "")"
[ -d "$DYNLOAD" ] && cp -r "$DYNLOAD" "$APPDIR/usr/lib/python${PYTHON_VERSION}/lib-dynload" || true

# ── 5. Bundle pip deps ─────────────────────────────────────────────────────
echo ""
echo ">>> Installing pip dependencies into AppDir..."
VENV="$BUILD_DIR/venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

SITE_PACKAGES="$VENV/lib/python${PYTHON_VERSION}/site-packages"
mkdir -p "$APPDIR/usr/lib/python${PYTHON_VERSION}/dist-packages"
cp -r "$SITE_PACKAGES/." "$APPDIR/usr/lib/python${PYTHON_VERSION}/dist-packages/"

# ── 6. Bundle Qt6 platform plugins ────────────────────────────────────────
echo ""
echo ">>> Bundling Qt6 platform plugins..."
# Search common locations: PyQt6 wheel bundle, system Qt, distro Qt6
QT_PLATFORMS=""
for candidate in \
    "$(python3 -c 'import PyQt6, os; print(os.path.dirname(PyQt6.__file__))')/Qt6/plugins/platforms" \
    "/usr/lib/qt6/plugins/platforms" \
    "/usr/lib/qt/plugins/platforms" \
    "/usr/lib/x86_64-linux-gnu/qt6/plugins/platforms"; do
    [ -d "$candidate" ] && QT_PLATFORMS="$candidate" && break
done

if [ -n "$QT_PLATFORMS" ]; then
    echo "    Found plugins at: $QT_PLATFORMS"
    for so in "$QT_PLATFORMS"/libqxcb.so "$QT_PLATFORMS"/libqwayland*.so; do
        [ -f "$so" ] && cp "$so" "$APPDIR/usr/plugins/platforms/" && echo "    Copied: $(basename $so)"
    done
else
    echo "    Warning: Qt6 platform plugins not found — AppImage may not launch without them"
fi

# Fix AppRun Python version placeholder
sed -i "s|python3/dist-packages|python${PYTHON_VERSION}/dist-packages|g" "$APPDIR/AppRun"

# ── 7. Download appimagetool ───────────────────────────────────────────────
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L --progress-bar -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# ── 8. Build AppImage ──────────────────────────────────────────────────────
echo ""
echo ">>> Building AppImage..."
OUTPUT="$SCRIPT_DIR/FTHRClips-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT" 2>&1

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║           FTHR Clips AppImage Ready!             ║"
echo "╠══════════════════════════════════════════════════╣"
printf "║  Output: %-40s║\n" "FTHRClips-x86_64.AppImage"
printf "║  Size:   %-40s║\n" "$(du -sh "$OUTPUT" | cut -f1)"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Before first run:                               ║"
echo "║    sudo usermod -aG input \$USER                  ║"
echo "║    (then log out and back in for hotkeys)        ║"
echo "╚══════════════════════════════════════════════════╝"
