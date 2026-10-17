// windows_microphone_audio_provider.h -- native WASAPI microphone replay.
//
// The provider is generation-local.  It keeps encoded AAC packets only, maps
// them to the common QPC timeline, and never exposes endpoint identifiers in a
// completed clip manifest.

#pragma once
#ifndef FTHR_WINDOWS_MICROPHONE_AUDIO_PROVIDER_H
#define FTHR_WINDOWS_MICROPHONE_AUDIO_PROVIDER_H

#include "audio_packet_ring.h"
#include "audio_format_converter.h"
#include "audio_timeline.h"
#include "save_clip_task.h"
#include "replay_pause_clock.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace fthr {

// Runtime/setup identity only. endpoint_id is persisted in local settings and
// passed to the engine, but is deliberately excluded from portable manifests.
struct WindowsMicrophoneEndpoint {
    std::wstring endpoint_id;
    std::string display_name;
    uint32_t device_state = 0;
    bool is_default = false;
};

// Enumerates native eCapture endpoints and their stable IDs.  The caller can
// choose to display active endpoints only while retaining a missing explicit ID
// in settings so it is never silently rebound to a different microphone.
bool EnumerateWindowsMicrophoneEndpoints(
    std::vector<WindowsMicrophoneEndpoint>* endpoints, std::string* error,
    bool render = false);

struct WindowsMicrophoneAudioProviderConfig {
    uint64_t generation = 0;
    // Empty only when use_default_endpoint is true.
    std::wstring endpoint_id;
    bool use_default_endpoint = true;
    uint32_t retention_seconds = 30;
    // Canonical 48 kHz stereo AAC is intentionally lower bitrate than the
    // Default Mix: a microphone is normally voice, while the stereo channel
    // layout keeps future native mixing format-stable.
    uint32_t bitrate_kbps = 96;
    // Capture/input gain only. It is burned exactly once into newly captured
    // microphone samples and is never reused as an editor/export gain.
    float input_gain = 1.0f;
    bool downmix_mono = false;
    AudioSourceMetadata source;
    std::shared_ptr<ReplayPauseClock> replay_pause_clock;
};

// Downmix canonical interleaved stereo samples and duplicate mono to both
// channels. This preserves the shared stereo encoder/mixer contract.
void DownmixWindowsMicrophoneToMono(float* samples, size_t sample_count);

// Applies the configured capture gain exactly once.  The input gain is
// clamped to the supported 0..2 range, but samples are intentionally not
// limited afterward; diagnostics must be able to expose over-range input.
void ApplyWindowsMicrophoneInputGain(float* samples, size_t sample_count,
                                     float configured_gain);

// Bounded timing/flag evidence for the native WASAPI capture loop.  These
// counters deliberately describe observations made by FTHR; the processing
// threshold is not an OS-level underrun claim.
struct WindowsMicrophoneProcessingDiagnosticsSnapshot {
    uint32_t capture_buffer_frames = 0;
    uint64_t capture_buffer_duration_100ns = 0;
    uint64_t timestamp_error_count = 0;
    uint64_t max_packet_processing_duration_100ns = 0;
    uint64_t packet_processing_at_or_above_buffer_duration_count = 0;
};

class WindowsMicrophoneProcessingDiagnosticsAccumulator {
public:
    static uint64_t CaptureBufferDuration100ns(uint32_t buffer_frames,
                                               uint32_t sample_rate);
    static bool ProcessingDurationMeetsBuffer(uint64_t processing_duration_100ns,
                                              uint64_t buffer_duration_100ns);

    void ConfigureCaptureBuffer(uint32_t buffer_frames, uint32_t sample_rate);
    void ObservePacket(bool timestamp_error, uint64_t processing_duration_100ns);
    WindowsMicrophoneProcessingDiagnosticsSnapshot Snapshot() const;
    void Reset();

private:
    std::atomic<uint32_t> capture_buffer_frames_{0};
    std::atomic<uint64_t> capture_buffer_duration_100ns_{0};
    std::atomic<uint64_t> timestamp_error_count_{0};
    std::atomic<uint64_t> max_packet_processing_duration_100ns_{0};
    std::atomic<uint64_t> packet_processing_at_or_above_buffer_duration_count_{0};
};

struct WindowsMicrophoneRuntimeInfo {
    enum class State : uint8_t { Stopped, Starting, Active, Failed };
    State state = State::Stopped;
    std::wstring active_endpoint_id;
    std::string active_display_name;
    AudioSourceFormat input_format;
    bool failed = false;
    int64_t max_observed_drift_samples = 0;
    uint64_t packet_count = 0;
    uint64_t discontinuity_count = 0;
    uint64_t first_packet_qpc_100ns = 0;
    uint64_t last_packet_qpc_100ns = 0;
    uint64_t last_packet_end_qpc_100ns = 0;
    uint64_t converted_frame_count = 0;
    uint64_t encoded_packet_count = 0;
    uint64_t encoder_error_count = 0;
    uint64_t rejected_packet_count = 0;
    uint64_t largest_no_packet_gap_100ns = 0;
    uint64_t synthetic_silence_frame_count = 0;
    uint64_t skipped_input_frame_count = 0;
    uint64_t resampler_correction_count = 0;
    uint64_t normal_jitter_absorbed_count = 0;
    uint64_t hard_discontinuity_recovery_count = 0;
    AudioCorrectionDistributionSnapshot silence_corrections;
    AudioCorrectionDistributionSnapshot skip_corrections;
    AudioCorrectionDistributionSnapshot resampler_corrections;
    float configured_input_gain = 1.0f;
    AudioSignalMetricsSnapshot native_input_signal;
    AudioSignalMetricsSnapshot converter_output_signal;
    AudioSignalMetricsSnapshot encoder_input_signal;
    WindowsMicrophoneProcessingDiagnosticsSnapshot processing_diagnostics;
};

class WindowsMicrophoneAudioProvider {
public:
    explicit WindowsMicrophoneAudioProvider(WindowsMicrophoneAudioProviderConfig config);
    ~WindowsMicrophoneAudioProvider();

    WindowsMicrophoneAudioProvider(const WindowsMicrophoneAudioProvider&) = delete;
    WindowsMicrophoneAudioProvider& operator=(const WindowsMicrophoneAudioProvider&) = delete;

    bool Start();
    void Stop();
    bool IsRunning() const;
    std::string last_error() const;
    WindowsMicrophoneRuntimeInfo runtime_info() const;
    AudioCorrectionEventSnapshot correction_events() const;

    // A track is returned only when the provider has native AAC history for
    // the requested video interval.  The source metadata is updated with the
    // real generation-local activity range, never a device identifier.
    std::optional<EncodedAudioTrack> TakeTrackForInterval(
        double presentation_start_qpc_s, double presentation_end_qpc_s,
        AudioSourceMetadata source) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace fthr

#endif  // FTHR_WINDOWS_MICROPHONE_AUDIO_PROVIDER_H
