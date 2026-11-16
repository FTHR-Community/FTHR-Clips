# FTHR Clips 2.1.2 — Windows source

Snapshot: 14 November 2026. See [BUILDING.md](BUILDING.md) for Windows prerequisites, dependency verification, build and test commands.

This tree includes the Windows app, native engine, shared code, optional uploader support, assets, tests and licences. Website files, standalone plugins, Linux engine and AppImage packaging are excluded. The app retains its plugin host so separately distributed plugins can be installed.

Three Linux-named Python helpers are retained because shared Windows UI modules import them. Generated builds, installers, runtimes, caches and personal data are excluded. SOURCE_MANIFEST.json records the committed source file sizes and SHA-256 hashes.
