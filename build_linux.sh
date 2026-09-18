#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_output"
APPDIR="$BUILD_DIR/AppDir"

# Prefer the project-local Linux venv when present so packaging runs in the same
# environment that installed PyInstaller/PySide6. This avoids stale Windows or
# global-user installs being picked up out of context.
VENV_PY="$SCRIPT_DIR/.venv-linux/bin/python"
if [ -x "$VENV_PY" ]; then
    export PATH="$SCRIPT_DIR/.venv-linux/bin:$HOME/.local/bin:$PATH"
    PYTHON_BIN="$VENV_PY"
else
    PYTHON_BIN="$(command -v python3 || true)"
    if [ -z "$PYTHON_BIN" ]; then
        echo "ERROR: python3 not found in PATH." >&2
        exit 1
    fi
    export PATH="$HOME/.local/bin:$PATH"
fi

# Product version comes from FTHR_UI/version.py — the single source of truth.
# Never hardcode it here; tools/verify_version_consistency.py enforces that.
APP_VERSION="$("$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$SCRIPT_DIR/FTHR_UI'); import version; print(version.__version__)")"

echo "=== FTHR Clips — Linux AppImage Builder ==="
echo "Version: $APP_VERSION"
echo "Python: $("$PYTHON_BIN" --version)"
echo "GCC:    $(gcc --version | head -1)"
echo ""

# 1. Check requirements
echo ">>> Checking dependencies..."
for cmd in cmake gcc pkg-config wayland-scanner curl readelf file sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: '$cmd' not found."
        exit 1
    }
done
# The project Python binary comes from the active venv if present; use it for
# all Python-dependent operations below, including version checks and the
# Redist/licence validation helpers.
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "ERROR: configured Python interpreter missing: $PYTHON_BIN"
    exit 1
}
"$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1 || {
    echo "ERROR: PyInstaller is missing from $PYTHON_BIN" >&2
    echo "  Install it in the active Linux venv: python -m pip install pyinstaller" >&2
    exit 1
}

pkg-config --exists libavcodec libpulse-simple wayland-client || {
    echo "ERROR: Missing C++ build deps (ffmpeg / pulseaudio / wayland)."
    echo "  Arch:          sudo pacman -S ffmpeg libpulse wayland"
    echo "  Debian/Ubuntu: sudo apt install libavcodec-dev libavformat-dev \\"
    echo "                   libavutil-dev libavdevice-dev libswscale-dev \\"
    echo "                   libswresample-dev libpulse-dev libwayland-dev \\"
    echo "                   wayland-protocols"
    exit 1
}

# Import each module separately. A combined import reports only the first
# failure, and the advice it printed ("pip install …") was actively misleading
# for sounddevice: that one fails on a missing *system* library (PortAudio),
# which no amount of pip installing will fix. Verified on Ubuntu 24.04.
_missing_py=()
_missing_sys=()
for _m in PySide6 jeepney cv2 numpy sounddevice; do
    _err="$("$PYTHON_BIN" -c "import $_m" 2>&1)" && continue
    case "$_err" in
        *PortAudio*)          _missing_sys+=("$_m: PortAudio runtime library") ;;
        *libGL*|*libEGL*|*libxkb*|*libxcb*)
                              _missing_sys+=("$_m: a system graphics library — ${_err##*$'\n'}") ;;
        *)                    _missing_py+=("$_m") ;;
    esac
done

if [ ${#_missing_py[@]} -gt 0 ]; then
    echo "ERROR: Missing Python packages: ${_missing_py[*]}"
    echo "  Install the pinned alpha set (do NOT pip install loose versions):"
    echo "    python3 -m pip install -r requirements-alpha.txt"
    exit 1
fi
if [ ${#_missing_sys[@]} -gt 0 ]; then
    echo "ERROR: Python packages are installed but their SYSTEM libraries are not:"
    for _m in "${_missing_sys[@]}"; do echo "    - $_m"; done
    echo "  This is not fixed by pip. Install the system packages:"
    echo "    Arch:          sudo pacman -S portaudio"
    echo "    Debian/Ubuntu: sudo apt install libportaudio2"
    echo "    Fedora:        sudo dnf install portaudio"
    exit 1
fi
echo "    All dependencies found."

# 1c. Pinned LGPL FFmpeg (AUDIT-014)
# The distribution's FFmpeg is a GPL build. The engine is compiled against this
# tree and ships with it; nothing here ever links /usr/lib FFmpeg.
FFMPEG_ROOT="$SCRIPT_DIR/FTHRcapture_linux/third_party/ffmpeg"
echo ""
echo ">>> Ensuring the pinned LGPL FFmpeg is present..."
if ! "$PYTHON_BIN" "$SCRIPT_DIR/tools/fetch_third_party.py" --ffmpeg-linux; then
    echo "ERROR: could not obtain the pinned LGPL FFmpeg."
    echo "  Without it the engine would link the distribution's GPL build and"
    echo "  the licence gate would refuse to package the result (AUDIT-014)."
    exit 1
fi

# 2. Build Linux C++ engine
echo ""
echo ">>> Building Linux capture engine (against the pinned LGPL FFmpeg)..."
cd "$SCRIPT_DIR/FTHRcapture_linux"
rm -rf build
cmake -B build -DCMAKE_BUILD_TYPE=Release -DFTHR_FFMPEG_ROOT="$FFMPEG_ROOT"
cmake --build build -j"$(nproc)"
[ -x build/FTHRclips ] || { echo "ERROR: engine build produced no binary."; exit 1; }
[ -f build/libFTHRPlaybackMixer.so ] || {
    echo "ERROR: playback bridge build produced no library."; exit 1;
}
echo "    Engine built: FTHRcapture_linux/build/FTHRclips"

echo ""
echo ">>> Running Linux native tests..."
ctest --test-dir build --output-on-failure

# Prove the engine really links the pinned libraries before we package anything.
echo ""
echo ">>> Verifying engine linkage..."
_bad=0
for so in $(readelf -d build/FTHRclips | grep -oE 'lib(avcodec|avformat|avutil|avdevice|swscale|swresample)\.so\.[0-9]+'); do
    if ! [ -e "$FFMPEG_ROOT/lib/$so" ]; then
        echo "    ERROR: engine needs $so, which is not in the pinned tree"
        _bad=1
    fi
done
for so in $(readelf -d build/libFTHRPlaybackMixer.so | grep -oE 'lib(avcodec|avformat|avutil|avdevice|swscale|swresample)\.so\.[0-9]+'); do
    if ! [ -e "$FFMPEG_ROOT/lib/$so" ]; then
        echo "    ERROR: playback bridge needs $so, which is not in the pinned tree"
        _bad=1
    fi
done
if readelf -d build/FTHRclips | grep -qE 'RPATH|RUNPATH'; then
    echo "    RPATH: $(readelf -d build/FTHRclips | grep -oE '\[.*\]' | head -1)"
else
    echo "    ERROR: engine has no RPATH — it would load system libraries."
    _bad=1
fi
[ "$_bad" -eq 0 ] || { echo "ERROR: engine linkage check failed."; exit 1; }
echo "    Engine links only the pinned LGPL FFmpeg."
cd "$SCRIPT_DIR"

# 3. Build the optional Linux uploader bundle before the main AppImage.
echo ""
echo ">>> Building optional Linux upload extension..."
"$PYTHON_BIN" "$SCRIPT_DIR/tools/build_linux_uploader.py" --python "$PYTHON_BIN"

# 4. Bundle with PyInstaller
echo ""
echo ">>> Bundling Python app with PyInstaller..."
"$PYTHON_BIN" -m PyInstaller FTHR_linux.spec --clean --noconfirm

PYINST_DIR="$SCRIPT_DIR/dist/FTHRClips"
[[ -f "$PYINST_DIR/FTHRClips" ]] || { echo "ERROR: PyInstaller output missing."; exit 1; }
echo "    Raw bundle: $(du -sh "$PYINST_DIR" | cut -f1)"

# 4. Strip unused libraries
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

# Qt Quick 2 and the small QML runtime are indirect dependencies of Qt's
# FFmpeg multimedia backend even though the FTHR UI itself uses Widgets.
# Removing them makes QMediaPlayer report "No QtMultimedia backends found".
# Quick3D and the unrelated optional modules below remain unused.
_rm "libQt6Quick3D*.so*"
_rm "libQt6ShaderTools*.so*"
_rm "libQt6Pdf*.so*"
_rm "libQt6WebEngine*.so*"

# PyInstaller's Qt hook can re-add the optional TIFF image plugin after the
# spec-level TOC filter.  The pinned PySide6 wheel builds it against
# libtiff.so.5, which is not part of the Ubuntu 24.04 baseline.  FTHR ships no
# TIFF assets, so omit the unusable plugin deterministically from every build.
_rm "libqtiff.so"
_rm "libQt6Location*.so*"
_rm "libQt6Positioning*.so*"
_rm "libQt6VirtualKeyboard*.so*"
_rm "*virtualkeyboard*"
_rm "libQt6Charts*.so*"
_rm "libQt6DataVisualization*.so*"

# OpenCV ML / DNN support libs (~12 MB)
_rm "libhdf5*.so*"
_rm "libprotobuf*.so*"

# ICU data — only needed for full Unicode / BIDI, Qt ships a smaller subset
# (don't remove — Qt itself needs libicudata)

AFTER="$(du -sh "$PYINST_DIR" | cut -f1)"
echo "    After strip: $AFTER"

# Remove unapproved system FFmpeg copies collected through Qt/OpenCV. Match
# exact library basenames: libav* also matches unrelated libraries such as
# libavif and libavc1394, whose removal would break the bundle.
_FFMPEG_LIB_RE='.*/lib(avcodec|avformat|avutil|avdevice|avfilter|swscale|swresample|postproc)\.so[.0-9]*$'

# Kept on purpose:
#   *.so.62/.60/.11/.9/.6  the pinned LGPL runtime the engine links (manifest)
#   *.so.61/.59/.8/.5      Qt Multimedia's own FFmpeg from the PySide6 wheel,
#                          LGPLv2.1, documented in ffmpeg_manifest_linux.json.
#                          Removing it would break Qt Multimedia playback.
_keep_regex='libavcodec\.so\.6[12]|libavformat\.so\.6[12]|libavutil\.so\.(59|60)|libavdevice\.so\.62|libavfilter\.so\.11|libswscale\.so\.[89]|libswresample\.so\.[56]'

echo ""
echo ">>> Removing GPL FFmpeg copies collected from the system..."
_removed=0
while IFS= read -r f; do
    base="$(basename "$f")"
    if echo "$base" | grep -qE "$_keep_regex"; then continue; fi
    if strings -a "$f" 2>/dev/null | grep -q -- '--enable-gpl'; then
        echo "    GPL, removed: $base"
    else
        echo "    undocumented FFmpeg copy, removed: $base"
    fi
    rm -f "$f"
    _removed=$((_removed + 1))
done < <(find "$INT" -regextype posix-extended -regex "$_FFMPEG_LIB_RE" | sort)
echo "    Removed $_removed file(s)."

echo ">>> FFmpeg libraries remaining in the bundle:"
find "$INT" -regextype posix-extended -regex "$_FFMPEG_LIB_RE" -printf '    %f\n' | sort

# 5. Verify the app still launches after stripping
echo ""
echo ">>> Smoke-testing stripped bundle..."
if timeout 6 "$PYINST_DIR/FTHRClips" 2>&1 | grep -q "Engine found\|successfully initiated"; then
    echo "    Smoke test passed."
else
    echo "    WARNING: Could not confirm launch (no display / normal in CI)."
fi

# 6. Set up AppDir
echo ""
echo ">>> Setting up AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -r "$PYINST_DIR/." "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.desktop" "$APPDIR/"
cp "$SCRIPT_DIR/AppDir/fthr-clips.png"     "$APPDIR/"

cat > "$APPDIR/AppRun" <<'APPRUN_EOF'
#!/bin/sh
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    export QT_QPA_PLATFORM="wayland"
elif [ -n "${DISPLAY:-}" ]; then
    export QT_QPA_PLATFORM="xcb"
fi

# The XDG GlobalShortcuts portal identifies us by the systemd scope we run
# in (app-fthr\x2dclips-<pid>.scope, matching fthr-clips.desktop). Desktop
# launchers create that scope; a start from a terminal would inherit the
# terminal's identity instead, so move into our own scope when we can.
if [ -z "${FTHR_SCOPED:-}" ] && command -v systemd-run >/dev/null 2>&1 \
        && ! grep -q 'app-fthr\\x2dclips' /proc/self/cgroup 2>/dev/null \
        && systemd-run --user --scope --quiet true 2>/dev/null; then
    FTHR_SCOPED=1
    export FTHR_SCOPED
    exec systemd-run --user --scope --quiet \
        --unit="app-fthr\\x2dclips-$$" "$0" "$@"
fi

export PYTHONUNBUFFERED=1
exec "$HERE/FTHRClips" "$@"
APPRUN_EOF
chmod +x "$APPDIR/AppRun"

# Host metadata must never enter a Linux package. FTHR_linux.spec filters it at
# collection time; this fail-closed check prevents a future copy rule from
# silently reintroducing it.
_metadata_junk="$(find "$APPDIR" -type f \( \
    -iname 'desktop.ini' -o -iname 'thumbs.db' -o -iname 'ehthumbs.db' \
    \) -print)"
if [ -n "$_metadata_junk" ]; then
    echo "ERROR: host-OS metadata entered AppDir:" >&2
    printf '  %s\n' "$_metadata_junk" >&2
    exit 1
fi

[ -x "$APPDIR/FTHRClips" ] || {
    echo "ERROR: packaged application executable is missing: $APPDIR/FTHRClips" >&2
    exit 1
}
[ -x "$APPDIR/AppRun" ] || {
    echo "ERROR: AppRun is missing or not executable." >&2
    exit 1
}
[ -f "$APPDIR/fthr-clips.desktop" ] || {
    echo "ERROR: AppImage desktop entry is missing." >&2
    exit 1
}
[ -f "$APPDIR/fthr-clips.png" ] || {
    echo "ERROR: AppImage root icon is missing." >&2
    exit 1
}
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$APPDIR/fthr-clips.desktop"
fi

# Licence paperwork (AUDIT-005/AUDIT-013). Bundled FFmpeg, PySide6, and Qt use
# LGPL options and require local licence/notices plus the documented source
# route; the user must not have to visit the repository to find them.
cp "$SCRIPT_DIR/LICENSE"                "$APPDIR/"
cp "$SCRIPT_DIR/THIRD_PARTY_NOTICES.md" "$APPDIR/"
cp -r "$SCRIPT_DIR/licenses"            "$APPDIR/"
echo "    Licence files copied into AppDir."

# 6b. Verify no GPL FFmpeg slipped into the bundle
# PyInstaller pulls in the shared libraries the engine links against, so a
# distro GPL build of FFmpeg can end up inside the AppImage without anyone
# choosing it. Fail the build rather than ship it.
echo ""
echo ">>> Verifying release licences..."
if ! "$PYTHON_BIN" "$SCRIPT_DIR/tools/verify_release_licenses.py" --appdir "$APPDIR"; then
    echo ""
    echo "ERROR: licence verification failed - refusing to build the AppImage."
    echo "  See THIRD_PARTY_NOTICES.md and tools/verify_release_licenses.py"
    exit 1
fi

# 7. Download appimagetool
APPIMAGETOOL="$BUILD_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo ""
    echo ">>> Downloading appimagetool..."
    curl -L --progress-bar -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

# 8. Pack AppImage
echo ""
echo ">>> Packing AppImage..."
OUTPUT="$BUILD_DIR/FTHRClips-${APP_VERSION}-x86_64.AppImage"
LINUX_STAGE="$HOME/fthr-appimage-build"
NATIVE_APPDIR="$LINUX_STAGE/AppDir"
NATIVE_TOOL="$LINUX_STAGE/appimagetool-x86_64.AppImage"
NATIVE_OUTPUT="$LINUX_STAGE/FTHR-Clips-Linux-x86_64.AppImage"

# AppImage tooling is unreliable when its source tree lives on a Windows mount
# whose path contains spaces. Keep the actual image construction on the native
# Linux filesystem, then copy only the finished artifact back to build_output.
[ "$LINUX_STAGE" = "$HOME/fthr-appimage-build" ] || {
    echo "ERROR: refusing unexpected native staging path: $LINUX_STAGE" >&2
    exit 1
}
mkdir -p "$LINUX_STAGE"
rm -rf "$NATIVE_APPDIR"
rm -f "$NATIVE_OUTPUT" "$OUTPUT"
cp -a "$APPDIR" "$NATIVE_APPDIR"
cp "$APPIMAGETOOL" "$NATIVE_TOOL"
chmod +x "$NATIVE_TOOL" "$NATIVE_APPDIR/AppRun"

ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 \
    "$NATIVE_TOOL" "$NATIVE_APPDIR" "$NATIVE_OUTPUT"
[ -f "$NATIVE_OUTPUT" ] || {
    echo "ERROR: appimagetool reported success but produced no AppImage." >&2
    exit 1
}
cp "$NATIVE_OUTPUT" "$OUTPUT"

# Ship a checksum beside the artifact so users and release automation can
# verify that they downloaded the intended binary.
( cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256" )

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              FTHR Clips Linux AppImage Ready                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  Output: %-52s║\n" "build_output/FTHRClips-${APP_VERSION}-x86_64.AppImage"
printf "║  SHA256: %-51s║\n" "build_output/FTHRClips-${APP_VERSION}-x86_64.AppImage.sha256"
printf "║  Size:   %-52s║\n" "$SIZE"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Linux-only — contains the Linux capture engine only.       ║"
echo "║                                                              ║"
echo "║  Global hotkeys use the desktop's GlobalShortcuts portal;   ║"
echo "║  no input group or root is needed.                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
