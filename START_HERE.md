# FTHR Clips 2.0.0 Alpha — source snapshot (17 October 2026)

This snapshot includes the short replay save fix: save available healthy footage
from 0.5 seconds onward during startup/recovery, even for longer clip requests.

Extract the entire folder before building. Read BUILDING.md for prerequisites,
pinned Python dependencies, native Windows/Linux builds, optional uploader,
verified dependency downloads, packaging and verification commands.

The archive preserves app and helper source, both native engines, shared code,
website, original assets, tests, licenses, build scripts, dependency manifests,
and Windows FFmpeg headers/import libraries. Source files are losslessly stored.
Generated builds, installers, downloadable runtimes, caches and personal data
are omitted. Fetch the verified dependencies using tools/fetch_third_party.py
as described in BUILDING.md; this is not an offline binary bundle.

SOURCE_MANIFEST.json contains the size and SHA-256 of every other archived file.
The ZIP uses maximum standard Deflate compression for Windows compatibility.
After building, tools/update_latest_build_shortcut.ps1 creates a shortcut to
the latest local packaged app.
