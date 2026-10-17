#!/usr/bin/env bash
# Print safe, redacted diagnostics for an AppImage launch report.
set -uo pipefail

usage() {
    echo "usage: linux_appimage_report.sh APPIMAGE [--write-checksum FILE]" >&2
}

if [[ $# -lt 1 || $# -gt 3 ]]; then
    usage
    exit 2
fi

IMAGE=$1
CHECKSUM_FILE=
if [[ $# -eq 3 && $2 == "--write-checksum" ]]; then
    CHECKSUM_FILE=$3
elif [[ $# -ne 1 ]]; then
    usage
    exit 2
fi

if [[ ! -f "$IMAGE" ]]; then
    echo "ERROR: AppImage not found" >&2
    exit 2
fi

if ! SHA256=$(sha256sum -- "$IMAGE" 2>/dev/null | awk '{print $1}'); then
    echo "ERROR: could not calculate the AppImage checksum" >&2
    exit 2
fi
if [[ -z "$SHA256" ]]; then
    echo "ERROR: could not calculate the AppImage checksum" >&2
    exit 2
fi

BASENAME=$(basename -- "$IMAGE")
if [[ -n "$CHECKSUM_FILE" ]]; then
    if ! printf '%s  %s\n' "$SHA256" "$BASENAME" >"$CHECKSUM_FILE"; then
        echo "ERROR: could not write the checksum file" >&2
        exit 2
    fi
fi

presence() {
    local name=$1 value=${!1-}
    if [[ -n "$value" ]]; then
        printf 'set'
    else
        printf 'unset'
    fi
}

echo "image: $BASENAME"
echo "sha256: $SHA256"
printf 'architecture: %s\n' "$(uname -m 2>/dev/null || echo unknown)"
printf 'kernel: %s\n' "$(uname -r 2>/dev/null || echo unknown)"
if command -v ldd >/dev/null 2>&1; then
    printf 'libc: %s\n' "$(ldd --version 2>&1 | head -n 1)"
else
    echo "libc: unavailable"
fi

# Report only whether potentially identifying desktop variables are present.
# Never print their values, which can contain display names and user paths.
printf 'session: %s\n' "$(presence XDG_SESSION_TYPE)"
printf 'desktop: %s\n' "$(presence XDG_CURRENT_DESKTOP)"
printf 'wayland_display: %s\n' "$(presence WAYLAND_DISPLAY)"
printf 'x_display: %s\n' "$(presence DISPLAY)"
printf 'runtime_dir: %s\n' "$(presence XDG_RUNTIME_DIR)"

if command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1; then
    echo "fuse: available"
else
    echo "fuse: unavailable"
fi

if command -v file >/dev/null 2>&1 && file -b -- "$IMAGE" 2>/dev/null | grep -q 'ELF'; then
    # ELF is necessary for an AppImage but does not by itself prove that the
    # payload is a valid, mountable AppImage.
    echo "format: ELF executable payload detected"
    status=0
else
    echo "format: not a valid AppImage ELF payload"
    status=1
fi

echo "appimage_extract_and_run=1 $BASENAME"
echo "helper_tools:"
for tool in file sha256sum fusermount3 fusermount grim nc xdotool xrandr \
        hyprctl pactl wmctrl xprop xdg-open; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "  $tool: present"
    else
        echo "  $tool: missing"
    fi
done

exit "$status"
