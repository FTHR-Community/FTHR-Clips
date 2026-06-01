# Encoder-Ausbau Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the capture engine to support H.264/HEVC/AV1 across NVENC/AMF/QSV/Software, with P1–P7 presets user-selectable in the Performance tab.

**Architecture:** Approach C — codec-family + vendor-probing. `EncoderConfig` gains `codec_pref` (Auto/H264/HEVC/AV1) and `preset` (1–7). `Open()` builds a dynamic priority list filtered to the chosen codec, then probes vendors in order. A new `ApplyPreset()` helper maps P1–P7 to vendor-specific strings. Shared memory gains 3 new fields; the Python bridge and UI are updated to match. Engine restart is triggered by `RECONFIGURE_ENCODER` command which calls `engine.Shutdown()` + `engine.Initialize()` in-process.

**Tech Stack:** C++17/FFmpeg (libavcodec), Python 3/PyQt6, ctypes shared memory, cmake

**Spec:** `docs/superpowers/specs/2026-06-01-encoder-ausbau-design.md`

---

## File Map

| File | Change |
|------|--------|
| `FTHRcapture_linux/src/encoder.h` | Add `CodecPref` enum + new `EncoderConfig` fields |
| `FTHRcapture_linux/src/encoder.cpp` | Dynamic priority list + `ApplyPreset()` |
| `FTHRcapture_linux/src/shared_memory.h` | 3 new fields, version bump to v2 |
| `FTHRcapture_linux/src/capture_engine.h` | `Reconfigure()` method + atomic members |
| `FTHRcapture_linux/src/capture_engine.cpp` | Implement `Reconfigure()`, pass codec_pref/preset, write active_codec |
| `FTHRcapture_linux/src/main.cpp` | Parse argv[11]/[12], init active_codec, handle `RECONFIGURE_ENCODER` |
| `FTHR_UI/core/capture_bridge.py` | Updated layout (v2), `set_encoder_config()`, `get_active_codec()` |
| `FTHR_UI/core/settings_manager.py` | 2 new defaults |
| `FTHR_UI/main.py` | Update `_make_performance_page()`, `start_engine()`, `_update_status()` |
| `tests/test_capture_bridge.py` | New: unit tests for bridge methods |

---

## Task 1: C++ Encoder — CodecPref + ApplyPreset + dynamic Open()

**Files:**
- Modify: `FTHRcapture_linux/src/encoder.h`
- Modify: `FTHRcapture_linux/src/encoder.cpp`

- [ ] **Step 1: Add CodecPref enum and new fields to EncoderConfig in encoder.h**

Replace the existing `EncoderConfig` struct with:

```cpp
// encoder.h — replace lines 17-24

enum class CodecPref : uint32_t { Auto = 0, H264 = 1, HEVC = 2, AV1 = 3 };

struct EncoderConfig {
    uint32_t  src_width;
    uint32_t  src_height;
    uint32_t  enc_width;
    uint32_t  enc_height;
    uint32_t  fps;
    uint32_t  bitrate_kbps;
    CodecPref codec_pref = CodecPref::Auto;
    int       preset     = 4;   // 1=fastest … 7=best quality
};
```

- [ ] **Step 2: Add ApplyPreset declaration to encoder.h**

After the `Encoder` class closing brace (line 57), before `} // namespace fthr`, add:

```cpp
// Maps P1–P7 to the codec-specific preset string. Call after avcodec_alloc_context3,
// before avcodec_open2. Returns false if codec_name is unrecognised.
bool ApplyPreset(AVCodecContext* ctx, const char* codec_name, int preset_1_7);
```

- [ ] **Step 3: Replace TryOpen + Open in encoder.cpp**

Replace the entire contents of encoder.cpp (keep includes and `clock_ns` helper, replace everything from `bool Encoder::TryOpen` to the end of `bool Encoder::Open`) with:

```cpp
// ---------------------------------------------------------------------------
// ApplyPreset — maps P1–P7 to vendor-specific preset strings
// ---------------------------------------------------------------------------

bool ApplyPreset(AVCodecContext* ctx, const char* codec_name, int p) {
    // Clamp to valid range
    if (p < 1) p = 1;
    if (p > 7) p = 7;

    if (strstr(codec_name, "nvenc")) {
        // NVENC uses literal "p1".."p7"
        char ps[3] = {'p', static_cast<char>('0' + p), '\0'};
        av_opt_set(ctx->priv_data, "preset", ps,     0);
        av_opt_set(ctx->priv_data, "tune",   "ll",   0);
        av_opt_set(ctx->priv_data, "rc",     "vbr",  0);
        av_opt_set(ctx->priv_data, "cbr",    "0",    0);
    } else if (strstr(codec_name, "amf")) {
        static const char* kAmf[] = {
            "speed","speed","balanced","balanced","balanced","quality","quality"};
        av_opt_set(ctx->priv_data, "quality", kAmf[p - 1],        0);
        av_opt_set(ctx->priv_data, "rc",      "vbr_latency",       0);
    } else if (strstr(codec_name, "qsv")) {
        static const char* kQsv[] = {
            "veryfast","fast","medium","medium","slow","slower","slowest"};
        av_opt_set(ctx->priv_data, "preset",      kQsv[p - 1], 0);
        av_opt_set(ctx->priv_data, "look_ahead",  "0",         0);
    } else if (strstr(codec_name, "libx26")) {
        // libx264 + libx265 share preset names
        static const char* kX26x[] = {
            "ultrafast","superfast","veryfast","fast","medium","slow","veryslow"};
        av_opt_set(ctx->priv_data, "preset", kX26x[p - 1],    0);
        av_opt_set(ctx->priv_data, "tune",   "zerolatency",   0);
    } else if (strstr(codec_name, "svtav1")) {
        // SVT-AV1: preset 0 (best quality) … 13 (fastest). Map P1–P7 to 12,9,7,6,4,1,0
        static const int kSvt[] = {12, 9, 7, 6, 4, 1, 0};
        char ps[4];
        snprintf(ps, sizeof(ps), "%d", kSvt[p - 1]);
        av_opt_set(ctx->priv_data, "preset", ps, 0);
    }
    // libaom-av1: no numeric preset option, skip
    return true;
}

// ---------------------------------------------------------------------------
// TryOpen — attempt to open one codec by name
// ---------------------------------------------------------------------------

bool Encoder::TryOpen(const char* codec_name, const EncoderConfig& cfg) {
    const AVCodec* codec = avcodec_find_encoder_by_name(codec_name);
    if (!codec) return false;

    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    if (!ctx) return false;

    ctx->width     = static_cast<int>(cfg.enc_width);
    ctx->height    = static_cast<int>(cfg.enc_height);
    ctx->time_base = { 1, static_cast<int>(cfg.fps) };
    ctx->framerate = { static_cast<int>(cfg.fps), 1 };
    ctx->bit_rate  = static_cast<int64_t>(cfg.bitrate_kbps) * 1000LL;
    ctx->gop_size  = static_cast<int>(cfg.fps) * 2;
    ctx->max_b_frames = 0;

    bool is_hw = (strstr(codec_name, "nvenc") || strstr(codec_name, "amf") ||
                  strstr(codec_name, "qsv"));
    ctx->pix_fmt = is_hw ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;

    ApplyPreset(ctx, codec_name, cfg.preset);

    ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    if (avcodec_open2(ctx, codec, nullptr) < 0) {
        avcodec_free_context(&ctx);
        return false;
    }

    codec_ctx_ = ctx;
    cfg_       = cfg;
    return true;
}

// ---------------------------------------------------------------------------
// Build priority list for Open()
// ---------------------------------------------------------------------------

static void BuildCodecList(CodecPref pref, std::vector<const char*>& out) {
    // Each family: NVENC → AMF → QSV → SW
    auto add = [&](const char* name) { out.push_back(name); };

    if (pref == CodecPref::Auto || pref == CodecPref::AV1) {
        add("av1_nvenc"); add("av1_amf"); add("av1_qsv"); add("libsvtav1"); add("libaom-av1");
    }
    if (pref == CodecPref::Auto || pref == CodecPref::HEVC) {
        add("hevc_nvenc"); add("hevc_amf"); add("hevc_qsv"); add("libx265");
    }
    if (pref == CodecPref::Auto || pref == CodecPref::H264) {
        add("h264_nvenc"); add("h264_amf"); add("h264_qsv"); add("libx264");
    }
}

// ---------------------------------------------------------------------------
// Open — try codecs in priority order
// ---------------------------------------------------------------------------

bool Encoder::Open(const EncoderConfig& cfg, std::string& codec_used_out) {
    std::vector<const char*> candidates;
    BuildCodecList(cfg.codec_pref, candidates);

    for (const char* name : candidates) {
        std::cout << "[Encoder] Trying codec: " << name << std::endl;
        if (TryOpen(name, cfg)) {
            codec_used_out = name;
            std::cout << "[Encoder] Using: " << name
                      << "  " << cfg.enc_width << "x" << cfg.enc_height
                      << "  " << cfg.fps << "fps  "
                      << cfg.bitrate_kbps << "kbps"
                      << "  preset=P" << cfg.preset << std::endl;
            break;
        }
    }

    if (!codec_ctx_) {
        std::cerr << "[Encoder] All codecs failed — no encoder available" << std::endl;
        return false;
    }

    AVPixelFormat dst_fmt = codec_ctx_->pix_fmt;
    sws_ctx_ = sws_getContext(
        static_cast<int>(cfg.src_width),
        static_cast<int>(cfg.src_height),
        AV_PIX_FMT_BGRA,
        codec_ctx_->width,
        codec_ctx_->height,
        dst_fmt,
        SWS_BILINEAR, nullptr, nullptr, nullptr
    );
    if (!sws_ctx_) {
        std::cerr << "[Encoder] sws_getContext failed" << std::endl;
        Close();
        return false;
    }

    yuv_frame_ = av_frame_alloc();
    if (!yuv_frame_) { Close(); return false; }
    yuv_frame_->format = dst_fmt;
    yuv_frame_->width  = codec_ctx_->width;
    yuv_frame_->height = codec_ctx_->height;
    if (av_frame_get_buffer(yuv_frame_, 32) < 0) {
        std::cerr << "[Encoder] av_frame_get_buffer failed" << std::endl;
        Close();
        return false;
    }

    pkt_ = av_packet_alloc();
    if (!pkt_) { Close(); return false; }

    next_pts_ = 0;
    return true;
}
```

Keep `Close()`, `EncodeFrame()`, and `GetExtradata()` exactly as they are.

- [ ] **Step 4: Build the engine to verify C++ compiles**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) 2>&1 | tail -20
```

Expected: `[100%] Linking CXX executable FTHRclips` with no errors.

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/encoder.h FTHRcapture_linux/src/encoder.cpp
git commit -m "feat(encoder): add CodecPref enum, ApplyPreset(), dynamic multi-codec priority list"
```

---

## Task 2: Shared Memory Layout + CaptureEngine + main.cpp

**Files:**
- Modify: `FTHRcapture_linux/src/shared_memory.h`
- Modify: `FTHRcapture_linux/src/capture_engine.h`
- Modify: `FTHRcapture_linux/src/capture_engine.cpp`
- Modify: `FTHRcapture_linux/src/main.cpp`

- [ ] **Step 1: Add 3 new fields to SharedMemoryLayout (shared_memory.h)**

After the `bool nvenc_active;` line (currently last field), add:

```cpp
    // Encoder config — written by UI before RECONFIGURE_ENCODER command.
    // active_codec is written by the engine after Open() succeeds.
    uint32_t cfg_codec_pref;     // 0=auto 1=h264 2=hevc 3=av1
    uint32_t cfg_preset;         // 1–7
    char     active_codec[64];   // e.g. "hevc_nvenc\0"
```

The struct must stay binary-compatible with the Python side (same field order).

- [ ] **Step 2: Add Reconfigure() method + atomic members to capture_engine.h**

After `bool IsNvencActive() const` and before `private:`, add:

```cpp
    // Hot-reconfigure: stops capture thread, reopens encoder with new config,
    // restarts capture thread. Brief gap in recording (~1–2s).
    void Reconfigure(uint32_t codec_pref, int preset);
```

In the `private:` section, after `std::atomic<uint64_t> frame_count_{0};`, add nothing — `Reconfigure()` uses the existing `cfg_` + `Shutdown()` + `Initialize()` pattern.

- [ ] **Step 3: Implement Reconfigure() in capture_engine.cpp**

Add this function after `CaptureEngine::SaveClip` at the end of the file, before the closing `} // namespace fthr`:

```cpp
void CaptureEngine::Reconfigure(uint32_t codec_pref, int preset) {
    // Stop the running capture loop
    Shutdown();

    // Update codec config
    cfg_.codec_pref = static_cast<CodecPref>(codec_pref);
    cfg_.preset     = preset;

    // Restart capture loop with new encoder settings
    running_.store(true);
    cap_thread_ = std::thread(&CaptureEngine::CaptureLoop, this);
}
```

Also update the `CaptureEngine::CaptureLoop` encoder-open block (around line 418–428 in the original) to pass codec_pref and preset from cfg_:

```cpp
    // Open encoder
    EncoderConfig enc_cfg;
    enc_cfg.src_width    = native_w;
    enc_cfg.src_height   = native_h;
    enc_cfg.enc_width    = enc_w;
    enc_cfg.enc_height   = enc_h;
    enc_cfg.fps          = cfg_.fps;
    enc_cfg.bitrate_kbps = cfg_.bitrate_kbps;
    enc_cfg.codec_pref   = cfg_.codec_pref;   // NEW
    enc_cfg.preset       = cfg_.preset;        // NEW
```

- [ ] **Step 4: Update CaptureConfig in capture_engine.h to include codec config**

In `struct CaptureConfig`, after `std::string target_output;`, add:

```cpp
    CodecPref   codec_pref = CodecPref::Auto;
    int         preset     = 4;
```

Add `#include "encoder.h"` at the top of `capture_engine.h` if not already there (it is — encoder.h is already included).

- [ ] **Step 5: Update main.cpp — parse argv[11]/[12], write active_codec, handle RECONFIGURE_ENCODER**

After `cfg.target_output = ...` (line 53), add:

```cpp
    cfg.codec_pref = static_cast<fthr::CodecPref>(
        (argc > 11) ? arg_u32(argv, 11, 0) : 0);
    cfg.preset     = (argc > 12) ? static_cast<int>(arg_u32(argv, 12, 4)) : 4;
    if (cfg.preset < 1) cfg.preset = 1;
    if (cfg.preset > 7) cfg.preset = 7;
```

After `layout->nvenc_active = engine.IsNvencActive();` (initialization block, line 91), write the initial active_codec. First, we need to expose it from the engine. Add to CaptureEngine a new method `GetActiveCodec()` — but that requires storing it. Simpler: after engine.Initialize() in main.cpp, send a GET_STATUS and have the loop write it. Actually simplest: write it after the capture loop is running (engine.Initialize already spawned the thread). Use a short poll:

Replace the single line `layout->nvenc_active = engine.IsNvencActive();` with:

```cpp
    layout->nvenc_active     = engine.IsNvencActive();
    // active_codec is written by the engine thread after Open() — poll briefly.
    // Falls through with empty string if engine hasn't opened yet; UI will update on first GET_STATUS.
    memset(layout->active_codec, 0, sizeof(layout->active_codec));
```

Add `RECONFIGURE_ENCODER` case to the command switch in main.cpp, after the `GET_STATUS` case:

```cpp
            case fthr::CommandType::RECONFIGURE_ENCODER: {
                uint32_t new_codec_pref = layout->cfg_codec_pref;
                int      new_preset     = static_cast<int>(layout->cfg_preset);
                if (new_preset < 1) new_preset = 1;
                if (new_preset > 7) new_preset = 7;

                std::cout << "[FTHR] RECONFIGURE_ENCODER  codec_pref="
                          << new_codec_pref << "  preset=" << new_preset << std::endl;

                engine.Reconfigure(new_codec_pref, new_preset);

                memset(layout->active_codec, 0, sizeof(layout->active_codec));
                layout->nvenc_active     = engine.IsNvencActive();
                layout->engine_response  =
                    static_cast<uint32_t>(fthr::ResponseType::STATUS_UPDATE);
                break;
            }
```

To write `active_codec` from the engine thread, add a method to CaptureEngine. In `capture_engine.h` private section, add `std::string active_codec_;` and a public getter:

```cpp
    const std::string& GetActiveCodec() const { return active_codec_; }
```

In `capture_engine.cpp` CaptureLoop, after `nvenc_active_.store(...)`, add:

```cpp
    active_codec_ = codec_used;
```

Then in the main.cpp command loop's 20ms tick, after `layout->nvenc_active = engine.IsNvencActive();`, add:

```cpp
        // Keep active_codec in shared memory up to date
        const std::string& ac = engine.GetActiveCodec();
        if (!ac.empty()) {
            strncpy(layout->active_codec, ac.c_str(),
                    sizeof(layout->active_codec) - 1);
            layout->active_codec[sizeof(layout->active_codec) - 1] = '\0';
        }
```

Also update the shared memory name in main.cpp line 72:

```cpp
    if (!shm.Initialize("FTHR_SharedMemory_v2")) {
```

- [ ] **Step 6: Build again to verify**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -20
```

Expected: builds cleanly.

- [ ] **Step 7: Smoke-test the engine manually**

```bash
/home/tom/FTHR_Clips/FTHRcapture_linux/build/FTHRclips 60 30 0 0 16000 1024 0 0 0 "" 0 4 2>&1 | head -20
```

Expected: lines like `[Encoder] Trying codec: av1_nvenc`, then `[Encoder] Using: <something>  ...  preset=P4`, then `[FTHR] Ready. Waiting for commands...`

Ctrl+C to stop.

- [ ] **Step 8: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHRcapture_linux/src/shared_memory.h FTHRcapture_linux/src/capture_engine.h FTHRcapture_linux/src/capture_engine.cpp FTHRcapture_linux/src/main.cpp
git commit -m "feat(engine): shared memory v2 with codec/preset fields, Reconfigure() handler"
```

---

## Task 3: Python CaptureBridge — layout v2 + new methods

**Files:**
- Modify: `FTHR_UI/core/capture_bridge.py`
- Create: `tests/test_capture_bridge.py`

- [ ] **Step 1: Write the failing tests first**

Create `tests/test_capture_bridge.py`:

```python
import ctypes
import sys
import pytest

# Tests run without a live engine — we build a fake shared memory layout in
# a bytearray and point the bridge at it.
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.capture_bridge import CaptureBridge, SharedMemoryLayout, CommandType


def _make_fake_layout():
    """Allocate a SharedMemoryLayout in a bytearray (no real shared memory)."""
    buf = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
    layout = SharedMemoryLayout.from_buffer(buf)
    layout.is_initialized = True
    return layout, buf   # keep buf alive


class _FakeBridge(CaptureBridge):
    """CaptureBridge subclass that uses an in-process fake layout."""
    def __init__(self, layout):
        self._layout = layout
        self._initialized = True


def test_set_encoder_config_writes_fields():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    result = bridge.set_encoder_config('hevc', 5)

    assert result is True
    assert layout.cfg_codec_pref == 2           # hevc = 2
    assert layout.cfg_preset     == 5
    assert layout.ui_command     == CommandType.RECONFIGURE_ENCODER


def test_set_encoder_config_auto_maps_to_zero():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    bridge.set_encoder_config('auto', 4)

    assert layout.cfg_codec_pref == 0


def test_get_active_codec_reads_string():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    layout.active_codec = b'hevc_nvenc'

    assert bridge.get_active_codec() == 'hevc_nvenc'


def test_get_active_codec_empty_when_not_set():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    assert bridge.get_active_codec() == ''


def test_set_encoder_config_clamps_preset():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    bridge.set_encoder_config('h264', 0)   # below min
    assert layout.cfg_preset == 1

    bridge.set_encoder_config('h264', 99)  # above max
    assert layout.cfg_preset == 7
```

- [ ] **Step 2: Run tests — expect failure (methods don't exist yet)**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_capture_bridge.py -v 2>&1 | tail -20
```

Expected: `AttributeError: 'CaptureBridge' object has no attribute 'set_encoder_config'`

- [ ] **Step 3: Update SharedMemoryLayout in capture_bridge.py (Linux branch)**

In the `else:` branch of `if sys.platform == 'win32':` (the `SharedMemoryLayout` class starting around line 83), add 3 new fields **after** `('nvenc_active', c_bool)`:

```python
    class SharedMemoryLayout(Structure):
        _fields_ = [
            ('ui_command',        c_uint32),
            ('ui_param1',         c_uint32),
            ('ui_param2',         c_uint32),
            ('ui_param3',         c_uint32),
            ('ui_string',         ctypes.c_char * 1024),
            ('engine_response',   c_uint32),
            ('engine_param1',     c_uint32),
            ('engine_param2',     c_uint32),
            ('engine_param3',     c_float),
            ('engine_string',     ctypes.c_char * 2048),
            ('is_recording',      c_bool),
            ('is_initialized',    c_bool),
            ('frames_captured',   c_uint64),
            ('bytes_written',     c_uint64),
            ('cfg_bitrate_kbps',  c_uint32),
            ('cfg_target_width',  c_uint32),
            ('cfg_target_height', c_uint32),
            ('nvenc_active',      c_bool),
            # v2 fields — must match shared_memory.h byte-for-byte
            ('cfg_codec_pref',    c_uint32),
            ('cfg_preset',        c_uint32),
            ('active_codec',      ctypes.c_char * 64),
        ]
```

- [ ] **Step 4: Update SHARED_MEM_NAME constant**

In `CaptureBridge` class, change:

```python
SHARED_MEM_NAME = 'FTHR_SharedMemory_v2'
```

- [ ] **Step 5: Add set_encoder_config() and get_active_codec() methods**

Add both methods to `CaptureBridge`, after `get_status()`:

```python
    _CODEC_PREF_MAP = {'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3}

    def set_encoder_config(self, codec_pref: str, preset: int) -> bool:
        if not self.is_connected():
            return False
        pref_int = self._CODEC_PREF_MAP.get(codec_pref.lower(), 0)
        preset   = max(1, min(7, preset))
        self._layout.cfg_codec_pref = pref_int
        self._layout.cfg_preset     = preset
        self._layout.ui_command     = CommandType.RECONFIGURE_ENCODER
        return True

    def get_active_codec(self) -> str:
        if not self.is_connected():
            return ''
        try:
            raw = self._layout.active_codec
            return raw.decode('utf-8', errors='ignore').rstrip('\x00')
        except Exception:
            return ''
```

- [ ] **Step 6: Run tests — expect pass**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_capture_bridge.py -v
```

Expected:
```
PASSED tests/test_capture_bridge.py::test_set_encoder_config_writes_fields
PASSED tests/test_capture_bridge.py::test_set_encoder_config_auto_maps_to_zero
PASSED tests/test_capture_bridge.py::test_get_active_codec_reads_string
PASSED tests/test_capture_bridge.py::test_get_active_codec_empty_when_not_set
PASSED tests/test_capture_bridge.py::test_set_encoder_config_clamps_preset
5 passed
```

- [ ] **Step 7: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/core/capture_bridge.py tests/test_capture_bridge.py
git commit -m "feat(bridge): shared memory v2 layout, set_encoder_config(), get_active_codec()"
```

---

## Task 4: Settings defaults

**Files:**
- Modify: `FTHR_UI/core/settings_manager.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capture_bridge.py` (or create `tests/test_settings_manager.py`):

```python
# tests/test_settings_manager.py
import sys
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.settings_manager import SettingsManager


def test_new_defaults_present(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4


def test_old_config_gets_new_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    import json, pathlib
    cfg_file = tmp_path / '.fthr' / 'settings.json'
    cfg_file.parent.mkdir(parents=True)
    with open(cfg_file, 'w') as f:
        json.dump({'clip_length': 60}, f)

    sm = SettingsManager()
    assert sm.get('codec_pref')     == 'auto'
    assert sm.get('encoder_preset') == 4
    assert sm.get('clip_length')    == 60  # existing value preserved
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_settings_manager.py -v 2>&1 | tail -10
```

Expected: `AssertionError` — key not found (returns None).

- [ ] **Step 3: Add defaults to settings_manager.py**

In `_load_settings`, in the `default_settings` dict, after `'upload_auto_delete': False,`, add:

```python
            # Encoder codec + preset (v2 settings)
            'codec_pref':     'auto',   # 'auto' | 'h264' | 'hevc' | 'av1'
            'encoder_preset': 4,        # 1–7
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/test_settings_manager.py -v
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/core/settings_manager.py tests/test_settings_manager.py
git commit -m "feat(settings): add codec_pref and encoder_preset defaults"
```

---

## Task 5: UI — Performance tab + MainWindow wiring

**Files:**
- Modify: `FTHR_UI/main.py`

- [ ] **Step 1: Replace the non-functional encoder_combo in _make_performance_page()**

Locate `_make_performance_page` at line 3365 in main.py. Replace the entire `enc_row` block and `enc_note` (lines 3375–3394) with:

```python
        # Codec row
        codec_row = QHBoxLayout()
        codec_row.setSpacing(10)
        codec_lbl = QLabel('Codec')
        codec_lbl.setFixedWidth(140)
        codec_lbl.setStyleSheet(_LABEL_STYLE)
        codec_row.addWidget(codec_lbl)
        self.codec_combo = _DropdownCombo()
        self.codec_combo.addItems(['Auto', 'H.264', 'HEVC', 'AV1'])
        self.codec_combo.setStyleSheet(_COMBO_STYLE)
        saved_codec = self.sm.get('codec_pref', 'auto')
        codec_idx = {'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3}.get(saved_codec, 0)
        self.codec_combo.setCurrentIndex(codec_idx)
        codec_row.addWidget(self.codec_combo, 1)
        layout.addLayout(codec_row)
        layout.addSpacing(8)

        # Preset row
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        preset_lbl = QLabel('Preset')
        preset_lbl.setFixedWidth(140)
        preset_lbl.setStyleSheet(_LABEL_STYLE)
        preset_row.addWidget(preset_lbl)
        self.preset_combo = _DropdownCombo()
        self.preset_combo.addItems([
            'P1 — Schnellst', 'P2', 'P3', 'P4 — Ausgeglichen', 'P5', 'P6',
            'P7 — Beste Qualität',
        ])
        self.preset_combo.setStyleSheet(_COMBO_STYLE)
        saved_preset = self.sm.get('encoder_preset', 4)
        self.preset_combo.setCurrentIndex(max(0, min(6, saved_preset - 1)))
        preset_row.addWidget(self.preset_combo, 1)
        layout.addLayout(preset_row)
        layout.addSpacing(8)

        # Active encoder label
        active_row = QHBoxLayout()
        active_row.setSpacing(10)
        active_lbl = QLabel('Aktiver Encoder')
        active_lbl.setFixedWidth(140)
        active_lbl.setStyleSheet(_LABEL_STYLE)
        active_row.addWidget(active_lbl)
        self.active_encoder_lbl = QLabel('—')
        self.active_encoder_lbl.setStyleSheet(
            label_body(Colors.ACCENT, Fonts.SIZE_BODY))
        active_row.addWidget(self.active_encoder_lbl, 1)
        layout.addLayout(active_row)
        layout.addSpacing(8)

        # Apply button (hidden until user changes a value)
        self.encoder_apply_btn = QPushButton('ÜBERNEHMEN + NEUSTART')
        self.encoder_apply_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.encoder_apply_btn.setVisible(False)
        self.encoder_apply_btn.clicked.connect(self._on_encoder_apply)
        layout.addWidget(self.encoder_apply_btn)

        self.codec_combo.currentIndexChanged.connect(self._on_encoder_setting_changed)
        self.preset_combo.currentIndexChanged.connect(self._on_encoder_setting_changed)

        enc_note = QLabel('Änderungen werden nach dem Neustart aktiv.')
        enc_note.setStyleSheet(label_body(Colors.TEXT_DIM,
            Fonts.SIZE_SMALL if hasattr(Fonts, 'SIZE_SMALL') else Fonts.SIZE_BODY))
        layout.addWidget(enc_note)
```

- [ ] **Step 2: Add _on_encoder_setting_changed and _on_encoder_apply to _SettingsPage**

After `_make_performance_page` closing `return page`, before the next method, add:

```python
    def _on_encoder_setting_changed(self, _idx: int):
        self.encoder_apply_btn.setVisible(True)

Add a new signal to `_SettingsPage`. Find the `_SettingsPage` class definition (line 2536). Add after `close_requested`:

```python
    encoder_config_changed = pyqtSignal()   # new: tells MainWindow to send RECONFIGURE_ENCODER
```

Then write `_on_encoder_apply` to use it:

```python
    def _on_encoder_apply(self):
        codec_map = {0: 'auto', 1: 'h264', 2: 'hevc', 3: 'av1'}
        codec  = codec_map.get(self.codec_combo.currentIndex(), 'auto')
        preset = self.preset_combo.currentIndex() + 1

        self.sm.set('codec_pref',     codec)
        self.sm.set('encoder_preset', preset)
        self.sm.save_settings()
        self.encoder_apply_btn.setVisible(False)
        self.encoder_config_changed.emit()
```

- [ ] **Step 3: Wire encoder_config_changed in MainWindow**

In `MainWindow.__init__`, after `self._settings_page_widget.imported_folders_changed.connect(...)`, add:

```python
        self._settings_page_widget.encoder_config_changed.connect(
            self._on_encoder_config_changed)
```

Add the handler method after `_restart_capture_engine` (around line 1881):

```python
    def _on_encoder_config_changed(self):
        codec  = self.settings_manager.get('codec_pref',     'auto')
        preset = self.settings_manager.get('encoder_preset', 4)
        if self.bridge.is_connected():
            self.bridge.set_encoder_config(codec, preset)
            self._set_status('NEUSTART', STATUS_IDLE)
        else:
            # Engine not running — settings will be picked up on next start via argv
            pass
```

- [ ] **Step 4: Update start_engine() to pass argv[11] and argv[12]**

In `start_engine()`, find the `subprocess.Popen` call (line 1845). The args list currently ends with `capture_monitor`. Replace that call:

```python
            codec_pref_int = {
                'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3
            }.get(self.settings_manager.get('codec_pref', 'auto'), 0)
            encoder_preset = self.settings_manager.get('encoder_preset', 4)

            self.engine_process = subprocess.Popen(
                [str(self.engine_path),
                 str(self.capture_fps), str(self.buffer_seconds),
                 str(self.capture_width), str(self.capture_height),
                 str(self.capture_bitrate), str(max_buffer_mb),
                 mode_arg, hwnd_arg, scale_arg, capture_monitor,
                 str(codec_pref_int), str(encoder_preset)],
                **_NO_WINDOW
            )
```

- [ ] **Step 5: Update _update_status() to populate active_encoder_lbl**

Find `_update_status` in `MainWindow`. After `status = self.bridge.get_status()` and inside the `if status.get('connected'):` branch, add:

```python
            codec = self.bridge.get_active_codec()
            preset = self.settings_manager.get('encoder_preset', 4)
            if codec:
                enc_label = f'{codec} — P{preset}'
                try:
                    self._settings_page_widget.active_encoder_lbl.setText(enc_label)
                except AttributeError:
                    pass
```

- [ ] **Step 6: Rebuild C++ engine (it needs the v2 shared memory name to match Python)**

```bash
cd /home/tom/FTHR_Clips/FTHRcapture_linux/build && make -j$(nproc) 2>&1 | tail -5
```

Expected: `make: Nothing to be done` or a quick rebuild.

- [ ] **Step 7: Launch the full app and verify the Performance tab**

```bash
cd /home/tom/FTHR_Clips/FTHR_UI && python main.py
```

Manual checks:
1. App launches and connects to engine (status shows CAPTURING)
2. Open Settings → Performance tab
3. Codec dropdown shows Auto/H.264/HEVC/AV1
4. Preset dropdown shows P1–P7
5. Active Encoder label shows e.g. `h264_nvenc — P4` within ~1s of connecting
6. Change Codec to "HEVC", click ÜBERNEHMEN + NEUSTART
7. Engine briefly restarts, Active Encoder updates to e.g. `hevc_nvenc — P4`

- [ ] **Step 8: Commit**

```bash
cd /home/tom/FTHR_Clips
git add FTHR_UI/main.py
git commit -m "feat(ui): Performance tab codec/preset/active-encoder controls + MainWindow wiring"
```

---

## Final check

Run the full Python test suite:

```bash
cd /home/tom/FTHR_Clips && python -m pytest tests/ -v
```

Expected: all tests pass.
