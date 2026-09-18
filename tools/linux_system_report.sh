#!/usr/bin/env bash
# Report Linux capture dependencies and runtime state without changing them.
# Omit personal identifiers and config contents; abbreviate home paths.
# Usage: bash tools/linux_system_report.sh > report.txt

set -uo pipefail

_redact() { sed "s|$HOME|~|g"; }
_have()   { command -v "$1" >/dev/null 2>&1; }
_h()      { printf '\n=== %s ===\n' "$1"; }
_kv()     { printf '  %-24s %s\n' "$1" "${2:-(unset)}"; }
_run()    { if _have "$1"; then "$@" 2>&1 | _redact | sed 's/^/  /'; else echo "  ($1 not installed)"; fi; }

printf 'FTHR Clips — Linux system report\n'
printf 'generated: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

_h 'Distribution and kernel'
if [ -r /etc/os-release ]; then
    . /etc/os-release
    _kv 'distribution' "${PRETTY_NAME:-$NAME ${VERSION:-}}"
    _kv 'id'           "${ID:-?}${ID_LIKE:+ (like $ID_LIKE)}"
fi
_kv 'kernel'       "$(uname -r)"
_kv 'architecture' "$(uname -m)"
_kv 'libc'         "$(getconf GNU_LIBC_VERSION 2>/dev/null || echo '?')"
if grep -qi microsoft /proc/version 2>/dev/null; then
    _kv 'NOTE' 'running under WSL — no real compositor; capture results do not
                           generalise to a bare-metal desktop'
fi

_h 'Session, display server and compositor'
_kv 'XDG_SESSION_TYPE'     "${XDG_SESSION_TYPE:-}"
_kv 'XDG_CURRENT_DESKTOP'  "${XDG_CURRENT_DESKTOP:-}"
_kv 'XDG_SESSION_DESKTOP'  "${XDG_SESSION_DESKTOP:-}"
_kv 'WAYLAND_DISPLAY'      "${WAYLAND_DISPLAY:-}"
_kv 'DISPLAY'              "${DISPLAY:-}"
_kv 'HYPRLAND_INSTANCE_SIG' "${HYPRLAND_INSTANCE_SIGNATURE:+present}"
_kv 'XDG_RUNTIME_DIR'      "${XDG_RUNTIME_DIR:-}"
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    _kv '  ownership/mode'  "$(stat -c 'uid=%u mode=%a' "$XDG_RUNTIME_DIR" 2>&1)"
fi
# Which capture backend the engine will pick, by the same rule it uses.
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    _kv 'engine will try'  'wlr-screencopy, then ext-image-copy-capture'
    _kv 'x11grab'          'XWayland fallback is refused'
elif [ -n "${DISPLAY:-}" ]; then
    _kv 'engine will try'  'native X11 x11grab with RandR-selected geometry'
    _kv 'cancellation'     'interrupt callback plus bounded engine-process kill/reap'
else
    _kv 'engine will try'  'no display backend (headless session)'
fi

_h 'Monitors and scaling'
if _have hyprctl && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
    hyprctl monitors 2>&1 | grep -E '^Monitor|^\s+[0-9]+x[0-9]+|scale:' | sed 's/^/  /'
elif _have wlr-randr && [ -n "${WAYLAND_DISPLAY:-}" ]; then
    _run wlr-randr
elif _have xrandr && [ -n "${DISPLAY:-}" ]; then
    xrandr --query 2>&1 | grep -E ' connected|\*' | sed 's/^/  /'
else
    echo '  (no monitor query tool available for this session)'
fi
_kv 'GDK_SCALE'          "${GDK_SCALE:-}"
_kv 'QT_SCALE_FACTOR'    "${QT_SCALE_FACTOR:-}"
_kv 'QT_ENABLE_HIGHDPI'  "${QT_ENABLE_HIGHDPI_SCALING:-}"

_h 'GPU and drivers'
if _have lspci; then
    lspci -nn 2>/dev/null | grep -Ei 'vga|3d|display' | sed 's/^/  /' || echo '  (none reported)'
else
    echo '  (lspci not installed)'
fi
for d in /dev/dri/*; do [ -e "$d" ] && _kv 'dri node' "$d"; done
if _have glxinfo && [ -n "${DISPLAY:-}" ]; then
    glxinfo -B 2>/dev/null | grep -E 'OpenGL renderer|OpenGL version' | sed 's/^/  /'
fi
if _have vainfo; then
    vainfo 2>/dev/null | grep -E 'Driver version|VAProfile.*Enc' | head -12 | sed 's/^/  /'
else
    echo '  (vainfo not installed — VA-API encoder support unknown)'
fi
if _have nvidia-smi; then
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1 | sed 's/^/  /'
fi

_h 'FFmpeg and available encoders'
if _have ffmpeg; then
    ffmpeg -version 2>/dev/null | head -1 | sed 's/^/  /'
    # Licence matters: a GPL FFmpeg linked into a release build is AUDIT-005.
    ffmpeg -version 2>/dev/null | grep -oE '\-\-enable-(gpl|nonfree|version3|libx264|libx265|libopenh264|libkvazaar)' \
        | sort -u | sed 's/^/  configure: /'
    echo '  encoders FTHR can use:'
    ffmpeg -hide_banner -encoders 2>/dev/null \
        | grep -E ' (h264|hevc|av1)(_nvenc|_vaapi|_qsv|_amf)?| libopenh264| libkvazaar| libsvtav1| libaom-av1' \
        | sed 's/^/    /'
else
    echo '  (ffmpeg not installed)'
fi
for lib in libavcodec libavformat libavutil libswscale libswresample libavdevice; do
    if _have pkg-config; then
        v=$(pkg-config --modversion "$lib" 2>/dev/null) && _kv "$lib" "$v"
    fi
done

_h 'Audio'
if _have pactl; then
    pactl info 2>&1 | grep -E 'Server String|Server Name|Server Version|Default Sink|Default Source' \
        | _redact | sed 's/^/  /'
    echo '  sources:'
    pactl list short sources 2>&1 | sed 's/^/    /'
else
    echo '  (pactl not installed — cannot query PulseAudio/PipeWire)'
fi
if pgrep -x pipewire >/dev/null 2>&1; then
    _kv 'pipewire' "running $( _have pipewire && pipewire --version 2>&1 | head -1 )"
else
    _kv 'pipewire' 'not running'
fi
pgrep -x pulseaudio >/dev/null 2>&1 && _kv 'pulseaudio' 'running (native)'

_h 'Build toolchain'
_run cmake --version
_run gcc --version
_run g++ --version
_run pkg-config --version
_have wayland-scanner && _kv 'wayland-scanner' "$(wayland-scanner --version 2>&1)"

_h 'Python'
_kv 'python3' "$(python3 --version 2>&1)"
python3 - <<'PY' 2>/dev/null | sed 's/^/  /' || echo '  (PySide6 not importable)'
try:
    import PySide6
    from PySide6.QtCore import qVersion
    print(f'Qt {qVersion()} / PySide6 {PySide6.__version__}')
except Exception as e:
    print(f'PySide6 unavailable: {e}')
for m in ('numpy', 'cv2', 'sounddevice', 'jeepney'):
    try:
        mod = __import__(m)
        print(f'{m} {getattr(mod, "__version__", "?")}')
    except Exception as e:
        print(f'{m} MISSING ({type(e).__name__})')
PY

_h 'FTHR external helper tools'
for t in hyprctl xdotool xprop xrandr grim nc xdg-open wmctrl; do
    p=$(command -v "$t" 2>/dev/null) && _kv "$t" "$p" || _kv "$t" 'NOT FOUND'
done

_h 'FTHR runtime state'
sock="${XDG_RUNTIME_DIR:-$HOME/.fthr/run}/fthr/hotkey.sock"
[ -n "${XDG_RUNTIME_DIR:-}" ] || sock="$HOME/.fthr/run/hotkey.sock"
if [ -e "$sock" ]; then
    _kv 'hotkey socket' "$(echo "$sock" | _redact)"
    _kv '  ownership/mode' "$(stat -c 'uid=%u mode=%a type=%F' "$sock" 2>&1)"
else
    _kv 'hotkey socket' "absent ($(echo "$sock" | _redact))"
fi
if [ -e /tmp/fthr_hotkey.sock ]; then
    _kv 'LEGACY /tmp socket' "PRESENT — $(stat -c 'uid=%u mode=%a' /tmp/fthr_hotkey.sock 2>&1)"
    echo '    (pre-AUDIT-003b builds used this world-writable location)'
fi
if [ -e "$HOME/.fthr/fthr.lock" ]; then
    _kv 'instance lock' "$(stat -c 'uid=%u mode=%a' "$HOME/.fthr/fthr.lock" 2>&1)"
else
    _kv 'instance lock' 'absent'
fi
for s in /dev/shm/FTHR_SharedMemory_v*; do
    [ -e "$s" ] && _kv 'shared memory' "$s $(stat -c 'size=%s uid=%u mode=%a' "$s")"
done
_kv 'clips dir'  "$( [ -d "$HOME/.fthr" ] && du -sh "$HOME/.fthr" 2>/dev/null | cut -f1 || echo 'no ~/.fthr' )"
if [ -f "$HOME/.fthr/logs/fthr.log" ]; then
    _kv 'log size' "$(stat -c %s "$HOME/.fthr/logs/fthr.log") bytes"
    echo '  last 5 log lines:'
    tail -5 "$HOME/.fthr/logs/fthr.log" | _redact | sed 's/^/    /'
fi

printf '\n=== end of report ===\n'
