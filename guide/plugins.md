# FTHR Clips pluhhh

Open **Settings → Plugins** and drop a ZIP, or use **Choose ZIP…**. Click **Install plugin** (or **Update plugin** for an existing plugin). Plugins are enabled when first installed. Installing a ZIP with the same ID updates that plugin. An update must retain the same type and use the same or a higher version. Use the `.zip` build of a plugin in this page; the picker and drop field expect ZIP files.

- **Export preset packs** add read-only choices to the export preset list while enabled. Disabling or uninstalling the pack removes its choices and leaves personal presets intact.
- **Theme packs** are available to apply while enabled. **Apply theme** replaces the current customization with an editable copy of the pack theme. Export your current theme from **Customize** first if you want to keep a copy. Updating, disabling, or uninstalling the plugin leaves the applied customization intact.
- **Feature plugins** run Python code inside the app. Only install packages from authors you trust: they have the app's permissions. Enabled features start automatically with the app, including when it starts in the tray. Ultra Performance Mode pauses feature plugins until it is turned off. Disabling, removing, or exiting stops their runtime. An update restarts an active feature if the feature ZIP changed. Use **Settings** on an installed feature's row to configure it.

Try the sample packs in [examples/plugins](../examples/plugins). Use local ZIPs from authors you trust. The optional uploader is managed through the app’s upload settings; it uses a separate package format.

### Harmless installation test

Drop [fthr-test-plugin.zip](../examples/plugins/fthr-test-plugin.zip) into the Plugins page. It appears as **FTHR Test Plugin** and can be enabled, disabled, updated, or removed. It contains only a manifest, runs no code, and contributes no themes or presets. Installation only records the plugin in the plugin manager.

To make your own no-operation test package, ZIP [test-plugin-manifest.json](../examples/plugins/test-plugin-manifest.json) at the archive root under the name `manifest.json`. Its type is `test` and its `contributes` object must be empty. Only an optional declared cover may accompany the manifest.

## Package format

Theme, export-preset, and feature ZIPs contain a manifest and contribution at their root, plus an optional cover image. Name the manifest `manifest.json` and use UTF-8 for JSON. The contribution is identified by the manifest. Test ZIPs require only the manifest. API 1 supports theme, export preset, no-operation test, and feature packs. Older app builds that only support declarative packs cannot install features.

```json
{
  "id": "example.review-presets",
  "name": "Review Presets",
  "version": "1.0.0",
  "api_version": 1,
  "author": "FTHR Examples",
  "type": "export-presets",
  "contributes": {
    "export_presets": "presets.json"
  }
}
```

Keep the ID stable across releases. IDs use lowercase ASCII letters and digits, with dots or hyphens between segments, up to 64 characters. Versions use three numeric parts of up to six digits each, without leading zeros, such as `1.2.0`. The `name` and `author` fields are required, each with a printable single line of up to 120 characters. Choose a unique ID for each pack.

An optional `description` provides a printable single-line summary of up to 1000 characters. It appears in the installation review and installed-plugin row. Feature authors should disclose capture or processing tradeoffs before users enable the package.

For `export-presets`, the contribution is a JSON array containing 1 to 64 presets. Each requires a unique `preset_id`, `name`, and `target_size_mb`. Presets use the same rules as built-in export planning. Optional fields include `target_width` and `target_height` (set together), `target_fps`, `audio_bitrate_kbps`, `video_codec` (`h264` or `source`), and `safety_margin`.

```json
[
  {
    "preset_id": "review-720p",
    "name": "Review copy · 30 MB",
    "target_size_mb": 30,
    "target_width": 1280,
    "target_height": 720,
    "target_fps": 30,
    "audio_bitrate_kbps": 96
  }
]
```

For a theme pack, use `"type": "theme"` and `"contributes": {"theme": "theme.zip"}`. Create the inner ZIP using **Customize → Export theme**, then place it alongside the manifest in the outer ZIP. Theme icons in plugin packs must use PNG, JPEG, BMP, or ICO. The inner archive supports the app's existing sound and font formats and must include every asset referenced by `theme.json`.

ZIPs must be unencrypted and use stored or deflate compression. Each archive has a limit of 32 MiB on disk and when unpacked, and at most 128 entries. JSON files are limited to 256 KiB.

### Cover images

Any plugin type can add `"cover": "cover.png"` to its manifest and include that PNG or JPEG at the outer ZIP root. Covers must decode successfully, be at most 4 MiB, and measure no more than 4096 pixels on either side. Remote URLs, SVGs, and nested paths are not supported. The cover appears in the installation review and installed-plugin row; it keeps its aspect ratio. Packages without covers use the compact text layout. Updates replace or remove the cover according to the new manifest; toggling a plugin preserves it.

## Gary Mode

Install [fthr-gary-mode.zip](../examples/plugins/fthr-gary-mode.zip) using **Settings → Plugins → Choose ZIP… → Install plugin** in the updated app. Version 1.1.0 restores **Gary Mode** to **Settings → Audio**, directly below microphone monitoring, and puts its MIN/MAX handles in the live-level meter. **Settings** on the installed Gary row takes you there. Existing Gary settings are reused, including the previous on/off preference. Audio capture must also be enabled for the desktop response. The default Gary image, occasional cat appearance, click-through fade, and cover artwork are included in the package.

Gary's code and assets remain in the plugin. Its Audio controls appear only while the plugin is installed and enabled. Disabling or removing it restores the ordinary live-level meter; saved preferences remain available for reinstallation. Version 1.1.0 requires the app's cover and settings-placement support. Update older builds that lack these features before installing it.

Source: [plugins/gary_mode](../plugins/gary_mode). Rebuild the ZIP with `python tools/build_gary_plugin.py`.

## Feature API 1

Use `"type": "feature"` and `"contributes": {"feature": "feature.zip"}`. The inner ZIP contains a Python package with `__init__.py` at its root and optional `.py`, PNG, JPEG, BMP, or ICO files. UTF-8 `.txt` attribution files (up to 256 KiB) and UTF-8 `.rnnn` audio models (up to 4 MiB) are also supported. Python source files are limited to 256 KiB each. Inspection validates the archive and source syntax without executing it. Feature packages are not sandboxed.

Export a `Plugin(context)` class from `__init__.py` with `start()` and `stop()` methods. Constructors should only initialize state; acquire resources in `start()` and release timers, windows, callbacks, and owned streams in `stop()`, including after partial startup failure. Provide `settings_widget()` for the default settings action, returning a QWidget displayed in the app's themed, frameless settings dialog. A plugin that provides `open_window()` instead does not need a settings widget. Use shared `ui.style` tokens and `set_theme_style` for controls, and `ui.dialogs` for message dialogs. Use relative imports for plugin modules. The host supplies `context.window`, `context.settings`, `context.ready`, and `context.microphone_device()`. Call `context.refresh_microphone_preview()` after starting or stopping an owned shared stream so the visible preview can reconnect. Runtime files live in a temporary directory; persist preferences through the settings service. Third-party dependencies must already be available in the app.

To place controls inside Audio, declare `settings_location = 'audio.microphone'` on the plugin class. The host creates `settings_widget()` lazily when Audio opens and mounts it below monitoring. The plugin's **Settings** button navigates to that section. Optionally implement `settings_meter()` returning a QWidget with a `set_level(float)` method; the host displays it in the live-level row and feeds it from the existing microphone preview. The host detaches contributed controls and disconnects meter signals before calling `stop()`. The plugin must release its widgets in `stop()`; a contributed meter is returned to the panel's ownership for cleanup.

## Optional Windows Border-Free Capture

[Windows Border-Free Capture installation and tradeoffs](border-free-capture-plugin.md) describes the separately installable [fthr-border-free-capture.zip](../examples/plugins/fthr-border-free-capture.zip). Enabling it switches new capture sessions to DXGI full-monitor capture without a Windows Graphics Capture yellow border. All visible apps, windows, and notifications on the selected monitor are recorded; the cursor is always included, and window/game targeting and cropping are unavailable. Saved source preferences are preserved. DXGI failure stops capture without falling back to WGC. This optional feature requires the updated Windows 2.1 host; stock 2.0 builds are unsupported.

Feature plugins can opt into this behavior with owner-scoped `context.register_border_free_capture()` and release it with `context.unregister_border_free_capture()`. Registration applies to new capture requests and is removed when the owning feature stops.

## Optional osu! Capture Compatibility

[osu! Capture Compatibility](osu-capture-plugin.md) requests WGC while
`osu!.exe` runs, bypassing the DXGI capture path that can repeatedly lose access
during fullscreen gameplay. Install
[fthr-osu-capture.zip](../examples/plugins/fthr-osu-capture.zip) directly in the shipped Windows 2.1.0 app. Version 1.1.0 includes an in-memory
compatibility adapter; no app update or source launcher is needed.
Selected sources and preferences are preserved; switches reset replay history
and may show a Windows capture border. Disable Border-Free Capture first.

Feature plugins register this policy with `context.register_wgc_capture()` and
release it with `context.unregister_wgc_capture()`. Registrations are owner-scoped
and removed when the feature stops. This does not guarantee exclusive-fullscreen
capture compatibility; see the linked test procedure.

## Optional Stream Deck controls

[Stream Deck installation and usage](stream-deck-plugin.md) describes the
separately installable FTHR bridge and Elgato companion. They provide replay save,
manual recording, highlight marking, screenshots, and live status over an
authenticated connection on the same computer.

## Optional Capture Startup Doctor

[Capture Startup Doctor installation and usage](capture-startup-doctor-plugin.md) describes the separately installable [fthr-capture-startup-doctor.zip](../examples/plugins/fthr-capture-startup-doctor.zip). It collects local startup diagnostics and can export a support report. Restart the app after installation to trace initial startup. Experimental retry is off by default.

## Standalone feature windows

[Interface Studio](#optional-interface-studio) is the optional visual interface
editor. Install `examples/plugins/fthr-interface-editor.zip` through Plugins,
then choose **Open studio**. Installation leaves the default layout intact;
custom designs are applied explicitly and can be reset with **Ctrl+Alt+Shift+R**.

A feature plugin may implement `open_window()` to open or focus its own modeless
window instead of using the host's small settings dialog. Set `configure_label`
to a short action such as `Open studio` to label its installed-plugin button.
The plugin owns this window and must close and release it in `stop()`.

Interface plugins may implement `interface_host_ready()`. The host calls it on
the next Qt event-loop turn after the feature starts. On a normal launch, the
main window has finished building and styling by then. On a tray launch, the
main UI may not exist yet; check for it before touching UI widgets. The host
skips callbacks for inactive plugins and stops a plugin if initialization fails. Installing an interface plugin should preserve the
stock layout until the user explicitly applies a design. Every change must be
reversible when the plugin stops.

## Optional Interface Studio

Use the updated application source or a new build containing the media-surface
hooks. Existing installed executables are not updated by installing the ZIP
alone. Version 1.3 reports an update requirement on older hosts.

Install [fthr-interface-editor.zip](../examples/plugins/fthr-interface-editor.zip)
in **Settings → Plugins**, then choose **Open studio**. The first window lists
your saved themes. Edit, apply, duplicate, delete, or import a theme there.
**New theme** offers the default layout or a blank main canvas. Opening the
plugin, choosing a theme, and editing drafts do not change the active app.
**Apply design** saves the named design and makes it active.

### Editing and navigation

- The inspector uses FTHR’s standard minus/plus controls. Typed values and button
  changes update the central preview immediately.
- Hold **Ctrl** and use the mouse wheel to zoom. **H** toggles Hand mode;
  **V** returns to Select. Hold **Space** for temporary panning, or drag with the
  middle mouse button. **Shift+1** fits all visible layers, **Shift+2** fits the
  selection. These navigation conventions follow [Figma’s zoom/view tools](https://help.figma.com/hc/en-us/articles/360041065034-Adjust-your-zoom-and-view-options).
- **Layers and order** combines the canvas layers with settings categories and
  sections. Drag the six-dot grip at the right of a row to reorder it. Its
  context menu also offers Move up/down for keyboard-friendly ordering.
- Ctrl+click or drag a selection box to select multiple elements. **Ctrl+G** or
  **Group selection** in the right-click menu groups them. Groups move together;
  **Ctrl+Shift+G** ungroups. Arrow keys nudge by one pixel, or ten with Shift.
  Undo/redo includes grouping, order, styling and geometry.
- **Add element** marks missing built-in controls with a red dot. Restore a
  missing control or add labels and action buttons. Missing Settings access
  remains a warning, not a restriction on saving.

### Editable surfaces

**Main window** previews loaded clip thumbnails. An empty library uses a clearly
labeled sample. The **Clip card** surface edits the reusable card template:
thumbnail, title, metadata, duration badge and actions. Card width (300–440 px),
height and gap affect the live library grid; templates also apply to newly
materialized or recycled cards.

**Clip editor** exposes its real header, video preview, video tools, inspector,
playback controls, timeline and export controls. Its studio preview uses a
static poster and filmstrip without creating a media player. Applied designs
also affect editors opened later. This edits the interface, not clip media.

**Settings** surfaces expose named controls and sections, including category and
section ordering. Radius, padding, margin, border, font size, fill, text and
outline colors, visibility and geometry are editable. Native widgets retain
their live actions. Geometry uses local pixels; preview designs at the window
sizes you use. Native file dialogs outside the studio retain their normal UI.

### Element content and fills

Controls expand in **Layers and order** to show their **Text** and **Icon**
layers. Select one of these children to move it, resize its bounds, change its
typography or image, or hide it independently. Removing a native content layer
hides that part; it does not remove the control's action. Moving a parent carries
its content layers with it, including content you have already repositioned.
Collapse a control to keep the layer list compact. The parent inspector's text
and icon shortcuts edit the same child layers. Existing themes retain their
saved content overrides when these layers are added. Untouched children inherit
the native layout; explicit content edits remain attached to their owner.

The inspector separates an element's **Layer name** from its displayed **Text**.
Native labels and buttons expose text overrides; supported buttons expose an
icon picker, icon size, Hide icon and Reset controls. Resetting content restores
the app's original content. Text edits do not change a control's action.
Settings headings and descriptions are separate selectable text layers. Styling
a native setting's value can change its typography without replacing the value
or changing what the input controls.

**Fill** offers Inherit, Solid, Linear gradient, Radial gradient and Image.
Gradients support two to eight color stops and linear gradients have an angle
control. Image fills offer Cover (center crop), Contain (preserve the whole image)
and Stretch. Select the main **Background** layer to change the canvas background.
**Add element → Image** places an image layer; **Rectangle** adds a shape that can
also use a gradient or image fill.
Image fills and replacement icons are embedded in the design, so moving the
source image does not break the theme. PNG, JPEG, WebP, BMP and self-contained SVG
imports are normalized to PNG (maximum 4,096 pixels per side and 8 MB per asset).

Preview and live widgets use the same content and appearance overrides, including
corner clipping, outlines and fills. Zero radius explicitly requests square
corners; Inherit restores the native radius. All these edits participate in
undo/redo and design import/export.

### Full appearance and sounds

**Customize** opens the same typography, colors, icon colors, icons,
capture-card styling and sounds controls available in FTHR’s Customize page.
It operates on an isolated draft. **Use in design** adds the choices to the
current design; **Apply design** publishes them to the app. Cancel leaves the
app and design untouched. Fonts, icons, sounds and sound volumes travel with
the design in JSON exports. Embedded theme archives are bounded to 32 MB;
the design library and exported JSON are bounded to 128 MB.

Studio confirmations, color pickers and file choosers use the app’s theme.

Press **Ctrl+,** to reach Settings even without its button. Press
**Ctrl+Alt+Shift+R** to restore the default interface and the appearance from
before the design was applied. Disabling or removing the plugin also restores
that state. Saved designs remain in `interface-designs.json` next to the app’s
settings file. A separate appearance baseline allows recovery after restarts.

Build the ZIP and distribution package with
`python tools/build_interface_editor_plugin.py`.

## Optional RNNoise

[RNNoise installation and behavior](rnnoise-plugin.md) describes the separately installable package in `examples/plugins/fthr-rnnoise.zip`. It adds opt-in speech noise suppression to identified microphone tracks during clip preview and export. It preserves original recordings and requires this updated host; older builds do not expose the audio-effect API. It is not installed automatically.

Feature plugins can register an effect using `context.register_microphone_effect(effect_id, filter_factory)` and remove it with `context.unregister_microphone_effect(effect_id)`. The no-argument factory returns a trusted FFmpeg filter description or an empty string. The host snapshots the description when a microphone source is opened, so preview and export use the same effect. Reopen the clip after changing an effect setting or disabling a plugin. Model files referenced by snapshots must remain available after plugin shutdown; use a persistent content-addressed cache rather than the temporary extracted package directory. Effects do not run on system, application, or already-combined tracks.
