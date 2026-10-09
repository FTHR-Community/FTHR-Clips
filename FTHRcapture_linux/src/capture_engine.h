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
#include <memory>
#include <cstdint>

namespace fthr {

// Forward declaration to avoid circular include with capture_backend.h
class ICaptureBackend;
struct RawFrame;

struct CaptureConfig {
    uint32_t    fps;
    uint32_t    buffer_seconds;
    uint32_t    target_width;    // 0 = native
    uint32_t    target_height;   // 0 = native
    uint32_t    bitrate_kbps;
    uint32_t    max_buffer_mb = 2048;
    uint32_t    scaling_mode;    // 0 = stretch, 1 = fit (letterbox)
    std::string target_output;   // wl_output name, e.g. "HDMI-A-1" — empty = first
    bool      multiband_enabled = false;
    // Default mode is combined. When enabled, the UI may expose separate
    // system/microphone tracks when the post-processing path has both inputs.
    bool      separate_audio_enabled = false;
    bool      audio_enabled    = true;
    std::vector<AudioCategoryConfig> audio_categories;
    CodecPref codec_pref = CodecPref::Auto;
    EncoderPref encoder_pref = EncoderPref::Auto;
    int       preset     = 4;
};

class CaptureEngine {
public:
    CaptureEngine();
    ~CaptureEngine();

    bool Initialize(const CaptureConfig& cfg);
    void Shutdown();

    // Captures a clip of duration_sec seconds and writes it to path.
    // Updates shm status fields during save. Blocking call.
    bool SaveClip(const std::string& path, uint32_t duration_sec,
                  SharedMemoryLayout* shm, std::string* error_message);

    bool     IsNvencActive()  const { return nvenc_active_.load(); }
    bool     IsCapturing()    const { return running_.load(); }
    void SetPaused(bool p) { paused_.store(p); }
    bool IsPaused()  const { return paused_.load(); }
    uint64_t GetFrameCount()  const { return frame_count_.load(); }
    uint32_t GetCaptureHealthFlags() const { return capture_health_flags_.load(); }
    uint32_t GetCaptureGeneration() const { return capture_generation_.load(); }
    uint32_t GetContentSampleSequence() const { return content_sample_sequence_.load(); }
    uint32_t GetContentSuspiciousStreak() const { return content_suspicious_streak_.load(); }
    float GetContentLumaMean() const { return content_luma_mean_.load(); }
    float GetContentLumaVariance() const { return content_luma_variance_.load(); }
    void Reconfigure(uint32_t codec_pref, int preset);
    std::string GetActiveCodec() const {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        return active_codec_;
    }
    // Why capture stopped for good, for the UI. Set before the health flags
    // publish CAPTURE_HEALTH_BACKEND_FAILED; empty while capture is alive.
    std::string GetCaptureFailureReason() const {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        return capture_failure_reason_;
    }
    std::string GetAudioMappingsJson() const;

private:
    enum class GenerationEnd {
        Stopped,          // running_ went false: normal shutdown
        Failed,           // backend or encoder failure worth a bounded retry
        FailedForGood,    // only user action can help (declined portal dialog)
    };

    void CaptureLoop();
    GenerationEnd RunCaptureGeneration();
    void SetCaptureFailureReason(const std::string& reason);
    void SampleContent(const RawFrame& frame, uint64_t produced_frame);

    CaptureConfig                      cfg_{};
    std::unique_ptr<ICaptureBackend>   backend_;
    Encoder                 encoder_;
    EncodedRingBuffer*      ring_     = nullptr;
    AudioCapture            audio_;
    std::thread             cap_thread_;
    std::atomic<bool>       running_{false};
    std::atomic<bool>       paused_{false};
    std::atomic<bool>       nvenc_active_{false};
    std::atomic<uint64_t>   frame_count_{0};
    std::atomic<uint32_t>   capture_health_flags_{CAPTURE_HEALTH_NONE};
    std::atomic<uint32_t>   capture_generation_{0};
    std::atomic<uint32_t>   content_sample_sequence_{0};
    std::atomic<uint32_t>   content_suspicious_streak_{0};
    std::atomic<float>      content_luma_mean_{0.0f};
    std::atomic<float>      content_luma_variance_{0.0f};
    std::string active_codec_;
    std::string capture_failure_reason_;   // guarded by codec_mutex_
    mutable std::mutex codec_mutex_;
    AudioMultiCapture   multi_audio_;
};

} // namespace fthr
