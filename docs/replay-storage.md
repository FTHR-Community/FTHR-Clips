# Replay buffer location

Replay buffering can use memory or a local storage folder. The choice applies
to video and participating audio tracks. Changing the buffer configuration
restarts capture and clears the current replay history; saved clips remain in
the clip library.

## Choosing a location

- **Memory:** retains compressed video and audio payloads in RAM. The RAM
  budget covers replay storage; it is not a cap on the entire application's
  memory use.
- **Storage:** writes replay payloads to temporary files in the selected
  folder. A bounded RAM queue, packet metadata, encoders, and save processing
  still use memory. The operating system may also cache file data.

The storage budget includes temporary chunks retained by clips being saved.
It is separate from the final clip library's space usage. Free space can change
after checking a folder, so runtime write errors are still possible.
Admission reserves room for the rolling history and one save. Explicit memory
and storage modes allow one native replay save at a time; another save request
receives a busy message until that save releases its retained history.

The settings check the selected folder in the background. Detected HDDs show
a warning about slower writes and contention while saving or running games.
An unknown drive type receives a general advisory rather than an SSD label.
Drive classification is best effort, especially for virtual, aggregate, and
network-backed volumes.

Storage mode writes continuously while capture runs. For scale, 50 Mbps video
alone generates about 375 MB per minute, or 22.5 GB per hour, regardless of how
often a clip is saved. Actual buffer space also includes audio, keyframe
pre-roll, variable-bitrate headroom, and active saves. Expired temporary data
is released as the replay window advances.

## Low-resource admission

Settings → Storage → Replay buffer includes **Attempt saves with low resources**
(off by default). Apply saves the choice and restarts capture. When enabled,
RAM and temporary-storage estimates produce a warning with the estimate and
available/configured budget instead of preventing startup. The engine attempts
capture and saving within its actual budgets. Exhaustion can still stop capture
or fail a save. Invalid folders, the 64 MiB minimum RAM budget, library quotas,
and actual allocation/write failures remain enforced.

Estimates cover rolling capture plus one concurrent save, including retained
chunks, audio, metadata and bitrate headroom; they are not the final MP4 size.
Windows per-program audio includes up to 32 retained sources in this worst case.
Available replay RAM is capped at 75% of free RAM, leaving 256 MiB where possible.
Below 512 MiB free it still leaves half available, as before. Manual budgets
remain caps. Automatic budgets allocate on demand and never exceed the available
replay allowance.

## Clip quality switches (Windows)

**Save low-FPS clips** is enabled by default. It permits saving when the actual
capture rate falls below 80% of the selected frame rate. Turn it off to restore
the strict frame-rate check. **Save clips with gaps** independently permits
large forward timestamp gaps and is also enabled by default. Both switches are in
Settings → Storage → Replay buffer and restart capture when changed.
Neither switch permits invalid timestamps, stale capture, mixed capture
generations, or insufficient replay coverage.

Storage mode removes retained replay payloads from RAM; hardware encoders still
need working buffers. NVENC now uses four raw input slots with the current
no-B-frame/no-lookahead settings instead of 32. At 1080p BGRA that reduces
nominal raw input allocation from about 253 MiB to 32 MiB; driver allocations
and the rest of the application remain separate.

## Implementation contract

Both native engines accept these trailing arguments after their existing
positional arguments:

```text
--replay-storage memory|disk
--replay-budget-mode auto|manual
--replay-low-resources attempt
--replay-memory-mb 2048
--replay-storage-path <absolute existing folder>
--replay-storage-mb 4096
```

The low-resources argument is optional. Automatic mode omits manual budget
arguments. Budget arguments are in MiB. The folder and storage budget apply to
disk mode. Native callers without these options retain legacy behavior; the
frontend passes an explicit selection.

The shared storage layer retains packet payloads independently from timeline
selection. Disk mode uses a single background writer shared by the streams.
Snapshots retain references to payloads, so a save can read packets incrementally
without copying the complete replay into RAM. Referenced chunks survive ring
wrap and reset until their last reader releases them.

Temporary files use delete-on-close on Windows and unlink-after-open on Linux.
Closing the process's handles also removes its temporary data after a crash;
the buffer is not a crash-recovery recording archive.

Storage errors must be surfaced. A full disk, exhausted budget, or writer that
cannot keep up must not silently change the user's storage mode or shorten the
requested history.

## Validation

See [replay-storage-validation-2026-11-06.md](replay-storage-validation-2026-11-06.md)
for prior Windows/Linux capture and save qualification. Low-resource policy
coverage is in `tests/test_replay_storage_settings.py` and the native
`replay_memory_budget_test.cpp` suite, including admission with bounded budgets,
minimum RAM enforcement, missing folders, argument parsing and persistence.
