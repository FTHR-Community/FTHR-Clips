# Linux Engine Hardening, Phases 0 and 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drain the open Linux PR queue, then fix the Linux capture engine's known correctness defects so that encoder failures are visible, saves cannot race recovery, a compositor stall no longer ends capture, and the smaller audio, timing and durability gaps are closed, all with CTest coverage.

**Architecture:** The engine is a single process in `FTHRcapture_linux/` with a capture thread (backend + encoder + encoded ring) and a 20 ms command loop over shared memory. Policy logic lives in header-only pure structs (`recovery_policy.h`) with CTest executables that use fakes; backend and encoder behaviour is verified physically on the maintainer's Hyprland + AMD VA-API machine. Each task group below becomes one pull request against `linux`.

**Tech Stack:** C++20, CMake ≥ 3.21, pinned LGPL FFmpeg n8.1.2 (`tools/fetch_third_party.py --ffmpeg-linux`), wayland-client, libpulse-simple, PipeWire/D-Bus via dlopen, CTest. Python 3.14 venv at `.venv-linux` for pytest and tooling. `gh` CLI authenticated as `kalopsia-space`.

**Spec:** `docs/superpowers/specs/2026-10-12-linux-engine-hardening-design.md`

## Global Constraints

- Branch from `origin/linux`; one PR per task group; merge only after the GitHub `Linux engine (CMake)`, `Python 3.12/3.14 (ubuntu-latest)`, `Release verification` and `Linux AppImage (glibc baseline)` checks are green. Windows jobs are informational for Linux-branch PRs.
- No AI attribution anywhere in git or GitHub. Author is `kalopsia-space`. Never add `Co-Authored-By` or "Generated with" lines.
- Cherry-picked contributor commits keep their author (`git cherry-pick` without `--reset-author`).
- The shared-memory struct `SharedMemoryLayout`, the command and response enums, and the name `FTHR_SharedMemory_v4` do not change. `python tools/verify_shared_memory_contract.py` must pass after every engine change.
- Release builds link only the pinned FFmpeg: configure with `-DCMAKE_BUILD_TYPE=Release -DFTHR_FFMPEG_ROOT=$PWD/FTHRcapture_linux/third_party/ffmpeg`. Never link `/usr/lib` FFmpeg (it is GPL on this host).
- C++: C++20, 4-space indent, snake_case, `-Wall -Wextra` clean (the local build is currently warning-free; keep it so).
- Comments explain reasons and constraints, not what the next line does. No decorative dividers.
- Build and test commands (run from repo root, after `source .venv-linux/bin/activate` or with `PATH=$PWD/.venv-linux/bin:$PATH` so `patchelf` is found):
  ```bash
  cmake -S FTHRcapture_linux -B FTHRcapture_linux/build -DCMAKE_BUILD_TYPE=Release -DFTHR_FFMPEG_ROOT=$PWD/FTHRcapture_linux/third_party/ffmpeg
  cmake --build FTHRcapture_linux/build --parallel
  ctest --test-dir FTHRcapture_linux/build --output-on-failure
  python tools/verify_shared_memory_contract.py
  python tools/verify_engine_response_contract.py
  ```

## Review Focus

Inputs the spec implies but no task's tests exercise directly; each line names the owning task where its test was added.

1. A compositor that answers the registry but never completes a screencopy frame (wlr frame timeout with a live display): must count as a transient backend failure and retry the same backend type, never the portal. Pinned in Task 3's factory test (`RequestedTypeOnly`).
2. A save requested while the encoder generation has just changed and the ring holds only old-generation packets: must fail with the "still rebuilding" message instead of muxing packets against new extradata. Pinned in Task 2's ring test and `SaveClip` guard.
3. An encoder that fails on exactly the first frame after open (VA-API upload refused): the failure counter minimum of 5 must still give up and the generation must end as an encoder failure that reopens the encoder. Pinned in Task 1's policy test and Task 3's `GenerationEnd::FailedEncoder` path.
4. `FTHR_CAPTURE_BACKEND` set to a name the engine does not know: must be ignored with a log line, not refuse to start. Pinned in Task 4's parse test.
5. PulseAudio absent at engine start and appearing 30 s later: audio must attach without a restart and the startup warning must appear exactly once. Pinned in Task 7 by the reconnect-loop structure and checked physically (`systemctl --user stop pipewire-pulse` before start, then start it).

---

## Phase 0: PR queue (operational, no TDD)

### Task 0.1: Merge #41

- [ ] `gh pr checks 41 --repo FTHR-Community/FTHR-Clips` shows all required Linux checks passing.
- [ ] `gh pr merge 41 --repo FTHR-Community/FTHR-Clips --squash --delete-branch` (squash keeps the contributor as author of the squash commit because GitHub attributes it to the PR author).
- [ ] `git fetch origin` and confirm `origin/linux` moved.

### Task 0.2: Rebase and merge #19

- [ ] In a throwaway clone under the scratchpad: `git fetch origin pull/19/head:pr19`, `git rebase origin/linux` on `pr19`. The PR touches `AppDir/fthr-clips.desktop` and `FTHR_UI/main.py` (desktop identity + launcher categories); conflicts are expected to be none.
- [ ] Push the rebased branch to a maintainer branch on origin: `git push origin pr19:fix/linux-desktop-identity-rebased`.
- [ ] Open a PR from that branch to `linux` with body: "Rebase of #19 by @Vaspyyy onto current linux so CI runs against the fixed base. Authorship preserved." Wait for green CI, merge with `--rebase` (keeps the author's commit), then close #19 with a comment pointing at the merged PR.

### Task 0.3: Cherry-pick the vaMapBuffer2 probe from #42

- [ ] `git fetch origin pull/42/head:pr42`; `git log --oneline origin/linux..pr42` and identify the commit(s) that add the `vaMapBuffer2` probe in `FTHRcapture_linux/src/encoder.cpp` and the `dl` link in CMake. Inspect with `git show <sha> -- FTHRcapture_linux/src/encoder.cpp FTHRcapture_linux/CMakeLists.txt`.
- [ ] Branch `fix/linux-vaapi-symbol-probe` from `origin/linux`; `git cherry-pick <sha>` for the probe commit only. If the commit also contains RAM-guard hunks in `ring_buffer.*`/`capture_engine.*`/`main.cpp`, cherry-pick with `-n`, `git checkout origin/linux -- FTHRcapture_linux/src/ring_buffer.cpp FTHRcapture_linux/src/ring_buffer.h FTHRcapture_linux/src/capture_engine.cpp FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/main.cpp FTHRcapture_linux/tests/linux_encoded_ring_duration_test.cpp docs/engine-startup.md`, then commit with `--author="$(git log -1 --format='%an <%ae>' <sha>)"`.
- [ ] Also cherry-pick the test-hygiene commit if separate (`pyproject.toml` pythonpath, `tests/test_linux_appimage_report.py` POSIX skip, `tests/test_upload_manager_retry.py`).
- [ ] Build, `ctest`, confirm `linux_vaapi_encoder` still passes on this host (libva here is new, so the probe finds the symbol).
- [ ] Push, open PR to `linux` titled "fix(linux): probe vaMapBuffer2 before opening VA-API (from #42)", body credits @Victor7996 and explains the RAM guard and 1800 s cap were left out per the review on #42. Merge after green CI. Comment on #42 with the merged PR link and leave #42 open for the author to rebase the remaining parts or close.

### Task 0.4: Fix and merge #57

- [ ] `git fetch origin fix/linux-wayland-screenshots-microphone` and check it out locally in a throwaway clone.
- [ ] In `tests/test_linux_microphone_selection.py`, the two `monkeypatch.setattr(mic_recorder_module, '_sd', audio_backend)` and `monkeypatch.setattr(main, '_sd', audio_backend)` calls get `raising=False` appended, because `_sd` is only defined when `sounddevice` imports and the ubuntu CI job has no PortAudio.
- [ ] In `FTHR.spec` (Windows), add `'PySide6.QtDBus'` to the `excludes` list next to the other unused Qt modules, because `wayland_screenshot_portal.py` imports QtDBus at function level and PyInstaller would otherwise bundle `Qt6DBus.dll` on Windows, which the Qt runtime manifest rejects.
- [ ] Run locally: `QT_QPA_PLATFORM=offscreen .venv-linux/bin/python -m pytest tests/test_linux_microphone_selection.py tests/test_wayland_screenshot_portal.py tests/test_camera_provider_failures.py tests/test_linux_camera_refresh.py -q` and `ruff check .`.
- [ ] Read the camera-refresh hunk in `FTHR_UI/core/camera_recorder.py` and `FTHR_UI/main.py` `_populate_camera_devices`: confirm the initial camera open does not depend on `currentIndexChanged` firing during refresh (grep for the signal's connected slot and check it is also called from the settings-load path). If it does depend on it, call the slot explicitly after `blockSignals(False)`.
- [ ] Commit as "test: tolerate missing sounddevice in mic selection tests; exclude QtDBus from the Windows bundle", push to the PR branch (org-owned), wait for CI, merge with `--squash`. Close #52 and #56 with a comment naming the merged PR and the pending field checks listed in its description.

### Task 0.5: Comments on deferred PRs and issues

- [ ] #43, #27, #58: one comment each: "Deferred until after the UI overhaul; the Linux engine hardening work (see docs/superpowers/specs/2026-10-12-linux-engine-hardening-design.md on linux) lands first. Leaving open." For #43 add: "and the replay memory budget needs a UI-side design before the engine cap can move."
- [ ] #28: comment "Fixed in 1.1.2-alpha (built on Ubuntu 22.04 with a GLIBC 2.35 ceiling): <release link>", then `gh issue close 28`.

---

## Phase 1, PR A: recovery and save correctness (Tasks 1 to 4, one branch `fix/linux-engine-recovery`)

### Task 1: EncodeFailurePolicy

**Files:**
- Modify: `FTHRcapture_linux/src/recovery_policy.h`
- Modify: `FTHRcapture_linux/src/capture_engine.cpp:198-215` (the encode call in `RunCaptureGeneration`)
- Modify: `FTHRcapture_linux/src/capture_engine.h` (new `GenerationEnd::FailedEncoder`)
- Test: `FTHRcapture_linux/tests/capture_recovery_test.cpp`

**Interfaces:**
- Produces: `class fthr::EncodeFailurePolicy { explicit EncodeFailurePolicy(uint32_t fps); bool OnEncodeResult(bool ok); uint32_t ConsecutiveFailures() const; }` where `OnEncodeResult` returns `true` when the generation must give up.
- Produces: `CaptureEngine::GenerationEnd::FailedEncoder`.

- [ ] **Step 1: Write the failing test** (append before `return 0;` in `capture_recovery_test.cpp`):

```cpp
    // Encoder failures give up after max(fps/2, 5) consecutive misses and
    // reset on any success.
    {
        fthr::EncodeFailurePolicy encode_policy(60);
        for (uint32_t i = 0; i < 29; ++i)
            assert(!encode_policy.OnEncodeResult(false));
        assert(encode_policy.ConsecutiveFailures() == 29);
        assert(encode_policy.OnEncodeResult(false));          // 30th = fps/2
        assert(!encode_policy.OnEncodeResult(true));
        assert(encode_policy.ConsecutiveFailures() == 0);

        fthr::EncodeFailurePolicy low_fps(1);
        for (uint32_t i = 0; i < 4; ++i)
            assert(!low_fps.OnEncodeResult(false));
        assert(low_fps.OnEncodeResult(false));                // minimum of 5
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cmake --build FTHRcapture_linux/build --parallel --target fthr_capture_recovery_test`
Expected: compile error, `EncodeFailurePolicy` not declared.

- [ ] **Step 3: Write minimal implementation** (append inside `namespace fthr` in `recovery_policy.h`):

```cpp
// A broken encoder (for example a VA-API upload that the driver refuses)
// otherwise leaves capture reporting healthy frames while the ring stays
// empty. Half a second of consecutive failures, but never fewer than five
// frames, ends the generation so the encoder is reopened.
class EncodeFailurePolicy {
public:
    explicit EncodeFailurePolicy(uint32_t fps)
        : threshold_(std::max<uint32_t>(5, fps / 2)) {}

    // Returns true when the generation must give up on this encoder.
    bool OnEncodeResult(bool ok) {
        if (ok) { consecutive_ = 0; return false; }
        ++consecutive_;
        return consecutive_ >= threshold_;
    }

    uint32_t ConsecutiveFailures() const noexcept { return consecutive_; }

private:
    uint32_t threshold_;
    uint32_t consecutive_ = 0;
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cmake --build FTHRcapture_linux/build --parallel --target fthr_capture_recovery_test && ctest --test-dir FTHRcapture_linux/build -R capture_recovery_policy --output-on-failure`
Expected: PASS.

- [ ] **Step 5: Wire it into the generation loop.** In `capture_engine.h` change the enum to:

```cpp
    enum class GenerationEnd {
        Stopped,          // running_ went false: normal shutdown
        Failed,           // backend failure worth a bounded retry
        FailedEncoder,    // encoder stopped accepting frames: reopen it
        FailedForGood,    // only user action can help (declined portal dialog)
    };
```

In `capture_engine.cpp` `RunCaptureGeneration`, declare `EncodeFailurePolicy encode_policy(cfg_.fps);` and `bool encoder_failed = false;` before the `while (running_.load())` loop, and replace the encode call with:

```cpp
        const bool encoded = encoder_.EncodeFrame(raw.data, raw.stride,
            raw.width, raw.height, raw.timestamp_ns,
            [this](EncodedPacket pkt) { ring_->Push(std::move(pkt)); });
        if (encode_policy.OnEncodeResult(encoded)) {
            std::cerr << "[Capture] Encoder stopped accepting frames after "
                      << encode_policy.ConsecutiveFailures()
                      << " consecutive failures" << std::endl;
            SetCaptureFailureReason(
                "The video encoder (" + codec_used + ") stopped accepting frames");
            encoder_failed = true;
            break;
        }
        if (encoded) frame_count_.fetch_add(1);
```

and change the final return to:

```cpp
    if (!running_.load()) return GenerationEnd::Stopped;
    return encoder_failed ? GenerationEnd::FailedEncoder : GenerationEnd::Failed;
```

`CaptureLoop` treats `FailedEncoder` like `Failed` for the retry policy (Task 3 changes what is torn down).

- [ ] **Step 6: Build, run all tests, commit**

Run: `cmake --build FTHRcapture_linux/build --parallel && ctest --test-dir FTHRcapture_linux/build --output-on-failure`
Expected: 18/18 pass.

```bash
git add FTHRcapture_linux/src/recovery_policy.h FTHRcapture_linux/src/capture_engine.cpp FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/tests/capture_recovery_test.cpp
git commit -m "fix(linux): end the generation when the encoder stops accepting frames"
```

### Task 2: Encoder snapshot and packet generation tags

**Files:**
- Modify: `FTHRcapture_linux/src/ring_buffer.h` (`EncodedPacket::generation`, `TakeSnapshot` filter)
- Modify: `FTHRcapture_linux/src/ring_buffer.cpp`
- Modify: `FTHRcapture_linux/src/encoder.h` (`EncodeFrame` stamps generation)
- Modify: `FTHRcapture_linux/src/encoder.cpp:319-372`
- Modify: `FTHRcapture_linux/src/capture_engine.h`, `capture_engine.cpp` (`EncoderSnapshot`, `SaveClip`)
- Test: `FTHRcapture_linux/tests/linux_encoded_ring_duration_test.cpp`

**Interfaces:**
- Produces: `EncodedPacket` gains `uint32_t generation = 0;` as the last member. `EncodedRingBuffer::TakeSnapshot(uint32_t duration_ms, int64_t target_end_ns, uint32_t generation)`: packets whose `generation` differs are ignored (a `generation` of 0 means "accept all", keeping existing tests valid).
- Produces: `Encoder::SetGeneration(uint32_t)`; every packet pushed carries it.
- Produces: `struct fthr::EncoderSnapshot { std::vector<uint8_t> extradata; uint32_t width = 0, height = 0; AVCodecID codec_id = AV_CODEC_ID_NONE; uint32_t generation = 0; bool valid = false; }` and `CaptureEngine::EncoderSnapshot TakeEncoderSnapshot() const`.

- [ ] **Step 1: Write the failing test** (add to `linux_encoded_ring_duration_test.cpp`, and call it from `main`):

```cpp
void TestSnapshotIgnoresOtherGenerations() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    fthr::EncodedRingBuffer ring(40'000, fps);
    for (int frame = 0; frame <= 10 * fps; ++frame) {
        auto packet = Packet(frame, frame * second / fps, frame % 60 == 0);
        packet.generation = 1;
        ring.Push(packet);
    }
    for (int frame = 0; frame <= 3 * fps; ++frame) {
        auto packet = Packet(frame, 10 * second + frame * second / fps, frame % 60 == 0);
        packet.generation = 2;
        ring.Push(packet);
    }

    // Generation 2 only: three seconds of history, starting at its keyframe.
    const auto newer = ring.TakeSnapshot(30'000, 13 * second, 2);
    assert(!newer.full_history);
    assert(!newer.packets.empty());
    assert(newer.packets.front().generation == 2);
    assert(newer.packets.front().wall_time_ns >= 10 * second);

    // Unknown generation yields nothing rather than mixing extradata.
    const auto none = ring.TakeSnapshot(30'000, 13 * second, 7);
    assert(none.packets.empty());

    // 0 keeps the historical behaviour.
    const auto all = ring.TakeSnapshot(30'000, 13 * second, 0);
    assert(all.packets.front().generation == 1);
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cmake --build FTHRcapture_linux/build --parallel --target fthr_linux_encoded_ring_duration_test`
Expected: compile error on `generation` / third argument.

- [ ] **Step 3: Implement the ring change.** In `ring_buffer.h` add `uint32_t generation = 0;` after `wall_time_ns` in `EncodedPacket`, and change the declaration to `EncodedRingSnapshot TakeSnapshot(uint32_t duration_ms, int64_t target_end_ns, uint32_t generation = 0) const;`. In `ring_buffer.cpp` replace the body's sample-building loop with a filtered copy:

```cpp
EncodedRingSnapshot EncodedRingBuffer::TakeSnapshot(
    uint32_t duration_ms, int64_t target_end_ns, uint32_t generation) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (packets_.empty() || duration_ms == 0 || fps_ == 0) return {};

    // Packets from an earlier encoder generation were produced with different
    // extradata and cannot share an MP4 track with the current ones.
    std::vector<const EncodedPacket*> eligible;
    eligible.reserve(packets_.size());
    for (const auto& packet : packets_)
        if (generation == 0 || packet.generation == generation)
            eligible.push_back(&packet);
    if (eligible.empty()) return {};
    if (target_end_ns <= 0) target_end_ns = eligible.back()->wall_time_ns;

    std::vector<replay_interval::Sample> samples;
    samples.reserve(eligible.size());
    for (const auto* packet : eligible)
        samples.push_back({packet->wall_time_ns, packet->is_keyframe});
    const auto selection = replay_interval::Select(
        samples, target_end_ns,
        static_cast<int64_t>(duration_ms) * 1'000'000LL);
    if (!selection.valid) return {};

    EncodedRingSnapshot snapshot;
    snapshot.packets.reserve(selection.end + 1 - selection.decode_start);
    for (size_t i = selection.decode_start; i <= selection.end; ++i)
        snapshot.packets.push_back(*eligible[i]);
    // ... remainder unchanged (full_history, presentation_* fields) ...
```

Keep the rest of the function as it is (it only reads `snapshot.packets`).

- [ ] **Step 4: Run the ring test** — Expected: PASS, and the three pre-existing cases still pass.

- [ ] **Step 5: Stamp packets in the encoder.** In `encoder.h` add `void SetGeneration(uint32_t g) { generation_ = g; }` and `uint32_t generation_ = 0;` in the private section. In `encoder.cpp` where `EncodedPacket` objects are built from `pkt_` (search `EncodedPacket out` or the `push_fn(` call near line 411-419), set `out.generation = generation_;` before `push_fn(std::move(out))`.

- [ ] **Step 6: Snapshot in the engine.** In `capture_engine.h` add, in `namespace fthr` before `class CaptureEngine`:

```cpp
// What SaveClip needs from the encoder, copied at open time so the save
// thread never reads codec state the capture thread may be tearing down.
struct EncoderSnapshot {
    std::vector<uint8_t> extradata;
    uint32_t  width = 0;
    uint32_t  height = 0;
    AVCodecID codec_id = AV_CODEC_ID_NONE;
    uint32_t  generation = 0;
    bool      valid = false;
};
```

and members `EncoderSnapshot encoder_snapshot_;  // guarded by codec_mutex_` and `uint32_t encoder_generation_ = 0;` plus a public `EncoderSnapshot TakeEncoderSnapshot() const { std::lock_guard<std::mutex> lk(codec_mutex_); return encoder_snapshot_; }`.

In `RunCaptureGeneration`, right after `encoder_.Open` succeeds:

```cpp
    ++encoder_generation_;
    encoder_.SetGeneration(encoder_generation_);
    {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        active_codec_ = codec_used;
        encoder_snapshot_ = EncoderSnapshot{
            encoder_.GetExtradata(), encoder_.GetWidth(), encoder_.GetHeight(),
            encoder_.GetCodecID(), encoder_generation_, true};
    }
```

(replace the existing `active_codec_ = codec_used;` block). Wherever `encoder_.Close()` is called on the capture thread, first do `{ std::lock_guard<std::mutex> lk(codec_mutex_); encoder_snapshot_ = EncoderSnapshot{}; }`.

In `SaveClip` replace the direct encoder reads:

```cpp
    const EncoderSnapshot enc = TakeEncoderSnapshot();
    if (!enc.valid) {
        if (error_message)
            *error_message = "Capture restarted; the replay buffer is still rebuilding";
        return false;
    }
    ...
    auto video_snapshot = ring_->TakeSnapshot(duration_ms, save_end_ns, enc.generation);
    if (video_packets.empty()) {
        if (error_message)
            *error_message = "Capture restarted; the replay buffer is still rebuilding";
        return false;
    }
    ...
    return save_clip_to_file(path, video_packets, video_snapshot.presentation_start_pts,
        audio_pcm, AudioCapture::kSampleRate, AudioCapture::kChannels,
        enc.extradata, cfg_.fps, enc.width, enc.height, enc.codec_id,
        shm, error_message, cfg_.separate_audio_enabled, cfg_.bitrate_kbps);
```

Keep the existing "Nothing to save" message when the ring is empty but `enc.valid` and the generation matches; distinguish by checking `ring_->PacketCount() == 0` first.

- [ ] **Step 7: Build, full ctest, contract checks, commit**

Run: `cmake --build FTHRcapture_linux/build --parallel && ctest --test-dir FTHRcapture_linux/build --output-on-failure && .venv-linux/bin/python tools/verify_shared_memory_contract.py`
Expected: all pass (the struct did not change).

```bash
git add FTHRcapture_linux/src/ring_buffer.h FTHRcapture_linux/src/ring_buffer.cpp FTHRcapture_linux/src/encoder.h FTHRcapture_linux/src/encoder.cpp FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/capture_engine.cpp FTHRcapture_linux/tests/linux_encoded_ring_duration_test.cpp
git commit -m "fix(linux): save from a per-generation encoder snapshot instead of live codec state"
```

### Task 3: Recovery keeps the backend type and the ring

**Files:**
- Modify: `FTHRcapture_linux/src/capture_backend.h`, `capture_backend.cpp`
- Modify: `FTHRcapture_linux/src/backend_wlr.cpp:227-295`, `backend_ext.cpp:205-235` (failure kinds)
- Modify: `FTHRcapture_linux/src/recovery_policy.h` (transient failures)
- Modify: `FTHRcapture_linux/src/capture_engine.h`, `capture_engine.cpp` (`CaptureLoop`, `RunCaptureGeneration`)
- Modify: `FTHRcapture_linux/src/main.cpp:219-245` (publish the waiting reason)
- Test: `FTHRcapture_linux/tests/capture_recovery_test.cpp`

**Interfaces:**
- Produces in `capture_backend.h`:

```cpp
enum class BackendFailureKind {
    None,
    Error,                 // genuine failure: counts toward the retry budget
    CompositorUnreachable, // display connect/registry roundtrip failed: wait
    OutputAbsent,          // the named output is not advertised: wait
    UserDeclined,          // portal dialog declined/unanswered: never retry
};
struct BackendFailure {
    std::string reason;
    BackendFailureKind kind = BackendFailureKind::None;
    bool retry_pointless = false;   // kept for existing callers; == UserDeclined
};
```

- `ICaptureBackend` gains `BackendFailureKind InitFailureKind() const { return init_failure_kind_; }` and `void SetStrictOutput(bool strict) { strict_output_ = strict; }` with protected members `init_failure_kind_ = BackendFailureKind::None; bool strict_output_ = false;`.
- `std::unique_ptr<ICaptureBackend> CreateBackend(BackendType type, const CaptureConfig& cfg, const std::atomic<bool>* running, BackendFailure* failure, bool strict_output);` (single type, no fallback). `CreateBestBackend` is rewritten to call it in order.
- `CaptureRecoveryPolicy::OnGenerationFailed(uint64_t produced_frames, bool transient = false)`: when `transient`, the attempt counter is untouched and the decision is `{retry=true, exhausted=false, attempt=attempts_, backoff=2000ms}`.

- [ ] **Step 1: Write the failing policy test** (append to `capture_recovery_test.cpp` before `return 0;`):

```cpp
    // A transient failure (compositor unreachable, output absent) retries
    // every 2 s without consuming the three-attempt budget.
    {
        fthr::CaptureRecoveryPolicy policy(300);
        for (int i = 0; i < 10; ++i) {
            const auto waiting = policy.OnGenerationFailed(0, /*transient=*/true);
            assert(waiting.retry && !waiting.exhausted);
            assert(waiting.backoff.count() == 2000);
        }
        assert(policy.Attempts() == 0);
        const auto real = policy.OnGenerationFailed(0, false);
        assert(real.retry && real.attempt == 1 && real.backoff.count() == 250);
    }
```

- [ ] **Step 2: Run to verify it fails** — compile error, two-argument `OnGenerationFailed`.

- [ ] **Step 3: Implement the policy change** in `recovery_policy.h`:

```cpp
    RecoveryDecision OnGenerationFailed(uint64_t produced_frames,
                                        bool transient = false) {
        if (transient)
            return {true, false, attempts_, kTransientBackoff};
        if (produced_frames >= healthy_reset_frames_)
            attempts_ = 0;
        ++attempts_;
        if (attempts_ > max_attempts_)
            return {false, true, attempts_, std::chrono::milliseconds(0)};
        const size_t index = std::min<size_t>(attempts_ - 1, kBackoffs.size() - 1);
        return {true, false, attempts_, kBackoffs[index]};
    }
    ...
    inline static constexpr std::chrono::milliseconds kTransientBackoff{2000};
```

- [ ] **Step 4: Run the policy test** — PASS.

- [ ] **Step 5: Failure kinds in the backends.** In `backend_wlr.cpp` `Initialize`: after `wl_display_connect` fails and after the `Roundtrip(... "registry discovery")`/`"output discovery"` failure, set `init_failure_kind_ = BackendFailureKind::CompositorUnreachable;` before returning. In the output-selection block, when `!target_output_.empty()` and no match: if `strict_output_` then `init_failure_kind_ = BackendFailureKind::OutputAbsent; Shutdown(); return false;` instead of falling back. When `!sc_mgr_` set `Error`. Same three edits in `backend_ext.cpp` `Initialize` (its output block at lines ~225-231 and the `!mgr_ || !src_mgr_` branch). In `backend_portal.cpp`, where `RetryIsPointless()` becomes true, also set `init_failure_kind_ = BackendFailureKind::UserDeclined;`; other portal failures set `Error`. In `backend_x11.cpp` `Initialize`, a failed `avformat_open_input` sets `Error`.

- [ ] **Step 6: Factory split** in `capture_backend.cpp`:

```cpp
std::unique_ptr<ICaptureBackend> CreateBackend(
    BackendType type, const CaptureConfig& cfg,
    const std::atomic<bool>* running, BackendFailure* failure,
    bool strict_output) {
    std::unique_ptr<ICaptureBackend> backend;
    switch (type) {
    case BackendType::WlrScreencopy: backend = std::make_unique<WlrBackend>(running); break;
    case BackendType::ExtImageCopy:  backend = std::make_unique<ExtBackend>(running); break;
    case BackendType::PortalScreenCast:
#if FTHR_PORTAL_BACKEND
        backend = std::make_unique<PortalBackend>(running); break;
#else
        if (failure) { failure->reason = "the ScreenCast portal backend is not compiled into this engine"; failure->kind = BackendFailureKind::Error; }
        return nullptr;
#endif
    case BackendType::X11Grab:
#if FTHR_X11_CAPTURE
        backend = std::make_unique<X11Backend>(running); break;
#else
        if (failure) { failure->reason = "Native X11 capture disabled at build time"; failure->kind = BackendFailureKind::Error; }
        return nullptr;
#endif
    }
    backend->SetStrictOutput(strict_output);
    if (backend->Initialize(cfg)) {
        std::cerr << "[Backend] Using " << BackendTypeName(type) << std::endl;
        return backend;
    }
    if (failure) {
        failure->kind = backend->InitFailureKind();
        failure->retry_pointless = failure->kind == BackendFailureKind::UserDeclined;
        failure->reason = backend->FailureReason();   // portal has one; others return ""
    }
    return nullptr;
}
```

Add `virtual std::string FailureReason() const { return {}; }` to `ICaptureBackend` (the portal backend already has a non-virtual `FailureReason()`; mark it `override`). Add `const char* BackendTypeName(BackendType)` to the header. Rewrite `CreateBestBackend` to iterate `{Wlr, Ext, Portal}` on Wayland (stop early on `UserDeclined` or cancellation, carry the portal reason into the final "No Wayland capture path" message exactly as today) and `{X11Grab}` otherwise, each via `CreateBackend(type, cfg, running, &f, /*strict_output=*/false)`.

- [ ] **Step 7: Factory test.** The real backends need Wayland headers, so the factory itself is not unit-tested; the pure ordering is. Add to `capture_backend.h` a free function `std::vector<BackendType> BackendCandidates(bool has_wayland)` returning `{Wlr, Ext, Portal}` or `{X11Grab}`, used by `CreateBestBackend`, and `BackendType RecoveryCandidate(BackendType last_good)` returning `last_good`. Test both plus `BackendTypeName` in the new CTest `capture_backend_candidates` (no Wayland linkage: the functions are defined in a new `capture_backend_policy.cpp` that includes only `capture_backend.h`'s enum section; split the enum and these functions into `capture_backend_policy.h`). Register it in `CMakeLists.txt` under `BUILD_TESTING` next to `fthr_capture_recovery_test`.

- [ ] **Step 8: Engine loop.** In `capture_engine.h` add `BackendType last_good_backend_{}; bool have_last_good_backend_ = false;` and a private `GenerationEnd RunCaptureGeneration(bool& transient_out);`. In `RunCaptureGeneration`:

```cpp
    BackendFailure failure;
    if (have_last_good_backend_) {
        backend_ = CreateBackend(last_good_backend_, cfg_, &running_, &failure,
                                 /*strict_output=*/true);
    } else {
        backend_ = CreateBestBackend(cfg_, &running_, &failure);
    }
    if (!backend_) {
        if (!running_.load()) return GenerationEnd::Stopped;
        transient_out = failure.kind == BackendFailureKind::CompositorUnreachable
                     || failure.kind == BackendFailureKind::OutputAbsent;
        SetCaptureFailureReason(
            failure.kind == BackendFailureKind::CompositorUnreachable ? "Waiting for the compositor"
          : failure.kind == BackendFailureKind::OutputAbsent ? "Waiting for output " + cfg_.target_output + " to return"
          : failure.reason.empty() ? "No capture backend is available" : failure.reason);
        return failure.retry_pointless ? GenerationEnd::FailedForGood
                                       : GenerationEnd::Failed;
    }
    const bool reuse_encoder = encoder_.IsOpen() && !last_generation_encoder_failed_;
    if (!reuse_encoder) {
        encoder_.Close();
        if (!encoder_.Open(enc_cfg, codec_used)) { ... as today, returns FailedEncoder ... }
        ++encoder_generation_; ... snapshot as in Task 2 ...
        if (ring_) ring_->Clear();
    } else {
        codec_used = GetActiveCodec();
        std::cout << "[Capture] Backend restarted; keeping encoder and replay history" << std::endl;
    }
    capture_generation_.fetch_add(1);
```

Record `have_last_good_backend_ = true; last_good_backend_ = backend_->Type();` the first time `frame_count_` advances in this generation. At the loop exit, call `encoder_.Close()` only when `encoder_failed || !running_.load()`; set `last_generation_encoder_failed_ = encoder_failed;`. `Reconfigure()` resets `have_last_good_backend_` to false (it is a fresh configuration), and `Shutdown()` still closes the encoder.

In `CaptureLoop`, pass `transient` to the policy, and never clear the ring there (the generation decides):

```cpp
        bool transient = false;
        const GenerationEnd end = RunCaptureGeneration(transient);
        if (end == GenerationEnd::Stopped) break;
        if (!running_.load()) break;
        content_suspicious_streak_.store(0);
        const auto decision = recovery.OnGenerationFailed(
            frame_count_.load() - frames_before, transient);
        if (decision.exhausted || end == GenerationEnd::FailedForGood) { ... unchanged ... }
        capture_health_flags_.store(CAPTURE_HEALTH_RECOVERING);
        if (transient)
            std::cerr << "[Capture] " << GetCaptureFailureReason()
                      << "; retrying in " << decision.backoff.count() << " ms" << std::endl;
        else
            std::cerr << "[Capture] Recovery attempt " << decision.attempt << "/3 in " ...
```

- [ ] **Step 9: Publish the waiting reason.** In `main.cpp`, extend the block that writes `engine_string` so that while `health_flags & CAPTURE_HEALTH_RECOVERING` and no save result is pending, `engine_string` is set to `engine.GetCaptureFailureReason()` when it changed since last publish (keep a `std::string last_published_reason`). The BACKEND_FAILED path is unchanged.

- [ ] **Step 10: Build, ctest, contract checks, physical reproduction.**

Run the build and `ctest`. Then with the standalone engine (`FTHRcapture_linux/build/FTHRclips 60 10 0 0 8000 0 0 0 0 "DP-3" 0 4 0 1`) running in a terminal, in another terminal run `hyprctl dispatch dpms off` for 10 s, then `hyprctl dispatch dpms on`; expected log: wlr frame timeout, then `Waiting for the compositor` or a same-backend restart, then `Backend restarted; keeping encoder and replay history`, no portal lines, frames resume. Repeat with the monitor cable, and with `systemctl suspend` if practical. Record outcomes in the PR description.

```bash
git add FTHRcapture_linux/src FTHRcapture_linux/tests FTHRcapture_linux/CMakeLists.txt
git commit -m "fix(linux): recover on the backend that last delivered frames and keep the replay history"
```

### Task 4: FTHR_CAPTURE_BACKEND override

**Files:**
- Modify: `FTHRcapture_linux/src/capture_backend_policy.h/.cpp` (from Task 3), `capture_backend.cpp`
- Modify: `docs/linux.md` (Diagnostics subsection)
- Test: `FTHRcapture_linux/tests/capture_backend_candidates_test.cpp` (from Task 3)

**Interfaces:**
- Produces: `bool ParseBackendOverride(const char* value, BackendType& out)` accepting `wlr`, `ext`, `portal`, `x11` (case-insensitive); returns false for anything else.

- [ ] **Step 1: Failing test** (in `capture_backend_candidates_test.cpp`):

```cpp
    fthr::BackendType parsed;
    assert(fthr::ParseBackendOverride("ext", parsed) && parsed == fthr::BackendType::ExtImageCopy);
    assert(fthr::ParseBackendOverride("PORTAL", parsed) && parsed == fthr::BackendType::PortalScreenCast);
    assert(!fthr::ParseBackendOverride("", parsed));
    assert(!fthr::ParseBackendOverride("x11grab", parsed));
    assert(!fthr::ParseBackendOverride(nullptr, parsed));
```

- [ ] **Step 2: Run, verify compile failure.**
- [ ] **Step 3: Implement** in `capture_backend_policy.cpp` with a lowercase compare over the four names.
- [ ] **Step 4: Run, PASS.**
- [ ] **Step 5: Honour it** at the top of `CreateBestBackend`: if `std::getenv("FTHR_CAPTURE_BACKEND")` parses, log `[Backend] FTHR_CAPTURE_BACKEND forces <name>` and return `CreateBackend(type, cfg, running, failure, false)` without any fallback; if it does not parse but is set, log `[Backend] Ignoring unknown FTHR_CAPTURE_BACKEND value '<value>'` and continue normally. Document in `docs/linux.md` under a new "Diagnostics" heading: the variable, the four values, and that a forced backend never falls back.
- [ ] **Step 6: Physical check:** `FTHR_CAPTURE_BACKEND=ext` and `=portal` with the standalone engine on this box; both must start (portal shows the picker once). Commit: `git commit -m "feat(linux): FTHR_CAPTURE_BACKEND forces one capture backend for diagnostics"`.

### Task 4b: Frame throttle re-anchors after a stall

**Files:**
- Modify: `FTHRcapture_linux/src/capture_engine.cpp` (the `next_encode_ns` block in `RunCaptureGeneration`)
- Modify: `FTHRcapture_linux/src/recovery_policy.h` (pure helper)
- Test: `FTHRcapture_linux/tests/capture_recovery_test.cpp`

**Interfaces:**
- Produces: `int64_t fthr::NextEncodeDeadline(int64_t next_encode_ns, int64_t frame_ns, int64_t frame_timestamp_ns)` returning the deadline to use for this frame: `frame_timestamp_ns` when the frame is more than two periods late (re-anchor), otherwise `next_encode_ns` unchanged.

- [ ] **Step 1: Failing test** (append to `capture_recovery_test.cpp` main):

```cpp
    // After a stall the throttle re-anchors instead of encoding a burst of
    // back-to-back frames to "catch up" with wall time.
    {
        constexpr int64_t frame_ns = 16'666'667;
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000) == 1'000'000'000);
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000 + frame_ns) == 1'000'000'000);
        assert(fthr::NextEncodeDeadline(1'000'000'000, frame_ns, 1'000'000'000 + 3 * frame_ns)
               == 1'000'000'000 + 3 * frame_ns);
    }
```

- [ ] **Step 2: compile failure. Step 3: implement** in `recovery_policy.h`:

```cpp
inline int64_t NextEncodeDeadline(int64_t next_encode_ns, int64_t frame_ns,
                                  int64_t frame_timestamp_ns) {
    return frame_timestamp_ns > next_encode_ns + 2 * frame_ns
        ? frame_timestamp_ns : next_encode_ns;
}
```

- [ ] **Step 4: PASS. Step 5: use it** in `RunCaptureGeneration`: `if (next_encode_ns == 0) next_encode_ns = raw.timestamp_ns; next_encode_ns = NextEncodeDeadline(next_encode_ns, frame_ns, raw.timestamp_ns); if (raw.timestamp_ns < next_encode_ns) continue; next_encode_ns += frame_ns;`. Build, ctest, commit `fix(linux): re-anchor the frame throttle after a capture stall`.

After Task 4b: push `fix/linux-engine-recovery`, open PR A to `linux` with the physical results, wait for CI, request a fresh code review (see Execution), merge.

---

## Phase 1, PR B: ext-image-copy-capture still screen (Task 5, branch `fix/linux-ext-static-screen`)

### Task 5: Repeat the last frame on a timeout

**Files:**
- Modify: `FTHRcapture_linux/src/backend_ext.h`, `backend_ext.cpp:276-337`
- Modify: `FTHRcapture_linux/src/backend_portal.cpp:543-556` (wall-time clamp, same PR)
- Test: physical only (no compositor in CTest). Add a unit test for the pure helper below.

**Interfaces:**
- Produces: `bool fthr::ShouldRepeatLastFrame(WaylandWaitResult result, bool have_last_frame)` in `backend_ext.h` (pure): true only for `Timeout` with a previous frame.

- [ ] **Step 1: Failing test** `tests/ext_repeat_policy_test.cpp` registered as CTest `ext_repeat_policy` (links `wayland_dispatch.cpp` for the enum only, like `fthr_wayland_dispatch_test`):

```cpp
#include "backend_ext.h"
#include <cassert>
int main() {
    using R = fthr::WaylandWaitResult;
    assert(fthr::ShouldRepeatLastFrame(R::Timeout, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Timeout, false));
    assert(!fthr::ShouldRepeatLastFrame(R::Disconnected, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Error, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Cancelled, true));
    return 0;
}
```

If including `backend_ext.h` drags in generated protocol headers that the test target cannot see, put the helper in a new `ext_repeat_policy.h` instead and include that from both.

- [ ] **Step 2: Run, verify compile failure. Step 3: implement the one-line helper. Step 4: PASS.**

- [ ] **Step 5: Backend change.** In `backend_ext.h` add members `std::vector<uint8_t> last_frame_; uint32_t last_w_ = 0, last_h_ = 0, last_format_ = 0; ext_image_copy_capture_frame_v1* pending_frame_ = nullptr;`. Restructure `CaptureOnce` so the frame request is created only when `pending_frame_ == nullptr`, and is destroyed and nulled only on `frame_ready_`, `frame_failed_` or `session_stopped_`. `WaitUntil` must expose its `WaylandWaitResult`; add an overload `WaylandWaitResult WaitUntilResult(...)` used here. In `CaptureFrame`:

```cpp
    const WaylandWaitResult result = CaptureOnceResult(deadline);
    if (result == WaylandWaitResult::EventReceived && frame_ready_) {
        // Keep an owned copy: the wl_shm buffer is reused for the next
        // request, and a repeated frame must not alias a buffer the
        // compositor may be writing.
        const size_t bytes = static_cast<size_t>(buf_width_) * 4 * buf_height_;
        last_frame_.assign(static_cast<const uint8_t*>(shm_data_),
                           static_cast<const uint8_t*>(shm_data_) + bytes);
        last_w_ = buf_width_; last_h_ = buf_height_; last_format_ = shm_format_;
    } else if (ShouldRepeatLastFrame(result, !last_frame_.empty())) {
        // The protocol lets the compositor hold the request until the screen
        // changes; a static desktop must still fill the replay buffer.
    } else {
        ... existing buffer_constraints retry and failure paths ...
    }
    return normalizer_.Normalize(last_frame_.data(), last_w_ * 4, last_w_, last_h_,
                                 last_format_, false, now_ns, out);
```

Shutdown destroys `pending_frame_` if set.

- [ ] **Step 6: Portal clamp.** In `backend_portal.cpp` `CaptureFrame`, after computing `timestamp_ns`, add `timestamp_ns = std::max(timestamp_ns, last_timestamp_ns_ + 1); last_timestamp_ns_ = timestamp_ns;` with a new `int64_t last_timestamp_ns_ = 0;` member, so a repeated frame stamped "now" can never precede a later real frame's compositor pts and the ring's sorted-timestamp assumption holds.

- [ ] **Step 7: Build, ctest, physical check:** `FTHR_CAPTURE_BACKEND=ext FTHRcapture_linux/build/FTHRclips 60 10 0 0 8000 0 0 0 0 "DP-3" 0 4 0 1`, do not move the mouse for 30 s; the log must show no `frame request Timeout` failure and `GetFrameCount` (visible via the UI or by adding a temporary stderr print) keeps rising at ~60/s. Then a save through the UI succeeds. Commit: `git commit -m "fix(linux): ext-image-copy-capture repeats the last frame while the screen is static"`. Open PR B.

---

## Phase 1, PR C: audio, durability, VA-API preset (Tasks 7 to 9, branch `fix/linux-audio-save-hygiene`)

### Task 7: Audio startup retry and ring sizing

**Files:**
- Modify: `FTHRcapture_linux/src/audio_capture.h:14-67`, `audio_capture.cpp:138-170` and the `CaptureLoop` reconnect path (~236-269)
- Modify: `FTHRcapture_linux/src/capture_engine.cpp:30-40, 300-320` (pass buffer seconds; warning once)
- Test: `FTHRcapture_linux/tests/audio_timing_test.cpp`

**Interfaces:**
- Produces: `bool AudioCapture::Start(const std::string& device_name, uint32_t keep_seconds)`; returns `true` whenever the thread started, `false` only if the device name is unresolvable *and* `follow_default_` is false. `AudioCapture::KeepSeconds() const`.
- Produces: `size_t fthr::AudioRingCapacitySamples(uint32_t keep_seconds, int sample_rate, int channels)` in `audio_timing.h`.

- [ ] **Step 1: Failing test** (append to `audio_timing_test.cpp` main):

```cpp
    // The ring keeps the replay window plus a ten-second margin, never a
    // fixed five minutes.
    assert(fthr::AudioRingCapacitySamples(30, 48000, 2) == (30u + 10u) * 48000u * 2u);
    assert(fthr::AudioRingCapacitySamples(300, 48000, 2) == 310u * 48000u * 2u);
    assert(fthr::AudioRingCapacitySamples(0, 48000, 2) == 10u * 48000u * 2u);
```

- [ ] **Step 2: compile failure. Step 3: implement** in `audio_timing.h` as `inline size_t AudioRingCapacitySamples(uint32_t keep_seconds, int sample_rate, int channels) { return (static_cast<size_t>(keep_seconds) + 10) * sample_rate * channels; }`. **Step 4: PASS.**

- [ ] **Step 5: Use it.** Replace `kMaxSeconds` usage in `audio_capture.cpp`'s trim logic with `ring_capacity_samples_` set in `Start` from `AudioRingCapacitySamples(keep_seconds, kSampleRate, kChannels)`. Keep `kMaxSeconds` as the default `keep_seconds` argument value (`= 300`) so existing callers compile.

- [ ] **Step 6: Startup retry.** In `Start`, when `follow_default_` and `OpenStream` (or resolution) fails, do not return false: log once `"[Audio] Default output monitor unavailable; will keep trying every second"`, leave `stream_ = nullptr`, and still start `thread_` and `watcher_`. In `CaptureLoop`, the existing "read error → free stream → retry every 1 s" path must also handle `stream_ == nullptr` on entry by attempting `resolve_default_sink_monitor()` + `OpenStream` once per second until it succeeds (the watcher already re-resolves every 2 s; reuse `pending_source_`). The engine's `FTHR_STARTUP_WARNING: DESKTOP_AUDIO_UNAVAILABLE` line stays, printed by `CaptureEngine::Initialize` when `Start` reports `!IsStreamOpen()` immediately after starting (add `bool IsStreamOpen() const` reading an atomic set by the capture thread).

- [ ] **Step 7: Engine passes buffer seconds:** `audio_.Start("", cfg.buffer_seconds)` in both `Initialize` and `Reconfigure`.

- [ ] **Step 8: Build, ctest, physical check:** `systemctl --user stop pipewire-pulse.socket pipewire-pulse.service`, start the standalone engine (expect the warning once), `systemctl --user start pipewire-pulse.socket`, expect `[Audio] PulseAudio capture started` within ~2 s without restarting. Commit `fix(linux): desktop audio attaches when PulseAudio appears after startup; ring sized to the replay window`.

### Task 8: Save durability and unchecked FFmpeg returns

**Files:**
- Modify: `FTHRcapture_common/transactional_save.h:116-147` (Linux branch of `rename_no_replace` and a new `sync_file` op)
- Modify: `FTHRcapture_linux/src/save_clip.cpp:160-230`
- Test: `FTHRcapture_linux/tests/transactional_save_test.cpp`

- [ ] **Step 1: Failing test.** `FileOps` gains `std::function<bool(const fs::path&, std::error_code&)> sync_file;` and `sync_directory;` called by `Run` after the writer succeeds (file) and after a successful rename (directory of `final_path`). Add to the test a fake `FileOps` whose `sync_file` records the path and whose `sync_directory` records the parent; assert `Run` calls `sync_file(temporary_path)` before `rename_no_replace` and `sync_directory(final_path.parent_path())` after it, and that a `sync_file` failure yields `Failure::Writer` with the temp file cleaned up. Existing tests construct `FileOps` partially; give the new members defaults that return `true` so they keep passing.

- [ ] **Step 2: compile/assert failure. Step 3: implement.** `DefaultFileOps` on Linux: `sync_file` opens `O_RDONLY`, `fsync`, closes; `sync_directory` opens `O_RDONLY | O_DIRECTORY`, `fsync`, closes. On Windows both return true (the rename already uses `MOVEFILE_WRITE_THROUGH`). `Run` calls them at the two points above. **Step 4: PASS.**

- [ ] **Step 5: save_clip.cpp return checks.** `aac_ctx = avcodec_alloc_context3(aac_codec); if (!aac_ctx) { has_audio = false; }` guard the whole block; `if (swr_init(swr_ctx) < 0) { ...free aac_ctx and swr_ctx, has_audio = false; }`; `if (av_frame_get_buffer(aac_frame, 0) < 0) { ...same... }`. Each path logs `[SaveClip] <call> failed; saving video only`. The existing integration test `short_save_actual_mp4` must still pass.

- [ ] **Step 6: Build, ctest, commit** `fix(linux): fsync clips before publishing them; handle AAC setup failures`.

### Task 9: VA-API preset mapping and command hygiene

**Files:**
- Modify: `FTHRcapture_linux/src/encoder.cpp:17-72` (`ApplyPreset`)
- Modify: `FTHRcapture_linux/src/main.cpp:178-213` (default branch)
- Test: `FTHRcapture_linux/tests/linux_vaapi_encoder_test.cpp`

- [ ] **Step 1: Failing test.** In `linux_vaapi_encoder_test.cpp` (which returns 77 when VA-API is absent, so it only runs on this host and CI skips it), after opening `h264_vaapi` with preset 1 and again with preset 7, read back `av_opt_get_int(ctx->priv_data, "compression_level", 0, &v)` through a new test-only accessor `Encoder::DebugCompressionLevel()` and assert 7 for P1 and 1 for P7. If exposing a debug accessor is distasteful, test `ApplyPreset` directly: allocate a context for `avcodec_find_encoder_by_name("h264_vaapi")`, call `ApplyPreset(ctx, "h264_vaapi", 1)`, read the option, assert, free.

- [ ] **Step 2: fails (ApplyPreset returns false for vaapi). Step 3: implement** in `ApplyPreset`: `if (strstr(codec_name, "_vaapi")) { av_opt_set_int(ctx->priv_data, "compression_level", 8 - p, 0); return true; }` with a comment that Mesa treats 1 as highest quality and 7 as fastest, opposite to P1..P7. **Step 4: PASS on this host.**

- [ ] **Step 5: main.cpp.** Replace the `default: break;` with:

```cpp
            default:
                // Retired or Windows-only commands (SET_FRAMERATE, SET_HOTKEY,
                // SET_TARGET_WINDOW). Acknowledge so a caller never waits.
                layout->engine_response =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;
```

and run `.venv-linux/bin/python tools/verify_engine_response_contract.py` (it parses `main.cpp`).

- [ ] **Step 6: Build, ctest, both contract scripts, commit** `fix(linux): honour the encoder preset on VA-API; acknowledge unimplemented commands`. Open PR C.

---

## Execution

- Phase 0 is done natively by the session agent in order 0.1 → 0.5, because it is GitHub operations, not code.
- PR A (Tasks 1 to 4) is implemented natively by the session agent: the four tasks share `capture_engine.cpp` and the session already holds its design.
- PR B (Task 5) and PR C (Tasks 7 to 9) are implemented by subagents in separate git worktrees in parallel with PR A, because they touch disjoint files. Each subagent gets its task text verbatim plus the Global Constraints.
- Every PR gets a fresh reviewer subagent on the most capable model, reviewing the diff against the spec section it implements, before merge. Merge order: PR A, then B rebased on A (both edit `backend_ext.cpp` `Initialize`; B's rebase takes A's failure-kind lines), then C.
- After all three merge: run the full `ctest`, `pytest tests/`, both contract scripts, and the Phase 1 physical checks on the maintainer machine; record results in a comment on each PR.
