# Capture save and exit recovery — 8 November 2026

## Observed incident

The local diagnostics session `11d5427c-893b-4f40-84eb-8ae852cd7bd9`
records capture frames continuing after focus resumes, but repeated save
rejections report that the accepted session no longer owns the native capture
generation. The native WGC protected-game path advances that generation on
focus resume, even though the process and selected game remain the same.

At 12:49:56 UTC a save made while focus-paused is rejected by the bridge before
native replay validation. The native Windows save path already supports retained
replay during its replay-preserving focus pause. The bridge's blanket PAUSED
veto prevents that behavior and reports only a generic busy/unavailable error.

At 12:50:36 UTC the UI records `ShutdownDeferredForAdmission` and `UIHidden`.
Launching again at 12:50:38 UTC activates the original instance. This establishes
that hiding the window was not a completed exit. The teardown path can retain
an outdated generation, fail to obtain a terminal mapping owner, and wait
indefinitely even after the native process has stopped.

## Fix and validation scope

Save submission now permits focus-paused replay validation by the native engine.
It still rejects recovering, failed, or reconfiguration-required backends, and
never overwrites an occupied command slot. Highlight submission remains blocked
while paused. Native capture source and replay validation remain authoritative.

Session recovery and teardown now follow the current generation only while
the exact process, bridge, settings, and source ownership remain valid. Existing
save snapshots remain immutable. Explicit application exit has a four-second
Qt deadline followed by the existing bounded emergency cleanup if teardown
cannot prove completion. Instance activation cannot restore an exiting window.

The historical reason healthy-poll reconciliation did not recover this session
is not conclusively established. Tests reproduce the hotkey race, paused-save
veto, and stale-generation teardown deadlock; the fixes address those paths
without treating an unrelated process or source as the accepted session.

The regression checks cover paused submission, failed backend combinations,
occupied command slots, generation rollover, retained owner cleanup, and exit
deadlines. They do not substitute for a live Valorant focus-switch session.
No native capture binary or installed application bundle is changed by the
Python source fixes. The local Desktop Dev shortcut uses this source checkout.

Combined targeted validation: **300 passed, 1 skipped** across shutdown recovery,
admission revocation/bridge/integration, game profile integration, engine error
recovery, save event loop, capture health/contracts, source authority/save
reporting, and transactional save contracts. Ruff passes for the bridge and its
tests; checking main.py also reports five unrelated existing UI lint findings.
