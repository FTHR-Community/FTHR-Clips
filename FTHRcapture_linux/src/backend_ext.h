#pragma once
#include "capture_backend.h"
#include "shm_frame.h"
#include "wayland_dispatch.h"
#include <atomic>
#include <chrono>
#include <wayland-client.h>
#include "ext-image-copy-capture-client-protocol.h"
#include "ext-image-capture-source-client-protocol.h"
#include <vector>
#include <string>

namespace fthr {

class ExtBackend final : public ICaptureBackend {
public:
    explicit ExtBackend(const std::atomic<bool>* running) : running_(running) {}
    ~ExtBackend() override { Shutdown(); }

    bool Initialize(const CaptureConfig& cfg) override;
    bool CaptureFrame(RawFrame& out) override;
    void Shutdown() override;

    BackendType Type() const override { return BackendType::ExtImageCopy; }
    uint32_t NativeWidth()  const override { return native_w_; }
    uint32_t NativeHeight() const override { return native_h_; }

    struct OutputEntry {
        wl_output*  handle = nullptr;
        std::string name;
        bool        done   = false;
    };

    // Public for file-scope Wayland listener structs
    static void RegistryGlobal(void*, wl_registry*, uint32_t, const char*, uint32_t);
    static void RegistryRemove(void*, wl_registry*, uint32_t);
    static void OutputDone(void*, wl_output*);
    static void OutputName(void*, wl_output*, const char*);
    static void SessionBufferSize(void*, ext_image_copy_capture_session_v1*, uint32_t, uint32_t);
    static void SessionShmFormat(void*, ext_image_copy_capture_session_v1*, uint32_t);
    static void SessionDmabufDevice(void*, ext_image_copy_capture_session_v1*, struct wl_array*);
    static void SessionDmabufFormat(void*, ext_image_copy_capture_session_v1*, uint32_t, struct wl_array*);
    static void SessionDone(void*, ext_image_copy_capture_session_v1*);
    static void SessionStopped(void*, ext_image_copy_capture_session_v1*);
    static void FrameReady(void*, ext_image_copy_capture_frame_v1*);
    static void FrameFailed(void*, ext_image_copy_capture_frame_v1*, uint32_t);
    static void FrameTransform(void*, ext_image_copy_capture_frame_v1*, uint32_t);
    static void FrameDamage(void*, ext_image_copy_capture_frame_v1*, int32_t, int32_t, int32_t, int32_t);
    static void FramePresentationTime(void*, ext_image_copy_capture_frame_v1*, uint32_t, uint32_t, uint32_t);

private:
    inline static constexpr auto kInitializationTimeout = std::chrono::seconds(5);
    inline static constexpr auto kFrameTimeout = std::chrono::seconds(2);

    const std::atomic<bool>* running_ = nullptr;
    wl_display*    display_    = nullptr;
    wl_registry*   registry_   = nullptr;
    wl_shm*        shm_        = nullptr;
    wl_output*     output_     = nullptr;
    ext_image_copy_capture_manager_v1*          mgr_     = nullptr;
    ext_output_image_capture_source_manager_v1* src_mgr_ = nullptr;
    std::vector<OutputEntry*> all_outputs_;
    std::string target_output_;

    ext_image_capture_source_v1*               source_  = nullptr;
    ext_image_copy_capture_session_v1*         session_ = nullptr;

    uint32_t buf_width_  = 0;
    uint32_t buf_height_ = 0;
    uint32_t shm_format_ = 0;
    // Constraints arrive as a batch closed by `done` and are re-sent in full
    // whenever they change; offered formats are collected per batch.
    std::vector<uint32_t> offered_formats_;
    bool     constraints_open_ = false;
    uint32_t constraints_serial_ = 0;   // bumped on every `done`
    bool     buf_done_        = false;
    uint32_t alloc_width_  = 0;
    uint32_t alloc_height_ = 0;
    uint32_t alloc_format_ = 0;
    uint32_t frame_failure_reason_ = 0;
    ShmFrameNormalizer normalizer_;
    bool     session_stopped_ = false;

    void*        shm_data_ = nullptr;
    size_t       shm_size_ = 0;
    int          shm_fd_   = -1;
    wl_shm_pool* shm_pool_ = nullptr;
    wl_buffer*   wl_buf_   = nullptr;

    bool frame_ready_  = false;
    bool frame_failed_ = false;

    uint32_t native_w_ = 0;
    uint32_t native_h_ = 0;

    void BeginConstraints();
    bool EnsureShmBuffer();
    bool CaptureOnce(std::chrono::steady_clock::time_point deadline);
    bool AllocShmBuffer();
    void FreeShmBuffer();
    void DestroyWayland();
    bool KeepRunning() const noexcept;
    bool WaitUntil(WaylandDeadline deadline,
                   const WaylandPredicate& complete,
                   const char* operation);
    bool Roundtrip(WaylandDeadline deadline, const char* operation);
};

} // namespace fthr
