# UI spacing, live themes, and Windows hit testing

The existing mint and dark-green palette and Oswald typography are preserved.
Settings now use a 24 px page inset, 12 px row gap, 24 px section gap, and shared
6 px control / 8 px panel corners. Customization sections and color swatches are
rounded, expanded sections can scroll, and the audio playback-device row has
explicit gaps between its label, dropdown, Refresh action, and hint. Visual
overlay options and the composite preview no longer have enclosing panel boxes.

Local theme styles now retain recipes that read the current semantic tokens on
Apply. This includes dialogs, popup controls, library empty states, editing and
sharing controls, and customization actions. Apply refreshes all application
windows, including detached editors and dialogs, and refreshes dropdown palettes
as well as QSS. Legacy palette aliases and helper-label defaults follow current
tokens. The existing color and icon inventories cover the shipped UI; the fix
addresses missing live application of those choices rather than adding duplicate
color settings. Preview artwork and user-selected overlay content retain their
own colors.

Windows frameless hit testing previously converted physical client coordinates
using OS DPI alone and deferred ordinary content hits to default frame handling.
It now uses the window's effective Qt device pixel ratio, returns HTCLIENT for
content and interactive controls, and reserves resize/caption hits for their
actual regions. This also protects controls near a resize edge. Signed virtual
desktop coordinates and native drag/resize behavior are preserved.

Qt documents that widget geometry uses device-independent pixels and that the
effective scale can include environment overrides:
[Qt high DPI](https://doc.qt.io/qt-6/highdpi.html).
Windows documents the client, resize and caption classifications:
[WM_NCHITTEST](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-nchittest).

Validation:

- 185 checks passed in the broader UI, library, popup, theme, overlay, editor,
  and Windows geometry regression run.
- The focused consistency suite passed with actual QT_SCALE_FACTOR values of
  1.5 and 2.0 (14 checks in the final 200% run).
- Regression cases cover 1920×1080, 2560×1440 and 3840×2160 coordinates at
  100%, 125%, 150% and 200%, including negative monitor origins and differences
  between Qt scale and OS DPI.
- Runtime tests verify the audio row's separation, every exposed color token,
  theme changes on existing widgets and detached dialogs, and flat overlay panels.
- Ruff and Python compilation passed. The Qt preview helper renders settled
  pages, expanded customization sections and large window sizes under
  `ui-preview/ui-consistency/` using an isolated profile and disabled capture
  services.

The reported failure was not reproduced on an affected physical monitor setup.
Tests cover the suspected native/scaling mechanism; mixed-monitor hardware
confirmation remains useful.
