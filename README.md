
## Contribute a plugin

Want to submit your own plugin? See [CONTRIBUTING.md](CONTRIBUTING.md) for the folder scheme, required details and pull request process.

## Install

Open **Settings → Plugins** and drop a ZIP, or use **Choose ZIP…**. Click **Install plugin** (or **Update plugin** for an existing plugin). Plugins are enabled when first installed. Installing a ZIP with the same ID updates that plugin. An update must retain the same type and use the same or a higher version. Use the `.zip` build of a plugin in this page; the picker and drop field expect ZIP files.

- **Export preset packs** add read-only choices to the export preset list while enabled. Disabling or uninstalling the pack removes its choices and leaves personal presets intact.
- **Theme packs** are available to apply while enabled. **Apply theme** replaces the current customization with an editable copy of the pack theme. Export your current theme from **Customize** first if you want to keep a copy. Updating, disabling, or uninstalling the plugin leaves the applied customization intact.
- **Feature plugins** run Python code inside the app. Only install packages from authors you trust: they have the app's permissions. Enabled features start automatically with the app, including when it starts in the tray. Ultra Performance Mode pauses feature plugins until it is turned off. Disabling, removing, or exiting stops their runtime. An update restarts an active feature if the feature ZIP changed. Use **Settings** on an installed feature's row to configure it.


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


