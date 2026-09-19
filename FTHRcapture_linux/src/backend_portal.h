#pragma once
// Capture through the XDG ScreenCast portal and PipeWire. Used when the
// compositor exports neither wlr-screencopy nor ext-image-copy-capture (KWin
// does not). The desktop shows its own source picker the first time; a
// persisted restore token makes later starts silent.
//
// libpipewire-0.3 and libdbus-1 are dlopen()ed on first use. When either is
// missing, Initialize() fails with an explanation and the engine keeps
// working with the other backends.

#include "capture_backend.h"
#include "pipewire_frame.h"
#include "portal_screencast.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <pipewire/pipewire.h>

namespace fthr {

struct PipeWireApi;

class PortalBackend final : public ICaptureBackend {
public:
    explicit PortalBackend(const std::atomic<bool>* running) : running_(running) {}
    ~PortalBackend() override { Shutdown(); }

    bool Initialize(const CaptureConfig& cfg) override;
    bool CaptureFrame(RawFrame& out) override;
    void Shutdown() override;

    BackendType Type() const override { return BackendType::PortalScreenCast; }
    uint32_t NativeWidth()  const override { return native_w_; }
    uint32_t NativeHeight() const override { return native_h_; }

    // Why Initialize() failed, in words meant for the UI. Empty on success.
    const std::string& FailureReason() const noexcept { return failure_reason_; }
    // True when the user dismissed or ignored the portal dialog: the capture
    // engine's automatic retries would only show the dialog again.
    bool RetryIsPointless() const noexcept { return retry_pointless_; }

    // Public for the file-scope PipeWire event tables.
    void OnStreamStateChanged(pw_stream_state old_state, pw_stream_state state,
                              const char* error);
    void OnStreamParamChanged(uint32_t id, const spa_pod* param);
    void OnStreamProcess();
    void OnCoreError(uint32_t id, int seq, int res, const char* message);

private:
    // The stream must reach a negotiated format before the encoder can be
    // sized; a compositor that never answers is a failed attempt, not a hang.
    inline static constexpr auto kStreamTimeout = std::chrono::seconds(10);
    // KWin sends the first frame right after the stream starts; a portal that
    // grants a session without frames is treated as failed.
    inline static constexpr auto kFirstFrameTimeout = std::chrono::seconds(5);
    inline static constexpr auto kWaitSlice = std::chrono::milliseconds(50);

    bool KeepRunning() const noexcept;
    bool OpenPortalSession(const CaptureConfig& cfg);
    bool ConnectStream(int pipewire_fd, uint32_t node_id, uint32_t fps);
    void DestroyStream();
    void Fail(const std::string& reason);

    const std::atomic<bool>* running_ = nullptr;
    const PipeWireApi* pw_ = nullptr;
    DBusApi dbus_{};
    std::unique_ptr<PortalScreenCastSession> session_;
    std::string restore_token_path_;

    pw_thread_loop* loop_ = nullptr;
    pw_context* context_ = nullptr;
    pw_core* core_ = nullptr;
    pw_stream* stream_ = nullptr;
    spa_hook stream_listener_{};
    spa_hook core_listener_{};

    // Frame hand-off between the PipeWire loop thread (producer) and the
    // capture thread (consumer). pending_ is written under frame_mutex_;
    // front_ belongs to the capture thread after the swap in CaptureFrame().
    std::mutex frame_mutex_;
    std::condition_variable frame_cv_;
    std::vector<uint8_t> pending_;
    std::vector<uint8_t> front_;
    uint64_t frame_seq_ = 0;
    uint64_t consumed_seq_ = 0;
    int64_t pending_pts_ns_ = 0;
    bool have_format_ = false;
    bool have_front_ = false;
    bool failed_ = false;
    bool shutting_down_ = false;
    std::string stream_error_;

    PixelLayout layout_{};
    uint32_t native_w_ = 0;
    uint32_t native_h_ = 0;
    std::chrono::nanoseconds frame_interval_{16'666'667};
    std::chrono::steady_clock::time_point last_delivery_{};
    uint32_t poll_counter_ = 0;

    std::string failure_reason_;
    bool retry_pointless_ = false;
};

} // namespace fthr
