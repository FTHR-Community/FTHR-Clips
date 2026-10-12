#include "backend_ext.h"
#include <iostream>
#include <cstring>
#include <algorithm>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <time.h>
extern "C" {
#include <libavutil/pixfmt.h>
}

namespace fthr {

// wl_output listener
static void _eo_geom(void*, wl_output*, int32_t, int32_t, int32_t,
                     int32_t, int32_t, const char*, const char*, int32_t) {}
static void _eo_mode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
void ExtBackend::OutputDone(void* d, wl_output*) {
    static_cast<ExtBackend::OutputEntry*>(d)->done = true;
}
static void _eo_scale(void*, wl_output*, int32_t) {}
void ExtBackend::OutputName(void* d, wl_output*, const char* n) {
    if (n) static_cast<ExtBackend::OutputEntry*>(d)->name = n;
}
static void _eo_desc(void*, wl_output*, const char*) {}
static const wl_output_listener kExtOutListener = {
    _eo_geom, _eo_mode, ExtBackend::OutputDone,
    _eo_scale, ExtBackend::OutputName, _eo_desc,
};

// wl_registry listener
void ExtBackend::RegistryGlobal(void* d, wl_registry* reg,
                                 uint32_t name, const char* iface, uint32_t ver) {
    auto* b = static_cast<ExtBackend*>(d);
    if (strcmp(iface, wl_shm_interface.name) == 0) {
        b->shm_ = static_cast<wl_shm*>(
            wl_registry_bind(reg, name, &wl_shm_interface, 1));
    } else if (strcmp(iface, wl_output_interface.name) == 0) {
        auto* e = new OutputEntry{};
        e->handle = static_cast<wl_output*>(
            wl_registry_bind(reg, name, &wl_output_interface, std::min(ver, 4u)));
        b->all_outputs_.push_back(e);
        wl_output_add_listener(e->handle, &kExtOutListener, e);
    } else if (strcmp(iface, ext_image_copy_capture_manager_v1_interface.name) == 0) {
        b->mgr_ = static_cast<ext_image_copy_capture_manager_v1*>(
            wl_registry_bind(reg, name, &ext_image_copy_capture_manager_v1_interface, 1));
    } else if (strcmp(iface, ext_output_image_capture_source_manager_v1_interface.name) == 0) {
        b->src_mgr_ = static_cast<ext_output_image_capture_source_manager_v1*>(
            wl_registry_bind(reg, name, &ext_output_image_capture_source_manager_v1_interface, 1));
    }
}
void ExtBackend::RegistryRemove(void*, wl_registry*, uint32_t) {}

// session listener
void ExtBackend::BeginConstraints() {
    if (constraints_open_) return;
    constraints_open_ = true;
    offered_formats_.clear();
}
void ExtBackend::SessionBufferSize(void* d,
        ext_image_copy_capture_session_v1*, uint32_t w, uint32_t h) {
    auto* b = static_cast<ExtBackend*>(d);
    b->BeginConstraints();
    b->buf_width_ = w; b->buf_height_ = h;
    b->native_w_  = w; b->native_h_   = h;
}
void ExtBackend::SessionShmFormat(void* d,
        ext_image_copy_capture_session_v1*, uint32_t fmt) {
    auto* b = static_cast<ExtBackend*>(d);
    b->BeginConstraints();
    b->offered_formats_.push_back(fmt);
}
void ExtBackend::SessionDmabufDevice(void*, ext_image_copy_capture_session_v1*,
        struct wl_array*) {}
void ExtBackend::SessionDmabufFormat(void*, ext_image_copy_capture_session_v1*,
        uint32_t, struct wl_array*) {}
void ExtBackend::SessionDone(void* d, ext_image_copy_capture_session_v1*) {
    auto* b = static_cast<ExtBackend*>(d);
    uint32_t chosen = 0;
    b->shm_format_ = PickWlShmFormat(b->offered_formats_, chosen) ? chosen : 0;
    b->constraints_open_ = false;
    b->constraints_serial_++;
    b->buf_done_ = true;
}
void ExtBackend::SessionStopped(void* d, ext_image_copy_capture_session_v1*) {
    static_cast<ExtBackend*>(d)->session_stopped_ = true;
}
static const ext_image_copy_capture_session_v1_listener kSessionListener = {
    ExtBackend::SessionBufferSize,
    ExtBackend::SessionShmFormat,
    ExtBackend::SessionDmabufDevice,
    ExtBackend::SessionDmabufFormat,
    ExtBackend::SessionDone,
    ExtBackend::SessionStopped,
};

// frame listener
void ExtBackend::FrameTransform(void*, ext_image_copy_capture_frame_v1*, uint32_t) {}
void ExtBackend::FrameDamage(void*, ext_image_copy_capture_frame_v1*,
                              int32_t, int32_t, int32_t, int32_t) {}
void ExtBackend::FramePresentationTime(void*, ext_image_copy_capture_frame_v1*,
                                       uint32_t, uint32_t, uint32_t) {}
void ExtBackend::FrameReady(void* d, ext_image_copy_capture_frame_v1*) {
    static_cast<ExtBackend*>(d)->frame_ready_ = true;
}
void ExtBackend::FrameFailed(void* d, ext_image_copy_capture_frame_v1*,
                              uint32_t reason) {
    auto* b = static_cast<ExtBackend*>(d);
    b->frame_failed_ = true;
    b->frame_failure_reason_ = reason;
}
static const ext_image_copy_capture_frame_v1_listener kFrameListener = {
    ExtBackend::FrameTransform,
    ExtBackend::FrameDamage,
    ExtBackend::FramePresentationTime,
    ExtBackend::FrameReady,
    ExtBackend::FrameFailed,
};

// buffer management
bool ExtBackend::AllocShmBuffer() {
    shm_size_ = static_cast<size_t>(buf_width_) * buf_height_ * 4;
    shm_fd_ = memfd_create("fthr_ext_frame", MFD_CLOEXEC);
    if (shm_fd_ < 0) return false;
    if (ftruncate(shm_fd_, static_cast<off_t>(shm_size_)) < 0) {
        close(shm_fd_); shm_fd_ = -1; return false;
    }
    shm_data_ = mmap(nullptr, shm_size_, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
    if (shm_data_ == MAP_FAILED) {
        close(shm_fd_); shm_fd_ = -1; shm_data_ = nullptr; return false;
    }
    shm_pool_ = wl_shm_create_pool(shm_, shm_fd_, static_cast<int32_t>(shm_size_));
    wl_buf_ = wl_shm_pool_create_buffer(shm_pool_, 0,
        static_cast<int32_t>(buf_width_), static_cast<int32_t>(buf_height_),
        static_cast<int32_t>(buf_width_ * 4), shm_format_);
    if (!wl_buf_) return false;
    alloc_width_  = buf_width_;
    alloc_height_ = buf_height_;
    alloc_format_ = shm_format_;
    return true;
}

bool ExtBackend::EnsureShmBuffer() {
    if (shm_format_ == 0 || buf_width_ == 0 || buf_height_ == 0) {
        std::cerr << "[ExtBackend] Compositor offered no usable shm buffer\n";
        return false;
    }
    if (wl_buf_ && alloc_width_ == buf_width_ && alloc_height_ == buf_height_ &&
            alloc_format_ == shm_format_)
        return true;
    FreeShmBuffer();
    return AllocShmBuffer();
}

void ExtBackend::FreeShmBuffer() {
    if (wl_buf_)   { wl_buffer_destroy(wl_buf_);     wl_buf_   = nullptr; }
    if (shm_pool_) { wl_shm_pool_destroy(shm_pool_); shm_pool_ = nullptr; }
    if (shm_data_ && shm_data_ != MAP_FAILED) { munmap(shm_data_, shm_size_); shm_data_ = nullptr; }
    if (shm_fd_ >= 0) { close(shm_fd_); shm_fd_ = -1; }
    alloc_width_ = alloc_height_ = alloc_format_ = 0;
}

void ExtBackend::DestroyWayland() {
    if (pending_frame_) {
        ext_image_copy_capture_frame_v1_destroy(pending_frame_);
        pending_frame_ = nullptr;
    }
    FreeShmBuffer();
    if (session_) { ext_image_copy_capture_session_v1_destroy(session_); session_ = nullptr; }
    if (source_)  { ext_image_capture_source_v1_destroy(source_);        source_  = nullptr; }
    if (mgr_)     { ext_image_copy_capture_manager_v1_destroy(mgr_);     mgr_     = nullptr; }
    if (src_mgr_) { ext_output_image_capture_source_manager_v1_destroy(src_mgr_); src_mgr_ = nullptr; }
    for (auto* e : all_outputs_) { wl_output_destroy(e->handle); delete e; }
    all_outputs_.clear();
    if (shm_)     { wl_shm_destroy(shm_);           shm_      = nullptr; }
    if (registry_){ wl_registry_destroy(registry_);  registry_ = nullptr; }
    if (display_) { wl_display_disconnect(display_); display_  = nullptr; }
    output_ = nullptr;
}

bool ExtBackend::KeepRunning() const noexcept {
    return !running_ || running_->load();
}

WaylandWaitResult ExtBackend::WaitUntilResult(WaylandDeadline deadline,
                                              const WaylandPredicate& complete) {
    return DispatchWaylandUntil(
        display_,
        deadline,
        [this] { return KeepRunning(); },
        complete);
}

bool ExtBackend::WaitUntil(WaylandDeadline deadline,
                           const WaylandPredicate& complete,
                           const char* operation) {
    const auto result = WaitUntilResult(deadline, complete);
    if (result == WaylandWaitResult::EventReceived) return true;
    std::cerr << "[ExtBackend] Wayland " << operation << " "
              << WaylandWaitResultName(result) << std::endl;
    return false;
}

bool ExtBackend::Roundtrip(WaylandDeadline deadline, const char* operation) {
    const auto result = BoundedWaylandRoundtrip(
        display_, deadline, [this] { return KeepRunning(); });
    if (result == WaylandWaitResult::EventReceived) return true;
    std::cerr << "[ExtBackend] Wayland " << operation << " "
              << WaylandWaitResultName(result) << std::endl;
    return false;
}

// ICaptureBackend impl
bool ExtBackend::Initialize(const CaptureConfig& cfg) {
    target_output_ = cfg.target_output;
    frame_interval_ = std::chrono::nanoseconds(
        1'000'000'000LL / std::max<uint32_t>(1, cfg.fps));
    const auto deadline = std::chrono::steady_clock::now() + kInitializationTimeout;
    init_failure_kind_ = BackendFailureKind::Error;
    display_ = wl_display_connect(nullptr);
    if (!display_) {
        init_failure_kind_ = BackendFailureKind::CompositorUnreachable;
        return false;
    }

    static const wl_registry_listener kRegListener = { RegistryGlobal, RegistryRemove };
    registry_ = wl_display_get_registry(display_);
    wl_registry_add_listener(registry_, &kRegListener, this);
    if (!Roundtrip(deadline, "registry discovery") ||
            !Roundtrip(deadline, "output discovery")) {
        init_failure_kind_ = BackendFailureKind::CompositorUnreachable;
        Shutdown();
        return false;
    }

    if (!mgr_ || !src_mgr_) {
        std::cerr << "[ExtBackend] ext-image-copy-capture not available" << std::endl;
        DestroyWayland(); return false;
    }

    OutputEntry* sel = nullptr;
    if (!target_output_.empty())
        for (auto* e : all_outputs_)
            if (e->name == target_output_) { sel = e; break; }
    if (!sel && !target_output_.empty() && strict_output_) {
        std::cerr << "[ExtBackend] Output '" << target_output_
                  << "' not found; waiting for it" << std::endl;
        init_failure_kind_ = BackendFailureKind::OutputAbsent;
        Shutdown();
        return false;
    }
    if (!sel && !all_outputs_.empty()) sel = all_outputs_[0];
    if (!sel) { std::cerr << "[ExtBackend] No wl_output\n"; DestroyWayland(); return false; }
    output_ = sel->handle;
    selected_output_ = sel->name;
    if (!WaitUntil(
            deadline,
            [sel] { return sel->done; },
            "output discovery")) {
        Shutdown();
        return false;
    }
    std::cerr << "[ExtBackend] Using output: "
              << (sel->name.empty() ? "(unnamed)" : sel->name) << std::endl;

    source_  = ext_output_image_capture_source_manager_v1_create_source(src_mgr_, output_);
    if (!source_) {
        std::cerr << "[ExtBackend] Failed to create capture source\n";
        Shutdown();
        return false;
    }
    session_ = ext_image_copy_capture_manager_v1_create_session(mgr_, source_, 0);
    if (!session_) {
        std::cerr << "[ExtBackend] Failed to create capture session\n";
        Shutdown();
        return false;
    }
    ext_image_copy_capture_session_v1_add_listener(session_, &kSessionListener, this);

    if (!WaitUntil(
            deadline,
            [this] { return buf_done_ || session_stopped_; },
            "session negotiation")) {
        Shutdown();
        return false;
    }
    if (session_stopped_ || buf_width_ == 0 || shm_format_ == 0) {
        std::cerr << "[ExtBackend] Session failed to negotiate buffer\n";
        DestroyWayland(); return false;
    }
    if (!AllocShmBuffer()) {
        std::cerr << "[ExtBackend] Failed to alloc shm buffer\n";
        DestroyWayland(); return false;
    }
    std::cerr << "[ExtBackend] Ready: " << native_w_ << "x" << native_h_ << std::endl;
    return true;
}

WaylandWaitResult ExtBackend::CaptureOnceResult(WaylandDeadline deadline) {
    if (!pending_frame_) {
        if (!EnsureShmBuffer()) return WaylandWaitResult::Error;
        frame_ready_ = frame_failed_ = false;
        frame_failure_reason_ = 0;
        frame_constraints_serial_ = constraints_serial_;

        pending_frame_ = ext_image_copy_capture_session_v1_create_frame(session_);
        if (!pending_frame_) {
            std::cerr << "[ExtBackend] Failed to create frame request\n";
            return WaylandWaitResult::Error;
        }
        ext_image_copy_capture_frame_v1_add_listener(pending_frame_, &kFrameListener, this);
        ext_image_copy_capture_frame_v1_attach_buffer(pending_frame_, wl_buf_);
        ext_image_copy_capture_frame_v1_damage_buffer(pending_frame_, 0, 0,
            static_cast<int32_t>(buf_width_), static_cast<int32_t>(buf_height_));
        ext_image_copy_capture_frame_v1_capture(pending_frame_);
    }

    const auto result = WaitUntilResult(
        deadline,
        [this] { return frame_ready_ || frame_failed_ || session_stopped_; });
    // Only a terminal frame event ends the request. After a timeout it stays
    // pending and the next call resumes waiting on it; after a disconnect or
    // error Shutdown() destroys it.
    if (result == WaylandWaitResult::EventReceived) {
        ext_image_copy_capture_frame_v1_destroy(pending_frame_);
        pending_frame_ = nullptr;
    }
    return result;
}

WaylandDeadline ExtBackend::FrameDeadline() const {
    const auto now = std::chrono::steady_clock::now();
    if (last_frame_.empty()) return now + kFrameTimeout;
    // Anchored on the previous delivery, like the portal backend, so a static
    // desktop still yields frames at the target rate.
    return std::max(
        last_delivery_ +
            std::chrono::duration_cast<std::chrono::steady_clock::duration>(frame_interval_),
        now + kMinFrameWait);
}

bool ExtBackend::CaptureFrame(RawFrame& out) {
    if (session_stopped_) return false;

    WaylandWaitResult result = CaptureOnceResult(FrameDeadline());
    if (result == WaylandWaitResult::EventReceived && frame_failed_ &&
            !session_stopped_ &&
            frame_failure_reason_ ==
                EXT_IMAGE_COPY_CAPTURE_FRAME_V1_FAILURE_REASON_BUFFER_CONSTRAINTS) {
        // A resolution or format change fails the in-flight frame with
        // buffer_constraints and re-sends the constraints. Reallocate once
        // against the new batch instead of restarting the whole generation.
        // The constraints wait gets the full timeout: the frame-rate deadline
        // is far too short for a compositor to re-send them. The baseline is
        // the serial at request creation: the new batch may have been
        // dispatched during an earlier call's wait, before `failed` arrived.
        const uint32_t serial_before = frame_constraints_serial_;
        if (!WaitUntil(
                std::chrono::steady_clock::now() + kFrameTimeout,
                [this, serial_before] {
                    return constraints_serial_ != serial_before || session_stopped_;
                },
                "buffer constraints update"))
            return false;
        if (session_stopped_) return false;
        result = CaptureOnceResult(FrameDeadline());
    }

    if (result == WaylandWaitResult::EventReceived && frame_ready_ &&
            !session_stopped_) {
        // Keep an owned copy: the wl_shm buffer is reused for the next
        // request, and a repeated frame must not alias a buffer the
        // compositor may be writing. Sized from the allocation, not buf_*:
        // a resize batch dispatched in the same wait as `ready` has already
        // overwritten those, and the mapping still has the old size.
        const size_t bytes = static_cast<size_t>(alloc_width_) * 4 * alloc_height_;
        const auto* src = static_cast<const uint8_t*>(shm_data_);
        last_frame_.assign(src, src + bytes);
        last_w_ = alloc_width_;
        last_h_ = alloc_height_;
        last_format_ = alloc_format_;
    } else if (ShouldRepeatLastFrame(result, !last_frame_.empty())) {
        // The protocol lets the compositor hold the request until the screen
        // changes; a static desktop must still fill the replay buffer.
    } else {
        if (result != WaylandWaitResult::EventReceived)
            std::cerr << "[ExtBackend] Wayland frame request "
                      << WaylandWaitResultName(result) << std::endl;
        else if (session_stopped_)
            std::cerr << "[ExtBackend] Capture session stopped" << std::endl;
        else if (frame_failed_)
            std::cerr << "[ExtBackend] Frame request failed, reason "
                      << frame_failure_reason_ << std::endl;
        return false;
    }

    last_delivery_ = std::chrono::steady_clock::now();
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    const int64_t now_ns =
        static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
    return normalizer_.Normalize(
        last_frame_.data(), last_w_ * 4,
        last_w_, last_h_, last_format_, false, now_ns, out);
}

void ExtBackend::Shutdown() {
    DestroyWayland();
    normalizer_.Reset();
    // A later Initialize may pick another output; never repeat this one.
    last_frame_.clear();
    last_w_ = last_h_ = last_format_ = 0;
}

} // namespace fthr
