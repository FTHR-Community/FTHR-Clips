#pragma once
#include "capture_backend.h"
#include "shm_frame.h"
#include "wayland_dispatch.h"
#include <atomic>
#include <chrono>
#include <string>
#include <vector>
#include <wayland-client.h>
#include "wlr-screencopy-client-protocol.h"
#include "xdg-output-client-protocol.h"

namespace fthr {

class WlrBackend final : public ICaptureBackend {
public:
    explicit WlrBackend(const std::atomic<bool>* running) : running_(running) {}
    ~WlrBackend() override { Shutdown(); }

    bool Initialize(const CaptureConfig& cfg) override;
    bool CaptureFrame(RawFrame& out) override;
    void Shutdown() override;

    BackendType Type() const override { return BackendType::WlrScreencopy; }
    uint32_t NativeWidth()  const override { return native_w_; }
    uint32_t NativeHeight() const override { return native_h_; }

    struct OutputEntry {
        wl_output*  handle = nullptr;
        std::string name;
        bool        done   = false;
    };

private:
    inline static constexpr auto kInitializationTimeout = std::chrono::seconds(5);
    inline static constexpr auto kFrameTimeout = std::chrono::seconds(2);

    const std::atomic<bool>*       running_    = nullptr;
    wl_display*                 display_    = nullptr;
    wl_registry*                registry_   = nullptr;
    wl_compositor*              compositor_ = nullptr;
    wl_shm*                     shm_        = nullptr;
    wl_output*                  output_     = nullptr;
    zwlr_screencopy_manager_v1* sc_mgr_     = nullptr;
    zxdg_output_manager_v1*     xdg_mgr_    = nullptr;
    std::vector<OutputEntry*>   all_outputs_;
    std::string                 target_output_;

    struct FrameBuffer {
        void*        data   = nullptr;
        size_t       size   = 0;
        wl_buffer*   buffer = nullptr;
        wl_shm_pool* pool   = nullptr;
        int          fd     = -1;
        uint32_t     width  = 0;
        uint32_t     height = 0;
        uint32_t     stride = 0;
        uint32_t     format = 0;
    } fb_;
    // Parameters the current wl_buffer was created with. The buffer event
    // overwrites fb_ for every frame, so these detect a size or format change.
    uint32_t alloc_width_  = 0;
    uint32_t alloc_height_ = 0;
    uint32_t alloc_stride_ = 0;
    uint32_t alloc_format_ = 0;
    bool     y_invert_     = false;
    ShmFrameNormalizer normalizer_;

    zwlr_screencopy_frame_v1* sc_frame_    = nullptr;
    bool frame_ready_  = false;
    bool frame_failed_ = false;
    bool buffer_done_  = false;

    uint32_t native_w_ = 0;
    uint32_t native_h_ = 0;

    bool AllocFramebuffer();
    void FreeFramebuffer();
    void DestroyPendingFrame();
    void DestroyWayland();
    bool KeepRunning() const noexcept;
    bool WaitUntil(WaylandDeadline deadline,
                   const WaylandPredicate& complete,
                   const char* operation);
    bool Roundtrip(WaylandDeadline deadline, const char* operation);

public:
    static void RegistryGlobal(void*, wl_registry*, uint32_t, const char*, uint32_t);
    static void RegistryGlobalRemove(void*, wl_registry*, uint32_t);
    static void ScFrameBuffer(void*, zwlr_screencopy_frame_v1*,
                               uint32_t, uint32_t, uint32_t, uint32_t);
    static void ScFrameFlags(void*, zwlr_screencopy_frame_v1*, uint32_t);
    static void ScFrameReady(void*, zwlr_screencopy_frame_v1*,
                              uint32_t, uint32_t, uint32_t);
    static void ScFrameFailed(void*, zwlr_screencopy_frame_v1*);
    static void ScFrameBufferDone(void*, zwlr_screencopy_frame_v1*);
};

} // namespace fthr
