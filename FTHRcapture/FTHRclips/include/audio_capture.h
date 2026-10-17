// WASAPI system-loopback capture on a dedicated event-driven thread.
// Convert endpoint PCM to float32, 48 kHz stereo before encoding. Inject
// silence during idle periods to keep the audio timeline continuous.
// AudioEncoder handles AAC; the replay ring owns retained packets.

#pragma once
#ifndef FTHR_AUDIO_CAPTURE_H
#define FTHR_AUDIO_CAPTURE_H

#include <cstdint>
#include <thread>
#include <atomic>
#include <string>
#include <vector>

#include "audio_format_converter.h"
#include "audio_timeline.h"


namespace fthr {

    class AudioRingBuffer;
    class AudioEncoder;


    struct AudioCaptureConfig {
        // Target sample rate and channel count for the canonical replay path.
        // The native endpoint is converted to this format before encoding.
        uint32_t preferred_sample_rate = 48000;
        uint32_t preferred_channels = 2;

        // AAC encode bitrate passed through to AudioEncoder
        uint32_t bitrate_kbps = 128;

        // Render device ID; empty selects the system default.
        std::wstring device_id;
    };


    class AudioCapture {
    public:
        AudioCapture();
        ~AudioCapture();

        // Initialize WASAPI loopback capture.
        // ring:   legacy raw-PCM destination. New replay paths pass nullptr and
        //         attach an AudioEncoder before Start(). At least one destination
        //         must be configured before Start().
        // config: device selection and format preferences.
        bool Initialize(AudioRingBuffer* ring, const AudioCaptureConfig& config);

        // Attach the persistent AAC encoder after Initialize() reveals the
        // endpoint's actual format and before Start() begins capture.
        void SetEncoder(AudioEncoder* encoder);

        // Start the capture thread. Call after Initialize() succeeds.
        // Returns true if the thread started successfully.
        bool Start();

        // Signal the capture thread to stop and block until it exits.
        // Safe to call even if Start() was never called.
        void Stop();

        // Release all WASAPI and COM resources.
        // Must be called after Stop().
        void Shutdown();

        // Canonical format exposed to the replay path. Native endpoint details
        // are available through GetInputSampleRate/GetInputChannels.
        uint32_t GetSampleRate() const { return sample_rate_; }
        uint32_t GetChannels()   const { return channels_; }
        uint32_t GetInputSampleRate() const { return input_sample_rate_; }
        uint32_t GetInputChannels() const { return input_channels_; }
        bool     IsRunning()     const { return running_.load(std::memory_order_relaxed); }

        // True if the WASAPI device was lost at runtime (device change, driver reset,
        // exclusive-mode app, etc.). Audio ring is stale once this is set.
        // Cleared by Shutdown() so a fresh Initialize()+Start() starts clean.
        bool     IsDeviceLost()  const { return device_lost_.load(std::memory_order_relaxed); }

        // QPC (100ns) of the first submitted audio frame for this generation.
        // A value of zero means no timestamped packet has arrived yet.
        uint64_t GetTimelineOriginQpc100ns() const {
            return timeline_origin_qpc_100ns_.load(std::memory_order_acquire);
        }
        uint32_t GetRequestedSampleRate() const { return requested_sample_rate_; }
        uint32_t GetRequestedChannels() const { return requested_channels_; }
        const std::string& GetFriendlyName() const { return friendly_name_; }
        uint64_t GetPacketCount() const {
            return packet_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetDiscontinuityCount() const {
            return discontinuity_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetFirstPacketQpc100ns() const {
            return first_packet_qpc_100ns_.load(std::memory_order_acquire);
        }
        uint64_t GetLastPacketQpc100ns() const {
            return last_packet_qpc_100ns_.load(std::memory_order_acquire);
        }
        uint64_t GetConvertedFrameCount() const {
            return converted_frame_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetEncoderErrorCount() const {
            return encoder_error_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetEncodedPacketCount() const;
        int64_t GetMaxObservedDriftSamples() const {
            return max_observed_drift_samples_.load(std::memory_order_acquire);
        }
        uint64_t GetLargestNoPacketGap100ns() const {
            return largest_no_packet_gap_100ns_.load(std::memory_order_acquire);
        }
        uint64_t GetLastPacketEndQpc100ns() const {
            return last_packet_end_qpc_100ns_.load(std::memory_order_acquire);
        }
        uint64_t GetSilentPacketCount() const {
            return silent_packet_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetNoPacketIntervalCount() const {
            return no_packet_interval_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetRestartCount() const {
            return restart_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetSyntheticSilenceFrameCount() const {
            return synthetic_silence_frame_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetSkippedInputFrameCount() const {
            return skipped_input_frame_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetResamplerCorrectionCount() const {
            return resampler_correction_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetNormalJitterAbsorbedCount() const {
            return normal_jitter_absorbed_count_.load(std::memory_order_relaxed);
        }
        uint64_t GetHardDiscontinuityRecoveryCount() const {
            return hard_discontinuity_recovery_count_.load(std::memory_order_relaxed);
        }
        AudioCorrectionDistributionSnapshot GetSilenceCorrectionDistribution() const {
            return silence_correction_distribution_.Snapshot();
        }
        AudioCorrectionDistributionSnapshot GetSkipCorrectionDistribution() const {
            return skip_correction_distribution_.Snapshot();
        }
        AudioCorrectionDistributionSnapshot GetResamplerCorrectionDistribution() const {
            return resampler_correction_distribution_.Snapshot();
        }
        AudioCorrectionEventSnapshot GetCorrectionEvents() const {
            return correction_events_.Snapshot();
        }
        AudioSignalMetricsSnapshot GetNativeInputSignalMetrics() const {
            return format_converter_.GetInputSignalMetrics();
        }
        AudioSignalMetricsSnapshot GetCanonicalOutputSignalMetrics() const {
            return format_converter_.GetOutputSignalMetrics();
        }
        AudioSignalMetricsSnapshot GetEncoderInputSignalMetrics() const {
            return encoder_input_metrics_.Snapshot();
        }
        uint64_t GetSubmittedFrameCount() const {
            return submitted_frame_count_.load(std::memory_order_relaxed);
        }
        uint32_t GetInputBytesPerFrame() const { return input_bytes_per_frame_; }
        const std::string& GetInputSampleFormat() const {
            return input_sample_format_;
        }


    private:
        // Thread entry point
        void CaptureThread();

        // Write 'num_frames' frames of silence to the encoder.
        // Used when WASAPI returns a silent buffer or no data is available.
        bool InjectSilence(uint32_t num_frames, uint64_t qpc_100ns = 0);
        bool SubmitSamples(const float* interleaved_data, uint32_t frame_count,
                           uint64_t qpc_100ns);
        bool SubmitNativeSamples(const uint8_t* interleaved_data,
                                 uint32_t frame_count, uint64_t qpc_100ns);

        // Create WASAPI COM objects (enumerator, device, audio_client, capture_client,
        // buffer_event). Sets sample_rate_, channels_, silence_buf_.
        // Assumes COM is initialized on the calling thread.
        // On failure, partial state is left — call TeardownWASAPISession() to clean up.
        bool SetupWASAPISession();

        // Release WASAPI COM objects. Safe to call with partial state (checks each
        // pointer). Does NOT touch ring_, silence_buf_, or the COM apartment.
        void TeardownWASAPISession();

        // Opaque WASAPI interfaces keep Windows headers out of this header.
        // Types: IMMDeviceEnumerator, IMMDevice, IAudioClient, IAudioCaptureClient.
        // mix_format_ is a WAVEFORMATEX freed with CoTaskMemFree; buffer_event_
        // is an auto-reset HANDLE.
        void* enumerator_;
        void* device_;
        void* audio_client_;
        void* capture_client_;
        void* mix_format_;
        void* buffer_event_;

        // Ring buffer reference (not owned) - receives raw PCM float32
        AudioRingBuffer* ring_;
        AudioEncoder* encoder_ = nullptr;

        // Native endpoint format (read from mix_format_ during Initialize).
        uint32_t input_sample_rate_ = 0;
        uint32_t input_channels_ = 0;
        uint32_t input_bytes_per_frame_ = 0;
        std::string input_sample_format_ = "unavailable:not_resolved";
        // Canonical replay format.
        uint32_t sample_rate_;
        uint32_t channels_;
        uint32_t requested_sample_rate_ = 48000;
        uint32_t requested_channels_ = 2;
        std::string friendly_name_ = "unavailable:not_resolved";

        // Thread state
        std::thread       capture_thread_;
        std::atomic<bool> running_{ false };
        std::atomic<bool> device_lost_{ false };
        std::atomic<uint64_t> timeline_origin_qpc_100ns_{ 0 };
        std::atomic<uint64_t> packet_count_{ 0 };
        std::atomic<uint64_t> discontinuity_count_{ 0 };
        std::atomic<uint64_t> first_packet_qpc_100ns_{ 0 };
        std::atomic<uint64_t> last_packet_qpc_100ns_{ 0 };
        std::atomic<uint64_t> last_packet_end_qpc_100ns_{ 0 };
        std::atomic<uint64_t> converted_frame_count_{ 0 };
        std::atomic<uint64_t> encoder_error_count_{ 0 };
        std::atomic<uint64_t> largest_no_packet_gap_100ns_{ 0 };
        std::atomic<uint64_t> silent_packet_count_{ 0 };
        std::atomic<uint64_t> no_packet_interval_count_{ 0 };
        std::atomic<uint64_t> restart_count_{ 0 };
        std::atomic<uint64_t> submitted_frame_count_{ 0 };
        std::atomic<uint64_t> synthetic_silence_frame_count_{ 0 };
        std::atomic<uint64_t> skipped_input_frame_count_{ 0 };
        std::atomic<uint64_t> resampler_correction_count_{ 0 };
        std::atomic<uint64_t> normal_jitter_absorbed_count_{ 0 };
        std::atomic<uint64_t> hard_discontinuity_recovery_count_{ 0 };
        std::atomic<int64_t> max_observed_drift_samples_{ 0 };
        std::atomic<uint64_t> last_swr_compensation_qpc_100ns_{ 0 };
        AudioCorrectionDistribution silence_correction_distribution_;
        AudioCorrectionDistribution skip_correction_distribution_;
        AudioCorrectionDistribution resampler_correction_distribution_;
        AudioCorrectionEventRing correction_events_;

        // Stored from the last Initialize() call so recovery can re-open the same
        // device (or fall back to default if empty).
        std::wstring device_id_;

        // Silence injection scratch buffer
        // Pre-allocated in Initialize() to avoid heap alloc in the hot path.
        // Size: preferred_channels * max_expected_frames_per_callback floats.
        std::vector<float> silence_buf_;
        std::vector<uint8_t> native_silence_buf_;
        std::vector<float> converted_samples_;
        AudioSignalMetrics encoder_input_metrics_;
        AudioFormatConverter format_converter_;
        AudioDriftController drift_controller_{ kCanonicalAudioSampleRate };
        int64_t submitted_frames_ = 0;
        bool initialize_thread_com_owned_ = false;
        std::thread::id initialize_thread_id_;
    };


} // namespace fthr

#endif // FTHR_AUDIO_CAPTURE_H
