# Customization reset and Apply performance — 2.0.0 Alpha

The fixed action row now has **Reset all** next to **Apply theme**. Reset restores
fonts, colors, icon artwork and tints, capture-card colors, sound overrides, and
the six notification volume controls. It saves the defaults and applies the
theme immediately. Unrelated capture settings are retained. Imported originals
remain on disk, while their active theme references are cleared.

Apply now restyles the existing customization controls and color preview rather
than rebuilding them. Expanded sections, scroll position, sliders, and sound
players retain their identities. Qt's Fusion style is configured once per
application, unchanged QSS is skipped, and unchanged fonts do not trigger a
widget-tree walk. The notification helper is reaped outside the UI thread when
it restarts, and theme application no longer pumps a nested event loop.

An isolated offscreen profile with the real settings page and customization
controls measured 456.4, 275.5, and 282.4 ms for three changed-color Apply actions,
including queued paints and Python profiling overhead. This is a local
instrumented check, not a native display or hardware-wide latency guarantee.
Profiles and summaries are under `Output/theme-apply-*`.

Regression coverage includes restoring all customization groups and saved
volumes, retaining unrelated settings, preserving controls and sound players
through repeated Apply, and replacing a slow notification helper without
waiting for it on the UI thread. Existing typography, detached-dialog theme,
library, keyboard, and Gary tests are also exercised.

The product version remains `2.0.0-alpha`; the build date is 2026-10-15. The
source package is named `FTHRClips-2.0.0-Alpha-Source.zip`, with a matching
top-level folder and a SHA-256 file manifest. It replaces the previous Desktop
source ZIP. The desktop executable and Windows installer are not rebuilt.
