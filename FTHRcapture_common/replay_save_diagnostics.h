#pragma once

// Bounded, platform-neutral policy data for replay-save diagnostics.  This
// header deliberately contains no logging or capture behavior.  The native
// save boundary supplies the current counters and the ring supplies its
// snapshot metadata; the policy only classifies evidence already observed.

#include "replay_health.h"
#include "replay_interval.h"

#include <cstdint>

namespace fthr::replay_save {

enum class SnapshotOutcome {
    Unknown,
    EmptyRing,
    NoEligiblePackets,
    MissingStartKeyframe,
    RetryExhaustedGenerationOrSlotRace,
    Valid,
};

inline const char* SnapshotOutcomeName(SnapshotOutcome value) noexcept {
    switch (value) {
    case SnapshotOutcome::Unknown: return "UNKNOWN";
    case SnapshotOutcome::EmptyRing: return "EMPTY_RING";
    case SnapshotOutcome::NoEligiblePackets: return "NO_ELIGIBLE_PACKETS";
    case SnapshotOutcome::MissingStartKeyframe: return "MISSING_START_KEYFRAME";
    case SnapshotOutcome::RetryExhaustedGenerationOrSlotRace:
        return "RETRY_EXHAUSTED_GENERATION_OR_SLOT_RACE";
    case SnapshotOutcome::Valid: return "VALID";
    }
    return "UNKNOWN";
}

enum class Classification {
    NotReadyAfterStart,
    InsufficientRetainedHistory,
    MissingStartKeyframe,
    VideoTimestampGap,
    VideoTimestampRegression,
    CaptureStarvation,
    EncoderStarvation,
    ReplaySnapshotRace,
    OtherProvenReason,
};

inline const char* ClassificationName(Classification value) noexcept {
    switch (value) {
    case Classification::NotReadyAfterStart: return "NOT_READY_AFTER_START";
    case Classification::InsufficientRetainedHistory:
        return "INSUFFICIENT_RETAINED_HISTORY";
    case Classification::MissingStartKeyframe: return "MISSING_START_KEYFRAME";
    case Classification::VideoTimestampGap: return "VIDEO_TIMESTAMP_GAP";
    case Classification::VideoTimestampRegression:
        return "VIDEO_TIMESTAMP_REGRESSION";
    case Classification::CaptureStarvation: return "CAPTURE_STARVATION";
    case Classification::EncoderStarvation: return "ENCODER_STARVATION";
    case Classification::ReplaySnapshotRace: return "REPLAY_SNAPSHOT_RACE";
    case Classification::OtherProvenReason: return "OTHER_PROVEN_REASON";
    }
    return "OTHER_PROVEN_REASON";
}

struct SnapshotEvidence {
    SnapshotOutcome outcome = SnapshotOutcome::Unknown;
};

struct RuntimeEvidence {
    bool capture_running = false;
    int64_t now_qpc = 0;
    int64_t qpc_frequency = 0;
    int64_t capture_start_qpc = 0;
    int64_t generation_start_qpc = 0;
    int64_t last_source_frame_qpc = 0;
    int64_t last_encoder_submission_qpc = 0;
    int64_t last_encoded_output_qpc = 0;
    int64_t last_ring_insertion_qpc = 0;
    uint64_t source_frames = 0;
    uint64_t encoder_submissions = 0;
    uint64_t encoded_packets = 0;
    uint64_t ring_insertions = 0;
    uint64_t encoder_failures = 0;
    uint32_t capture_generation = 0;
    uint32_t capture_restart_count = 0;
};

inline double AgeSeconds(int64_t now_qpc, int64_t event_qpc,
                         int64_t qpc_frequency) noexcept {
    if (now_qpc <= 0 || event_qpc <= 0 || qpc_frequency <= 0
        || now_qpc < event_qpc) return -1.0;
    return static_cast<double>(now_qpc - event_qpc)
        / static_cast<double>(qpc_frequency);
}

inline Classification Classify(
    replay_health::Failure failure,
    replay_health::TimelineSubtype timeline_subtype,
    const SnapshotEvidence& snapshot,
    const RuntimeEvidence& runtime,
    double requested_duration_s) noexcept {
    if (snapshot.outcome == SnapshotOutcome::MissingStartKeyframe) {
        return Classification::MissingStartKeyframe;
    }
    if (snapshot.outcome == SnapshotOutcome::RetryExhaustedGenerationOrSlotRace) {
        return Classification::ReplaySnapshotRace;
    }
    if (failure == replay_health::Failure::GenerationDiscontinuity) {
        return Classification::ReplaySnapshotRace;
    }
    if (failure == replay_health::Failure::LargeTimelineGap) {
        return Classification::VideoTimestampGap;
    }
    if (failure == replay_health::Failure::InvalidTimeline) {
        if (timeline_subtype == replay_health::TimelineSubtype::WallRegressionOrDuplicate
            || timeline_subtype == replay_health::TimelineSubtype::PtsRegressionOrDuplicate) {
            return Classification::VideoTimestampRegression;
        }
    }
    if (failure == replay_health::Failure::InsufficientCoverage) {
        const double generation_age = AgeSeconds(
            runtime.now_qpc, runtime.generation_start_qpc,
            runtime.qpc_frequency);
        if (generation_age >= 0.0 && generation_age < requested_duration_s) {
            return Classification::NotReadyAfterStart;
        }
        return Classification::InsufficientRetainedHistory;
    }
    if (failure == replay_health::Failure::StaleEnd
        || failure == replay_health::Failure::LowEffectiveRate) {
        const double source_age = AgeSeconds(
            runtime.now_qpc, runtime.last_source_frame_qpc,
            runtime.qpc_frequency);
        const double output_age = AgeSeconds(
            runtime.now_qpc, runtime.last_encoded_output_qpc,
            runtime.qpc_frequency);
        const double submission_age = AgeSeconds(
            runtime.now_qpc, runtime.last_encoder_submission_qpc,
            runtime.qpc_frequency);
        if (source_age >= 1.0 && (output_age < 0.0 || source_age > output_age)) {
            return Classification::CaptureStarvation;
        }
        if (runtime.encoder_submissions > runtime.encoded_packets
            && output_age >= 1.0
            && (submission_age < 0.0 || submission_age < output_age)) {
            return Classification::EncoderStarvation;
        }
    }
    if (snapshot.outcome == SnapshotOutcome::EmptyRing
        || snapshot.outcome == SnapshotOutcome::NoEligiblePackets) {
        const double generation_age = AgeSeconds(
            runtime.now_qpc, runtime.generation_start_qpc,
            runtime.qpc_frequency);
        if (generation_age >= 0.0 && generation_age < requested_duration_s) {
            return Classification::NotReadyAfterStart;
        }
        return Classification::OtherProvenReason;
    }
    return Classification::OtherProvenReason;
}

inline Classification Classify(
    replay_health::Failure failure,
    const SnapshotEvidence& snapshot,
    const RuntimeEvidence& runtime,
    double requested_duration_s) noexcept {
    return Classify(failure, replay_health::TimelineSubtype::None,
                    snapshot, runtime, requested_duration_s);
}

} // namespace fthr::replay_save
