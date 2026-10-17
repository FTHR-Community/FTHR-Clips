# Interface performance presets — 13 October 2026

Settings → Performance now offers Full experience, Balanced, Low power,
Minimal / viewer only, and Custom. Existing preferences remain in effect until
the user chooses a preset. Custom is derived from the actual switches, including
changes to the shared timeline option in the Clip tab.

Eight individual controls cover the full editor, live edit preview, color preview,
timeline thumbnails, library thumbnails, animated camera/keyboard settings
previews, interface animations, and background UI activity. Each has an impact
label and a visible explanation. Impact levels are qualitative estimates, not
measured CPU/GPU/RAM reductions. Presets do not change capture, encoder, audio,
recorded overlays, saved drafts, or export settings.

- Balanced disables animations, timeline thumbnails and animated settings previews.
- Low power additionally disables library thumbnails and live/color edit previews.
- Minimal additionally replaces new library clip opens with a basic original-media
  player. It does not create editor sessions, load drafts, build timelines or process
  edits. Existing editor tabs remain intact; close them to release their resources.
- Full experience restores all optional UI features and keeps background pausing on.

Library thumbnail disabling cancels work, drops card images and ignores late
results; re-enabling queues visible thumbnails. Color disabling applies to native
GPU and software previews without modifying export effects. Camera/keyboard
preview callbacks stop their timers before reading frames when disabled. Reopen
Visuals after re-enabling these previews.

The basic player supports play/pause, seeking, volume and media errors. Closing,
replacement, Escape and application shutdown release playback. It shows the
original media, without saved edits or the editor's separate-track mixer.

Validation: 84 tests passed in the combined UI/library/editor/settings regression
run. The final preset suite passed all eight tests (including two later additions
for editor bypass and real generated-video playback/seek/pause). An additional
65 editor-session, preview, UI-efficiency and encoder tests passed; three native
GPU tests were skipped because the offscreen platform lacks a hardware scene graph.
Ruff passed for
all changed application modules and the new tests. The initialized Qt page was
rendered and inspected at 1100×900; its content scrolls at smaller sizes. Preview:
`Output/performance-presets.png`. No executable or installer was rebuilt.

SFTP investigation: `FTHR_Uploader/sftp_provider.py` requires a manually configured
SHA256 fingerprint and compares it before authentication. It does not load trusted
known-host keys. Manual fingerprint entry is therefore an app policy, not a
universal SFTP requirement. A trusted known_hosts implementation could replace
manual entry while retaining identity verification. Connection policy was not changed.
