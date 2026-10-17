#pragma once

// Small, platform-neutral diagnostics policy for the capture -> encoder ->
// replay-ring pipeline.  This header deliberately has no Windows, FFmpeg,
// or shared-memory dependency so failure classification can be tested with
// synthetic counters on every build host.
//
// WGC publishes frames when the desktop changes.  A quiet desktop can
// therefore legitimately produce cached-image observations while the replay
// cadence and encoder continue.  WGC_SOURCE_STALE_CANDIDATE is diagnostic
// evidence only; it must never trigger automatic recovery by itself.

#include <cmath>
#include <cstdint>

namespace fthr::capture_pipeline_health {

inline constexpr int64_t kEncoderOutputStallThresholdMs = 2000;
inline constexpr int64_t kWgcSourceStaleThresholdMs = 5000;

enum class Classification {
    Healthy,
    DegradedCaptureCadence,
    AwaitingFirstSource,
    IntentionalPauseRecovery,
    WgcSourceStaleCandidate,
    DxgiImageIdle,
    CaptureSourceStallCandidate,
    CaptureToEncoderStallCandidate,
    EncoderOutputStallCandidate,
    ReplayRingRejectionCandidate,
    BackendFailure,
};

inline const char* ClassificationName(Classification value) noexcept {
    switch (value) {
    case Classification::Healthy:
        return "HEALTHY";
    case Classification::DegradedCaptureCadence:
        return "DEGRADED_CAPTURE_CADENCE";
    case Classification::AwaitingFirstSource:
        return "AWAITING_FIRST_SOURCE";
    case Classification::IntentionalPauseRecovery:
        return "INTENTIONAL_PAUSE_RECOVERY";
    case Classification::WgcSourceStaleCandidate:
        return "WGC_SOURCE_STALE_CANDIDATE";
    case Classification::DxgiImageIdle:
        return "DXGI_IMAGE_IDLE";
    case Classification::CaptureSourceStallCandidate:
        return "CAPTURE_SOURCE_STALL_CANDIDATE";
    case Classification::CaptureToEncoderStallCandidate:
        return "CAPTURE_TO_ENCODER_STALL_CANDIDATE";
    case Classification::EncoderOutputStallCandidate:
        return "ENCODER_OUTPUT_STALL_CANDIDATE";
    case Classification::ReplayRingRejectionCandidate:
        return "REPLAY_RING_REJECTION_CANDIDATE";
    case Classification::BackendFailure:
        return "BACKEND_FAILURE";
    }
    return "UNKNOWN";
}

inline bool IsDiagnosticCandidate(Classification value) noexcept {
    switch (value) {
    case Classification::Healthy:
    case Classification::AwaitingFirstSource:
    case Classification::IntentionalPauseRecovery:
    case Classification::DxgiImageIdle:
        return false;
    default:
        return true;
    }
}

struct Snapshot {
    bool backend_failed = false;
    bool intentional_pause_or_recovery = false;
    bool wgc_backend = false;
    double expected_fps = 0.0;
    int64_t observation_interval_ms = 0;
    uint64_t source_frames = 0;
    // Raw DXGI AcquireNextFrame successes in this observation interval. This
    // is deliberately separate from accepted_source_frame_delta: the latter
    // is after FTHR pacing/scheduler policy, while this value describes the
    // cadence offered by the duplication API. WGC does not use this field for
    // classification because it owns a cached latest frame instead.
    uint64_t raw_source_frame_delta = 0;
    uint64_t accepted_source_frame_delta = 0;
    uint64_t pacing_drop_frames = 0;
    uint64_t interval_pacing_drops = 0;
    uint64_t interval_recovery_attempts = 0;
    uint64_t interval_generation_resets = 0;
    uint64_t repeated_frame_observations = 0;
    uint64_t encoder_submissions = 0;
    uint64_t encoded_packets = 0;
    uint64_t replay_ring_insertions = 0;
    int64_t source_age_ms = 0;
    int64_t encoder_submission_age_ms = 0;
    int64_t encoded_output_age_ms = 0;
    int64_t replay_ring_insertion_age_ms = 0;
    // DXGI held presentations count outputs made from the owned image without
    // a new image-bearing acquire during this observation interval. WGC also
    // uses repeated_frame_observations for its unchanged-image sampler.
    uint64_t held_presentation_observations = 0;
    // A completed API call is separate from acquire-attempt progress: a call
    // that is blocked inside AcquireNextFrame is not live completion evidence.
    bool api_completion_recent = false;
    bool dxgi_replay_cache_armed = false;
    bool acquire_loop_recent = false;
    bool wait_timeout_current = false;
    bool pointer_only_recent = false;
    bool idle_evidence = false;
    bool encoder_idle = false;
    bool has_pending_work = false;
};

inline uint64_t SaturatingDifference(
    uint64_t minuend, uint64_t subtrahend) noexcept {
    return minuend > subtrahend ? minuend - subtrahend : 0;
}

inline Classification Classify(const Snapshot& snapshot) noexcept {
    // The order is part of the diagnostic contract: a known terminal/failure
    // state wins over transient counters, followed by intentional gaps, then
    // source/encoder/ring evidence from the narrowest boundary outward.
    if (snapshot.backend_failed) {
        return Classification::BackendFailure;
    }
    if (snapshot.intentional_pause_or_recovery) {
        return Classification::IntentionalPauseRecovery;
    }
    if (snapshot.source_frames == 0) {
        return Classification::AwaitingFirstSource;
    }

    const uint64_t ring_rejections = SaturatingDifference(
        snapshot.encoded_packets, snapshot.replay_ring_insertions);
    const bool output_current = snapshot.encoded_output_age_ms
        <= kEncoderOutputStallThresholdMs;
    if (ring_rejections > 0
        && output_current
        && snapshot.replay_ring_insertion_age_ms
            > kEncoderOutputStallThresholdMs) {
        return Classification::ReplayRingRejectionCandidate;
    }

    const bool submission_current = snapshot.encoder_submission_age_ms
        <= kEncoderOutputStallThresholdMs;
    const bool source_current = snapshot.source_age_ms
        <= kEncoderOutputStallThresholdMs;
    if (snapshot.wgc_backend
        && snapshot.repeated_frame_observations > 0
        && snapshot.source_age_ms > kWgcSourceStaleThresholdMs
        && snapshot.encoder_submissions > 0
        && submission_current && output_current) {
        // Static WGC content is a candidate, not proof of a capture fault.
        return Classification::WgcSourceStaleCandidate;
    }
    const bool static_dxgi = !snapshot.wgc_backend
        && !source_current && !submission_current && !output_current;
    const double interval_seconds = static_cast<double>(
        snapshot.observation_interval_ms) / 1000.0;
    const double expected_frames = snapshot.expected_fps * interval_seconds;
    const bool cadence_observable = std::isfinite(snapshot.expected_fps)
        && snapshot.expected_fps > 0.0
        && snapshot.observation_interval_ms >= 5000
        && std::isfinite(expected_frames)
        && expected_frames > 0.0;
    const bool repeated_recovery = cadence_observable
        && (snapshot.interval_recovery_attempts >= 2
            || snapshot.interval_generation_resets >= 2);
    // A repeated recovery/reset interval is degraded evidence even when the
    // source clocks are old. This prevents the intentional static-DXGI
    // exemption from hiding repeated topology/capture instability. Preserve
    // narrower capture/encoder boundary classifications when their clocks are
    // currently failing.
    if (repeated_recovery
        && (static_dxgi || (submission_current && output_current))) {
        return Classification::DegradedCaptureCadence;
    }
    // Held packets must not make a recently changing, under-target DXGI
    // source appear healthy. Unlike the all-old static-image exception below,
    // this catches accepted source cadence below 90% even when it is sparser
    // than the ordinary moving-source lower bound.
    const bool dxgi_held_subtarget_cadence = cadence_observable
        && !snapshot.wgc_backend
        && snapshot.held_presentation_observations > 0
        && source_current && submission_current && output_current
        && static_cast<double>(snapshot.accepted_source_frame_delta)
            < expected_frames * 0.90;
    if (dxgi_held_subtarget_cadence) {
        return Classification::DegradedCaptureCadence;
    }
    const bool dxgi_held_output_current = !snapshot.wgc_backend
        && snapshot.held_presentation_observations > 0
        && !source_current && submission_current && output_current;
    if (dxgi_held_output_current) {
        const bool live_static_api_evidence = snapshot.dxgi_replay_cache_armed
            && snapshot.api_completion_recent
            && snapshot.acquire_loop_recent
            && (snapshot.wait_timeout_current
                || snapshot.pointer_only_recent);
        return live_static_api_evidence
            ? Classification::DxgiImageIdle
            : Classification::CaptureSourceStallCandidate;
    }
    if (static_dxgi) {
        if (snapshot.has_pending_work) {
            return Classification::EncoderOutputStallCandidate;
        }
        if (snapshot.dxgi_replay_cache_armed
            && snapshot.api_completion_recent && snapshot.acquire_loop_recent
            && snapshot.idle_evidence
            && snapshot.encoder_idle) {
            // DXGI reports only changed desktop images. Explicit recent API
            // and encoder-idle evidence makes this an informational static
            // image, never a healthy-flow claim or stall candidate.
            return Classification::DxgiImageIdle;
        }
        return Classification::CaptureSourceStallCandidate;
    }
    // A cadence interval is meaningful only after enough wall time has
    // elapsed to distinguish pacing from a single missed deadline. Static
    // DXGI intervals have already been classified using live API evidence.
    const double offered_frames = static_cast<double>(
        snapshot.accepted_source_frame_delta
        + snapshot.interval_pacing_drops);
    const bool accepted_below_target = static_cast<double>(
        snapshot.accepted_source_frame_delta) < expected_frames * 0.90;
    const bool pacing_drop_evidence = static_cast<double>(
        snapshot.interval_pacing_drops) >= expected_frames * 0.05;
    // A moving DXGI source can be below the requested cadence without any
    // scheduler drops: the display/desktop compositor may simply offer fewer
    // distinct frames than the configured target.  Classify that evidence
    // separately from a static desktop.  The lower bound avoids treating one
    // occasional desktop update as an active cadence failure.
    const bool active_dxgi_subtarget_cadence = !snapshot.wgc_backend
        && static_cast<double>(snapshot.raw_source_frame_delta)
            >= expected_frames * 0.10
        && static_cast<double>(snapshot.raw_source_frame_delta)
            < expected_frames * 0.90
        && accepted_below_target
        && source_current && submission_current && output_current;
    const bool cadence_degraded = cadence_observable
        && (((offered_frames >= expected_frames * 0.95
              && accepted_below_target)
             // A bursty DXGI source can offer fewer than 95% of nominal
             // frames while still proving that active presents are being
             // rejected by pacing. A quiet/static desktop has zero pacing
             // drops; its separate idle classification requires live API and
             // encoder-idle evidence.
             || (accepted_below_target && pacing_drop_evidence))
            || active_dxgi_subtarget_cadence
            || snapshot.interval_recovery_attempts >= 2
            || snapshot.interval_generation_resets >= 2);
    if (cadence_degraded && output_current && submission_current) {
        return Classification::DegradedCaptureCadence;
    }
    // WGC samples its owned cached image at cadence, so an old source age
    // does not excuse missing/old submissions there. DXGI with all-old ages
    // has already been separated into idle or source-stall evidence above.
    if ((source_current || snapshot.wgc_backend)
        && (snapshot.encoder_submissions == 0 || !submission_current)) {
        return Classification::CaptureToEncoderStallCandidate;
    }
    if (!output_current) {
        return Classification::EncoderOutputStallCandidate;
    }
    return Classification::Healthy;
}

} // namespace fthr::capture_pipeline_health
