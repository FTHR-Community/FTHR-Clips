#pragma once
#include "encoder.h"
#include "ring_buffer.h"
#include "audio_capture.h"
#include "audio_multi_capture.h"
#include "shared_memory.h"
#include <string>
#include <thread>
#include <atomic>
#include <mutex>
#include <cstdint>

namespace fthr {

struct CaptureConfig {
    uint32_t    fps;
    uint32_t    buffer_seconds;
    uint32_t    target_width;    // 0 = native
    uint32_t    target_height;   // 0 = native
    uint32_t    bitrate_kbps;
    uint32_t    scaling_mode;    // 0 = stretch, 1 = fit (letterbox)
    std::string target_output;   // wl_output name, e.g. "HDMI-A-1" — empty = first
    bool      multiband_enabled = false;
    bool      audio_enabled    = true;
    std::vector<AudioCategoryConfig> audio_categories;
    CodecPref codec_pref = CodecPref::Auto;
    int       preset     = 4;
};

class CaptureEngine {
public:
    CaptureEngine() = default;
    ~CaptureEngine() { Shutdown(); }

    bool Initialize(const CaptureConfig& cfg);
    void Shutdown();

    // Captures a clip of duration_sec seconds and writes it to path.
    // Updates shm status fields during save. Blocking call.
    bool SaveClip(const std::string& path, uint32_t duration_sec,
                  SharedMemoryLayout* shm);

    bool     IsNvencActive() const { return nvenc_active_.load(); }
    void SetPaused(bool p) { paused_.store(p); }
    bool IsPaused()  const { return paused_.load(); }
    uint64_t GetFrameCount()  const { return frame_count_.load(); }
    void Reconfigure(uint32_t codec_pref, int preset);
    std::string GetActiveCodec() const {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        return active_codec_;
    }
    std::string GetAudioMappingsJson() const;

private:
    void CaptureLoop();

    // Wayland state (managed entirely within CaptureLoop)
    struct WaylandState;

    CaptureConfig           cfg_{};
    Encoder                 encoder_;
    EncodedRingBuffer*      ring_     = nullptr;
    AudioCapture            audio_;
    std::thread             cap_thread_;
    std::atomic<bool>       running_{false};
    std::atomic<bool>       paused_{false};
    std::atomic<bool>       nvenc_active_{false};
    std::atomic<uint64_t>   frame_count_{0};
    std::string active_codec_;
    mutable std::mutex codec_mutex_;
    AudioMultiCapture   multi_audio_;
};

} // namespace fthr
