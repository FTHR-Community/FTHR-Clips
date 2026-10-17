# Windows installer alpha 2.0 — 13 October 2026

Built `Output/FTHRClips-Setup-2.0.0-alpha-x64.exe` from the current source.
Product version is `2.0.0-alpha`; numeric Windows version is `2.0.0.0`.
The release date is 2026-10-13. The uploader package was rebuilt as 1.3.1,
and its verified package hash was regenerated in Core.

Resolved the existing Ko-fi asset gate by recording the official promotional
permission and the existing symbol's verified visual match. The original PNG
acquisition URL remains unknown and is recorded as such. The permission notice
is bundled separately from the application licence.

Corrected stale Qt Quick/QML exclusions, retained required Quick, QML and
MultimediaQuick runtime libraries, and limited QML assets to the modules used
by the application. The first core build ran before the final QML inventory
filter edit; its unused QML files were removed with the identical inventory
rule before compiling the installer. Future builds apply the filter in FTHR.spec.

Source and Windows bundle licence checks, installer-input checks, final installer
lifecycle checks, and version consistency passed. Packaging/version-related
tests and the performance/SFTP tests passed in focused runs. Both bundled
Snapshot.qml and VideoPreview.qml loaded using the bundle's QML import paths
in an offscreen Qt smoke check; this is not a physical GPU/capture qualification.

The installer is unsigned, suitable for local testing. It was built, not installed
or published. Its SHA256 is saved alongside it in the .exe.sha256 file.

Build logs:
- build/windows-installer-alpha-2.0-build.txt (optional package builds)
- build/core-alpha-2.0-build.txt (application bundle)
- build/installer-alpha-2.0-build.txt (installer and final gates)
