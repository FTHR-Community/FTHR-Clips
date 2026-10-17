# Windows project cleanup and installer refresh

The active source tree targets 64-bit Windows. Linux capture sources, AppImage
packaging, Linux helper modules, Linux-only tests, and Linux CI jobs were removed
from the active project. Shared capture/save headers and Windows microphone
preview code remain because the Windows application uses them.

The existing folder had no Git repository. Original files are recoverable from
`build/windows-only-backup.zip`. Recursive deletion of the two source directories
was blocked by automatic approval review; they were instead moved into
`build/retired-platform-files/`, outside the active source and packaging inventory.

The installer follows the supplied reference: a dark green surface, white
Oswald headline, grey circular step indicators, and simple rectangular controls.
Body text and paths use Segoe UI. Both notebooks and custom page surfaces are
explicitly resized, so the location fields and shortcut option remain visible.

The location page includes an unchecked **Add a desktop shortcut** option. On
continuing, the checkbox selects or clears Inno Setup's `desktopicon` task.
The Start Menu shortcuts are created independently of this option.

The final bundle verifier also validates the existing optional uploader's exact
licence inventory, file sizes, and SHA-256 hashes. Its previous four-file-only
expectation incorrectly rejected the dependency licences added by the package
builder. Tests confirm that reviewed licences are accepted and extra executables
or altered payloads are rejected.

Validation:

- Full Windows Python suite: 1,348 passed, 2 skipped.
- Installer/uploader gate tests after the verifier correction: 12 passed.
- Repository lint and Python compilation passed.
- Windows shared-memory, engine-response, version, and exception checks passed.
- Source and Windows bundle dependency/asset verification passed.
- The real compiled preview was visually checked on both initial pages; keyboard
  navigation reached and toggled the desktop-shortcut checkbox.

Build output: `Output/FTHRClips-Setup-1.1.0-alpha-x64.exe`.
This is a locally built unsigned installer. Manual install/update/uninstall
qualification and code signing remain separate release steps.
