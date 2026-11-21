// Windows capture, compressed replay storage, and asynchronous clip saves.
// Desktop Auto prefers DXGI with the cursor enabled; exact-monitor WGC is used
// when DXGI startup fails or system-cursor exclusion is requested.
// WGC's optional border opt-out is best effort and retains the OS privacy
// border when unavailable. Window and protected capture remain WGC-only.
// Capture and hardware encoding stay on the selected monitor adapter. Public-alpha
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
#include <optional>

#include "save_clip_task.h"      // SaveClipTask + SaveClipQueue
#include "replay_encoder.h"      // codec-neutral Windows replay encoder seam
#include "encoded_ring_buffer.h" // EncodedRingBuffer
#include "window_capture_identity.h"
#include "async_content_sampler.h"
#include "audio_capture.h"       // AudioCapture (WASAPI loopback -> PCM ring)
#include "audio_encoder.h"
#include "audio_packet_ring.h"
#include "audio_timeline.h"
#include "audio_device_tracks.h"
#include "windows_microphone_audio_provider.h"
#include "windows_process_loopback_audio_provider.h"
#include "continuous_recording_writer.h"
#include "audio_ring_buffer.h"   // AudioRingBuffer (raw float32 PCM)
#include "shared_memory.h"       // typed v4 capture-health flags
#include "windows_capture_border_policy.h"
#include "windows_capture_backend_policy.h"
#include "windows_dxgi_recovery.h"
#include "windows_monitor_resolver.h"
#include "dxgi_replay_cadence.h"
#include "dxgi_cursor.h"
#include "replay_pause_clock.h"


namespace fthr {


    struct SharedMemoryLayout;
    class BgraFrameScaler;


    struct CaptureConfig {
        uint32_t framerate = 60;
        uint32_t buffer_seconds = 30;
        uint32_t target_width = 0;
        uint32_t target_height = 0;
        uint32_t bitrate_kbps = 16000;
        ReplayStorageConfig replay_storage;
        bool allow_replay_gaps = false;
        bool allow_low_fps_clips = true;
        uint32_t max_buffer_mb = 512; // Compressed video ring + live save snapshots (MiB)
        // Auto prefers 10-bit HEVC on an HDR source when supported, and uses H.264 for SDR.
        VideoCodec video_codec = VideoCodec::H264;
        bool video_codec_auto = false;
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
        WindowsCaptureBackendPreference capture_backend_preference =
            WindowsCaptureBackendPreference::Auto;

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
        // Excludes the system pointer when false. App-rendered cursors remain
        // part of captured content. Desktop exclusion requires WGC.
        bool capture_cursor = true;

        // Stable native eCapture endpoint ID. Empty means the documented
        // "Default microphone" policy. An explicit missing ID never falls
        // back to another microphone.
        std::wstring microphone_endpoint_id;
        uint32_t microphone_gain_percent = 100;
        std::wstring playback_endpoint_id;
        bool microphone_mono = false;
        std::vector<AudioDeviceTrackConfig> audio_device_tracks;
        // nullopt preserves the pre-device-track contract for direct native
        // callers: separate_audio_enabled also enables process-loopback tracks.
        // The command-line entry point supplies an explicit value when argv[27]
        // is present (or resolves the legacy default when it is absent).
        std::optional<bool> application_audio_tracks_enabled = std::nullopt;
        std::vector<std::string> application_audio_exclusions;
        bool keep_system_audio_backup = true;

        bool ResolvedApplicationAudioTracksEnabled() const noexcept {
            return application_audio_tracks_enabled.value_or(separate_audio_enabled);
        }
        bool HasApplicationAudioExclusions() const noexcept {
            return ResolvedApplicationAudioTracksEnabled() && !application_audio_exclusions.empty();
        }
        bool RetainDefaultMixAudio() const noexcept {
            return audio_device_tracks.empty() && !HasApplicationAudioExclusions()
                && (!ResolvedApplicationAudioTracksEnabled() || keep_system_audio_backup);
        }

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
        bool MarkHighlight(const std::string& marker_id,
            int64_t out_point_qpc = 0);

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
        std::wstring GetReplayFailureMessage() const;
        ReplayStartupError GetLastStartupErrorCode() const {
            return last_startup_error_code_;
        }
        const std::string& GetStartupCaptureBackend() const {
            return startup_capture_backend_;
        }
        const ActiveReplayCapability& GetActiveReplayCapability() const {
            return active_replay_capability_;
        }
        uint32_t GetCaptureHealthFlags() const {
            const uint32_t flags = capture_health_flags_.load();
            return replay_storage_ && !replay_storage_->healthy()
                ? flags | CAPTURE_HEALTH_BACKEND_FAILED : flags;
        }
        bool HasReplayMemoryFailure() const {
            return replay_memory_failed_.load()
                || (replay_storage_ && !replay_storage_->healthy());
        }
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
        struct SaveWorkerTiming;
        bool ProcessSaveClipTask(const SaveClipTask& task, SaveWorkerTiming* timing);
        void ForwardAudioToRecording(const AudioSourceMetadata& source, const uint8_t* data,
            uint32_t size, int64_t timeline_100ns, int64_t duration_samples);
        bool MuxEncodedClip(const SaveClipTask& task,
            const std::wstring& output_path, SaveWorkerTiming* timing);
        bool EncodeRawClip(const SaveClipTask& task,
            const std::wstring& output_path);               // Legacy raw path: encode + mux

        // D3D11 / DXGI / WGC helpers
        bool InitializeWGC();             // WGC capture of the resolved desktop monitor
        bool InitializeWindowCapture();   // WGC window/game capture
        // Returns true when the required WGC session is usable. Borderless is
        // best effort; callers retain the requested WGC target when its optional
        // opt-out is unavailable or denied.
        bool ApplyCaptureBorderPolicy(const char* capture_target);
        void ApplyCaptureCursorPolicy();
        void ShutdownWGC();
        bool InitializeD3D11();           // DXGI fallback for the resolved adapter/output
        void ShutdownD3D11();
        void InvalidateDxgiReplayCache() noexcept;
        bool EnsureDxgiReplayTexture(const D3D11_TEXTURE2D_DESC& source);
        bool RecoverDxgiDuplication(
            const char* api_call, HRESULT trigger);
        dxgi::RecoveryAttemptResult TryRecreateDxgiDuplication(
            uint32_t attempt, std::string& detail);
        bool WaitForDxgiRecoveryBackoff(uint32_t delay_ms) const;
        bool ValidateCaptureTexture(
            ID3D11Texture2D* texture, const char* backend_name);
        bool ValidateMappedCaptureRowPitch(
            uint32_t expected_width, uint32_t row_pitch, const char* backend_name);
        void SampleContentBGRA(const uint8_t* data, uint32_t stride,
            uint32_t width, uint32_t height, uint64_t produced_frame);
        void SampleContentTexture(ID3D11Texture2D* texture, uint64_t produced_frame);
        void PublishContentMetrics(uint64_t sum, uint64_t sum_sq, uint32_t count);
        void ClearReplayForRecovery(int64_t recovery_cutoff_qpc = 0);
        bool ResolveSelectedMonitor(const char* backend_name);
        bool InitializeMonitorCaptureDevice(
            const char* backend_name, IDXGIOutput** selected_output);
        bool CreateStagingTexture(uint32_t width, uint32_t height);
        bool ConfigureCrop(const CaptureConfig& config);
        ID3D11Texture2D* PrepareEncodeTexture(ID3D11Texture2D* source);
        ID3D11Texture2D* PrepareHybridReadbackTexture(
            ID3D11Texture2D* source, const char* backend_name);
        void FailHybridReadback(const char* backend_name, const char* operation,
                                std::string detail);
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
        ID3D11Texture2D*        staging_texture_;  // CPU readback on the capture adapter
        ID3D11Texture2D*        crop_texture_;     // source crop before encoder scaling
        std::unique_ptr<BgraFrameScaler> hybrid_bgra_scaler_;
        AsyncContentSampler   content_sampler_; // Two nonblocking 16x9 readbacks
        ID3D11Texture2D*        dxgi_replay_texture_ = nullptr; // owned latest DXGI image
        dxgi_replay::Cadence    dxgi_replay_cadence_;
        dxgi_cursor::Composer  dxgi_cursor_composer_;
        bool capture_cursor_ = true;

        // Legacy cross-adapter resources. They remain null under the final alpha
        // policy; automatic cross-adapter replay is deliberately unsupported.
        ID3D11Device*        nvenc_device_;
        ID3D11DeviceContext* nvenc_context_;

        // WGC state (PIMPL — WinRT types confined to capture_engine.cpp)
        bool              wgc_active_;
        bool              source_hdr_ = false;
        bool              replay_hdr_ = false;
        float             sdr_reference_white_ = 1.0f;
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
        bool focus_gated_ = false;
        WindowCaptureIdentity window_source_identity_;
        std::shared_ptr<ReplayPauseClock> replay_pause_clock_;
        std::atomic<int64_t> replay_audio_origin_100ns_{0};

        // Config
        uint32_t fps_;
        bool allow_replay_gaps_ = false;
        bool allow_low_fps_clips_ = true;
        uint32_t buffer_seconds_;
        bool replay_duration_adapted_ = false;
        CaptureConfig::CaptureModeEnum capture_mode_ =
            CaptureConfig::CaptureModeEnum::DESKTOP;
        uint32_t configured_target_width_ = 0;
        uint32_t configured_target_height_ = 0;
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
        std::shared_ptr<ReplayStorage> replay_storage_;
        std::atomic<bool>                  replay_memory_failed_{false};
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

        // AudioCapture feeds lossless float packetizers and bounded disk rings.
        // The first WASAPI QPC timestamp anchors source-sample PTS; saves select
        // audio against the video presentation interval on that same clock.
        // audio_active_ requires successful initialization of the pipeline.
        bool                             audio_active_;
        bool                             audio_requested_ = false;
        AudioCapture                     audio_capture_;
        AudioEncoder                      default_mix_audio_encoder_;
        std::unique_ptr<EncodedAudioPacketRing> default_mix_audio_ring_;
        AudioSourceMetadata               default_mix_audio_source_;
        // Endpoint IDs remain local setup details and never cross the manifest
        // or the frozen Shared Memory v4 boundary.
        std::unique_ptr<WindowsMicrophoneAudioProvider> microphone_audio_source_;
        AudioSourceMetadata               microphone_audio_metadata_;
        struct DeviceAudioTrack {
            std::unique_ptr<WindowsMicrophoneAudioProvider> provider;
            AudioSourceMetadata metadata;
        };
        std::vector<DeviceAudioTrack> device_audio_tracks_;
        bool application_audio_tracks_enabled_ = false;
        bool omit_default_mix_track_ = false;
        // Process-loopback application stems are opt-in and exist only for
        // separate-audio generations. The manager owns every provider and is
        // stopped before the system/microphone audio teardown.
        std::unique_ptr<WindowsApplicationAudioSourceManager>
            application_audio_source_manager_;
        // Continuous recording state
        mutable std::mutex record_writer_mutex_;
        std::condition_variable record_writer_cv_;
        std::shared_ptr<ContinuousRecordingWriter> record_writer_;
        bool recording_recovery_in_progress_ = false;
        bool recording_stop_in_progress_ = false;
        bool recovery_stop_result_valid_ = false;
        bool recovery_stop_result_ = false;
        std::string recovery_stop_error_;
        std::string last_recording_error_;

        // Stats
        std::atomic<uint64_t> frames_captured_;
        std::atomic<uint64_t> frames_dropped_;
        std::atomic<uint64_t> capture_loop_iterations_{0};
        // Missing scheduled acquisitions are distinct from source-image drops
        // and API timeouts. Expose them in the periodic health snapshot.
        std::atomic<uint64_t> dxgi_skipped_capture_slots_{0};
        std::atomic<uint32_t> dxgi_acquire_wait_ms_{0};
        std::atomic<uint64_t> dxgi_processing_budget_us_{0};
        std::atomic<uint64_t> capture_acquire_attempts_{0};
        std::atomic<uint64_t> capture_acquire_successes_{0};
        std::atomic<uint64_t> capture_timeouts_{0};
        std::atomic<uint64_t> capture_frames_released_{0};
        std::atomic<uint64_t> pointer_only_frames_{0};
        // DXGI reports how many desktop presents were coalesced into an
        // acquired frame.  Keep this as evidence only; it never changes
        // scheduler or ownership behavior.
        std::atomic<uint64_t> dxgi_coalesced_extra_frames_{0};
        std::atomic<uint64_t> source_textures_received_{0};
        std::atomic<uint64_t> dxgi_held_presentations_{0};
        std::atomic<uint64_t> dxgi_resampled_presentations_{0};
        std::atomic<bool> dxgi_replay_cache_armed_{false};
        // Set only after an AcquireNextFrame call actually completes. It is
        // separate from acquire-attempt progress so a blocked call cannot
        // impersonate live API evidence in health snapshots.
        std::atomic<int64_t> last_dxgi_completed_qpc_{0};
        std::atomic<int64_t> last_dxgi_pointer_only_completed_qpc_{0};
        std::atomic<int64_t> last_dxgi_presentation_qpc_{0};
        // WGC publishes only changed images.  Count cadence samples that had
        // to reuse the last owned image; DXGI leaves this at zero.
        std::atomic<uint64_t> wgc_repeated_frame_observations_{0};
        std::atomic<uint64_t> wgc_missed_output_deadlines_{0};
        CaptureStageTiming wgc_acquisition_timings_;
        CaptureStageTiming wgc_readback_timings_;
        CaptureStageTiming wgc_readback_copy_timings_;
        CaptureStageTiming wgc_readback_map_timings_;
        CaptureStageTiming wgc_scale_timings_;
        CaptureStageTiming wgc_submit_timings_;
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
        // O(1) monotonic evidence sampled at existing capture/encoder
        // boundaries. These values are save-time diagnostics only.
        std::atomic<int64_t> capture_start_qpc_{0};
        std::atomic<int64_t> generation_start_qpc_{0};
        std::atomic<int64_t> last_source_frame_qpc_{0};
        std::atomic<WgcCaptureSize> protected_monitor_source_size_{{0, 0}};
        std::atomic<int64_t> last_encoder_submission_qpc_{0};
        std::atomic<int64_t> last_encoded_output_qpc_{0};
        std::atomic<int64_t> last_ring_insertion_qpc_{0};
        std::atomic<uint64_t> encoder_failure_count_{0};
        // Retain enough bounded evidence to cover a short replay-save window.
        // The snapshot is emitted only at a failure boundary, while Record()
        // remains a fixed-cost in-memory write on the capture thread.
        dxgi::RecentDxgiEvidence<512> dxgi_recent_evidence_;
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
