// Windows capture, compressed replay storage, and asynchronous clip saves.
// Prefer borderless WGC; use DXGI when WGC cannot suppress its border. Capture
// and hardware encoding stay on the selected monitor adapter. Public-alpha
// startup requires NVENC, AMF, or QSV; raw replay is legacy-only. CaptureThread
// feeds the ring, SaveClipThread muxes snapshots, and ContinuousRecordingWriter
// can also write incoming packets to fragmented MP4.

#pragma once
#ifndef FTHR_CAPTURE_ENGINE_H
#define FTHR_CAPTURE_ENGINE_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <thread>
#include <atomic>
#include <vector>
#include <condition_variable>
#include <mutex>
#include <string>
#include <cstdint>
#include <memory>

#include "save_clip_task.h"      // SaveClipTask + SaveClipQueue
#include "replay_encoder.h"      // codec-neutral Windows replay encoder seam
#include "encoded_ring_buffer.h" // EncodedRingBuffer
#include "audio_capture.h"       // AudioCapture (WASAPI loopback -> PCM ring)
#include "audio_encoder.h"
#include "audio_packet_ring.h"
#include "audio_timeline.h"
#include "windows_microphone_audio_provider.h"
#include "windows_process_loopback_audio_provider.h"
#include "continuous_recording_writer.h"
#include "replay_disk_spooler.h"
#include "audio_ring_buffer.h"   // AudioRingBuffer (raw float32 PCM)
#include "shared_memory.h"       // typed v4 capture-health flags
#include "windows_capture_border_policy.h"
#include "windows_dxgi_recovery.h"
#include "windows_monitor_resolver.h"


namespace fthr {


    struct SharedMemoryLayout;


    struct CaptureConfig {
        uint32_t framerate = 60;
        uint32_t buffer_seconds = 30;
        uint32_t target_width = 0;
        uint32_t target_height = 0;
        uint32_t bitrate_kbps = 16000;
        uint32_t max_buffer_mb = 512;
        // argv[11]: Auto resolves to H.264; explicit HEVC/AV1 must not fall back to H.264.
        VideoCodec video_codec = VideoCodec::H264;
        EncoderPreference encoder_preference = EncoderPreference::Auto;
        uint32_t encoder_preset = 4;
        // Retired multiband ABI field remains available for old callers.
        bool multiband_enabled = false;
        // 0 = the UI publishes one combined system + microphone stream;
        // 1 = retain individual source streams in the finished MP4.
        bool separate_audio_enabled = false;

        // Capture mode — set at startup, requires engine restart to change.
        enum class CaptureModeEnum : uint32_t { DESKTOP = 0, WINDOW = 1 };
        CaptureModeEnum capture_mode = CaptureModeEnum::DESKTOP;
        uintptr_t       target_hwnd  = 0;  // non-zero only when capture_mode == WINDOW

        // How to fit non-native target resolutions in the encoded clip.
        //   STRETCH = scale to target dims, may distort if aspect differs (legacy default).
        //   FIT     = preserve aspect, pad with black bars (letterbox/pillarbox).
        // Only consulted when target_width/target_height are set (non-zero) and the
        // source aspect ratio differs from the target.
        enum class ScalingModeEnum : uint32_t { STRETCH = 0, FIT = 1 };
        ScalingModeEnum scaling_mode = ScalingModeEnum::STRETCH;

        // argv[14]: 0 = disable WASAPI loopback audio capture entirely.
        // Useful when the user turns off audio in settings (no system audio in clips).
        bool audio_enabled = true;

        // Stable native eCapture endpoint ID. Empty means the documented
        // "Default microphone" policy. An explicit missing ID never falls
        // back to another microphone.
        std::wstring microphone_endpoint_id;
        uint32_t microphone_gain_percent = 100;

        // Optional per-game crop in normalized source coordinates. It is
        // resolved to safe pixel geometry after the capture source opens and
        // applied to encoder input frames, never guessed from encoded output.
        bool crop_enabled = false;
        double crop_x = 0.0;
        double crop_y = 0.0;
        double crop_width = 1.0;
        double crop_height = 1.0;

        // Stable Windows monitor device interface path from the UI. Resolved
        // into transient HMONITOR/LUID/DXGI objects for each capture generation.
        std::wstring monitor_device_path;
    };


    // Legacy raw-frame storage; public-alpha replay uses encoded packets.
    class FramePool {
    public:
        FramePool() = default;

        void     Allocate(size_t frame_count, size_t bytes_per_frame);
        uint8_t* GetSlot(size_t index) noexcept;

        size_t BytesPerFrame() const noexcept { return bytes_per_frame_; }
        size_t FrameCount()    const noexcept { return frame_count_; }
        size_t TotalBytes()    const noexcept { return storage_.size(); }

    private:
        std::vector<uint8_t> storage_;
        size_t               bytes_per_frame_ = 0;
        size_t               frame_count_ = 0;
    };


    class CaptureEngine {
    public:
        CaptureEngine();
        ~CaptureEngine();

        bool Initialize(const CaptureConfig& config);
        void Shutdown();

        bool StartRecording(const wchar_t* path);
        bool StopRecording();

        bool SaveClip(const wchar_t* path, uint32_t duration_seconds,
            SharedMemoryLayout* shared_memory);

        bool     IsRecording()   const;
        std::string GetLastRecordingError() const;
        uint64_t GetFrameCount() const;
        bool     IsNvencActive() const { return nvenc_active_; }
        std::string GetActiveEncoderName() const {
            return nvenc_active_ && replay_encoder_
                ? replay_encoder_->GetActiveEncoderInfo().name
                : "unavailable";
        }
        VideoCodec GetActiveVideoCodec() const {
            return nvenc_active_ && replay_encoder_
                ? replay_encoder_->GetActiveEncoderInfo().codec
                : VideoCodec::H264;
        }
        const std::string& GetLastStartupError() const {
            return last_startup_error_;
        }
        ReplayStartupError GetLastStartupErrorCode() const {
            return last_startup_error_code_;
        }
        const ActiveReplayCapability& GetActiveReplayCapability() const {
            return active_replay_capability_;
        }
        uint32_t GetCaptureHealthFlags() const { return capture_health_flags_.load(); }
        uint32_t GetCaptureGeneration() const { return capture_generation_.load(); }
        uint32_t GetContentSampleSequence() const { return content_sample_sequence_.load(); }
        uint32_t GetContentSuspiciousStreak() const { return content_suspicious_streak_.load(); }
        float GetContentLumaMean() const { return content_luma_mean_.load(); }
        float GetContentLumaVariance() const { return content_luma_variance_.load(); }

        struct Stats {
            uint64_t frames_captured;
            uint64_t frames_dropped;
            float    capture_fps;
            size_t   pool_memory_mb;
            size_t   ring_used_frames;
            size_t   ring_max_frames;
            float    effective_buffer_seconds;
        };
        Stats GetStats() const;


    private:
        // Thread entry points
        void CaptureThread();       // dispatches to WGC or DXGI
        void CaptureThreadWGC();    // WGC fixed-cadence image sampler
        void ReplayStallWatchdogThread();
        void SaveClipThread();

        // Save workers return false and publish SetEngineError() on failure.
        // Publish CLIP_SAVED only after the transactional write succeeds.
        bool ProcessSaveClipTask(const SaveClipTask& task); // transactional wrapper
        bool MuxEncodedClip(const SaveClipTask& task,
            const std::wstring& output_path);               // Compressed replay: mux only
        bool EncodeRawClip(const SaveClipTask& task,
            const std::wstring& output_path);               // Legacy raw path: encode + mux

        // D3D11 / DXGI / WGC helpers
        bool InitializeWGC();             // WGC capture of the resolved desktop monitor
        bool InitializeWindowCapture();   // WGC window/game capture
        // Returns true only when the WGC session read back as borderless.
        // Callers fail closed to DXGI when it returns false.
        bool ApplyCaptureBorderPolicy(const char* capture_target);
        void ShutdownWGC();
        bool InitializeD3D11();           // DXGI fallback for the resolved adapter/output
        void ShutdownD3D11();
        bool RecoverDxgiDuplication(
            const char* api_call, HRESULT trigger);
        dxgi::RecoveryAttemptResult TryRecreateDxgiDuplication(
            uint32_t attempt, std::string& detail);
        bool WaitForDxgiRecoveryBackoff(uint32_t delay_ms) const;
        bool ValidateCaptureTexture(
            ID3D11Texture2D* texture, const char* backend_name);
        bool ValidateMappedCaptureRowPitch(
            uint32_t row_pitch, const char* backend_name);
        void SampleContentBGRA(const uint8_t* data, uint32_t stride,
            uint32_t width, uint32_t height, uint64_t produced_frame);
        void SampleContentTexture(ID3D11Texture2D* texture, uint64_t produced_frame);
        void PublishContentMetrics(uint64_t sum, uint64_t sum_sq, uint32_t count);
        void ClearReplayForRecovery(int64_t recovery_cutoff_qpc = 0);
        bool ResolveSelectedMonitor(const char* backend_name);
        bool InitializeMonitorCaptureDevice(
            const char* backend_name, IDXGIOutput** selected_output);
        bool EnsureStagingTexture();
        bool ConfigureCrop(const CaptureConfig& config);
        ID3D11Texture2D* PrepareEncodeTexture(ID3D11Texture2D* source);
        const uint8_t* CropMappedData(
            const uint8_t* data, uint32_t stride) const;
        bool EncodeGpuReplayTexture(
            ID3D11Texture2D* source,
            int64_t present_qpc,
            uint64_t produced_frame);
        // Emits the bounded in-memory DXGI evidence ring only at a recovery or
        // watchdog boundary.  No per-frame diagnostic I/O is performed.
        void EmitRecentDxgiEvidence(const char* reason) const;
        void FailReplayEncoder(const char* operation);
        void SetCaptureFailure(std::string detail);
        std::string BuildStartupDiagnosticContext() const;
        bool FailStartup(ReplayStartupError code, std::string detail);

        // D3D11 state (shared by WGC and DXGI paths)
        ID3D11Device*           device_;           // D3D11 device on the selected capture adapter
        ID3D11DeviceContext*    context_;
        IDXGIOutputDuplication* duplication_;      // null when WGC is active
        ID3D11Texture2D*        staging_texture_;  // null on WGC+NVENC path
        ID3D11Texture2D*        crop_texture_;     // encoder-sized GPU crop
        ID3D11Texture2D*        health_staging_texture_; // 16x9 grid, sampled ~1 Hz

        // Legacy cross-adapter resources. They remain null under the final alpha
        // policy; automatic cross-adapter replay is deliberately unsupported.
        ID3D11Device*        nvenc_device_;
        ID3D11DeviceContext* nvenc_context_;

        // WGC state (PIMPL — WinRT types confined to capture_engine.cpp)
        bool              wgc_active_;
        struct WGCState;                        // defined in capture_engine.cpp
        std::unique_ptr<WGCState> wgc_state_;
        std::mutex              wgc_frame_mutex_;
        std::condition_variable wgc_frame_cv_;
        std::atomic<bool>       monitor_source_invalidated_{false};
        // Thread handles
        std::thread* capture_thread_;
        std::thread* stall_watchdog_thread_;
        std::thread* save_clip_thread_;
        std::atomic<bool> running_;
        std::atomic<bool> is_recording_;

        // Capture dimensions (set by whichever backend initializes first)
        uint32_t  width_;
        uint32_t  height_;
        bool      crop_enabled_;
        uint32_t  crop_x_;
        uint32_t  crop_y_;
        uint32_t  crop_width_;
        uint32_t  crop_height_;
        uintptr_t target_hwnd_;  // 0 = desktop mode; non-zero = window capture mode

        // Focus gate: when true, CaptureThreadWGC drops every frame where
        // GetForegroundWindow() != target_hwnd_. Used for anti-cheat games
        // captured via WGC monitor — we still capture the whole monitor (the
        // path Vanguard allows), but only encode while the protected game is
        // foreground so we don't bake the user's desktop into clips.
        bool focus_gated_;

        // Config
        uint32_t fps_;
        uint32_t buffer_seconds_;
        uint32_t target_width_;
        uint32_t target_height_;
        uint32_t bitrate_kbps_;
        uint32_t scaling_mode_;   // 0 = stretch, 1 = fit/letterbox
        bool separate_audio_enabled_;
        std::wstring monitor_device_path_;
        monitor::WindowsMonitorTopologySource monitor_topology_source_;
        monitor::MonitorResolver monitor_resolver_;
        monitor::MonitorTopologyEntry resolved_monitor_;
        monitor::DxgiOutputIdentity resolved_dxgi_output_;
        monitor::AdapterLuid capture_device_adapter_luid_{};
        monitor::AdapterLuid encoder_adapter_luid_{};
        bool capture_device_adapter_luid_available_ = false;
        bool encoder_adapter_luid_available_ = false;
        std::string startup_capture_backend_ = "unavailable:not_reached";
        std::string startup_encoder_backend_ = "unavailable:not_reached";
        std::string startup_codec_ = "unavailable:not_requested";
        std::string last_capture_failure_detail_;

        // nvenc_active_ is the shared-memory v4 name for any active hardware
        // replay encoder, including AMF and QSV. Retain it for ABI compatibility.
        bool                              nvenc_active_;
        bool                              nvidia_device_; // true when D3D11 device is on the NVIDIA adapter (GPU zero-copy enabled)
        bool                              replay_encoder_cpu_input_;
        EncoderVendor                     capture_adapter_vendor_;
        std::unique_ptr<IReplayEncoder>    replay_encoder_;
        std::unique_ptr<EncodedRingBuffer> encoded_ring_;
        std::atomic<bool>                  replay_config_publish_failed_{false};
        ActiveReplayCapability             active_replay_capability_{};
        ReplayStartupError                 last_startup_error_code_ =
            ReplayStartupError::None;
        std::string                        last_startup_error_;

        // Legacy raw replay
        FramePool            frame_pool_;
        size_t               max_frames_;
        std::atomic<size_t>  ring_head_{ 0 };
        std::atomic<size_t>  ring_count_{ 0 };
        mutable std::mutex   ring_mutex_;

        // Async SaveClip
        SaveClipQueue          save_clip_queue_;
        std::atomic<uint32_t>  next_task_id_{ 1 };

        // AudioCapture feeds persistent AAC encoders and bounded packet rings.
        // The first WASAPI QPC timestamp anchors source-sample PTS; saves select
        // audio against the video presentation interval on that same clock.
        // audio_active_ requires successful initialization of the pipeline.
        bool                             audio_active_;
        AudioCapture                     audio_capture_;
        AudioEncoder                      default_mix_audio_encoder_;
        std::unique_ptr<EncodedAudioPacketRing> default_mix_audio_ring_;
        AudioSourceMetadata               default_mix_audio_source_;
        // Endpoint IDs remain local setup details and never cross the manifest
        // or the frozen Shared Memory v4 boundary.
        std::unique_ptr<WindowsMicrophoneAudioProvider> microphone_audio_source_;
        AudioSourceMetadata               microphone_audio_metadata_;
        // Process-loopback application stems are opt-in and exist only for
        // separate-audio generations. The manager owns every provider and is
        // stopped before the system/microphone audio teardown.
        std::unique_ptr<WindowsApplicationAudioSourceManager>
            application_audio_source_manager_;
        // Continuous recording state
        mutable std::mutex record_writer_mutex_;
        std::shared_ptr<ContinuousRecordingWriter> record_writer_;
        std::string last_recording_error_;

        // Disk-backed replay buffer spooler (for long retention e.g. 10m-30m+)
        mutable std::mutex replay_disk_spooler_mutex_;
        std::unique_ptr<ReplayDiskSpooler> replay_disk_spooler_;

        // Stats
        std::atomic<uint64_t> frames_captured_;
        std::atomic<uint64_t> frames_dropped_;
        std::atomic<uint64_t> capture_loop_iterations_{0};
        std::atomic<uint64_t> capture_acquire_attempts_{0};
        std::atomic<uint64_t> capture_acquire_successes_{0};
        std::atomic<uint64_t> capture_timeouts_{0};
        std::atomic<uint64_t> capture_frames_released_{0};
        std::atomic<uint64_t> pointer_only_frames_{0};
        std::atomic<uint64_t> source_textures_received_{0};
        std::atomic<uint64_t> conversion_submissions_{0};
        std::atomic<uint64_t> conversion_completions_{0};
        std::atomic<uint64_t> video_packets_produced_{0};
        std::atomic<uint64_t> video_ring_insertions_{0};
        std::atomic<uint32_t> capture_thread_stage_{0};
        std::atomic<int32_t> last_capture_hresult_{0};
        std::atomic<uint32_t> capture_health_flags_{CAPTURE_HEALTH_NONE};
        std::atomic<uint32_t> capture_generation_{0};
        std::atomic<uint32_t> capture_restart_count_{0};
        std::atomic<uint32_t> capture_recovery_attempts_{0};
        std::atomic<uint32_t> capture_recovery_failures_{0};
        dxgi::RecentDxgiEvidence<> dxgi_recent_evidence_;
        mutable std::mutex dxgi_context_mutex_;
        RECT resolved_desktop_coordinates_{};
        bool resolved_desktop_coordinates_available_ = false;
        std::atomic<uint32_t> content_sample_sequence_{0};
        std::atomic<uint32_t> content_suspicious_streak_{0};
        std::atomic<float> content_luma_mean_{0.0f};
        std::atomic<float> content_luma_variance_{0.0f};
    };


} // namespace fthr


#endif // FTHR_CAPTURE_ENGINE_H
