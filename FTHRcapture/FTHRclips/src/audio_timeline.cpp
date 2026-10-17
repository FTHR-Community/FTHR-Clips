#include "audio_timeline.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <limits>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif
#endif

namespace fthr {

namespace {

size_t CorrectionBucket(uint64_t magnitude) {
    if (magnitude <= 1) return 0;
    if (magnitude <= 4) return 1;
    if (magnitude <= 16) return 2;
    if (magnitude <= 64) return 3;
    if (magnitude <= 256) return 4;
    return 5;
}

}  // namespace

const char* AudioCorrectionSourceName(AudioCorrectionSource source) {
    switch (source) {
    case AudioCorrectionSource::System: return "system";
    case AudioCorrectionSource::Microphone: return "microphone";
    }
    return "unknown";
}

const char* AudioCorrectionDirectionName(AudioCorrectionDirection direction) {
    switch (direction) {
    case AudioCorrectionDirection::Insert: return "insert";
    case AudioCorrectionDirection::Skip: return "skip";
    }
    return "unknown";
}

const char* AudioTimelineAdjustmentClassName(AudioTimelineAdjustmentClass value) {
    switch (value) {
    case AudioTimelineAdjustmentClass::Aligned: return "aligned";
    case AudioTimelineAdjustmentClass::NormalJitterAbsorbed:
        return "normal_jitter_absorbed";
    case AudioTimelineAdjustmentClass::HardGap: return "hard_gap_recovery";
    case AudioTimelineAdjustmentClass::HardOverlap: return "hard_overlap_recovery";
    case AudioTimelineAdjustmentClass::LargeGap: return "large_gap_unrecoverable";
    }
    return "unknown";
}

void AudioCorrectionEventRing::Record(AudioCorrectionEvent event) {
    if (event.correction_frames == 0) return;

    std::lock_guard<std::mutex> lock(mutex_);
    const int64_t signed_frames = event.direction == AudioCorrectionDirection::Insert
        ? static_cast<int64_t>(event.correction_frames)
        : -static_cast<int64_t>(event.correction_frames);
    if ((signed_frames > 0
            && cumulative_signed_correction_frames_
                > std::numeric_limits<int64_t>::max() - signed_frames)
        || (signed_frames < 0
            && cumulative_signed_correction_frames_
                < std::numeric_limits<int64_t>::min() - signed_frames)) {
        cumulative_signed_correction_frames_ = signed_frames > 0
            ? std::numeric_limits<int64_t>::max()
            : std::numeric_limits<int64_t>::min();
    } else {
        cumulative_signed_correction_frames_ += signed_frames;
    }
    event.correction_ms = event.sample_rate > 0
        ? static_cast<double>(event.correction_frames) * 1000.0
            / static_cast<double>(event.sample_rate)
        : 0.0;
    event.cumulative_signed_correction_frames = cumulative_signed_correction_frames_;
    if (event.correction_qpc_100ns > 0 && last_correction_qpc_100ns_ > 0
            && event.correction_qpc_100ns >= last_correction_qpc_100ns_) {
        event.time_since_previous_correction_ms = static_cast<double>(
            event.correction_qpc_100ns - last_correction_qpc_100ns_) / 10'000.0;
    } else {
        event.time_since_previous_correction_ms = 0.0;
    }
    if (event.correction_qpc_100ns > 0) {
        last_correction_qpc_100ns_ = event.correction_qpc_100ns;
    }

    events_[next_index_] = event;
    next_index_ = (next_index_ + 1) % kAudioCorrectionEventRingCapacity;
    if (size_ < kAudioCorrectionEventRingCapacity) {
        ++size_;
    } else {
        ++overwritten_count_;
    }
}

void AudioCorrectionEventRing::Reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    events_.fill(AudioCorrectionEvent{});
    next_index_ = 0;
    size_ = 0;
    overwritten_count_ = 0;
    cumulative_signed_correction_frames_ = 0;
    last_correction_qpc_100ns_ = 0;
}

AudioCorrectionEventSnapshot AudioCorrectionEventRing::Snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    AudioCorrectionEventSnapshot result;
    result.overwritten_count = overwritten_count_;
    result.events.reserve(size_);
    const size_t first = size_ == kAudioCorrectionEventRingCapacity
        ? next_index_ : 0;
    for (size_t offset = 0; offset < size_; ++offset) {
        result.events.push_back(events_[(first + offset)
            % kAudioCorrectionEventRingCapacity]);
    }
    return result;
}

namespace {

void AddCorrectionToLargest(const AudioCorrectionEvent& event,
                            AudioCorrectionLargest* largest) {
    if (!largest || (largest->present
            && largest->correction_frames >= event.correction_frames)) return;
    largest->present = true;
    largest->correction_frames = event.correction_frames;
    largest->correction_qpc_100ns = event.correction_qpc_100ns;
    largest->clip_relative_time_ms = event.clip_relative_time_ms;
}

AudioCorrectionDensity MaxDensity(
    const std::vector<AudioCorrectionEvent>& events, uint64_t window_100ns) {
    AudioCorrectionDensity result;
    size_t right = 0;
    uint64_t corrected_frames = 0;
    for (size_t left = 0; left < events.size(); ++left) {
        const uint64_t window_end = events[left].correction_qpc_100ns
                > std::numeric_limits<uint64_t>::max() - window_100ns
            ? std::numeric_limits<uint64_t>::max()
            : events[left].correction_qpc_100ns + window_100ns;
        while (right < events.size()
                && events[right].correction_qpc_100ns < window_end) {
            corrected_frames += events[right].correction_frames;
            ++right;
        }
        result.event_count = std::max<uint64_t>(result.event_count,
            static_cast<uint64_t>(right - left));
        result.corrected_frames = std::max(result.corrected_frames,
            corrected_frames);
        corrected_frames -= events[left].correction_frames;
    }
    return result;
}

}  // namespace

AudioCorrectionClipSummary SummarizeAudioCorrections(
    const AudioCorrectionEventSnapshot& system,
    const AudioCorrectionEventSnapshot& microphone,
    uint64_t selected_start_qpc_100ns, uint64_t selected_end_qpc_100ns) {
    AudioCorrectionClipSummary result;
    result.selected_start_qpc_100ns = selected_start_qpc_100ns;
    result.selected_end_qpc_100ns = selected_end_qpc_100ns;
    result.system_events_overwritten = system.overwritten_count;
    result.mic_events_overwritten = microphone.overwritten_count;
    if (selected_end_qpc_100ns <= selected_start_qpc_100ns) return result;

    const auto append = [&](const AudioCorrectionEventSnapshot& source,
                            AudioCorrectionSource expected_source) {
        for (const auto& original : source.events) {
            if (original.source != expected_source
                    || original.correction_qpc_100ns < selected_start_qpc_100ns
                    || original.correction_qpc_100ns >= selected_end_qpc_100ns) {
                continue;
            }
            AudioCorrectionEvent event = original;
            event.clip_relative_time_ms = static_cast<double>(
                event.correction_qpc_100ns - selected_start_qpc_100ns) / 10'000.0;
            result.events.push_back(event);
            const bool is_system = event.source == AudioCorrectionSource::System;
            if (is_system) {
                ++result.system_corrections_inside_clip;
                if (event.direction == AudioCorrectionDirection::Insert)
                    result.system_inserted_frames_inside_clip += event.correction_frames;
                else
                    result.system_skipped_frames_inside_clip += event.correction_frames;
                AddCorrectionToLargest(event,
                    &result.largest_system_correction_inside_clip);
            } else {
                ++result.mic_corrections_inside_clip;
                if (event.direction == AudioCorrectionDirection::Insert)
                    result.mic_inserted_frames_inside_clip += event.correction_frames;
                else
                    result.mic_skipped_frames_inside_clip += event.correction_frames;
                AddCorrectionToLargest(event,
                    &result.largest_mic_correction_inside_clip);
            }
        }
    };
    append(system, AudioCorrectionSource::System);
    append(microphone, AudioCorrectionSource::Microphone);
    std::sort(result.events.begin(), result.events.end(),
        [](const AudioCorrectionEvent& left, const AudioCorrectionEvent& right) {
            return left.correction_qpc_100ns < right.correction_qpc_100ns;
        });
    result.maximum_density_250ms = MaxDensity(result.events, 2'500'000ULL);
    result.maximum_density_1s = MaxDensity(result.events, 10'000'000ULL);
    return result;
}

void AudioCorrectionDistribution::Observe(int64_t frames) {
    if (frames == 0) return;
    const uint64_t magnitude = frames == std::numeric_limits<int64_t>::min()
        ? static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) + 1ULL
        : static_cast<uint64_t>(std::llabs(frames));
    event_count_.fetch_add(1, std::memory_order_relaxed);
    total_magnitude_frames_.fetch_add(magnitude, std::memory_order_relaxed);
    signed_total_frames_.fetch_add(frames, std::memory_order_relaxed);
    uint64_t maximum = max_single_frames_.load(std::memory_order_relaxed);
    while (magnitude > maximum
            && !max_single_frames_.compare_exchange_weak(
                maximum, magnitude, std::memory_order_relaxed,
                std::memory_order_relaxed)) {}
    buckets_[CorrectionBucket(magnitude)].fetch_add(1, std::memory_order_relaxed);
}

void AudioCorrectionDistribution::Reset() {
    event_count_.store(0, std::memory_order_relaxed);
    total_magnitude_frames_.store(0, std::memory_order_relaxed);
    signed_total_frames_.store(0, std::memory_order_relaxed);
    max_single_frames_.store(0, std::memory_order_relaxed);
    for (auto& bucket : buckets_) bucket.store(0, std::memory_order_relaxed);
}

AudioCorrectionDistributionSnapshot AudioCorrectionDistribution::Snapshot() const {
    AudioCorrectionDistributionSnapshot result;
    result.event_count = event_count_.load(std::memory_order_relaxed);
    result.total_magnitude_frames = total_magnitude_frames_.load(
        std::memory_order_relaxed);
    result.signed_total_frames = signed_total_frames_.load(
        std::memory_order_relaxed);
    result.max_single_frames = max_single_frames_.load(std::memory_order_relaxed);
    for (size_t index = 0; index < result.buckets.size(); ++index) {
        result.buckets[index] = buckets_[index].load(std::memory_order_relaxed);
    }
    return result;
}

AvSyncTimelineEvidence EvaluateAvSyncTimeline(
    double selected_window_start_s, double selected_window_end_s,
    MediaTimelineRange video, MediaTimelineRange audio,
    bool generation_continuous) {
    AvSyncTimelineEvidence result;
    result.generation_continuous = generation_continuous;
    if (!generation_continuous || !video.valid || !audio.valid
            || selected_window_end_s <= selected_window_start_s
            || video.end_s < video.start_s || audio.end_s < audio.start_s) {
        return result;
    }
    result.valid = true;
    result.video_duration_s = video.end_s - video.start_s;
    result.audio_duration_s = audio.end_s - audio.start_s;
    result.video_start_relative_to_window_ms =
        (video.start_s - selected_window_start_s) * 1000.0;
    result.audio_start_relative_to_window_ms =
        (audio.start_s - selected_window_start_s) * 1000.0;
    result.video_end_relative_to_window_ms =
        (video.end_s - selected_window_end_s) * 1000.0;
    result.audio_end_relative_to_window_ms =
        (audio.end_s - selected_window_end_s) * 1000.0;
    result.av_start_delta_ms = (audio.start_s - video.start_s) * 1000.0;
    result.av_end_delta_ms = (audio.end_s - video.end_s) * 1000.0;
    return result;
}

uint64_t CurrentAudioTimeline100ns() {
#ifdef _WIN32
    LARGE_INTEGER counter{};
    LARGE_INTEGER frequency{};
    if (!QueryPerformanceCounter(&counter) || !QueryPerformanceFrequency(&frequency)
            || frequency.QuadPart <= 0) {
        return 0;
    }
    const uint64_t ticks = static_cast<uint64_t>(counter.QuadPart);
    const uint64_t ticks_per_second = static_cast<uint64_t>(frequency.QuadPart);
    return (ticks / ticks_per_second) * 10'000'000ULL
        + ((ticks % ticks_per_second) * 10'000'000ULL) / ticks_per_second;
#else
    return 0;
#endif
}

AudioTimelinePacketAdjustment ReconcileAudioTimelinePacket(
    uint64_t next_timeline_100ns, uint64_t packet_qpc_100ns,
    uint32_t input_frames, uint32_t input_sample_rate,
    uint32_t canonical_sample_rate, uint32_t max_synthetic_gap_seconds) {
    AudioTimelinePacketAdjustment result;
    if (packet_qpc_100ns == 0 || input_sample_rate == 0
            || canonical_sample_rate == 0) return result;
    const uint64_t packet_duration = (static_cast<uint64_t>(input_frames)
        * 10'000'000ULL) / input_sample_rate;
    const uint64_t packet_end = packet_qpc_100ns + packet_duration;
    if (next_timeline_100ns == 0) {
        result.next_timeline_100ns = packet_end;
        return result;
    }
    if (packet_qpc_100ns > next_timeline_100ns) {
        result.gap_100ns = packet_qpc_100ns - next_timeline_100ns;
        result.timing_error_100ns = result.gap_100ns >
                static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
            ? std::numeric_limits<int64_t>::max()
            : static_cast<int64_t>(result.gap_100ns);
        if (result.gap_100ns <= kAudioTimelineNormalJitterTolerance100ns) {
            // Keep the real packet boundary.  A sub-millisecond endpoint
            // scheduling error must not become literal zero samples in the
            // waveform; the persistent resampler handles the slow clock
            // component without repeatedly editing speech/transient edges.
            result.classification = AudioTimelineAdjustmentClass::NormalJitterAbsorbed;
            result.normal_jitter_absorbed = true;
            result.next_timeline_100ns = packet_end;
            return result;
        }
        const uint64_t max_gap_100ns = max_synthetic_gap_seconds >
                std::numeric_limits<uint64_t>::max() / 10'000'000ULL
            ? std::numeric_limits<uint64_t>::max()
            : static_cast<uint64_t>(max_synthetic_gap_seconds) * 10'000'000ULL;
        if (result.gap_100ns > max_gap_100ns) {
            result.classification = AudioTimelineAdjustmentClass::LargeGap;
            result.hard_discontinuity = true;
            result.large_gap = true;
            // Do not advance the cursor on a gap that the caller cannot
            // represent with bounded synthetic audio. Providers must fail or
            // restart the source; advancing here would collapse media time and
            // encode the resumed packet at an earlier AAC PTS.
            result.next_timeline_100ns = next_timeline_100ns;
            return result;
        }
        const uint64_t gap_frames = (result.gap_100ns * canonical_sample_rate)
            / 10'000'000ULL;
        const uint64_t maximum = static_cast<uint64_t>(max_synthetic_gap_seconds)
            * canonical_sample_rate;
        if (gap_frames > maximum) {
            result.classification = AudioTimelineAdjustmentClass::LargeGap;
            result.hard_discontinuity = true;
            result.large_gap = true;
            result.next_timeline_100ns = next_timeline_100ns;
            return result;
        }
        result.silence_frames = static_cast<uint32_t>(std::min<uint64_t>(
            gap_frames, std::numeric_limits<uint32_t>::max()));
        result.classification = AudioTimelineAdjustmentClass::HardGap;
        result.hard_discontinuity = true;
    } else if (packet_qpc_100ns < next_timeline_100ns && input_frames > 0) {
        const uint64_t overlap_100ns = next_timeline_100ns - packet_qpc_100ns;
        result.timing_error_100ns = overlap_100ns >
                static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
            ? std::numeric_limits<int64_t>::min()
            : -static_cast<int64_t>(overlap_100ns);
        if (overlap_100ns <= kAudioTimelineNormalJitterTolerance100ns) {
            result.classification = AudioTimelineAdjustmentClass::NormalJitterAbsorbed;
            result.normal_jitter_absorbed = true;
            result.next_timeline_100ns = std::max(next_timeline_100ns, packet_end);
            return result;
        }
        const uint64_t overlap_frames = overlap_100ns >= packet_duration
            ? input_frames
            : (overlap_100ns * input_sample_rate) / 10'000'000ULL;
        result.skip_input_frames = static_cast<uint32_t>(std::min<uint64_t>(
            overlap_frames, input_frames));
        result.classification = AudioTimelineAdjustmentClass::HardOverlap;
        result.hard_discontinuity = true;
    }
    result.next_timeline_100ns = std::max(next_timeline_100ns, packet_end);
    return result;
}

AudioSourcePresentationRange MapAudioSourcePresentationRange(
    double presentation_start_qpc_s, double presentation_end_qpc_s,
    uint64_t source_timeline_origin_100ns, uint32_t sample_rate) {
    AudioSourcePresentationRange result;
    if (source_timeline_origin_100ns == 0 || sample_rate == 0
            || presentation_end_qpc_s <= presentation_start_qpc_s) {
        return result;
    }
    const double origin_s = static_cast<double>(source_timeline_origin_100ns) / 10'000'000.0;
    result.start_pts_samples = static_cast<int64_t>(std::llround(
        (presentation_start_qpc_s - origin_s) * sample_rate));
    result.end_pts_samples = std::max<int64_t>(result.start_pts_samples + 1,
        static_cast<int64_t>(std::llround(
            (presentation_end_qpc_s - origin_s) * sample_rate)));
    return result;
}

int64_t AudioSamplePositionToTimeline100ns(
    uint64_t source_timeline_origin_100ns, int64_t sample_position,
    uint32_t sample_rate) {
    if (source_timeline_origin_100ns == 0 || sample_rate == 0) return 0;

    constexpr int64_t kTimelineUnitsPerSecond = 10'000'000;
    const int64_t whole_seconds = sample_position / static_cast<int64_t>(sample_rate);
    const int64_t remaining_samples = sample_position % static_cast<int64_t>(sample_rate);
    if (whole_seconds > std::numeric_limits<int64_t>::max() / kTimelineUnitsPerSecond) {
        return std::numeric_limits<int64_t>::max();
    }
    if (whole_seconds < std::numeric_limits<int64_t>::min() / kTimelineUnitsPerSecond) {
        return 0;
    }
    const int64_t whole_delta = whole_seconds * kTimelineUnitsPerSecond;
    const int64_t remainder_delta =
        (remaining_samples * kTimelineUnitsPerSecond)
            / static_cast<int64_t>(sample_rate);
    if (remainder_delta > 0
            && whole_delta > std::numeric_limits<int64_t>::max() - remainder_delta) {
        return std::numeric_limits<int64_t>::max();
    }
    if (remainder_delta < 0
            && whole_delta < std::numeric_limits<int64_t>::min() - remainder_delta) {
        return 0;
    }
    const int64_t delta = whole_delta + remainder_delta;

    if (delta >= 0) {
        const uint64_t positive_delta = static_cast<uint64_t>(delta);
        const uint64_t maximum = static_cast<uint64_t>(
            std::numeric_limits<int64_t>::max());
        if (source_timeline_origin_100ns > maximum - positive_delta) {
            return std::numeric_limits<int64_t>::max();
        }
        return static_cast<int64_t>(source_timeline_origin_100ns + positive_delta);
    }

    const uint64_t negative_delta = static_cast<uint64_t>(-(delta + 1)) + 1;
    if (negative_delta >= source_timeline_origin_100ns) return 0;
    const uint64_t result = source_timeline_origin_100ns - negative_delta;
    const uint64_t maximum = static_cast<uint64_t>(
        std::numeric_limits<int64_t>::max());
    return result > maximum
        ? std::numeric_limits<int64_t>::max()
        : static_cast<int64_t>(result);
}

AudioDriftController::AudioDriftController(uint32_t sample_rate, uint32_t max_ppm)
    : sample_rate_(std::max<uint32_t>(1, sample_rate)),
      max_ppm_(std::clamp<uint32_t>(max_ppm, 1, 500)) {}

AudioDriftCorrection AudioDriftController::Observe(uint64_t qpc_100ns,
                                                    int64_t submitted_frames) {
    AudioDriftCorrection result;
    if (qpc_100ns == 0 || submitted_frames < 0) return result;
    if (origin_qpc_100ns_ == 0) {
        origin_qpc_100ns_ = qpc_100ns;
        last_adjustment_qpc_100ns_ = qpc_100ns;
        return result;
    }
    if (qpc_100ns < origin_qpc_100ns_) return result;

    const int64_t expected_frames = static_cast<int64_t>(std::llround(
        static_cast<double>(qpc_100ns - origin_qpc_100ns_) * sample_rate_ / 10'000'000.0));
    result.observed_drift_samples = expected_frames - submitted_frames;
    max_observed_drift_samples_ = std::max(max_observed_drift_samples_,
        std::llabs(result.observed_drift_samples));

    constexpr uint64_t kMinimumAdjustmentInterval100ns = 5'000'000;  // 500 ms
    if (qpc_100ns - last_adjustment_qpc_100ns_ < kMinimumAdjustmentInterval100ns
            || std::llabs(result.observed_drift_samples) <= 1) {
        return result;
    }

    const uint64_t elapsed_100ns = qpc_100ns - last_adjustment_qpc_100ns_;
    const uint64_t distance = std::max<uint64_t>(sample_rate_ / 2,
        (elapsed_100ns * sample_rate_) / 10'000'000ULL);
    const int64_t maximum_delta = std::max<int64_t>(1,
        static_cast<int64_t>((distance * max_ppm_) / 1'000'000ULL));
    const int64_t clamped_delta = std::clamp(result.observed_drift_samples,
        -maximum_delta, maximum_delta);
    result.sample_delta = static_cast<int32_t>(std::clamp<int64_t>(clamped_delta,
        std::numeric_limits<int32_t>::min(), std::numeric_limits<int32_t>::max()));
    result.compensation_distance_samples = static_cast<uint32_t>(std::min<uint64_t>(
        distance, std::numeric_limits<uint32_t>::max()));
    last_adjustment_qpc_100ns_ = qpc_100ns;
    return result;
}

void AudioDriftController::Reset() {
    origin_qpc_100ns_ = 0;
    last_adjustment_qpc_100ns_ = 0;
    max_observed_drift_samples_ = 0;
}

}  // namespace fthr
