# Website-hosted Windows updates

The Windows application checks a small HTTPS JSON manifest and offers to download a newer Windows x64 installer. The default endpoint is `https://downloads.fthrclips.com/updates/windows-x64.json`. This is the proposed deployment path; it must be created on the website before update checks can succeed. The repository change does not publish a manifest or installer.

Controls are in **Settings → General → Updates**, also reachable through settings search. Automatic checks are disabled by default. Enabling them requires a privacy notice explaining internet access, IP/request metadata exposure, and that clips are not uploaded. Only after consent do packaged Windows builds check shortly after startup and every 24 hours while running. Existing preferences without recorded privacy consent also remain off. Manual checks and downloads use the internet even when automatic checks are off. Source checkouts only check when requested manually. Network failures appear in the Updates status without interrupting capture. Downloaded installers are kept under `~/.fthr/updates`, accessible with **Show in folder**. **Open installer** asks first and rechecks the file hash. After confirmation, the main app takes the same full-exit path as its power button, including editor-save checks and capture/engine teardown. Only after normal shutdown completes is a hidden helper armed; it waits up to 60 seconds for the UI and its owned engine/capture-card process IDs to exit, rechecks SHA-256, and opens Setup. A blocked editor save leaves the app open and does not arm the installer. An uncertain/forced teardown does not arm it either. Helper failures keep the installer and write `installer-handoff.log` next to it. Finish recordings and exports before confirming. Newly compiled installers also block installation while the FTHR single-instance mutex is held, including by a source build. Linux does not download Windows installers.

The application downloads on user request, checks the exact byte count and SHA-256, and only then makes the completed `.exe` available. It never runs an installer without the user choosing and confirming Open installer. Unsigned installers are supported. Windows may show its usual unknown-publisher or SmartScreen prompt when the user runs one. SHA-256 detects corruption or a mismatch with the manifest; because the manifest and installer share the website's trust, it is not an independent publisher signature. Protect the hosting account and HTTPS configuration.

## Manifest format

```json
{
  "schema_version": 1,
  "platform": "windows-x64",
  "version": "2.1.0-alpha.1",
  "url": "https://downloads.fthrclips.com/releases/FTHR-Clips-2.1.0-alpha.1-windows-x64.exe",
  "sha256": "<64 hexadecimal characters generated from the installer>",
  "size": 123456789,
  "notes": "Brief plain text release notes."
}
```

`notes` is optional and limited to 8,000 characters. Manifest responses are limited to 64 KiB and installers to 2 GiB. The URL must be HTTPS, without credentials or fragments, and point to an `.exe`; all redirects must remain HTTPS. Redirected installer URLs must also end in `.exe`. Serve real installers directly rather than HTML landing pages. Each network operation has a 30-second timeout; downloads also have a 30-minute overall limit. Cancellation takes effect between network reads, so a stalled read can take up to the request timeout to stop. Failed and cancelled downloads remove their temporary files and preserve any existing completed installer.

Versions use strict Semantic Versioning. Examples in ascending order are `2.1.0-alpha`, `2.1.0-alpha.1`, `2.1.0-beta.1`, `2.1.0-rc.1`, `2.1.0`, `2.1.1`. Only strictly newer versions are offered; there is no downgrade. Build metadata such as `+build2` does not change precedence. Rebuilding the existing `2.1.0-alpha` version will not notify users already running that version: increment the version, for example to `2.1.0-alpha.1`. There is one feed, so an explicitly published newer prerelease is offered to all clients using this feed.

## Publishing a release

1. Build and finish the installer separately. Set the application and installer version consistently to a version newer than the installed release. This updater does not build installers.
2. Give each release an immutable filename including its version. Never replace the bytes at a published release URL.
3. Generate the manifest from the exact final installer:

   ```powershell
   python tools/generate_update_manifest.py Output/FTHRClips-Setup-2.1.0-alpha.1-x64.exe --version 2.1.0-alpha.1 --url https://downloads.fthrclips.com/releases/FTHRClips-Setup-2.1.0-alpha.1-x64.exe --output Output/windows-x64.json --notes "Update details"
   ```

4. Upload the installer first. Verify that its public HTTPS URL serves the expected bytes, size and hash. Use `Content-Type: application/octet-stream` and a correct `Content-Length`; a suitable cache policy for immutable installers is `Cache-Control: public, max-age=31536000, immutable`.
5. Publish the manifest last, replacing it atomically after the installer is available. Serve `Content-Type: application/json` with `Cache-Control: no-cache, max-age=0, must-revalidate` (or `no-store`) and purge any stale CDN copy. Clients also request revalidation. Keep older installers available for clients that already fetched an earlier manifest.

The generator uses only Python's standard library and reads but does not execute the installer. It rejects invalid versions, URL types, empty files, and oversized files. It calculates rather than accepts the byte count and hash.

## Verification

Run `python -m pytest tests/test_app_updates.py tests/test_app_updates_ui.py`. Tests use mocked network responses and dummy bytes; no real installer is fetched or executed.

For manual testing, use a staging HTTPS feed and a test Windows installation. Check the current-version case, a newer alpha/beta/stable version, invalid JSON, a missing feed, a missing installer, a changed hash, an interrupted connection and cancellation. Confirm the UI stays responsive and failed downloads leave no `.part` file. Confirm a completed download appears only after verification, and opening its location does not launch it. Separately, on a test machine, manually run the intended release installer to check normal Windows prompts and the existing installation upgrade path.

The core API is `check_for_update(manifest_url, current_version) -> UpdateRelease | None` and `download_update(release, destination_dir, progress=None, cancel_event=None) -> Path`. `progress` receives downloaded and total bytes; `cancel_event` supports `is_set()`. Both operations raise `UpdateError`; cancellation raises its subclass `UpdateCancelled`. Run these blocking calls on a background thread and marshal UI changes through Qt signals.
