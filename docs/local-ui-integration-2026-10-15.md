# Local 2.0 UI/source integration — 2026-10-15

Scope: merge the colleague's `FTHRClips-2.0.0-Alpha-Source.zip` into the
existing local `FTHR-Clips-master` directory. This is not a GitHub branch merge
or a public-release qualification. No commit, staging, push or pull request.

## Inputs and preservation

- Local branch: `integrate/latest-project-source`.
- Existing HEAD: `d990630f5befd0d19ddf371dddf1e39dd5f87c02`.
- Incoming ZIP SHA-256:
  `BEBBD311D00DC65731AF37187F1E1D91E6F9CF67F0F138D3983DF7F50F8EC020`.
- The 771 incoming manifest entries were checked against the ZIP bytes.
- The 837 existing source files were backed up and hash-verified before merge.
  The sibling review directory contains `before-merge-source`, the unmodified
  extracted ZIP, isolated validation builds, and detailed comparison reports.
- Missing ZIP files are not deletions: Linux, the website, newer camera
  safeguards, the window preview, and existing release/ABI gates are retained.
- Original recordings, sidecars, settings, running sessions and existing
  build artifacts are not replaced. The source archive's `SOURCE_MANIFEST.json`
  is not an integrity manifest for the modified tree and is not copied over.

## Corrected integration defects

1. Reject a fourth editor before closing the active editor. Existing edits and
   export cancellation state survive a rejected open request.
2. Software H.264 uses elapsed-QPC presentation timestamps rather than frame
   count, preserving real elapsed time when source cadence is irregular.
3. Keep audio-enabled focus-gated captures on raw QPC across all sources;
   resume starts a fresh replay generation. Compact pause time is restricted
   to video-only captures. This deliberately requires fresh replay warmup
   after an audio-enabled focus pause, rather than mixing incompatible clocks.
4. Restore the requested-duration coverage check for partial replay history;
   a sub-second tail cannot be an ordinary successful 30-second replay.
5. Both WGC size-change paths request monitor reconfiguration consistently;
   a selected-window failure does not silently change the source.
6. Retain Linux dispatch and no-overwrite screenshot publishing alongside the
   new Windows UI. Bundle the shared QML/Qt Quick resources on both platforms;
   remove build instructions that deleted required Quick/QML libraries.
7. Install optional SFTP test dependencies and build its actual frozen package
   in Windows CI. The local SFTP fixture now waits for its controlled lifetime,
   so slow one-file startup does not silently terminate the test server.
8. Restore viewer import preflight before changing the active viewer, camera
   timer or editor sessions. Missing multimedia DLLs produce bounded actionable
   diagnostics rather than destroying the previous playback state.

The Windows package filter previously discarded the application's own QML
files as if they were unapproved Qt modules. It now distinguishes application
resources from Qt modules; source and Windows/Linux artifact gates require the
three playback QML/shader resources with exact hashes. A deliberately incomplete
intermediate Windows bundle failed this gate before the corrected bundle passed.

The existing combined-audio finalizer's AST is unchanged from the pre-merge
source. No gain, audio continuity, drift policy or shared-memory layout change
was added by these integration fixes. The mapping remains `FTHR_SharedMemory_v4`.

UI test fixtures isolate global application style/motion state and supply an
explicit offscreen hover/style state. Midpoint colors, reversal, click-through
and overlay disposal remain asserted. Library tests wait for bounded observable
layout completion rather than assuming 50 ms is always enough; the production
4-ms materialization budget is unchanged and its retry path has a regression test.

## Validation

- Windows Release x64 engine: built successfully, zero errors/warnings.
- Playback mixer: built successfully, four existing FFmpeg header warnings.
- Native deterministic suite: **20,160 checks passed**; rerun by the reviewer.
- Software-timestamp and replay-coverage regressions: incoming implementation
  fails, corrected implementation passes in independent sandbox builds.
- Qt Quick shader: rebuilt separately with the pinned compiler; byte-identical
  to the shipped `color.frag.qsb`.
- Isolated offscreen UI rendering: library, settings, popup and editor rendered
  without capture/hotkey services. This is not physical capture/playback proof.
- Source shared-memory, response-order and version gates: passed.
- Source license/hash gate: 198 checks, zero failures; missing vendored Linux
  FFmpeg is a warning, not a Linux artifact qualification.
- Final Windows PyInstaller bundle: built successfully; 121 artifact checks,
  zero failures/warnings. Bundled engine and mixer hashes match the independently
  built native artifacts. Test executable:
  `C:\Users\Tom\Documents\FTHR-Clips-review-2.0-20261015\validation\windows-bundle-final\FTHRClips\FTHRClips.exe`.
  SHA-256: `2A5FC2E8E3155BE9921B47CD73FC679A553E24CA6DF38E0CA2DE265FD06B3F30`.
  Native engine SHA-256:
  `19F42703E7F1F50301D8B280649D5033CE14DB846FCAF192A4B79EE970544706`.
- Ruff, dependency consistency, source compilation, exception-debt and version
  gates: passed. Linux and Windows shared-memory contracts are unchanged.
- Full Python suite on the isolated candidate: **1,651 passed, 56 skipped**,
  two existing Qt mouse-event deprecation warnings, 189.77 seconds. Evidence:
  `validation/merged-full-suite-accepted.xml` in the review directory.
  The earlier failed runs remain separately recorded in
  `merged-full-suite.xml` and `merged-full-suite-final.xml`; neither is a pass.
- Polluted-order hover reproduction: 766 passed, eight skipped; the isolated
  hover suite also passed all seven tests after test-environment isolation.

## Local deployment

The allowlisted plan contains 146 updated source files, 212 additions and 691
unchanged files (1,049 total). The preflight independently verifies all 837
backup files and all 771 incoming manifest entries, rejects concurrent edits,
and permits no deletions. Detailed Apply and independent post-copy hash reports
are stored outside the repository in the review directory's `reports` folder.
The original branch/HEAD and empty staging index must remain unchanged. A dirty
worktree is intentional: these are uncommitted local integration changes.

## Remaining qualification limits

- Physical NVIDIA/AMD/Intel gameplay, real microphone/system A/V sync,
  repeated F9 saves and recovery on this integrated build: **NOT RUN**.
- Linux native/AppImage build and physical capture: **NOT RUN** here. Linux
  source/build gates remain present; the RPATH baseline is unchanged.
- Existing field reports remain **FIELD VALIDATION PENDING**.
- Asset-origin declarations from the incoming archive were retained, not
  independently proven by this merge. Technical hash/notice checks do not
  resolve any outstanding provenance or legal-release decision.
- Existing local binaries and optional plugin packages are deliberately not
  overwritten. Rebuild using `BUILDING.md` before evaluating the merged source;
  the isolated review bundle is a separate test artifact, not a release.
  Its generated uploader binding belongs to that isolated bundle only. The
  source merge uses the archive's reviewed source binding; existing old plugin
  packages are not made compatible merely by copying the source.
