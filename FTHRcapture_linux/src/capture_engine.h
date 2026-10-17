#pragma once
#include "encoder.h"
#include "ring_buffer.h"
#include "audio_capture.h"
#include "audio_multi_capture.h"
#include "shared_memory.h"
#include "capture_backend_policy.h"
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

// What SaveClip needs from the encoder, copied at open time so the save
// thread never reads codec state the capture thread may be tearing down.
struct EncoderSnapshot {
    std::vector<uint8_t> extradata;
    uint32_t  width = 0;
    uint32_t  height = 0;
    AVCodecID codec_id = AV_CODEC_ID_NONE;
    uint32_t  generation = 0;
    bool      valid = false;
};

struct CaptureConfig {
    uint32_t    fps;
    uint32_t    buffer_seconds;
    uint32_t    target_width;    // 0 = native
    uint32_t    target_height;   // 0 = native
    uint32_t    bitrate_kbps;
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
    // PulseAudio sink whose monitor is captured; empty follows the default
    // output (argv[24]).
    std::string playback_sink;
    // Upper bound on the encoded video ring, 0 = time-bounded only (argv[6]).
    uint64_t  max_buffer_bytes = 0;
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

    // True while a compressed replay encoder is open (any backend, software
    // included); published as SharedMemoryLayout::nvenc_active.
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
    // What the UI should know about capture trouble: while recovering, what
    // is being waited for; after a terminal failure, why. Set before the
    // health flags publish RECOVERING or BACKEND_FAILED; empty while healthy.
    std::string GetCaptureFailureReason() const {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        return capture_failure_reason_;
    }
    std::string GetAudioMappingsJson() const;
    EncoderSnapshot TakeEncoderSnapshot() const {
        std::lock_guard<std::mutex> lk(codec_mutex_);
        return encoder_snapshot_;
    }

private:
    enum class GenerationEnd {
        Stopped,          // running_ went false: normal shutdown
        Failed,           // backend failure worth a bounded retry
        FailedEncoder,    // encoder stopped accepting frames: reopen it
        FailedForGood,    // only user action can help (declined portal dialog)
    };

    void CaptureLoop();
    // transient_out is set when the generation could not start for a reason
    // worth waiting out (compositor unreachable, output absent).
    GenerationEnd RunCaptureGeneration(bool& transient_out);
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
    EncoderSnapshot encoder_snapshot_;     // guarded by codec_mutex_
    uint32_t encoder_generation_ = 0;      // capture thread only
    // Recovery retries the backend that last delivered frames and keeps the
    // open encoder (and with it the replay ring) unless the encoder itself
    // failed. Capture thread only.
    BackendType last_good_backend_{};
    bool have_last_good_backend_ = false;
    // Insist on cfg_.target_output during recovery only when the backend
    // actually captured it; a name that never matched keeps its fallback.
    bool strict_recovery_output_ = false;
    bool last_generation_encoder_failed_ = false;
    mutable std::mutex codec_mutex_;
    AudioMultiCapture   multi_audio_;
};

} // namespace fthr
