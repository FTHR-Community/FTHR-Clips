# Multiband Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-app audio categories with preset volumes baked into clips at save time, using PulseAudio virtual null sinks to capture each category separately.

**Architecture:** A new C++ `AudioMultiCapture` class uses the PA async API (`pa_threaded_mainloop`) to load one `module-null-sink` per category, subscribe to sink-input events, move matching apps to their category sink, and record each monitor. On clip save, `CaptureEngine` writes per-category WAV files; Python's new `audio_mixer.py` runs a single ffmpeg call to bake all tracks with preset volumes into the final clip. The feature is gated by `multiband_audio_enabled` in settings and a new argv flag.

**Tech Stack:** C++20 / libpulse (async) / FFmpeg / Python 3 / PyQt6 / ctypes shared memory

**Spec:** `docs/superpowers/specs/2026-06-01-multiband-audio-design.md`

---

## File Map

| File | Change |
|------|--------|
| `FTHRcapture_linux/src/audio_multi_capture.h` | New: AudioMultiCapture class |
| `FTHRcapture_linux/src/audio_multi_capture.cpp` | New: PA async implementation |
| `FTHRcapture_linux/src/shared_memory.h` | v3: add `active_audio_mappings[1024]` + `multiband_enabled` |
| `FTHRcapture_linux/src/capture_engine.h` | Add `multi_audio_`, `multiband_enabled_`, `GetAudioMappings()` |
| `FTHRcapture_linux/src/capture_engine.cpp` | Start/Stop multi_audio_, WAV writing in SaveClip |
| `FTHRcapture_linux/src/main.cpp` | argv[13]=multiband, poll mappings, CaptureConfig |
| `FTHRcapture_linux/CMakeLists.txt` | Add `audio_multi_capture.cpp`, link `libpulse` |
| `FTHR_UI/core/audio_mixer.py` | New: `mix_multiband_clip()` |
| `FTHR_UI/core/capture_bridge.py` | Layout v3, `get_audio_mappings()` |
| `FTHR_UI/core/settings_manager.py` | `audio_categories` defaults + `source_volumes` migration |
| `FTHR_UI/main.py` | `_save_clip` multiband flow, argv[13], UI Multiband section |
| `tests/test_audio_mixer.py` | New: unit tests for mix_multiband_clip |
| `tests/test_settings_multiband.py` | New: settings defaults + migration tests |

---

## Task 1: Settings defaults + source_volumes migration

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py`
- Create: `tests/test_settings_multiband.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_settings_multiband.py`:

```python
import sys, json
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')
from core.settings_manager import SettingsManager


def test_multiband_defaults_present(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('multiband_audio_enabled') is False
    cats = sm.get('audio_categories')
    assert isinstance(cats, list)
    names = [c['name'] for c in cats]
    assert 'Game' in names
    assert 'Sonstige' in names
    for c in cats:
        assert 'name' in c and 'volume' in c and 'patterns' in c


def test_old_source_volumes_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    cfg = tmp_path / '.fthr' / 'settings.json'
    cfg.parent.mkdir(parents=True)
    with open(cfg, 'w') as f:
        json.dump({'source_volumes': {'game': 90, 'discord': 70}}, f)

    sm = SettingsManager()
    cats = {c['name']: c for c in sm.get('audio_categories')}
    assert cats['Game']['volume'] == 90
    assert cats['Discord']['volume'] == 70


def test_multiband_toggle_default_false(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('multiband_audio_enabled') is False
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_settings_multiband.py -v 2>&1 | tail -10
```

Expected: `AssertionError` — keys not present yet.

- [ ] **Step 3: Add defaults + migration to settings_manager.py**

In `_load_settings`, after `'upload_auto_delete': False,`, add:

```python
            # Multiband audio
            'multiband_audio_enabled': False,
            'audio_categories': [
                {'name': 'Game',     'volume': 100, 'patterns': []},
                {'name': 'Discord',  'volume': 100, 'patterns': ['discord', 'Discord', 'WebRTC']},
                {'name': 'Browser',  'volume': 100, 'patterns': ['firefox', 'chrome', 'chromium', 'brave']},
                {'name': 'Musik',    'volume': 80,  'patterns': ['spotify', 'Spotify', 'vlc', 'mpv']},
                {'name': 'Sonstige', 'volume': 100, 'patterns': []},
            ],
```

Then add a migration step at the end of `_load_settings`, before the `return merged` line:

```python
            # Migrate old source_volumes to audio_categories if present
            if 'source_volumes' in loaded and 'audio_categories' not in loaded:
                sv = loaded['source_volumes']
                name_map = {'game': 'Game', 'discord': 'Discord',
                            'browser': 'Browser', 'music': 'Musik'}
                cats = merged['audio_categories']
                for old_key, new_name in name_map.items():
                    if old_key in sv:
                        for cat in cats:
                            if cat['name'] == new_name:
                                cat['volume'] = sv[old_key]
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_settings_multiband.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/core/settings_manager.py tests/test_settings_multiband.py
git commit -m "feat(settings): audio_categories defaults + source_volumes migration"
```

---

## Task 2: C++ AudioMultiCapture (PA async)

**Files:**
- Create: `FTHRcapture_linux/src/audio_multi_capture.h`
- Create: `FTHRcapture_linux/src/audio_multi_capture.cpp`

- [ ] **Step 1: Create audio_multi_capture.h**

```cpp
// FTHRcapture_linux/src/audio_multi_capture.h
#pragma once
#include <string>
#include <vector>
#include <deque>
#include <mutex>
#include <map>
#include <atomic>
#include <cstdint>

struct pa_threaded_mainloop;
struct pa_context;
struct pa_stream;

namespace fthr {

struct AudioCategoryConfig {
    std::string              name;       // display name, e.g. "Discord"
    std::string              sink_name;  // PA name,      e.g. "fthr_discord"
    std::vector<std::string> patterns;   // process binary substrings
};

class AudioMultiCapture {
public:
    static constexpr int kSampleRate = 48000;
    static constexpr int kChannels   = 2;
    static constexpr int kMaxSeconds = 120;

    AudioMultiCapture() = default;
    ~AudioMultiCapture() { Stop(); }

    bool Start(const std::vector<AudioCategoryConfig>& categories);
    void Stop();
    bool IsRunning() const { return running_.load(); }

    // Returns float32 stereo PCM for the given category over the last duration_ms
    // ending at end_time_ns (CLOCK_MONOTONIC). 0 = most-recent.
    std::vector<float> ExtractSegment(const std::string& category_name,
                                      int64_t end_time_ns,
                                      uint32_t duration_ms) const;

    // Current app→category mappings (for display in UI)
    std::map<std::string, std::string> GetCurrentMappings() const;

private:
    struct TimedChunk {
        std::vector<float> samples;
        int64_t            start_ns;
    };

    struct CategoryState {
        AudioCategoryConfig     config;
        uint32_t                module_idx = PA_INVALID_INDEX_VALUE;
        uint32_t                sink_idx   = PA_INVALID_INDEX_VALUE;
        pa_stream*              stream     = nullptr;
        mutable std::mutex      mutex;
        std::deque<TimedChunk>  chunks;

        static constexpr uint32_t PA_INVALID_INDEX_VALUE = UINT32_MAX;
    };

    static void context_state_cb(pa_context*, void* userdata);
    static void subscribe_cb(pa_context*, pa_subscription_event_type_t,
                              uint32_t idx, void* userdata);
    static void sink_input_info_cb(pa_context*, const struct pa_sink_input_info*,
                                   int eol, void* userdata);
    static void stream_read_cb(pa_stream*, size_t, void* userdata);

    void HandleNewSinkInput(uint32_t sink_input_idx);

    pa_threaded_mainloop*      mainloop_  = nullptr;
    pa_context*                context_   = nullptr;
    std::vector<CategoryState> categories_;

    mutable std::mutex                 mappings_mutex_;
    std::map<std::string, std::string> current_mappings_;  // process → category name

    std::atomic<bool> running_{false};
};

} // namespace fthr
```

- [ ] **Step 2: Create audio_multi_capture.cpp**

```cpp
// FTHRcapture_linux/src/audio_multi_capture.cpp
#include "audio_multi_capture.h"
#include <pulse/pulseaudio.h>
#include <iostream>
#include <cstring>
#include <time.h>
#include <algorithm>

namespace fthr {

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static int64_t mono_ns_multi() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1'000'000'000LL + ts.tv_nsec;
}

static constexpr size_t kMaxRingSamples =
    (size_t)AudioMultiCapture::kSampleRate *
    (size_t)AudioMultiCapture::kChannels   *
    (size_t)AudioMultiCapture::kMaxSeconds;

static constexpr size_t kChunkFrames = (size_t)AudioMultiCapture::kSampleRate / 100; // 10ms

// Sanitize category name to a valid PA sink name (lowercase, underscores only)
static std::string make_sink_name(const std::string& name) {
    std::string s = "fthr_";
    for (char c : name)
        s += (std::isalnum((unsigned char)c) ? (char)std::tolower((unsigned char)c) : '_');
    return s;
}

// ---------------------------------------------------------------------------
// Static callbacks
// ---------------------------------------------------------------------------

void AudioMultiCapture::context_state_cb(pa_context*, void* userdata) {
    pa_threaded_mainloop_signal((pa_threaded_mainloop*)userdata, 0);
}

void AudioMultiCapture::stream_read_cb(pa_stream* s, size_t /*nbytes*/, void* userdata) {
    auto* state = (CategoryState*)userdata;
    const void* data = nullptr;
    size_t      nbytes = 0;

    if (pa_stream_peek(s, &data, &nbytes) < 0 || !data || nbytes == 0) {
        pa_stream_drop(s);
        return;
    }

    const float* samples = (const float*)data;
    size_t       nfloats = nbytes / sizeof(float);

    TimedChunk chunk;
    chunk.samples.assign(samples, samples + nfloats);
    chunk.start_ns = mono_ns_multi();
    pa_stream_drop(s);

    {
        std::lock_guard<std::mutex> lk(state->mutex);
        state->chunks.push_back(std::move(chunk));
        size_t total = 0;
        for (auto& c : state->chunks) total += c.samples.size();
        while (total > kMaxRingSamples && !state->chunks.empty()) {
            total -= state->chunks.front().samples.size();
            state->chunks.pop_front();
        }
    }
}

struct SinkInputCtx {
    AudioMultiCapture* self;
    uint32_t           sink_input_idx;
};

void AudioMultiCapture::sink_input_info_cb(pa_context* ctx,
                                            const pa_sink_input_info* i,
                                            int eol, void* userdata) {
    auto* c = (SinkInputCtx*)userdata;
    if (!eol && i) {
        const char* binary = pa_proplist_gets(i->proplist,
                                              PA_PROP_APPLICATION_PROCESS_BINARY);
        if (!binary) binary = "";
        std::string app = binary;

        // Find matching category
        std::string matched_cat;
        uint32_t    target_sink = UINT32_MAX;

        auto& cats = c->self->categories_;
        for (auto& cat : cats) {
            for (auto& pat : cat.config.patterns) {
                if (app.find(pat) != std::string::npos) {
                    matched_cat  = cat.config.name;
                    target_sink  = cat.sink_idx;
                    break;
                }
            }
            if (!matched_cat.empty()) break;
        }

        // Fallback to "Sonstige" (last category with empty patterns as catch-all)
        if (matched_cat.empty()) {
            for (auto& cat : cats) {
                if (cat.config.patterns.empty() &&
                    cat.sink_idx != UINT32_MAX) {
                    matched_cat = cat.config.name;
                    target_sink = cat.sink_idx;
                }
            }
        }

        if (target_sink != UINT32_MAX && target_sink != i->sink) {
            pa_operation* op = pa_context_move_sink_input_by_index(
                ctx, i->index, target_sink, nullptr, nullptr);
            if (op) pa_operation_unref(op);
        }

        if (!app.empty() && !matched_cat.empty()) {
            std::lock_guard<std::mutex> lk(c->self->mappings_mutex_);
            c->self->current_mappings_[app] = matched_cat;
        }
    }
    if (eol) {
        delete c;
    }
}

void AudioMultiCapture::subscribe_cb(pa_context* ctx,
                                      pa_subscription_event_type_t t,
                                      uint32_t idx, void* userdata) {
    auto* self = (AudioMultiCapture*)userdata;
    auto facility = (t & PA_SUBSCRIPTION_EVENT_FACILITY_MASK);
    auto type     = (t & PA_SUBSCRIPTION_EVENT_TYPE_MASK);

    if (facility == PA_SUBSCRIPTION_EVENT_SINK_INPUT &&
        (type == PA_SUBSCRIPTION_EVENT_NEW ||
         type == PA_SUBSCRIPTION_EVENT_CHANGE)) {
        auto* c = new SinkInputCtx{self, idx};
        pa_operation* op = pa_context_get_sink_input_info(
            ctx, idx, sink_input_info_cb, c);
        if (op) pa_operation_unref(op);
    }
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

bool AudioMultiCapture::Start(const std::vector<AudioCategoryConfig>& configs) {
    if (running_.load()) return true;

    mainloop_ = pa_threaded_mainloop_new();
    if (!mainloop_) {
        std::cerr << "[MultiAudio] pa_threaded_mainloop_new failed" << std::endl;
        return false;
    }

    pa_mainloop_api* api = pa_threaded_mainloop_get_api(mainloop_);
    context_ = pa_context_new(api, "FTHRclips_multi");
    if (!context_) {
        pa_threaded_mainloop_free(mainloop_);
        mainloop_ = nullptr;
        return false;
    }

    pa_context_set_state_callback(context_, context_state_cb, mainloop_);

    pa_threaded_mainloop_lock(mainloop_);
    pa_threaded_mainloop_start(mainloop_);

    if (pa_context_connect(context_, nullptr, PA_CONTEXT_NOFLAGS, nullptr) < 0) {
        pa_threaded_mainloop_unlock(mainloop_);
        Stop();
        return false;
    }

    // Wait for context ready
    for (;;) {
        pa_context_state_t st = pa_context_get_state(context_);
        if (st == PA_CONTEXT_READY) break;
        if (st == PA_CONTEXT_FAILED || st == PA_CONTEXT_TERMINATED) {
            std::cerr << "[MultiAudio] PA context failed" << std::endl;
            pa_threaded_mainloop_unlock(mainloop_);
            Stop();
            return false;
        }
        pa_threaded_mainloop_wait(mainloop_);
    }

    // Build CategoryState list with sanitized sink names
    categories_.clear();
    for (const auto& cfg : configs) {
        CategoryState cs;
        cs.config = cfg;
        if (cs.config.sink_name.empty())
            cs.config.sink_name = make_sink_name(cfg.name);
        categories_.push_back(std::move(cs));
    }

    // Load module-null-sink for each category
    for (auto& cat : categories_) {
        std::string args = "sink_name=" + cat.config.sink_name +
                           " sink_properties=device.description=" + cat.config.sink_name;

        struct LoadCtx { pa_threaded_mainloop* ml; uint32_t idx; };
        LoadCtx lctx{mainloop_, UINT32_MAX};

        pa_operation* op = pa_context_load_module(
            context_, "module-null-sink", args.c_str(),
            [](pa_context*, uint32_t idx, void* ud) {
                auto* l = (LoadCtx*)ud;
                l->idx  = idx;
                pa_threaded_mainloop_signal(l->ml, 0);
            }, &lctx);

        if (op) {
            while (pa_operation_get_state(op) == PA_OPERATION_RUNNING)
                pa_threaded_mainloop_wait(mainloop_);
            pa_operation_unref(op);
        }

        cat.module_idx = lctx.idx;
        if (cat.module_idx == UINT32_MAX) {
            std::cerr << "[MultiAudio] Failed to load null-sink for "
                      << cat.config.name << std::endl;
            continue;
        }

        // Get sink index by name
        struct SinkCtx { pa_threaded_mainloop* ml; uint32_t idx; };
        SinkCtx sctx{mainloop_, UINT32_MAX};
        std::string mon_name = cat.config.sink_name + ".monitor";

        op = pa_context_get_sink_info_by_name(context_,
            cat.config.sink_name.c_str(),
            [](pa_context*, const pa_sink_info* i, int eol, void* ud) {
                auto* s = (SinkCtx*)ud;
                if (!eol && i) s->idx = i->index;
                if (eol) pa_threaded_mainloop_signal(s->ml, 0);
            }, &sctx);

        if (op) {
            while (pa_operation_get_state(op) == PA_OPERATION_RUNNING)
                pa_threaded_mainloop_wait(mainloop_);
            pa_operation_unref(op);
        }
        cat.sink_idx = sctx.idx;

        // Open record stream on the monitor source
        pa_sample_spec ss;
        ss.format   = PA_SAMPLE_FLOAT32LE;
        ss.rate     = kSampleRate;
        ss.channels = kChannels;

        cat.stream = pa_stream_new(context_, cat.config.name.c_str(), &ss, nullptr);
        if (!cat.stream) {
            std::cerr << "[MultiAudio] pa_stream_new failed for " << cat.config.name << std::endl;
            continue;
        }

        pa_stream_set_read_callback(cat.stream, stream_read_cb, &cat);

        pa_buffer_attr ba;
        ba.maxlength = (uint32_t)-1;
        ba.fragsize  = kChunkFrames * kChannels * sizeof(float);
        ba.prebuf = ba.minreq = ba.tlength = (uint32_t)-1;

        if (pa_stream_connect_record(cat.stream, mon_name.c_str(), &ba,
                                     PA_STREAM_ADJUST_LATENCY) < 0) {
            std::cerr << "[MultiAudio] connect_record failed for " << mon_name << std::endl;
            pa_stream_unref(cat.stream);
            cat.stream = nullptr;
        }
    }

    // Subscribe to sink input events
    pa_context_set_subscribe_callback(context_, subscribe_cb, this);
    pa_operation* sub_op = pa_context_subscribe(
        context_, PA_SUBSCRIPTION_MASK_SINK_INPUT,
        [](pa_context*, int, void* ud) {
            pa_threaded_mainloop_signal((pa_threaded_mainloop*)ud, 0);
        }, mainloop_);
    if (sub_op) {
        while (pa_operation_get_state(sub_op) == PA_OPERATION_RUNNING)
            pa_threaded_mainloop_wait(mainloop_);
        pa_operation_unref(sub_op);
    }

    pa_threaded_mainloop_unlock(mainloop_);
    running_.store(true);
    std::cout << "[MultiAudio] Started with " << categories_.size()
              << " categories" << std::endl;
    return true;
}

// ---------------------------------------------------------------------------
// Stop
// ---------------------------------------------------------------------------

void AudioMultiCapture::Stop() {
    if (!running_.load() && !mainloop_) return;
    running_.store(false);

    if (mainloop_) {
        pa_threaded_mainloop_lock(mainloop_);
        for (auto& cat : categories_) {
            if (cat.stream) {
                pa_stream_disconnect(cat.stream);
                pa_stream_unref(cat.stream);
                cat.stream = nullptr;
            }
            if (cat.module_idx != UINT32_MAX && context_) {
                pa_operation* op = pa_context_unload_module(
                    context_, cat.module_idx, nullptr, nullptr);
                if (op) pa_operation_unref(op);
            }
        }
        categories_.clear();
        if (context_) {
            pa_context_disconnect(context_);
            pa_context_unref(context_);
            context_ = nullptr;
        }
        pa_threaded_mainloop_unlock(mainloop_);
        pa_threaded_mainloop_stop(mainloop_);
        pa_threaded_mainloop_free(mainloop_);
        mainloop_ = nullptr;
    }
}

// ---------------------------------------------------------------------------
// ExtractSegment (identical logic to AudioCapture::ExtractSegment)
// ---------------------------------------------------------------------------

std::vector<float> AudioMultiCapture::ExtractSegment(
        const std::string& category_name,
        int64_t end_time_ns,
        uint32_t duration_ms) const {

    const CategoryState* state = nullptr;
    for (const auto& cat : categories_)
        if (cat.config.name == category_name) { state = &cat; break; }
    if (!state) return {};

    std::lock_guard<std::mutex> lk(state->mutex);
    if (state->chunks.empty()) return {};

    if (end_time_ns == 0) {
        const auto& last = state->chunks.back();
        int64_t ns_per_sample = 1'000'000'000LL / kSampleRate;
        end_time_ns = last.start_ns +
            (int64_t)(last.samples.size() / kChannels) * ns_per_sample;
    }

    int64_t start_ns = end_time_ns - (int64_t)duration_ms * 1'000'000LL;
    std::vector<float> result;
    int64_t ns_per_frame = 1'000'000'000LL / kSampleRate;

    for (const auto& chunk : state->chunks) {
        int64_t n_frames   = (int64_t)(chunk.samples.size() / kChannels);
        int64_t chunk_end  = chunk.start_ns + n_frames * ns_per_frame;
        if (chunk_end < start_ns)  continue;
        if (chunk.start_ns > end_time_ns) break;

        int64_t skip = 0;
        if (chunk.start_ns < start_ns)
            skip = (start_ns - chunk.start_ns) / ns_per_frame;
        int64_t take = n_frames - skip;
        int64_t over = (chunk_end - end_time_ns) / ns_per_frame;
        if (over > 0) take -= over;
        if (take <= 0) continue;

        size_t ss = (size_t)(skip * kChannels);
        size_t ts = (size_t)(take * kChannels);
        if (ss + ts > chunk.samples.size()) ts = chunk.samples.size() - ss;
        result.insert(result.end(),
                      chunk.samples.begin() + (ptrdiff_t)ss,
                      chunk.samples.begin() + (ptrdiff_t)(ss + ts));
    }
    return result;
}

// ---------------------------------------------------------------------------
// GetCurrentMappings
// ---------------------------------------------------------------------------

std::map<std::string, std::string> AudioMultiCapture::GetCurrentMappings() const {
    std::lock_guard<std::mutex> lk(mappings_mutex_);
    return current_mappings_;
}

} // namespace fthr
```

- [ ] **Step 3: Build to verify compilation**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && cmake .. && make -j$(nproc) 2>&1 | grep -E "error:|warning:" | head -20
```

Note: The build will fail until Task 3 (CMakeLists update). But the file should at least parse. Verify by checking for syntax errors:

```bash
g++ -std=c++20 -fsyntax-only \
  -I/home/tom/FTHR_Clips/FTHRcapture_linux/src \
  $(pkg-config --cflags libpulse) \
  /home/tom/FTHR_Clips/FTHRcapture_linux/src/audio_multi_capture.cpp
```

Expected: no syntax errors (linker errors are OK at this stage).

- [ ] **Step 4: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/audio_multi_capture.h FTHRcapture_linux/src/audio_multi_capture.cpp
git commit -m "feat(audio): AudioMultiCapture — PA async virtual sinks per category"
```

---

## Task 3: CMakeLists + Shared Memory v3

**Files:**
- Modify: `FTHRcapture_linux/CMakeLists.txt`
- Modify: `FTHRcapture_linux/src/shared_memory.h`

- [ ] **Step 1: Update CMakeLists.txt**

Add `libpulse` (async API) to pkg_check_modules:

```cmake
pkg_check_modules(PULSE REQUIRED IMPORTED_TARGET
    libpulse-simple libpulse)
```

Add `audio_multi_capture.cpp` to the `add_executable` source list, after `src/audio_capture.cpp`:

```cmake
    src/audio_multi_capture.cpp
```

- [ ] **Step 2: Add v3 fields to shared_memory.h**

After `char active_codec[64];` (the last v2 field), add:

```cpp
    // v3 fields
    bool     multiband_enabled;           // mirrored from CaptureConfig
    char     active_audio_mappings[1024]; // JSON: {"firefox":"Browser",...}
```

- [ ] **Step 3: Build — must succeed**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && cmake .. && make -j$(nproc) 2>&1 | tail -10
```

Expected: `[100%] Built target FTHRclips` with no errors.

- [ ] **Step 4: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/CMakeLists.txt FTHRcapture_linux/src/shared_memory.h
git commit -m "build: add libpulse async, audio_multi_capture.cpp; shm v3 fields"
```

---

## Task 4: CaptureEngine wiring + SaveClip WAV writing

**Files:**
- Modify: `FTHRcapture_linux/src/capture_engine.h`
- Modify: `FTHRcapture_linux/src/capture_engine.cpp`

- [ ] **Step 1: Update capture_engine.h**

Add `#include "audio_multi_capture.h"` at the top.

Add `multiband_enabled` to `CaptureConfig`:

```cpp
    bool      multiband_enabled = false;
    // audio_categories is passed separately to AudioMultiCapture::Start()
    std::vector<AudioCategoryConfig> audio_categories;
```

Add to `CaptureEngine` public section:

```cpp
    std::string GetAudioMappingsJson() const;
```

Add to private section:

```cpp
    AudioMultiCapture   multi_audio_;
```

- [ ] **Step 2: Update CaptureEngine::Initialize in capture_engine.cpp**

After `audio_.Start("");` (single-track start), replace with conditional logic. Find this line and replace:

```cpp
    // Start audio capture — single-track or multiband
    if (cfg.multiband_enabled && !cfg.audio_categories.empty()) {
        multi_audio_.Start(cfg.audio_categories);
        // Don't start audio_ — multiband takes over
    } else {
        audio_.Start("");
    }
```

- [ ] **Step 3: Update CaptureEngine::Shutdown in capture_engine.cpp**

After `audio_.Stop();`, add:

```cpp
    multi_audio_.Stop();
```

- [ ] **Step 4: Add GetAudioMappingsJson() to capture_engine.cpp**

Add before the closing `} // namespace fthr`:

```cpp
std::string CaptureEngine::GetAudioMappingsJson() const {
    if (!cfg_.multiband_enabled) return "{}";
    auto maps = multi_audio_.GetCurrentMappings();
    std::string json = "{";
    bool first = true;
    for (auto& [app, cat] : maps) {
        if (!first) json += ",";
        json += "\"" + app + "\":\"" + cat + "\"";
        first = false;
    }
    json += "}";
    return json;
}
```

- [ ] **Step 5: Update SaveClip to write per-category WAVs**

In `CaptureEngine::SaveClip`, after `auto video_packets = ring_->TakeSnapshot(duration_ms);`, add:

```cpp
    // Write per-category WAV files when multiband is active.
    // Filenames: <path>_fthr_game.wav, <path>_fthr_discord.wav, etc.
    // These are consumed by Python's audio_mixer and deleted after mixing.
    if (cfg_.multiband_enabled) {
        for (const auto& cat_cfg : cfg_.audio_categories) {
            std::vector<float> pcm = multi_audio_.ExtractSegment(
                cat_cfg.name, now_ns, duration_ms);
            if (pcm.empty()) continue;

            // Derive WAV path: strip .mp4, append _fthr_<sinkname>.wav
            std::string wav_path = path;
            size_t dot = wav_path.rfind('.');
            if (dot != std::string::npos) wav_path = wav_path.substr(0, dot);
            wav_path += "_fthr_" + cat_cfg.sink_name + ".wav";

            write_pcm_wav(wav_path, pcm,
                          AudioMultiCapture::kSampleRate,
                          AudioMultiCapture::kChannels);
        }
    }
```

Add the helper `write_pcm_wav` function to capture_engine.cpp (before `SaveClip`):

```cpp
static void write_pcm_wav(const std::string& path,
                            const std::vector<float>& pcm,
                            int sample_rate, int channels) {
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) return;

    uint32_t data_bytes = (uint32_t)(pcm.size() * sizeof(float));
    uint32_t file_size  = 36 + data_bytes;

    // RIFF header
    fwrite("RIFF", 1, 4, f);
    fwrite(&file_size, 4, 1, f);
    fwrite("WAVE", 1, 4, f);

    // fmt chunk — IEEE float PCM
    fwrite("fmt ", 1, 4, f);
    uint32_t fmt_size   = 16;
    uint16_t audio_fmt  = 3;  // IEEE float
    uint16_t ch         = (uint16_t)channels;
    uint32_t sr         = (uint32_t)sample_rate;
    uint32_t byte_rate  = sr * ch * 4;
    uint16_t block_align = (uint16_t)(ch * 4);
    uint16_t bits        = 32;
    fwrite(&fmt_size,    4, 1, f);
    fwrite(&audio_fmt,   2, 1, f);
    fwrite(&ch,          2, 1, f);
    fwrite(&sr,          4, 1, f);
    fwrite(&byte_rate,   4, 1, f);
    fwrite(&block_align, 2, 1, f);
    fwrite(&bits,        2, 1, f);

    // data chunk
    fwrite("data", 1, 4, f);
    fwrite(&data_bytes, 4, 1, f);
    fwrite(pcm.data(), sizeof(float), pcm.size(), f);
    fclose(f);
}
```

- [ ] **Step 6: Also update Reconfigure() to handle multiband**

In `CaptureEngine::Reconfigure`, replace `audio_.Start("")` with:

```cpp
    if (cfg_.multiband_enabled && !cfg_.audio_categories.empty()) {
        multi_audio_.Start(cfg_.audio_categories);
    } else {
        audio_.Start("");
    }
```

- [ ] **Step 7: Build to verify**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -10
```

Expected: clean build.

- [ ] **Step 8: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/capture_engine.cpp
git commit -m "feat(engine): wire AudioMultiCapture, write per-category WAVs on clip save"
```

---

## Task 5: main.cpp changes

**Files:**
- Modify: `FTHRcapture_linux/src/main.cpp`

- [ ] **Step 1: Parse argv[13] (multiband toggle)**

After the `cfg.preset` parsing block, add:

```cpp
    cfg.multiband_enabled = (argc > 13) && (arg_u32(argv, 13, 0) == 1);
```

- [ ] **Step 2: Initialize shared memory v3 fields**

In the initialization block after `memset(layout->active_codec, ...)`, add:

```cpp
    layout->multiband_enabled = cfg.multiband_enabled;
    memset(layout->active_audio_mappings, 0, sizeof(layout->active_audio_mappings));
```

- [ ] **Step 3: Build category configs from shared memory**

The category configs are passed via Python as argv params or via a separate channel. For simplicity in this iteration: use a hardcoded default set from the shared memory string field `ui_string` is already used for clip paths. Instead, category configs are passed at startup as a JSON string in a new shared memory field.

**Actually simpler:** Pass `multiband_enabled` via argv[13] and read category config from a JSON file that Python writes before starting the engine. Path: `~/.fthr/audio_categories.json`.

Add to main.cpp, after argv parsing:

```cpp
    // Load audio_categories from JSON file if multiband is enabled.
    // Python writes ~/.fthr/audio_categories.json before starting the engine.
    if (cfg.multiband_enabled) {
        std::string cat_path = std::string(getenv("HOME") ? getenv("HOME") : "") +
                               "/.fthr/audio_categories.json";
        FILE* fp = fopen(cat_path.c_str(), "r");
        if (fp) {
            // Minimal JSON parse: read lines of format {"name":"X","sink":"Y","patterns":["a","b"]}
            // Use a simple line-by-line approach — each category on its own line.
            // Full JSON parsing would require a library; we control the write format.
            char line[2048];
            while (fgets(line, sizeof(line), fp)) {
                fthr::AudioCategoryConfig c;
                // Parse "name":"VALUE"
                auto extract = [](const char* s, const char* key) -> std::string {
                    std::string k = std::string("\"") + key + "\":\"";
                    const char* p = strstr(s, k.c_str());
                    if (!p) return {};
                    p += k.size();
                    const char* e = strchr(p, '"');
                    return e ? std::string(p, e) : std::string(p);
                };
                c.name      = extract(line, "name");
                c.sink_name = extract(line, "sink");
                if (c.name.empty()) continue;
                // Parse patterns array — simple: find all "pattern" quoted strings after "patterns"
                const char* pat_start = strstr(line, "\"patterns\":");
                if (pat_start) {
                    const char* arr = strchr(pat_start, '[');
                    const char* arr_end = arr ? strchr(arr, ']') : nullptr;
                    if (arr && arr_end) {
                        const char* p = arr;
                        while (p < arr_end) {
                            p = strchr(p, '"');
                            if (!p || p >= arr_end) break;
                            ++p;
                            const char* e = strchr(p, '"');
                            if (!e || e >= arr_end) break;
                            c.patterns.push_back(std::string(p, e));
                            p = e + 1;
                        }
                    }
                }
                cfg.audio_categories.push_back(std::move(c));
            }
            fclose(fp);
            std::cout << "[FTHR] Loaded " << cfg.audio_categories.size()
                      << " audio categories" << std::endl;
        } else {
            std::cerr << "[FTHR] multiband enabled but " << cat_path
                      << " not found — disabling multiband" << std::endl;
            cfg.multiband_enabled = false;
        }
    }
```

- [ ] **Step 4: Update poll loop to write audio mappings**

In the 20ms poll loop, after the `active_codec` update block, add:

```cpp
        if (cfg.multiband_enabled) {
            std::string json = engine.GetAudioMappingsJson();
            if (json.size() < sizeof(layout->active_audio_mappings)) {
                strncpy(layout->active_audio_mappings, json.c_str(),
                        sizeof(layout->active_audio_mappings) - 1);
                layout->active_audio_mappings[sizeof(layout->active_audio_mappings)-1] = '\0';
            }
        }
```

- [ ] **Step 5: Bump shared memory name to v3**

```cpp
    if (!shm.Initialize("FTHR_SharedMemory_v3")) {
```

- [ ] **Step 6: Build + smoke test**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -5
```

```bash
/home/tom/FTHR_Clips/FTHRcapture_linux/build/FTHRclips 60 30 0 0 16000 1024 0 0 0 "" 0 4 0 &
sleep 3 && kill %1
```

Expected: engine starts with `[MultiAudio]` lines absent (multiband disabled), `[FTHR] Ready.`

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/main.cpp
git commit -m "feat(engine): argv[13] multiband toggle, audio_categories.json, shm v3"
```

---

## Task 6: Python bridge v3 + audio_mixer.py

**Files:**
- Modify: `FTHR_UI/core/capture_bridge.py`
- Create: `FTHR_UI/core/audio_mixer.py`
- Create: `tests/test_audio_mixer.py`

- [ ] **Step 1: Write failing tests for audio_mixer**

Create `tests/test_audio_mixer.py`:

```python
import sys, os, struct, tempfile, wave
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')
from core.audio_mixer import mix_multiband_clip, write_category_wavs_from_pcm


def _make_wav(path: str, n_samples: int = 48000):
    """Write a 1-second silent float32 stereo WAV."""
    with open(path, 'wb') as f:
        data_bytes = n_samples * 2 * 4  # stereo float32
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_bytes))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<IHHIIHH', 16, 3, 2, 48000, 48000*2*4, 8, 32))
        f.write(b'data')
        f.write(struct.pack('<I', data_bytes))
        f.write(bytes(data_bytes))


def test_mix_multiband_clip_runs(tmp_path):
    # Create a minimal valid MP4 stand-in (just check ffmpeg is called correctly)
    # We use a WAV as the "clip" to avoid needing a real MP4.
    clip = str(tmp_path / 'clip.wav')
    _make_wav(clip)

    game_wav  = str(tmp_path / 'game.wav')
    disc_wav  = str(tmp_path / 'disc.wav')
    _make_wav(game_wav)
    _make_wav(disc_wav)

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        import pytest; pytest.skip('ffmpeg not available')

    result = mix_multiband_clip(
        clip_path=clip,
        category_wavs={'Game': game_wav, 'Discord': disc_wav},
        volumes={'Game': 1.0, 'Discord': 0.7},
        ffmpeg_exe=ffmpeg,
    )
    assert result is True
    assert os.path.exists(clip)


def test_mix_multiband_clip_empty_wavs(tmp_path):
    """If no WAV files provided, returns False without crashing."""
    from core.audio_mixer import mix_multiband_clip
    result = mix_multiband_clip(
        clip_path=str(tmp_path / 'nonexistent.mp4'),
        category_wavs={},
        volumes={},
        ffmpeg_exe='/usr/bin/ffmpeg',
    )
    assert result is False
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_audio_mixer.py -v 2>&1 | tail -5
```

Expected: `ModuleNotFoundError: No module named 'core.audio_mixer'`

- [ ] **Step 3: Create audio_mixer.py**

Create `FTHR_UI/core/audio_mixer.py`:

```python
"""
audio_mixer.py — mix per-category WAV files into a video clip using ffmpeg.

Called after clip save when multiband audio is active. Mirrors the mic-mux
pattern in main.py but handles N category tracks instead of one mic track.
"""
import os
import subprocess
import tempfile
from typing import Optional


def mix_multiband_clip(
    clip_path: str,
    category_wavs: dict,   # {"Game": "/tmp/clip_fthr_game.wav", ...}
    volumes: dict,          # {"Game": 1.0, "Discord": 0.7, ...}
    ffmpeg_exe: str,
) -> bool:
    """
    Mix category WAV files into clip_path with the given volumes.
    Overwrites clip_path in-place on success.
    Returns True on success, False on any failure.
    """
    if not category_wavs:
        return False

    # Build filter_complex: volume each input then amix
    inputs = []
    filter_parts = []
    for i, (cat, wav_path) in enumerate(category_wavs.items()):
        if not os.path.exists(wav_path):
            continue
        vol = volumes.get(cat, 1.0)
        inputs.extend(['-i', wav_path])
        filter_parts.append(f'[{i+1}:a]volume={vol:.3f}[a{i}]')

    if not filter_parts:
        return False

    n = len(filter_parts)
    mix_inputs = ''.join(f'[a{i}]' for i in range(n))
    filter_complex = (
        ';'.join(filter_parts) +
        f';{mix_inputs}amix=inputs={n}:duration=first:dropout_transition=0[aout]'
    )

    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, 'mixed.mp4')
        cmd = (
            [ffmpeg_exe, '-y', '-i', clip_path]
            + inputs
            + ['-filter_complex', filter_complex,
               '-map', '0:v', '-map', '[aout]',
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-shortest', out_path]
        )
        try:
            result = subprocess.run(cmd, capture_output=True)
        except Exception as e:
            print(f'[AudioMixer] ffmpeg error: {e}')
            return False

        if result.returncode != 0:
            err = result.stderr.decode(errors='replace').strip().splitlines()
            print(f'[AudioMixer] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
            return False

        try:
            os.replace(out_path, clip_path)
        except OSError as e:
            print(f'[AudioMixer] Could not replace clip: {e}')
            return False

    print(f'[AudioMixer] Mixed {n} tracks into {os.path.basename(clip_path)}')
    return True
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_audio_mixer.py -v
```

Expected: `2 passed` (or 1 if ffmpeg not available — skip is acceptable).

- [ ] **Step 5: Update capture_bridge.py — layout v3 + new methods**

Add v3 fields after `('active_codec', ctypes.c_char * 64)` in BOTH the Windows and Linux `SharedMemoryLayout`:

```python
            # v3 fields
            ('multiband_enabled',      c_bool),
            ('active_audio_mappings',  ctypes.c_char * 1024),
```

Update `SHARED_MEM_NAME`:
```python
SHARED_MEM_NAME = 'FTHR_SharedMemory_v3'
```

Add two new methods after `get_active_preset()`:

```python
    def get_audio_mappings(self) -> dict:
        """Returns dict of {app_name: category_name} from shared memory."""
        if not self.is_connected():
            return {}
        try:
            raw = self._layout.active_audio_mappings
            text = raw.decode('utf-8', errors='ignore').rstrip('\x00')
            if not text or text == '{}':
                return {}
            import json
            return json.loads(text)
        except Exception:
            return {}

    def is_multiband_active(self) -> bool:
        if not self.is_connected():
            return False
        try:
            return bool(self._layout.multiband_enabled)
        except Exception:
            return False
```

- [ ] **Step 6: Run full test suite**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/ -v 2>&1 | tail -15
```

Expected: all tests pass (10+ passing).

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/core/capture_bridge.py FTHR_UI/core/audio_mixer.py tests/test_audio_mixer.py
git commit -m "feat(bridge,mixer): shm v3 layout, get_audio_mappings(), audio_mixer.py"
```

---

## Task 7: _save_clip multiband flow + audio_categories.json writing

**Files:**
- Modify: `FTHR_UI/main.py`

- [ ] **Step 1: Write audio_categories.json before engine start**

In `MainWindow.start_engine()`, after computing `codec_pref_int` and `encoder_preset`, add code to write the category config file and compute the multiband arg:

```python
            # Write audio categories config for the engine
            multiband_enabled = self.settings_manager.get('multiband_audio_enabled', False)
            if multiband_enabled:
                self._write_audio_categories_json()
            multiband_arg = '1' if multiband_enabled else '0'
```

Update the Popen args list to add `multiband_arg` after `str(encoder_preset)`:

```python
            self.engine_process = subprocess.Popen(
                [str(self.engine_path),
                 str(self.capture_fps), str(self.buffer_seconds),
                 str(self.capture_width), str(self.capture_height),
                 str(self.capture_bitrate), str(max_buffer_mb),
                 mode_arg, hwnd_arg, scale_arg, capture_monitor,
                 str(codec_pref_int), str(encoder_preset),
                 multiband_arg],
                **_NO_WINDOW
            )
```

Add `_write_audio_categories_json` to `MainWindow`:

```python
    def _write_audio_categories_json(self):
        """Write ~/.fthr/audio_categories.json for the C++ engine to read."""
        import json as _json
        cats = self.settings_manager.get('audio_categories', [])
        path = Path.home() / '.fthr' / 'audio_categories.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write one category per line as simple JSON objects for the engine's
        # minimal JSON parser. No nested objects, no special characters in names.
        with open(path, 'w') as f:
            for cat in cats:
                sink_name = 'fthr_' + ''.join(
                    c if c.isalnum() else '_' for c in cat['name'].lower())
                obj = {
                    'name': cat['name'],
                    'sink': sink_name,
                    'patterns': cat.get('patterns', []),
                }
                f.write(_json.dumps(obj, ensure_ascii=True) + '\n')
```

- [ ] **Step 2: Add multiband mix step to _save_clip**

In `_save_clip`, after the existing `self._mux_mic_into_clip(...)` call, add:

```python
                if self.settings_manager.get('multiband_audio_enabled', False):
                    self._mux_multiband_into_clip(
                        str(output_path), duration_seconds, mic_end_time)
```

Add the new method to `MainWindow`:

```python
    def _mux_multiband_into_clip(self, clip_path: str, duration_seconds: int,
                                  audio_end_time: float):
        """Mix per-category WAV files (written by engine) into the clip."""
        threading.Thread(
            target=self._multiband_mux_worker,
            args=(clip_path, duration_seconds, audio_end_time),
            daemon=True,
        ).start()

    def _multiband_mux_worker(self, clip_path: str, duration_seconds: int,
                               audio_end_time: float):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            print('[MultiAudio] imageio-ffmpeg missing — skipping multiband mix')
            return

        from core.audio_mixer import mix_multiband_clip

        # Wait for clip to stabilize (same pattern as mic mux)
        deadline = time.monotonic() + max(duration_seconds * 2, 15)
        last_size = -1
        while time.monotonic() < deadline:
            try:
                if os.path.exists(clip_path):
                    size = os.path.getsize(clip_path)
                    if size > 0 and size == last_size:
                        break
                    last_size = size
            except OSError:
                pass
            time.sleep(0.25)
        else:
            print(f'[MultiAudio] Clip {clip_path} did not stabilize')
            return

        # Find WAV files written by the engine: clip_path_without_ext_fthr_*.wav
        base = os.path.splitext(clip_path)[0]
        cats = self.settings_manager.get('audio_categories', [])
        category_wavs = {}
        volumes = {}
        for cat in cats:
            sink_name = 'fthr_' + ''.join(
                c if c.isalnum() else '_' for c in cat['name'].lower())
            wav_path = f'{base}_{sink_name}.wav'
            if os.path.exists(wav_path):
                category_wavs[cat['name']] = wav_path
                volumes[cat['name']] = cat.get('volume', 100) / 100.0

        if not category_wavs:
            print('[MultiAudio] No category WAVs found — skipping mix')
            return

        ok = mix_multiband_clip(clip_path, category_wavs, volumes, ffmpeg)

        # Clean up WAV files regardless of mix result
        for wav in category_wavs.values():
            try:
                os.remove(wav)
            except OSError:
                pass

        if not ok:
            print('[MultiAudio] Mix failed')
```

- [ ] **Step 3: Verify Python parses**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI && python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Run tests**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/ -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(clip): write audio_categories.json, multiband WAV mix on clip save"
```

---

## Task 8: UI — Multiband Audio section in Audio Settings page

**Files:**
- Modify: `FTHR_UI/main.py`

- [ ] **Step 1: Add Multiband section at bottom of _make_audio_page()**

Find `_make_audio_page` in `_SettingsPage` (around line 3003). The method ends with `return page` after the mic enumeration timer. Before `return page`, add:

```python
        # ── Multiband Audio section ───────────────────────────────────────
        outer.addSpacing(24)
        outer.addWidget(_settings_hsep())
        outer.addSpacing(20)
        outer.addWidget(_flat_section_header('Multiband Audio'))
        outer.addSpacing(8)

        mb_desc = QLabel(
            'Nimmt jede App-Kategorie separat auf und brennt die Lautstärke-Presets '
            'beim Clip-Save ein. Deaktiviere es für maximale Performance.')
        mb_desc.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        mb_desc.setWordWrap(True)
        outer.addWidget(mb_desc)
        outer.addSpacing(12)

        # Toggle
        self.multiband_check = QCheckBox('Multiband Audio aktivieren')
        self.multiband_check.setStyleSheet(CHECKBOX_QSS)
        self.multiband_check.setChecked(self.sm.get('multiband_audio_enabled', False))
        self.multiband_check.toggled.connect(self._on_multiband_toggled)
        outer.addWidget(self.multiband_check)
        outer.addSpacing(12)

        # Category container (visible only when enabled)
        self.multiband_container = QWidget()
        mb_layout = QVBoxLayout(self.multiband_container)
        mb_layout.setContentsMargins(0, 0, 0, 0)
        mb_layout.setSpacing(6)

        self._cat_rows = []  # list of (name_lbl, slider, del_btn) per category
        self._rebuild_category_rows(mb_layout)

        outer.addWidget(self.multiband_container)
        self.multiband_container.setVisible(self.sm.get('multiband_audio_enabled', False))

        # Erkannte Apps label
        self.mappings_lbl = QLabel('Erkannte Apps: —')
        self.mappings_lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.mappings_lbl.setWordWrap(True)
        outer.addWidget(self.mappings_lbl)
        outer.addSpacing(8)

        # + Kategorie hinzufügen
        add_cat_row = QHBoxLayout()
        self.new_cat_name = QLineEdit()
        self.new_cat_name.setPlaceholderText('Name (z.B. "Musik")')
        self.new_cat_name.setStyleSheet(COMBO_QSS)
        self.new_cat_patterns = QLineEdit()
        self.new_cat_patterns.setPlaceholderText('Patterns: spotify,Spotify,vlc')
        self.new_cat_patterns.setStyleSheet(COMBO_QSS)
        add_btn = QPushButton('+ Hinzufügen')
        add_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        add_btn.clicked.connect(self._on_add_category)
        add_cat_row.addWidget(self.new_cat_name, 1)
        add_cat_row.addWidget(self.new_cat_patterns, 2)
        add_cat_row.addWidget(add_btn)
        outer.addLayout(add_cat_row)

        # Timer für Mappings-Label aktualisieren
        self._mappings_timer = QTimer(self)
        self._mappings_timer.setInterval(2000)
        self._mappings_timer.timeout.connect(self._update_mappings_label)
```

- [ ] **Step 2: Add helper methods to _SettingsPage**

After `_make_audio_page`, add:

```python
    def _rebuild_category_rows(self, layout: QVBoxLayout):
        # Clear existing rows
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cat_rows = []

        cats = self.sm.get('audio_categories', [])
        for i, cat in enumerate(cats):
            row = QHBoxLayout()
            name_lbl = QLabel(cat['name'])
            name_lbl.setFixedWidth(100)
            name_lbl.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setStyleSheet(SLIDER_QSS)
            slider.setRange(0, 100)
            slider.setValue(cat.get('volume', 100))
            val_lbl = QLabel(f"{cat.get('volume', 100)}%")
            val_lbl.setFixedWidth(38)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(
                lambda v, idx=i, lbl=val_lbl: self._on_cat_volume(idx, v, lbl))
            del_btn = QPushButton('✕')
            del_btn.setFixedSize(24, 24)
            del_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
            del_btn.clicked.connect(lambda _, idx=i: self._on_delete_category(idx))
            row.addWidget(name_lbl)
            row.addWidget(slider, 1)
            row.addWidget(val_lbl)
            row.addWidget(del_btn)
            w = QWidget()
            w.setLayout(row)
            layout.addWidget(w)
            self._cat_rows.append((name_lbl, slider, val_lbl, del_btn))

    def _on_multiband_toggled(self, checked: bool):
        self.sm.set('multiband_audio_enabled', checked)
        self.sm.save_settings()
        self.multiband_container.setVisible(checked)
        if checked:
            self._mappings_timer.start()
        else:
            self._mappings_timer.stop()

    def _on_cat_volume(self, idx: int, vol: int, lbl: QLabel):
        lbl.setText(f'{vol}%')
        cats = self.sm.get('audio_categories', [])
        if 0 <= idx < len(cats):
            cats[idx]['volume'] = vol
            self.sm.set('audio_categories', cats)
            self.sm.save_settings()

    def _on_delete_category(self, idx: int):
        cats = self.sm.get('audio_categories', [])
        if 0 <= idx < len(cats):
            cats.pop(idx)
            self.sm.set('audio_categories', cats)
            self.sm.save_settings()
            mb_layout = self.multiband_container.layout()
            self._rebuild_category_rows(mb_layout)

    def _on_add_category(self):
        name = self.new_cat_name.text().strip()
        if not name:
            return
        pats_raw = self.new_cat_patterns.text().strip()
        patterns = [p.strip() for p in pats_raw.split(',') if p.strip()]
        cats = self.sm.get('audio_categories', [])
        cats.append({'name': name, 'volume': 100, 'patterns': patterns})
        self.sm.set('audio_categories', cats)
        self.sm.save_settings()
        self.new_cat_name.clear()
        self.new_cat_patterns.clear()
        mb_layout = self.multiband_container.layout()
        self._rebuild_category_rows(mb_layout)

    def _update_mappings_label(self):
        # Only callable if a bridge reference is available — pass it in via _SettingsPage init
        # For simplicity, access via the parent MainWindow
        try:
            main_win = self.parent().parent()  # SettingsPage → stackedWidget → MainWindow
            mappings = main_win.bridge.get_audio_mappings()
            if not mappings:
                self.mappings_lbl.setText('Erkannte Apps: (keine)')
            else:
                parts = [f'{app} → {cat} ✓' for app, cat in mappings.items()]
                self.mappings_lbl.setText('Erkannte Apps: ' + '   '.join(parts))
        except Exception:
            pass
```

- [ ] **Step 3: Wire mappings timer when settings page becomes visible**

In `MainWindow._toggle_settings_page`, find where the settings page is shown (`self.main_stack.setCurrentIndex(1)`). After it, add:

```python
            if self.settings_manager.get('multiband_audio_enabled', False):
                try:
                    self._settings_page_widget._mappings_timer.start()
                except AttributeError:
                    pass
```

And when hiding the settings page (fade out), add:

```python
            try:
                self._settings_page_widget._mappings_timer.stop()
            except AttributeError:
                pass
```

- [ ] **Step 4: Add missing imports to main.py**

Verify `QLineEdit` is in the existing imports. If not, add it to the PyQt6 import block at the top:

```python
from PyQt6.QtWidgets import (
    ...,
    QLineEdit,
)
```

- [ ] **Step 5: Verify Python parses**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI && python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Run full test suite**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/ -v 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): Multiband Audio section in Audio settings — toggle, categories, volume, mappings"
```

---

## Final verification

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/ -v
```

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -5
```

Both must pass cleanly.
