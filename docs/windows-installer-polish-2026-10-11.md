# Windows installer visual polish

- Added 24 px of clearance between the display heading and step indicators, with a taller header and window to preserve page space.
- Replaced GDI ellipses with supersampled, antialiased numbered circles at nine common Windows display sizes. The indicators keep equal pixel width and height.
- Replaced the obsolete close PNG with artwork rendered from `FTHR_UI/assets/icons/close.svg`, matching the current application's icon mapping. The button has square bounds, inset padding, and preserves the SVG's proportions.
- Made Segoe UI the default font for body copy, built-in dialogs, legacy cleanup options, and downgrade errors. Oswald remains the display face for headings and navigation.
- Normalized the Privacy Policy link's casing and weight.
- Gave preparation errors consistent side margins and a visible Cancel action.
- Preserved the optional, unchecked desktop shortcut choice and existing downgrade protection.

`tools/generate_installer_assets.py` regenerates the artwork before the source asset gate during the Windows build. The generated files have provenance and locked hashes in `tools/release_asset_manifest.json`.

The preview build now always stops before installation. Its simulated downgrade warning permits visual checks without writing application files or registry entries.

Validation: compiled Inno Setup previews; visually inspected consent, location, and simulated downgrade screens; installer contract tests passed. The production package is built with the Windows bundle and prerequisite checks using `python tools/build_windows_installer.py --skip-pyinstaller --skip-fetch-vcredist`. No software was installed during these checks.
