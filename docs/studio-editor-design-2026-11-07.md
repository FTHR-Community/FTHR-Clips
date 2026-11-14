# Studio editor and application audio controls

The user selected installer concept B (guided setup) and editor concept C (Studio), with collapsible sections wherever space becomes tight. Existing charcoal surfaces, mint primary actions, and Oswald typography remain the visual authority.

## Editor layout

- Video tools, speed, color/effects, and clip/file details occupy the left inspector.
- The central preview keeps its existing playback and crop behavior.
- The right inspector contains only the clip's audio mix. Capture app exclusions live in Audio settings.
- Undo/redo sit in the timeline toolbar. Export quality, export jobs, Share, and Export are above the workspace. Export is the primary action.
- The existing editable timeline spans the window below all three columns. No illustrative waveform data from the concept image is used as recorded audio.
- Each inspector scrolls independently. The minimum supported 960×600 window retains a 422×268 preview region and the full-width timeline in the isolated layout render.

## Audio identity

Audio rows show 24-pixel executable icons resolved from the local running-app inventory. They keep readable names, tooltips, accessible labels, gain, mute, and remove actions. An unavailable icon uses a fallback; clip metadata cannot specify an arbitrary icon file path.

The running-app picker is searchable and refreshable. The exclusion controls affect future Programs-mode capture, not the current clip's recorded sources. Existing audio edits remain clip-local.

## Validation evidence

`tools/render_editor_studio_review.py` renders an isolated settings profile, sample poster, and synthetic audio-source metadata at 1440×900, 1100×760, and 960×600. It does not start capture. Images are under `ui-preview/editor-studio/`, including expanded controls, the exclusion section, and the running-app picker. These images establish layout behavior, not live playback or recording correctness.

`tests/test_editor_studio_layout.py` covers minimum-size layout, expanded-panel scrolling, and icon/mute controls. Existing timeline, speed, export, editor-state, and session tests cover the unchanged editing paths.

Installer build and audio capture qualification are documented separately. Building the native engine or installer does not update the running installed application.

## Original concept implementation review (historical)

Disposition: ship for the requested UI scope, with the main-editor keyboard check below remaining unverified. No material visual clipping defect was found in the reviewed evidence.

The finished surfaces retain the approved guided installer B and Studio editor C compositions. Charcoal surfaces, mint primary actions, gray borders, Oswald typography, and compact desktop controls follow the incumbent system. The editor's normative token sources remain `FTHR_UI/core/theme_manager.py` and `FTHR_UI/ui/style.py`; this review introduces no palette, typography, or theme-system changes. The installer follows the palette and guided sidebar documented in `windows-installer-guided-2026-11-07.md`.

Evidence checked:

- `ui-preview/editor-studio/studio-1440.png`, `studio-1100.png`, and `studio-960.png`: Export remains prominent, preview and full-width timeline remain visible, and the inspectors use independent scrolling at smaller sizes.
- `studio-expanded.png` and `studio-exclusions.png`: expanded sections remain within their inspector columns; capture exclusions explain that they affect future Programs captures. `running-apps.png` and the editor renders show application icons alongside readable identities.
- Native installer evidence in `build/guided-installer-evidence/`: Privacy, Folders, synthetic progress, and synthetic finish retain the guided sidebar; the finish preview visibly includes the checked Open FTHR Clips option.
- Live native checks in `FTHR Studio isolated review`, launched with `python tools/render_editor_studio_review.py --interactive`: pointer collapse/expand worked; the app picker opened; typing `discord` filtered to Discord and DiscordSystemHelper with icons; Tab/Tab/Down selected a result and enabled Exclude app; Escape dismissed the picker. No exclusions were saved, and only the isolated review window was closed.

One low-priority design suggestion remains: at 960×600, capture exclusions require scrolling past the audio mix or collapsing it. Preserving direct access to that section heading could improve findability. This is not an observed clipping or functional defect; an acceptance check would be immediate access to exclusions at the minimum size.

Main-editor keyboard traversal was inconclusive in the isolated native session: Tab/Space attempts did not establish consistent visible focus movement, and the accessibility readout repeatedly reported a stale offscreen detail field. The picker keyboard checks succeeded. Recheck main-editor traversal directly before claiming full keyboard acceptance; the evidence does not establish a product defect.

The screenshots and isolated session use synthetic clip metadata and a sample poster. Real playback, recording, timeline-thumbnail loading, installation, and completion launch were not qualified by this review. Synthetic progress/finish controls establish layout only; see the installer document for its separate lifecycle checks and remaining VM qualification.

## Responsive refinement

The editor now distributes inspector widths, text sizes, and filmstrip height according to logical window size and clip aspect ratio. The preview preserves its aspect ratio; expanded panels still scroll independently. Undo/Redo are next to the timeline controls. The limiter checkbox, imported-audio button, backup explanation, output peak readout, and capture exclusions were removed from the editor. Existing saved audio edits and limiter processing remain supported.

Capture app exclusions and the new **Keep System Audio Backup** preference are in Audio settings. The preference applies to new Programs captures; exclusions still force backup omission so excluded applications cannot leak into a full-device backup.

Current isolated render evidence is `ui-preview/editor-responsive/`: 960×600, 1440×900, 1920×1080, 2560×1440, 3840×2160, expanded panels, and a portrait crop. Regression checks include resizing back from a large portrait layout, timeline history placement, scroll bounds, and dynamic filmstrip painting. 67 focused editor/timeline/project/export/speed tests pass. These sample-poster renders do not qualify live recording or playback.

Independent review accepted the refreshed renders after correcting high-resolution text clipping and filmstrip geometry. No material visual defect remained. Latest review was screenshot/code based; live playback and full keyboard navigation remain unqualified.

## Fitted-preview follow-up

The user's 1920×1032 screenshot exposed wide gutters with a 1280×1024 source. The earlier sample only exercised a widescreen source. The layout now calculates the width usable by the fitted source image, allocates surplus width to the inspectors, and keeps the timeline compact (116–145 logical pixels for the filmstrip). At broad inspector widths, video/speed and clip/file details reflow into two columns. Narrow windows retain a single scrollable column. Crop overlays preserve the full-source preview geometry; only displayed stretch changes its aspect ratio.

Fresh evidence is `ui-preview/editor-fitted/`, using an extracted frame from the same local Valorant clip. At 1920×1032 the preview container is 888×689 with approximately 13-pixel side gutters, replacing the roughly 220-pixel gaps in the user's screenshot. The source's own letterboxing and taskbar remain part of the recorded image. Rendered checks cover 960, 1440, 1920, 2560 and 3840 widths, expanded controls and crop overlays. Independent review accepted these states. Automated regression checks cover bounded gutters for 5:4, 4:3 and portrait source clips, two-column reflow and restoration, and stable crop-overlay sizing. The native Python layout scan returned no findings; rendered evidence remains the visual authority.

This follow-up changes source files used by the running development checkout. Restart the source app to load the changes. The preceding packaged installer is not refreshed by a source edit.
