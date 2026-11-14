# Ultra Performance Mode

Enable Ultra Performance Mode under Settings → Performance. The app remembers the mode across launches. The compact page keeps the source and monitor, clip length, FPS, quality, audio sources and devices, and save shortcut accessible. It also shows capture status, saves a clip, and opens the Clips folder. Select **Return to full app** to restore the normal shell.

## Capture and saves

Entering or leaving the mode keeps the native capture session and its replay buffer. Source changes and other edits still use the app’s normal apply and restart rules. Capture admission, hotkeys, manual recording, audio, storage quota checks, crash recovery, and save diagnostics stay active.

New replay saves skip image, camera, and keyboard overlays and other cosmetic effects. Source crop remains in effect. Audio muxing, required timing repairs and validation, duration checks, metadata, and safe publication continue. A save already admitted uses its original settings. Its camera and keyboard providers are released after existing work and any late engine response settle, then restored from the current capture request when returning to the full app.

## Background work

- Routine library scans, file watchers, thumbnail decoding, window previews, settings previews, and optional feature plugins pause. The library rescans after the full app returns. An explicit save may still request the accounting scan needed to enforce a configured storage quota.
- Pending uploads wait and active transfers finish. Clips saved in the mode are not added to the immediate upload queue. They remain available for manual upload or a later scheduled scan according to the normal upload preferences.
- Ordinary capture cards and their sounds pause. Errors and prompts that require a decision remain available.
- An active export, backup, or other blocking library operation must finish before the app can enter the mode. Parked editor sessions retain their edits and release playback resources.

The mode stores its own preference. Leaving it restores the applicable runtime behavior from the existing upload, overlay, notification, and presentation preferences.
