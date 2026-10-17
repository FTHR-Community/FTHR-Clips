// Shared QPC-to-audio timing and drift policy for native providers.
// This platform-neutral model carries no device handles or IPC state.

#pragma once
#ifndef FTHR_AUDIO_TIMELINE_H
#define FTHR_AUDIO_TIMELINE_H

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

namespace fthr {

inline constexpr uint32_t kCanonicalAudioSampleRate = 48000;
inline constexpr uint32_t kCanonicalAudioChannels = 2;
// A hard-correction event is marked SWR-active/recent when the last
// successful swr_set_compensation application is within +/-1.0 seconds of its
// correction boundary in this same 100 ns QPC domain.
inline constexpr uint64_t kAudioCorrectionRecentWindow100ns = 10'000'000ULL;
// Endpoint packet QPC jitter below one millisecond is normal scheduling/device
// variance, not a missing audio interval.  Keep it in the shared timeline and
// let the persistent resampler absorb the long-term clock error instead of
// inserting/dropping waveform samples at every packet boundary.  The value is
// deliberately time based so the policy is shared by 44.1/48/96 kHz sources.
inline constexpr uint64_t kAudioTimelineNormalJitterTolerance100ns = 10'000ULL;
// Synthetic silence is only a bounded continuity repair. A longer gap is
// treated as a device/session failure by the provider instead of synchronously
// encoding an unbounded amount of audio on the capture thread.
inline constexpr uint32_t kMaxSyntheticAudioGapSeconds = 5;

enum class AudioCorrectionSource : uint8_t {
    System,
    Microphone,
};

enum class AudioCorrectionDirection : uint8_t {
    Insert,
    Skip,
};

const char* AudioCorrectionSourceName(AudioCorrectionSource source);
const char* AudioCorrectionDirectionName(AudioCorrectionDirection direction);

enum class AudioTimelineAdjustmentClass : uint8_t {
    Aligned,
    NormalJitterAbsorbed,
    HardGap,
    HardOverlap,
    LargeGap,
};

const char* AudioTimelineAdjustmentClassName(AudioTimelineAdjustmentClass value);

// One hard QPC continuity edit. These records describe timing decisions only;
// no audio samples are retained. The event ring enriches cumulative_signed_*
// and time_since_previous_* while recording without allocating.
struct AudioCorrectionEvent {
    AudioCorrectionSource source = AudioCorrectionSource::System;
    AudioCorrectionDirection direction = AudioCorrectionDirection::Insert;
    uint64_t correction_qpc_100ns = 0;
    uint64_t packet_qpc_100ns = 0;
    uint64_t expected_qpc_100ns = 0;
    uint32_t correction_frames = 0;
    uint32_t sample_rate = 0;
    double correction_ms = 0.0;
    int64_t cumulative_signed_correction_frames = 0;
    double time_since_previous_correction_ms = 0.0;
    bool swr_compensation_active_or_recent = false;
    // Set by SummarizeAudioCorrections; remains negative for ring snapshots.
    double clip_relative_time_ms = -1.0;
};

inline constexpr size_t kAudioCorrectionEventRingCapacity = 1024;

struct AudioCorrectionEventSnapshot {
    std::vector<AudioCorrectionEvent> events;
    uint64_t overwritten_count = 0;
};

// Fixed-capacity, thread-safe event retention. Record() performs no dynamic
// allocation and is safe on the native audio capture thread while Snapshot()
// is called by diagnostics/save code.
class AudioCorrectionEventRing {
public:
    void Record(AudioCorrectionEvent event);
    void Reset();
    AudioCorrectionEventSnapshot Snapshot() const;

private:
    mutable std::mutex mutex_;
    std::array<AudioCorrectionEvent, kAudioCorrectionEventRingCapacity> events_{};
    size_t next_index_ = 0;
    size_t size_ = 0;
    uint64_t overwritten_count_ = 0;
    int64_t cumulative_signed_correction_frames_ = 0;
    uint64_t last_correction_qpc_100ns_ = 0;
};

struct AudioCorrectionDensity {
    uint64_t event_count = 0;
    uint64_t corrected_frames = 0;
};

struct AudioCorrectionLargest {
    bool present = false;
    uint32_t correction_frames = 0;
    uint64_t correction_qpc_100ns = 0;
    double clip_relative_time_ms = 0.0;
};

struct AudioCorrectionClipSummary {
    uint64_t selected_start_qpc_100ns = 0;
    uint64_t selected_end_qpc_100ns = 0;
    std::vector<AudioCorrectionEvent> events;
    uint64_t system_corrections_inside_clip = 0;
    uint64_t mic_corrections_inside_clip = 0;
    uint64_t system_inserted_frames_inside_clip = 0;
    uint64_t system_skipped_frames_inside_clip = 0;
    uint64_t mic_inserted_frames_inside_clip = 0;
    uint64_t mic_skipped_frames_inside_clip = 0;
    AudioCorrectionLargest largest_system_correction_inside_clip;
    AudioCorrectionLargest largest_mic_correction_inside_clip;
    AudioCorrectionDensity maximum_density_250ms;
    AudioCorrectionDensity maximum_density_1s;
    uint64_t system_events_overwritten = 0;
    uint64_t mic_events_overwritten = 0;
};

// Maps and summarizes events in the half-open replay window [start, end).
// Events are returned in chronological order and retain their source-specific
// cumulative counters. The density fields count combined system+microphone
// events and corrected frames in sliding 250 ms and 1 s windows.
AudioCorrectionClipSummary SummarizeAudioCorrections(
    const AudioCorrectionEventSnapshot& system,
    const AudioCorrectionEventSnapshot& microphone,
    uint64_t selected_start_qpc_100ns, uint64_t selected_end_qpc_100ns);

// Converts the process-wide QueryPerformanceCounter clock to 100-nanosecond
// units. WASAPI's non-zero QPC timestamps are already in this same domain.
// Providers use this only when a driver omits a packet timestamp, so every
// native source still has a monotonic timeline anchor.
uint64_t CurrentAudioTimeline100ns();

struct AudioSourcePresentationRange {
    // Negative starts intentionally preserve a source that became audible
    // after the beginning of the selected video interval.
    int64_t start_pts_samples = 0;
    int64_t end_pts_samples = 0;
};

struct AudioTimelinePacketAdjustment {
    uint32_t silence_frames = 0;
    uint32_t skip_input_frames = 0;
    // Signed packet-start error relative to the previous timeline cursor.
    // Positive means a gap; negative means overlap.
    int64_t timing_error_100ns = 0;
    uint64_t gap_100ns = 0;
    uint64_t next_timeline_100ns = 0;
    AudioTimelineAdjustmentClass classification =
        AudioTimelineAdjustmentClass::Aligned;
    bool normal_jitter_absorbed = false;
    bool hard_discontinuity = false;
    bool large_gap = false;
};

// Bounded aggregate evidence for timeline repairs. Buckets are intentionally
// coarse so field reports can distinguish frequent single-frame corrections
// from audible packet-sized edits without logging individual audio packets.
inline constexpr size_t kAudioCorrectionBucketCount = 6;

struct AudioCorrectionDistributionSnapshot {
    uint64_t event_count = 0;
    uint64_t total_magnitude_frames = 0;
    int64_t signed_total_frames = 0;
    uint64_t max_single_frames = 0;
    // 1, 2-4, 5-16, 17-64, 65-256, 257+ frames.
    std::array<uint64_t, kAudioCorrectionBucketCount> buckets{};
};

class AudioCorrectionDistribution {
public:
    void Observe(int64_t frames);
    void Reset();
    AudioCorrectionDistributionSnapshot Snapshot() const;

private:
    std::atomic<uint64_t> event_count_{0};
    std::atomic<uint64_t> total_magnitude_frames_{0};
    std::atomic<int64_t> signed_total_frames_{0};
    std::atomic<uint64_t> max_single_frames_{0};
    std::array<std::atomic<uint64_t>, kAudioCorrectionBucketCount> buckets_{};
};

struct MediaTimelineRange {
    double start_s = 0.0;
    double end_s = 0.0;
    bool valid = false;
};

struct AvSyncTimelineEvidence {
    bool valid = false;
    bool generation_continuous = true;
    double video_duration_s = 0.0;
    double audio_duration_s = 0.0;
    double video_start_relative_to_window_ms = 0.0;
    double audio_start_relative_to_window_ms = 0.0;
    double video_end_relative_to_window_ms = 0.0;
    double audio_end_relative_to_window_ms = 0.0;
    // Positive means audio is later than video at the corresponding edge.
    double av_start_delta_ms = 0.0;
    double av_end_delta_ms = 0.0;
};

// Compare already-authoritative media ranges without rebasing either one.
// This is shared by save diagnostics and deterministic timeline tests.
AvSyncTimelineEvidence EvaluateAvSyncTimeline(
    double selected_window_start_s, double selected_window_end_s,
    MediaTimelineRange video, MediaTimelineRange audio,
    bool generation_continuous = true);

// Computes bounded continuity work for one native endpoint packet. The caller
// owns the cursor and performs the returned silence insertion/overlap skip.
AudioTimelinePacketAdjustment ReconcileAudioTimelinePacket(
    uint64_t next_timeline_100ns, uint64_t packet_qpc_100ns,
    uint32_t input_frames, uint32_t input_sample_rate,
    uint32_t canonical_sample_rate = kCanonicalAudioSampleRate,
    uint32_t max_synthetic_gap_seconds = 5);

AudioSourcePresentationRange MapAudioSourcePresentationRange(
    double presentation_start_qpc_s, double presentation_end_qpc_s,
    uint64_t source_timeline_origin_100ns, uint32_t sample_rate);

// Converts an AAC/sample position back to the shared QPC timeline. Negative
// positions are valid for encoder delay and must remain signed during the
// conversion instead of wrapping through uint64_t.
int64_t AudioSamplePositionToTimeline100ns(
    uint64_t source_timeline_origin_100ns, int64_t sample_position,
    uint32_t sample_rate);

struct AudioDriftCorrection {
    int64_t observed_drift_samples = 0;
    int32_t sample_delta = 0;
    uint32_t compensation_distance_samples = 0;
};

// Bounds how quickly an independent endpoint may be resampled toward the
// common QPC clock.  A device clock is never jumped: at most max_ppm worth of
// samples is added or removed over one bounded compensation window.
class AudioDriftController {
public:
    explicit AudioDriftController(uint32_t sample_rate = kCanonicalAudioSampleRate,
                                  uint32_t max_ppm = 100);

    AudioDriftCorrection Observe(uint64_t qpc_100ns, int64_t submitted_frames);
    void Reset();

    int64_t max_observed_drift_samples() const { return max_observed_drift_samples_; }
    uint64_t origin_qpc_100ns() const { return origin_qpc_100ns_; }

private:
    uint32_t sample_rate_;
    uint32_t max_ppm_;
    uint64_t origin_qpc_100ns_ = 0;
    uint64_t last_adjustment_qpc_100ns_ = 0;
    int64_t max_observed_drift_samples_ = 0;
};

}  // namespace fthr

#endif  // FTHR_AUDIO_TIMELINE_H
