#!/usr/bin/env bash
# Produce a redacted diagnostic report for a FTHR Clips Linux AppImage.
set -uo pipefail

usage() {
    printf 'Usage: %s APPIMAGE [--write-checksum FILE]\n' "$0" >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    usage
    exit 2
fi
IMAGE=$1
CHECKSUM_FILE=
if [ "${2:-}" = '--write-checksum' ] && [ -n "${3:-}" ]; then
    CHECKSUM_FILE=$3
elif [ "$#" -gt 1 ]; then
    usage
    exit 2
fi

if [ ! -f "$IMAGE" ]; then
    printf 'ERROR: AppImage not found: %s\n' "$IMAGE" >&2
    exit 2
fi

sha=$(sha256sum "$IMAGE" 2>/dev/null | awk '{print $1}')
if [ -z "$sha" ]; then
    printf 'ERROR: sha256sum is unavailable or failed.\n' >&2
    exit 2
fi
if [ -n "$CHECKSUM_FILE" ]; then
    printf '%s  %s\n' "$sha" "$(basename "$IMAGE")" > "$CHECKSUM_FILE"
fi

printf 'FTHR Clips Linux AppImage diagnostic report\n'
printf 'image: %s\n' "$(basename "$IMAGE")"
printf 'sha256: %s\n' "$sha"
printf 'architecture: %s\n' "$(uname -m)"
printf 'kernel: %s\n' "$(uname -r)"
printf 'libc: %s\n' "$(getconf GNU_LIBC_VERSION 2>/dev/null || printf 'unknown')"
printf 'session: %s\n' "${XDG_SESSION_TYPE:-unset}"
printf 'desktop: %s\n' "${XDG_CURRENT_DESKTOP:-unset}"
printf 'wayland_display: %s\n' "${WAYLAND_DISPLAY:+set}"
printf 'x_display: %s\n' "${DISPLAY:+set}"
printf 'fuse: '
if [ -e /dev/fuse ] && [ -r /dev/fuse ] && [ -w /dev/fuse ]; then
    printf 'available\n'
else
    printf 'unavailable\n'
fi
printf 'fallback: APPIMAGE_EXTRACT_AND_RUN=1 %s\n' "$(basename "$IMAGE")"
printf 'helpers:\n'
for tool in grim nc xdotool xrandr hyprctl pactl; do
    if command -v "$tool" >/dev/null 2>&1; then
        printf '  %s: %s\n' "$tool" "$(command -v "$tool")"
    else
        printf '  %s: missing\n' "$tool"
    fi
done

if file "$IMAGE" 2>/dev/null | grep -q 'ELF'; then
    printf 'format: AppImage ELF payload detected\n'
else
    printf 'format: not a valid AppImage ELF payload\n'
    exit 1
fi

if [ "$(uname -m)" != 'x86_64' ]; then
    printf 'warning: this release is x86_64-only\n'
fi
printf 'notes: report engine.log and this output when requesting support; private paths and values are intentionally omitted.\n'
exit 0
