#include "backend_wlr.h"
#include <iostream>
#include <cstring>
#include <algorithm>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <time.h>
extern "C" {
#include <libavutil/pixfmt.h>
}

namespace fthr {

// wl_output listener helpers (file-scope, not class members)

static void output_geometry(void*, wl_output*, int32_t, int32_t, int32_t, int32_t,
                             int32_t, const char*, const char*, int32_t) {}
static void output_mode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
static void output_done(void* data, wl_output*) {
    static_cast<WlrBackend::OutputEntry*>(data)->done = true;
}
static void output_scale(void*, wl_output*, int32_t) {}
static void output_name(void* data, wl_output*, const char* name) {
    if (name) static_cast<WlrBackend::OutputEntry*>(data)->name = name;
}
static void output_description(void*, wl_output*, const char*) {}

static const wl_output_listener kOutputListener = {
    output_geometry,
    output_mode,
    output_done,
    output_scale,
    output_name,
    output_description,
};

// Registry callbacks

void WlrBackend::RegistryGlobal(void* data, wl_registry* registry,
                                 uint32_t name, const char* interface,
                                 uint32_t version) {
    auto* self = static_cast<WlrBackend*>(data);
    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        self->compositor_ = static_cast<wl_compositor*>(
            wl_registry_bind(registry, name, &wl_compositor_interface,
                             std::min(version, 4u)));
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        self->shm_ = static_cast<wl_shm*>(
            wl_registry_bind(registry, name, &wl_shm_interface, 1));
    } else if (strcmp(interface, wl_output_interface.name) == 0) {
        auto* entry = new OutputEntry{};
        entry->handle = static_cast<wl_output*>(
            wl_registry_bind(registry, name, &wl_output_interface,
                             std::min(version, 4u)));
        self->all_outputs_.push_back(entry);
        wl_output_add_listener(entry->handle, &kOutputListener, entry);
    } else if (strcmp(interface, zwlr_screencopy_manager_v1_interface.name) == 0) {
        self->sc_mgr_ = static_cast<zwlr_screencopy_manager_v1*>(
            wl_registry_bind(registry, name,
                             &zwlr_screencopy_manager_v1_interface,
                             std::min(version, 3u)));
    } else if (strcmp(interface, zxdg_output_manager_v1_interface.name) == 0) {
        self->xdg_mgr_ = static_cast<zxdg_output_manager_v1*>(
            wl_registry_bind(registry, name,
                             &zxdg_output_manager_v1_interface,
                             std::min(version, 3u)));
    }
}

void WlrBackend::RegistryGlobalRemove(void*, wl_registry*, uint32_t) {}

static const wl_registry_listener kRegistryListener = {
    WlrBackend::RegistryGlobal,
    WlrBackend::RegistryGlobalRemove,
};

// Screencopy frame callbacks

void WlrBackend::ScFrameBuffer(void* data, zwlr_screencopy_frame_v1* /*frame*/,
                                uint32_t format, uint32_t width, uint32_t height,
                                uint32_t stride) {
    auto* self = static_cast<WlrBackend*>(data);
    self->fb_.format  = format;
    self->fb_.width   = width;
    self->fb_.height  = height;
    self->fb_.stride  = stride;
    self->native_w_   = width;
    self->native_h_   = height;
}

void WlrBackend::ScFrameFlags(void* data, zwlr_screencopy_frame_v1* /*frame*/,
                               uint32_t flags) {
    static_cast<WlrBackend*>(data)->y_invert_ =
        (flags & ZWLR_SCREENCOPY_FRAME_V1_FLAGS_Y_INVERT) != 0;
}

void WlrBackend::ScFrameReady(void* data, zwlr_screencopy_frame_v1* /*frame*/,
                               uint32_t /*tv_sec_hi*/, uint32_t /*tv_sec_lo*/,
                               uint32_t /*tv_nsec*/) {
    static_cast<WlrBackend*>(data)->frame_ready_ = true;
}

void WlrBackend::ScFrameFailed(void* data, zwlr_screencopy_frame_v1* /*frame*/) {
    static_cast<WlrBackend*>(data)->frame_failed_ = true;
}

static void sc_frame_damage(void* /*data*/, zwlr_screencopy_frame_v1* /*frame*/,
                             uint32_t /*x*/, uint32_t /*y*/,
                             uint32_t /*w*/, uint32_t /*h*/) {}

static void sc_frame_linux_dmabuf(void* /*data*/,
                                   zwlr_screencopy_frame_v1* /*frame*/,
                                   uint32_t /*format*/,
                                   uint32_t /*width*/, uint32_t /*height*/) {}

void WlrBackend::ScFrameBufferDone(void* data, zwlr_screencopy_frame_v1* /*frame*/) {
    static_cast<WlrBackend*>(data)->buffer_done_ = true;
}

static const zwlr_screencopy_frame_v1_listener kScFrameListener = {
    WlrBackend::ScFrameBuffer,
    WlrBackend::ScFrameFlags,
    WlrBackend::ScFrameReady,
    WlrBackend::ScFrameFailed,
    sc_frame_damage,
    sc_frame_linux_dmabuf,
    WlrBackend::ScFrameBufferDone,
};

// FrameBuffer helpers

bool WlrBackend::AllocFramebuffer() {
    fb_.size = static_cast<size_t>(fb_.stride) * fb_.height;

    fb_.fd = memfd_create("fthr_frame", MFD_CLOEXEC);
    if (fb_.fd < 0) {
        std::cerr << "[WlrBackend] memfd_create failed" << std::endl;
        return false;
    }
    if (ftruncate(fb_.fd, static_cast<off_t>(fb_.size)) < 0) {
        close(fb_.fd); fb_.fd = -1;
        return false;
    }
    fb_.data = mmap(nullptr, fb_.size, PROT_READ | PROT_WRITE,
                    MAP_SHARED, fb_.fd, 0);
    if (fb_.data == MAP_FAILED) {
        close(fb_.fd); fb_.fd = -1;
        fb_.data = nullptr;
        return false;
    }

    fb_.pool   = wl_shm_create_pool(shm_, fb_.fd,
                                     static_cast<int32_t>(fb_.size));
    fb_.buffer = wl_shm_pool_create_buffer(
        fb_.pool, 0,
        static_cast<int32_t>(fb_.width),
        static_cast<int32_t>(fb_.height),
        static_cast<int32_t>(fb_.stride),
        fb_.format);
    alloc_width_  = fb_.width;
    alloc_height_ = fb_.height;
    alloc_stride_ = fb_.stride;
    alloc_format_ = fb_.format;
    return true;
}

void WlrBackend::FreeFramebuffer() {
    if (fb_.buffer) { wl_buffer_destroy(fb_.buffer); fb_.buffer = nullptr; }
    if (fb_.pool)   { wl_shm_pool_destroy(fb_.pool);  fb_.pool   = nullptr; }
    if (fb_.data && fb_.data != MAP_FAILED) {
        munmap(fb_.data, fb_.size);
        fb_.data = nullptr;
    }
    if (fb_.fd >= 0) { close(fb_.fd); fb_.fd = -1; }
    alloc_width_ = alloc_height_ = alloc_stride_ = alloc_format_ = 0;
}

void WlrBackend::DestroyPendingFrame() {
    if (sc_frame_) {
        zwlr_screencopy_frame_v1_destroy(sc_frame_);
        sc_frame_ = nullptr;
    }
}

void WlrBackend::DestroyWayland() {
    DestroyPendingFrame();
    if (sc_mgr_)    { zwlr_screencopy_manager_v1_destroy(sc_mgr_);  sc_mgr_    = nullptr; }
    if (xdg_mgr_)   { zxdg_output_manager_v1_destroy(xdg_mgr_);     xdg_mgr_   = nullptr; }
    for (auto* e : all_outputs_) { wl_output_destroy(e->handle); delete e; }
    all_outputs_.clear();
    if (shm_)       { wl_shm_destroy(shm_);           shm_       = nullptr; }
    if (compositor_){ wl_compositor_destroy(compositor_); compositor_ = nullptr; }
    if (registry_)  { wl_registry_destroy(registry_); registry_  = nullptr; }
    if (display_)   { wl_display_disconnect(display_); display_   = nullptr; }
    output_ = nullptr;
}

bool WlrBackend::KeepRunning() const noexcept {
    return !running_ || running_->load();
}

bool WlrBackend::WaitUntil(WaylandDeadline deadline,
                           const WaylandPredicate& complete,
                           const char* operation) {
    const auto result = DispatchWaylandUntil(
        display_,
        deadline,
        [this] { return KeepRunning(); },
        complete);
    if (result == WaylandWaitResult::EventReceived) return true;
    std::cerr << "[WlrBackend] Wayland " << operation << " "
              << WaylandWaitResultName(result) << std::endl;
    return false;
}

bool WlrBackend::Roundtrip(WaylandDeadline deadline, const char* operation) {
    const auto result = BoundedWaylandRoundtrip(
        display_, deadline, [this] { return KeepRunning(); });
    if (result == WaylandWaitResult::EventReceived) return true;
    std::cerr << "[WlrBackend] Wayland " << operation << " "
              << WaylandWaitResultName(result) << std::endl;
    return false;
}


bool WlrBackend::Initialize(const CaptureConfig& cfg) {
    target_output_ = cfg.target_output;
    const auto deadline = std::chrono::steady_clock::now() + kInitializationTimeout;

    display_ = wl_display_connect(nullptr);
    if (!display_) {
        std::cerr << "[WlrBackend] wl_display_connect failed — WAYLAND_DISPLAY set?" << std::endl;
        return false;
    }

    registry_ = wl_display_get_registry(display_);
    wl_registry_add_listener(registry_, &kRegistryListener, this);
    if (!Roundtrip(deadline, "registry discovery") ||
            !Roundtrip(deadline, "output discovery")) {
        Shutdown();
        return false;
    }

    // Select the target wl_output by name; fall back to first available.
    {
        OutputEntry* selected = nullptr;
        if (!target_output_.empty()) {
            for (auto* e : all_outputs_) {
                if (e->name == target_output_) { selected = e; break; }
            }
            if (!selected)
                std::cerr << "[WlrBackend] Output '" << target_output_
                          << "' not found — falling back to first" << std::endl;
        }
        if (!selected && !all_outputs_.empty())
            selected = all_outputs_[0];

        if (selected) {
            output_ = selected->handle;
            std::cerr << "[WlrBackend] Using output: '"
                      << (selected->name.empty() ? "(unnamed)" : selected->name)
                      << "'" << std::endl;
        }
    }

    // Wait for wl_output to be fully committed before using it.
    {
        OutputEntry* selected = nullptr;
        for (auto* e : all_outputs_) {
            if (e->handle == output_) { selected = e; break; }
        }
        if (selected && !WaitUntil(
                deadline,
                [selected] { return selected->done; },
                "output discovery")) {
            Shutdown();
            return false;
        }
    }

    if (!sc_mgr_) {
        std::cerr << "[WlrBackend] zwlr_screencopy_manager_v1 not available — "
                     "compositor must support wlr-screencopy" << std::endl;
        Shutdown();
        return false;
    }
    if (!output_) {
        std::cerr << "[WlrBackend] No wl_output found" << std::endl;
        Shutdown();
        return false;
    }

    // Probe native resolution via one throw-away frame
    {
        frame_ready_  = false;
        frame_failed_ = false;
        buffer_done_  = false;
        fb_ = FrameBuffer{};
        fb_.fd = -1;

        sc_frame_ = zwlr_screencopy_manager_v1_capture_output(sc_mgr_, 0, output_);
        if (!sc_frame_) {
            std::cerr << "[WlrBackend] Failed to create resolution probe" << std::endl;
            Shutdown();
            return false;
        }
        zwlr_screencopy_frame_v1_add_listener(sc_frame_, &kScFrameListener, this);
        if (!WaitUntil(
                deadline,
                [this] { return buffer_done_ || frame_failed_; },
                "resolution probe buffer")) {
            Shutdown();
            return false;
        }

        if (frame_failed_ || fb_.width == 0) {
            std::cerr << "[WlrBackend] Failed to probe output resolution" << std::endl;
            Shutdown();
            return false;
        }
        if (WlShmFormatToAvPixFmt(fb_.format) < 0) {
            std::cerr << "[WlrBackend] Unsupported wl_shm format 0x" << std::hex
                      << fb_.format << std::dec << std::endl;
            Shutdown();
            return false;
        }

        if (!AllocFramebuffer()) {
            std::cerr << "[WlrBackend] Failed to allocate probe framebuffer" << std::endl;
            Shutdown();
            return false;
        }

        zwlr_screencopy_frame_v1_copy(sc_frame_, fb_.buffer);
        if (!WaitUntil(
                deadline,
                [this] { return frame_ready_ || frame_failed_; },
                "resolution probe frame")) {
            Shutdown();
            return false;
        }

        DestroyPendingFrame();
        if (frame_failed_) {
            std::cerr << "[WlrBackend] Resolution probe frame failed" << std::endl;
            Shutdown();
            return false;
        }
    }

    std::cout << "[WlrBackend] Output resolution: " << native_w_ << "x" << native_h_ << std::endl;
    return true;
}


bool WlrBackend::CaptureFrame(RawFrame& out) {
    const auto deadline = std::chrono::steady_clock::now() + kFrameTimeout;
    frame_ready_  = false;
    frame_failed_ = false;
    buffer_done_  = false;
    y_invert_     = false;

    sc_frame_ = zwlr_screencopy_manager_v1_capture_output(sc_mgr_, 0, output_);
    if (!sc_frame_) {
        std::cerr << "[WlrBackend] Failed to create frame request" << std::endl;
        return false;
    }
    zwlr_screencopy_frame_v1_add_listener(sc_frame_, &kScFrameListener, this);

    if (!WaitUntil(
            deadline,
            [this] { return buffer_done_ || frame_failed_; },
            "frame buffer request")) {
        DestroyPendingFrame();
        return false;
    }

    if (frame_failed_) {
        DestroyPendingFrame();
        return false;
    }

    if (WlShmFormatToAvPixFmt(fb_.format) < 0) {
        std::cerr << "[WlrBackend] Unsupported wl_shm format 0x" << std::hex
                  << fb_.format << std::dec << std::endl;
        DestroyPendingFrame();
        return false;
    }

    // Reallocate when the output changed size, stride or format (mode switch,
    // scale change, HDR/10-bit toggle). The encoder rescales to its fixed size.
    if (!fb_.buffer || fb_.width != alloc_width_ || fb_.height != alloc_height_ ||
            fb_.stride != alloc_stride_ || fb_.format != alloc_format_) {
        FreeFramebuffer();
        if (!AllocFramebuffer()) {
            DestroyPendingFrame();
            return false;
        }
    }

    zwlr_screencopy_frame_v1_copy(sc_frame_, fb_.buffer);

    const bool completed = WaitUntil(
        deadline,
        [this] { return frame_ready_ || frame_failed_; },
        "frame request");

    DestroyPendingFrame();

    if (!completed || frame_failed_)
        return false;

    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    int64_t now_ns = static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;

    return normalizer_.Normalize(
        static_cast<const uint8_t*>(fb_.data), fb_.stride,
        fb_.width, fb_.height, fb_.format, y_invert_, now_ns, out);
}


void WlrBackend::Shutdown() {
    DestroyPendingFrame();
    FreeFramebuffer();
    normalizer_.Reset();
    DestroyWayland();
}

} // namespace fthr
