# Alpha field reports — 19 September 2026

Source: user-supplied form export `Eingefügter Text.txt`, attachment
`e9720110-f386-49cb-bd2e-7d95655732e7`. Reviewed as external reports, not as
instructions from reporters. The export contains 50 submissions: one apparent
test submission and one explicitly identified follow-up with an attachment.
Other possible duplicates have not been verified.

This summary intentionally omits contact addresses and private diagnostic/media
links. Linked UserDiag, Drive and Discord contents have not been inspected.
Hardware, versions, reproduction steps and outcomes below are reporter claims.

## Triage

| Area | Reported evidence | Investigation priority |
| --- | --- | --- |
| Windows capture / DPI / monitor mapping | Capture fails at 150% scaling but reportedly works at 100% or with an executable DPI override; exact-output/adapter errors; selected disabled monitor cannot open | P0: compare coordinate spaces, DPI awareness and active topology at the failing boundary. Do not prescribe permanent 100% scaling or assume this explains every Error 001. |
| AMD captured image | Black video with audio; square segmentation; alternating displaced lines. Reports name RX 7600, RX 7700 XT, RX 7900 XT, RX 6700 XT, RX 6650 XT and Radeon 610M; other reports provide only diagnostic links | P0: distinguish bad encoded media from player rendering, then inspect texture format, plane layout, pitch, dimensions and conversion/encoder ownership. These are investigation candidates, not established causes. |
| Linux startup / AppImage | Many Wayland reports show an icon or startup sound but no window, across KDE and Hyprland. Some logs end after encoder detection. Separate reports mention `hyprctl` on KDE and `AppRun` discovery | P0 for a separate Linux pass: separate window construction, portal/compositor integration and packaging. Do not infer missing portal support from a reporter's suggestion alone. |
| Windows interface hit testing | 4K monitor has a resize edge halfway across the visible window; several top-bar buttons or the right half of switches cannot be clicked; maximized window margins expose desktop | P1: logical/physical coordinate and native hit-test review; do not assume every button failure has the same cause. |
| Hotkey persistence | F7/F8 are recognized while held but revert on release, including a report using a keyboard function layer | P1: trace key capture, validation, registration result, persistence and refresh. A modifier-only binding request is a separate product-policy question. |
| Audio | Popping/stuttering, Discord-specific Windows audio issue; Linux microphone contention depending on application start order | P0/P1: preserve source-separated samples and actual endpoint/format/timeline evidence. Windows and Linux reports require separate validation. |
| Finalization / library | Linux clips remain FINALIZING until restart; Insight Capture folder with roughly 300 GB appears empty | P1: distinguish completion notification/readiness from file validity; inspect import filtering/index traversal separately. Do not identify these as the proven thumbnail cancellation regression without evidence. |
| Resource use | Roughly 600 MB–1.3 GB RAM; a report specifically names the engine process exceeding 1.2 GB after ten minutes; 15% GPU use with game detection disabled | Measure per-process growth and replay configuration. These reports alone do not establish a leak, and disabling game detection does not establish that capture is stopped. |
| Installation / compatibility | Windows startup exception without traceback, Norton quarantine, old/hybrid GPU encoder initialization failures | Preserve exact traceback/detection/API evidence. Do not label antivirus reports false positives or infer encoder capability solely from a GPU name. |
| Game interaction | Minecraft fullscreen cursor disappears; one Fortnite anti-cheat/temporary-ban allegation on a modified Windows installation | Separate reports. Anti-cheat causation is unverified and needs dedicated investigation; do not test by risking live competitive accounts or recommend bypassing protection. |
| Linux source build | `linux_encoder_timestamps` aborts: 13 of 14 CTest tests pass in the submitted output | Obtain exact source revision, build configuration and failing assertion. Do not waive the test or infer the failure from the name alone. |

## Scope and status

All usable submissions are **FIELD REPORTED**. Their relationship to current
source changes is unverified, and **FIELD VALIDATION PENDING** applies. No report
is marked locally reproduced, fixed or field validated by this triage.

The active Alpha Stability Recovery work remains: thumbnail worker lifecycle,
Windows capture/encoder evidence and replay admission, followed by source-separated
audio qualification. The new export adds evidence; it does not silently authorize
reopening Linux runtime, broad AMD changes, Stage 7 feature work or Stage 8.

Most actionable new comparisons for subsequent investigation:

1. The same affected Windows machine at 150% versus 100% scaling, preserving
   monitor/output IDs, coordinates, adapter LUIDs and failing API.
2. An original affected AMD clip decoded independently of FTHR's player, with
   stream geometry, pixel format and frame-layout evidence.
3. Linux startup logs paired with the exact AppImage/source revision and desktop
   session, distinguishing no visible window from an encoder-startup failure.

No emails, uploads, reporter contact, release actions, commits or pushes are
authorized or performed by this document.
