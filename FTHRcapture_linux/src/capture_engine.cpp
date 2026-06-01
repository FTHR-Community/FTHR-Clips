#include "capture_engine.h"
#include <iostream>
#include <cstring>
#include <cstdlib>
#include <cassert>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include <fcntl.h>

// Wayland / wlr-screencopy headers
#include <wayland-client.h>
#include "wlr-screencopy-client-protocol.h"
#include "xdg-output-client-protocol.h"

namespace fthr {

// ---------------------------------------------------------------------------
// Wayland state container
// ---------------------------------------------------------------------------

struct FrameBuffer {
    void*        data    = nullptr;
    size_t       size    = 0;
    wl_buffer*   buffer  = nullptr;
    wl_shm_pool* pool    = nullptr;
    int          fd      = -1;
    uint32_t     width   = 0;
    uint32_t     height  = 0;
    uint32_t     stride  = 0;
    uint32_t     format  = 0;   // WL_SHM_FORMAT_*
};

// Per-output tracking — each wl_output gets its own entry as listener user-data.
struct OutputEntry {
    wl_output*   handle = nullptr;
    std::string  name;
    bool         done   = false;
};

struct WaylandCtx {
    // Globals
    wl_display*                  display   = nullptr;
    wl_registry*                 registry  = nullptr;
    wl_compositor*               compositor= nullptr;
    wl_shm*                      shm       = nullptr;
    wl_output*                   output    = nullptr;  // selected after roundtrip
    bool                         output_done = false;
    zwlr_screencopy_manager_v1*  sc_mgr    = nullptr;
    zxdg_output_manager_v1*      xdg_out_mgr = nullptr;

    // All discovered outputs; target_output is the requested name (empty = first)
    std::vector<OutputEntry*>    all_outputs;
    std::string                  target_output;

    // Frame state (reset each capture round)
    zwlr_screencopy_frame_v1*    sc_frame  = nullptr;
    FrameBuffer                  fb;
    bool                         frame_ready  = false;
    bool                         frame_failed = false;
    bool                         buffer_done  = false;

    // Output geometry (filled from buffer event)
    uint32_t native_width  = 0;
    uint32_t native_height = 0;
};

// ---------------------------------------------------------------------------
// Wayland registry listener
// ---------------------------------------------------------------------------

// Forward declaration: kOutputListener is defined after kRegistryListener,
// but registry_global needs to reference it when binding wl_output.
extern const wl_output_listener kOutputListener;

static void registry_global(void* data, wl_registry* registry,
                             uint32_t name, const char* interface,
                             uint32_t version) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        ctx->compositor = static_cast<wl_compositor*>(
            wl_registry_bind(registry, name, &wl_compositor_interface,
                             std::min(version, 4u)));
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        ctx->shm = static_cast<wl_shm*>(
            wl_registry_bind(registry, name, &wl_shm_interface, 1));
    } else if (strcmp(interface, wl_output_interface.name) == 0) {
        auto* entry = new OutputEntry{};
        entry->handle = static_cast<wl_output*>(
            wl_registry_bind(registry, name, &wl_output_interface,
                             std::min(version, 4u)));
        ctx->all_outputs.push_back(entry);
        wl_output_add_listener(entry->handle, &kOutputListener, entry);
    } else if (strcmp(interface, zwlr_screencopy_manager_v1_interface.name) == 0) {
        ctx->sc_mgr = static_cast<zwlr_screencopy_manager_v1*>(
            wl_registry_bind(registry, name,
                             &zwlr_screencopy_manager_v1_interface,
                             std::min(version, 3u)));
    } else if (strcmp(interface, zxdg_output_manager_v1_interface.name) == 0) {
        ctx->xdg_out_mgr = static_cast<zxdg_output_manager_v1*>(
            wl_registry_bind(registry, name,
                             &zxdg_output_manager_v1_interface,
                             std::min(version, 3u)));
    }
}

static void registry_global_remove(void*, wl_registry*, uint32_t) {}

static const wl_registry_listener kRegistryListener = {
    registry_global,
    registry_global_remove,
};

// ---------------------------------------------------------------------------
// wl_output listener — we only need the 'done' event
// ---------------------------------------------------------------------------

static void output_geometry(void*, wl_output*, int32_t, int32_t, int32_t, int32_t,
                             int32_t, const char*, const char*, int32_t) {}
static void output_mode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
static void output_done(void* data, wl_output*) {
    static_cast<OutputEntry*>(data)->done = true;
}
static void output_scale(void*, wl_output*, int32_t) {}
static void output_name(void* data, wl_output*, const char* name) {
    if (name) static_cast<OutputEntry*>(data)->name = name;
}
static void output_description(void*, wl_output*, const char*) {}

const wl_output_listener kOutputListener = {
    output_geometry,
    output_mode,
    output_done,
    output_scale,
    output_name,
    output_description,
};

// ---------------------------------------------------------------------------
// Screencopy frame listeners
// ---------------------------------------------------------------------------

static void sc_frame_buffer(void* data, zwlr_screencopy_frame_v1* /*frame*/,
                              uint32_t format, uint32_t width, uint32_t height,
                              uint32_t stride) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    ctx->fb.format  = format;
    ctx->fb.width   = width;
    ctx->fb.height  = height;
    ctx->fb.stride  = stride;
    ctx->native_width  = width;
    ctx->native_height = height;
}

static void sc_frame_flags(void* /*data*/, zwlr_screencopy_frame_v1* /*frame*/,
                            uint32_t /*flags*/) {}

static void sc_frame_ready(void* data, zwlr_screencopy_frame_v1* /*frame*/,
                            uint32_t /*tv_sec_hi*/, uint32_t /*tv_sec_lo*/,
                            uint32_t /*tv_nsec*/) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    ctx->frame_ready = true;
}

static void sc_frame_failed(void* data, zwlr_screencopy_frame_v1* /*frame*/) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    ctx->frame_failed = true;
}

static void sc_frame_damage(void* /*data*/, zwlr_screencopy_frame_v1* /*frame*/,
                             uint32_t /*x*/, uint32_t /*y*/,
                             uint32_t /*w*/, uint32_t /*h*/) {}

static void sc_frame_linux_dmabuf(void* /*data*/,
                                   zwlr_screencopy_frame_v1* /*frame*/,
                                   uint32_t /*format*/,
                                   uint32_t /*width*/, uint32_t /*height*/) {}

static void sc_frame_buffer_done(void* data, zwlr_screencopy_frame_v1* /*frame*/) {
    auto* ctx = static_cast<WaylandCtx*>(data);
    ctx->buffer_done = true;
}

static const zwlr_screencopy_frame_v1_listener kScFrameListener = {
    sc_frame_buffer,
    sc_frame_flags,
    sc_frame_ready,
    sc_frame_failed,
    sc_frame_damage,
    sc_frame_linux_dmabuf,
    sc_frame_buffer_done,
};

// ---------------------------------------------------------------------------
// FrameBuffer helpers
// ---------------------------------------------------------------------------

static bool alloc_framebuffer(WaylandCtx* ctx) {
    FrameBuffer& fb = ctx->fb;
    fb.size = static_cast<size_t>(fb.stride) * fb.height;

    // Create anonymous shared memory
    fb.fd = memfd_create("fthr_frame", MFD_CLOEXEC);
    if (fb.fd < 0) {
        std::cerr << "[Capture] memfd_create failed" << std::endl;
        return false;
    }
    if (ftruncate(fb.fd, static_cast<off_t>(fb.size)) < 0) {
        close(fb.fd); fb.fd = -1;
        return false;
    }
    fb.data = mmap(nullptr, fb.size, PROT_READ | PROT_WRITE,
                   MAP_SHARED, fb.fd, 0);
    if (fb.data == MAP_FAILED) {
        close(fb.fd); fb.fd = -1;
        fb.data = nullptr;
        return false;
    }

    fb.pool   = wl_shm_create_pool(ctx->shm, fb.fd,
                                    static_cast<int32_t>(fb.size));
    fb.buffer = wl_shm_pool_create_buffer(
        fb.pool, 0,
        static_cast<int32_t>(fb.width),
        static_cast<int32_t>(fb.height),
        static_cast<int32_t>(fb.stride),
        fb.format);
    return true;
}

static void free_framebuffer(FrameBuffer& fb) {
    if (fb.buffer) { wl_buffer_destroy(fb.buffer); fb.buffer = nullptr; }
    if (fb.pool)   { wl_shm_pool_destroy(fb.pool);  fb.pool   = nullptr; }
    if (fb.data && fb.data != MAP_FAILED) {
        munmap(fb.data, fb.size);
        fb.data = nullptr;
    }
    if (fb.fd >= 0) { close(fb.fd); fb.fd = -1; }
}

// ---------------------------------------------------------------------------
// CaptureEngine::Initialize
// ---------------------------------------------------------------------------

bool CaptureEngine::Initialize(const CaptureConfig& cfg) {
    cfg_ = cfg;

    // Allocate ring buffer (buffer_seconds + small margin)
    size_t ring_ms = (static_cast<size_t>(cfg.buffer_seconds) + 5) * 1000;
    ring_ = new EncodedRingBuffer(ring_ms);

    // Start audio capture (loopback via PulseAudio monitor)
    audio_.Start("");

    // Start capture loop thread
    running_.store(true);
    cap_thread_ = std::thread(&CaptureEngine::CaptureLoop, this);

    return true;
}

// ---------------------------------------------------------------------------
// CaptureEngine::Shutdown
// ---------------------------------------------------------------------------

void CaptureEngine::Shutdown() {
    running_.store(false);
    if (cap_thread_.joinable())
        cap_thread_.join();
    audio_.Stop();
    encoder_.Close();
    delete ring_;
    ring_ = nullptr;
}

// ---------------------------------------------------------------------------
// CaptureEngine::CaptureLoop — Wayland wlr-screencopy main loop
// ---------------------------------------------------------------------------

void CaptureEngine::CaptureLoop() {
    WaylandCtx ctx;

    ctx.display = wl_display_connect(nullptr);
    if (!ctx.display) {
        std::cerr << "[Capture] wl_display_connect failed — WAYLAND_DISPLAY set?" << std::endl;
        running_.store(false);
        return;
    }

    ctx.target_output = cfg_.target_output;

    ctx.registry = wl_display_get_registry(ctx.display);
    wl_registry_add_listener(ctx.registry, &kRegistryListener, &ctx);
    wl_display_roundtrip(ctx.display);  // discovers all globals + outputs
    wl_display_roundtrip(ctx.display);  // flushes output name/done events

    // Select the target wl_output by name; fall back to first available.
    {
        OutputEntry* selected = nullptr;
        if (!ctx.target_output.empty()) {
            for (auto* e : ctx.all_outputs) {
                if (e->name == ctx.target_output) { selected = e; break; }
            }
            if (!selected)
                std::cerr << "[Capture] Output '" << ctx.target_output
                          << "' not found — falling back to first" << std::endl;
        }
        if (!selected && !ctx.all_outputs.empty())
            selected = ctx.all_outputs[0];

        if (selected) {
            ctx.output      = selected->handle;
            ctx.output_done = selected->done;
            std::cerr << "[Capture] Using output: '"
                      << (selected->name.empty() ? "(unnamed)" : selected->name)
                      << "'" << std::endl;
        }
    }

    // Wait for wl_output to be fully committed before using it.
    while (ctx.output && !ctx.output_done)
        wl_display_dispatch(ctx.display);

    if (!ctx.sc_mgr) {
        std::cerr << "[Capture] zwlr_screencopy_manager_v1 not available — "
                     "compositor must support wlr-screencopy" << std::endl;
        if (ctx.sc_mgr)    zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
        for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; } ctx.all_outputs.clear();
        if (ctx.shm)       wl_shm_destroy(ctx.shm);
        if (ctx.compositor) wl_compositor_destroy(ctx.compositor);
        if (ctx.registry)  wl_registry_destroy(ctx.registry);
        wl_display_disconnect(ctx.display);
        running_.store(false);
        return;
    }
    if (!ctx.output) {
        std::cerr << "[Capture] No wl_output found" << std::endl;
        if (ctx.sc_mgr)    zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
        for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; } ctx.all_outputs.clear();
        if (ctx.shm)       wl_shm_destroy(ctx.shm);
        if (ctx.compositor) wl_compositor_destroy(ctx.compositor);
        if (ctx.registry)  wl_registry_destroy(ctx.registry);
        wl_display_disconnect(ctx.display);
        running_.store(false);
        return;
    }

    // ------------- Probe native resolution via one throw-away frame ----------
    {
        ctx.frame_ready  = false;
        ctx.frame_failed = false;
        ctx.buffer_done  = false;
        memset(&ctx.fb, 0, sizeof(ctx.fb));
        ctx.fb.fd = -1;

        ctx.sc_frame = zwlr_screencopy_manager_v1_capture_output(
            ctx.sc_mgr, 0, ctx.output);
        zwlr_screencopy_frame_v1_add_listener(ctx.sc_frame,
                                               &kScFrameListener, &ctx);
        while (!ctx.buffer_done && !ctx.frame_failed)
            wl_display_dispatch(ctx.display);

        if (ctx.frame_failed || ctx.fb.width == 0) {
            std::cerr << "[Capture] Failed to probe output resolution" << std::endl;
            zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
            if (ctx.sc_mgr)    zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
            for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; } ctx.all_outputs.clear();
            if (ctx.shm)       wl_shm_destroy(ctx.shm);
            if (ctx.compositor) wl_compositor_destroy(ctx.compositor);
            if (ctx.registry)  wl_registry_destroy(ctx.registry);
            wl_display_disconnect(ctx.display);
            running_.store(false);
            return;
        }

        if (!alloc_framebuffer(&ctx)) {
            std::cerr << "[Capture] Failed to allocate probe framebuffer" << std::endl;
            zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
            if (ctx.sc_mgr)    zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
            for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; } ctx.all_outputs.clear();
            if (ctx.shm)       wl_shm_destroy(ctx.shm);
            if (ctx.compositor) wl_compositor_destroy(ctx.compositor);
            if (ctx.registry)  wl_registry_destroy(ctx.registry);
            wl_display_disconnect(ctx.display);
            running_.store(false);
            return;
        }
        zwlr_screencopy_frame_v1_copy(ctx.sc_frame, ctx.fb.buffer);
        while (!ctx.frame_ready && !ctx.frame_failed)
            wl_display_dispatch(ctx.display);
        zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
        ctx.sc_frame = nullptr;
    }

    uint32_t native_w = ctx.native_width;
    uint32_t native_h = ctx.native_height;
    std::cout << "[Capture] Output resolution: " << native_w << "x" << native_h << std::endl;

    // Determine encode dimensions
    uint32_t enc_w = (cfg_.target_width  == 0) ? native_w : cfg_.target_width;
    uint32_t enc_h = (cfg_.target_height == 0) ? native_h : cfg_.target_height;

    // If fit mode, maintain aspect ratio
    if (cfg_.scaling_mode == 1 && (enc_w != native_w || enc_h != native_h)) {
        double ar = static_cast<double>(native_w) / native_h;
        uint32_t fit_h = static_cast<uint32_t>(enc_w / ar);
        if (fit_h <= enc_h) {
            enc_h = fit_h & ~1u;  // make even
        } else {
            enc_w = static_cast<uint32_t>(enc_h * ar) & ~1u;
        }
    }
    // Ensure dimensions are even (H.264 requirement)
    enc_w &= ~1u;
    enc_h &= ~1u;

    // Open encoder
    EncoderConfig enc_cfg;
    enc_cfg.src_width   = native_w;
    enc_cfg.src_height  = native_h;
    enc_cfg.enc_width   = enc_w;
    enc_cfg.enc_height  = enc_h;
    enc_cfg.fps         = cfg_.fps;
    enc_cfg.bitrate_kbps = cfg_.bitrate_kbps;
    enc_cfg.codec_pref  = cfg_.codec_pref;
    enc_cfg.preset      = cfg_.preset;

    std::string codec_used;
    if (!encoder_.Open(enc_cfg, codec_used)) {
        std::cerr << "[Capture] Encoder open failed" << std::endl;
        free_framebuffer(ctx.fb);
        if (ctx.sc_mgr)    zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
        for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; } ctx.all_outputs.clear();
        if (ctx.shm)       wl_shm_destroy(ctx.shm);
        if (ctx.compositor) wl_compositor_destroy(ctx.compositor);
        if (ctx.registry)  wl_registry_destroy(ctx.registry);
        wl_display_disconnect(ctx.display);
        running_.store(false);
        return;
    }

    nvenc_active_.store(codec_used.find("nvenc") != std::string::npos);
    { std::lock_guard<std::mutex> lk(codec_mutex_); active_codec_ = codec_used; }

    // Frame timing
    int64_t frame_ns = 1'000'000'000LL / cfg_.fps;
    int64_t next_encode_ns = 0;

    std::cout << "[Capture] Loop started: "
              << enc_w << "x" << enc_h
              << " @ " << cfg_.fps << "fps  codec=" << codec_used << std::endl;

    while (running_.load()) {
        // Request a new frame
        ctx.frame_ready  = false;
        ctx.frame_failed = false;
        ctx.buffer_done  = false;

        // Reuse framebuffer if dimensions match, else reallocate
        uint32_t prev_w = ctx.fb.width;
        uint32_t prev_h = ctx.fb.height;

        ctx.sc_frame = zwlr_screencopy_manager_v1_capture_output(
            ctx.sc_mgr, 0, ctx.output);
        zwlr_screencopy_frame_v1_add_listener(ctx.sc_frame,
                                               &kScFrameListener, &ctx);

        while (!ctx.buffer_done && !ctx.frame_failed && running_.load())
            wl_display_dispatch(ctx.display);

        if (ctx.frame_failed || !running_.load()) {
            if (ctx.sc_frame) {
                zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
                ctx.sc_frame = nullptr;
            }
            if (!running_.load()) break;
            continue;
        }

        // Reallocate framebuffer if size changed
        if (ctx.fb.width != prev_w || ctx.fb.height != prev_h ||
            !ctx.fb.buffer) {
            free_framebuffer(ctx.fb);
            if (!alloc_framebuffer(&ctx)) {
                zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
                ctx.sc_frame = nullptr;
                continue;
            }
        }

        zwlr_screencopy_frame_v1_copy(ctx.sc_frame, ctx.fb.buffer);

        while (!ctx.frame_ready && !ctx.frame_failed && running_.load())
            wl_display_dispatch(ctx.display);

        zwlr_screencopy_frame_v1_destroy(ctx.sc_frame);
        ctx.sc_frame = nullptr;

        if (ctx.frame_failed || !running_.load())
            continue;

        // Frame rate throttling: skip encode if we're ahead of schedule
        struct timespec ts_now;
        clock_gettime(CLOCK_MONOTONIC, &ts_now);
        int64_t now_ns = static_cast<int64_t>(ts_now.tv_sec) * 1'000'000'000LL
                       + ts_now.tv_nsec;

        if (next_encode_ns == 0) next_encode_ns = now_ns;
        if (now_ns < next_encode_ns) {
            // Too early — skip this frame
            continue;
        }
        next_encode_ns += frame_ns;

        // Encode frame (fb.data is XRGB/BGRA depending on compositor format)
        // Wayland typically returns WL_SHM_FORMAT_XRGB8888 or ARGB8888,
        // which maps to BGRA in FFmpeg layout on little-endian.
        const uint8_t* frame_data = static_cast<const uint8_t*>(ctx.fb.data);
        encoder_.EncodeFrame(frame_data, ctx.fb.stride, now_ns,
            [this](EncodedPacket pkt) {
                ring_->Push(std::move(pkt));
            });

        frame_count_.fetch_add(1);
    }

    free_framebuffer(ctx.fb);
    if (ctx.sc_mgr)  zwlr_screencopy_manager_v1_destroy(ctx.sc_mgr);
    for (auto* e : ctx.all_outputs) { wl_output_destroy(e->handle); delete e; }
    if (ctx.shm)     wl_shm_destroy(ctx.shm);
    if (ctx.registry)wl_registry_destroy(ctx.registry);
    wl_display_disconnect(ctx.display);

    std::cout << "[Capture] Loop exited. Frames captured: "
              << frame_count_.load() << std::endl;
}

// ---------------------------------------------------------------------------
// CaptureEngine::SaveClip
// ---------------------------------------------------------------------------

bool CaptureEngine::SaveClip(const std::string& path, uint32_t duration_sec,
                               SharedMemoryLayout* shm) {
    if (!ring_) return false;

    uint32_t duration_ms = duration_sec * 1000;
    auto video_packets   = ring_->TakeSnapshot(duration_ms);

    if (video_packets.empty()) {
        std::cerr << "[SaveClip] No video packets in buffer" << std::endl;
        return false;
    }

    // Get audio segment
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    int64_t now_ns = static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
    std::vector<float> audio_pcm = audio_.ExtractSegment(now_ns, duration_ms);

    // Delegate to save_clip module
    extern bool save_clip_to_file(
        const std::string& path,
        const std::vector<EncodedPacket>& video_packets,
        const std::vector<float>& audio_pcm,
        int audio_sample_rate,
        int audio_channels,
        const std::vector<uint8_t>& extradata,
        uint32_t fps,
        uint32_t width,
        uint32_t height,
        SharedMemoryLayout* shm
    );

    return save_clip_to_file(
        path,
        video_packets,
        audio_pcm,
        AudioCapture::kSampleRate,
        AudioCapture::kChannels,
        encoder_.GetExtradata(),
        cfg_.fps,
        encoder_.GetWidth(),
        encoder_.GetHeight(),
        shm
    );
}

// ---------------------------------------------------------------------------
// CaptureEngine::Reconfigure — hot-swap codec/preset without full reinit
// ---------------------------------------------------------------------------

void CaptureEngine::Reconfigure(uint32_t codec_pref, int preset) {
    Shutdown();   // stops thread, deletes ring_, stops audio
    cfg_.codec_pref = static_cast<CodecPref>(codec_pref);
    cfg_.preset     = preset;
    if (cfg_.preset < 1) cfg_.preset = 1;
    if (cfg_.preset > 7) cfg_.preset = 7;
    // Re-allocate ring (Shutdown() freed it)
    size_t ring_ms = (static_cast<size_t>(cfg_.buffer_seconds) + 5) * 1000;
    ring_ = new EncodedRingBuffer(ring_ms);
    // Restart audio (Shutdown() stopped it)
    audio_.Start("");
    // Reset stale state
    nvenc_active_.store(false);
    { std::lock_guard<std::mutex> lk(codec_mutex_); active_codec_.clear(); }
    // Restart capture thread
    running_.store(true);
    cap_thread_ = std::thread(&CaptureEngine::CaptureLoop, this);
}

} // namespace fthr
