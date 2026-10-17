#pragma once

// Pure replay packet health policy shared by the native save boundary and
// deterministic tests.  This intentionally has no FFmpeg, Windows, or ring
// buffer dependency: a caller supplies the already selected packet timeline.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

namespace fthr::replay_health {

struct Sample {
    int64_t wall_qpc = 0;
    int64_t pts = 0;
    uint64_t generation = 0;
};

struct Input {
    std::vector<Sample> packets;
    uint32_t requested_duration_seconds = 0;
    double expected_fps = 0.0;
    int64_t wall_qpc_frequency = 0;
    int64_t target_end_wall_qpc = 0;
    double pts_per_second = 0.0;
    bool full_history = false;
};

enum class Failure {
    None,
    Empty,
    InsufficientPackets,
    InsufficientCoverage,
    StaleEnd,
    GenerationDiscontinuity,
    InvalidTimeline,
    LargeTimelineGap,
    LowEffectiveRate,
};

enum class TimelineSubtype {
    None,
    InvalidClock,
    WallRegressionOrDuplicate,
    PtsRegressionOrDuplicate,
    NonpositiveCoverage,
};

inline const char* TimelineSubtypeName(TimelineSubtype subtype) noexcept {
    switch (subtype) {
    case TimelineSubtype::None: return "NONE";
    case TimelineSubtype::InvalidClock: return "INVALID_CLOCK";
    case TimelineSubtype::WallRegressionOrDuplicate:
        return "WALL_REGRESSION_OR_DUPLICATE";
    case TimelineSubtype::PtsRegressionOrDuplicate:
        return "PTS_REGRESSION_OR_DUPLICATE";
    case TimelineSubtype::NonpositiveCoverage: return "NONPOSITIVE_COVERAGE";
    }
    return "NONE";
}

inline const char* FailureName(Failure failure) noexcept {
    switch (failure) {
    case Failure::None: return "NONE";
    case Failure::Empty: return "EMPTY";
    case Failure::InsufficientPackets: return "INSUFFICIENT_PACKETS";
    case Failure::InsufficientCoverage: return "INSUFFICIENT_COVERAGE";
    case Failure::StaleEnd: return "STALE_END";
    case Failure::GenerationDiscontinuity: return "GENERATION_DISCONTINUITY";
    case Failure::InvalidTimeline: return "INVALID_TIMELINE";
    case Failure::LargeTimelineGap: return "LARGE_TIMELINE_GAP";
    case Failure::LowEffectiveRate: return "LOW_EFFECTIVE_RATE";
    }
    return "UNKNOWN";
}

struct Report {
    bool accepted = false;
    Failure failure = Failure::Empty;
    size_t packet_count = 0;
    double requested_duration_s = 0.0;
    double actual_coverage_s = 0.0;
    double largest_gap_s = 0.0;
    double largest_wall_gap_s = 0.0;
    double largest_pts_gap_s = 0.0;
    int64_t largest_wall_gap_start_qpc = 0;
    int64_t largest_wall_gap_end_qpc = 0;
    int64_t largest_pts_gap_start = 0;
    int64_t largest_pts_gap_end = 0;
    double effective_packet_rate = 0.0;
    bool effective_packet_rate_measured = false;
    int64_t first_wall_qpc = 0;
    int64_t last_wall_qpc = 0;
    int64_t first_pts = 0;
    int64_t last_pts = 0;
    uint64_t generation = 0;
    bool generation_continuous = false;
    bool full_history = false;
    bool partial_history = false;
    double wall_coverage_s = 0.0;
    double required_min_coverage_s = 0.0;
    double max_allowed_gap_s = 0.0;
    size_t intervals_over_gap_threshold = 0;
    double minimum_effective_rate = 0.0;
    double end_staleness_s = 0.0;
    double allowed_end_staleness_s = 1.0;
    bool wall_monotonic = true;
    bool pts_monotonic = true;
    TimelineSubtype timeline_subtype = TimelineSubtype::None;
};

inline Report Evaluate(const Input& input) noexcept {
    Report report;
    report.packet_count = input.packets.size();
    report.requested_duration_s =
        static_cast<double>(input.requested_duration_seconds);
    report.full_history = input.full_history;
    report.partial_history = !input.full_history;

    if (input.packets.empty()) {
        report.failure = Failure::Empty;
        return report;
    }
    report.first_pts = input.packets.front().pts;
    report.last_pts = input.packets.back().pts;
    report.generation = input.packets.front().generation;
    report.generation_continuous = true;
    for (const auto& packet : input.packets) {
        if (packet.generation != report.generation) {
            report.generation_continuous = false;
            report.failure = Failure::GenerationDiscontinuity;
            return report;
        }
    }
    if (input.packets.size() < 2
        || !std::isfinite(input.expected_fps)
        || input.expected_fps <= 0.0
        || input.expected_fps > 1000.0) {
        report.failure = Failure::InsufficientPackets;
        return report;
    }

    const bool use_qpc = input.wall_qpc_frequency > 0
        && input.packets.front().wall_qpc > 0;
    if (use_qpc) {
        report.first_wall_qpc = input.packets.front().wall_qpc;
        report.last_wall_qpc = input.packets.back().wall_qpc;
    }
    const double units_per_second = use_qpc
        ? static_cast<double>(input.wall_qpc_frequency)
        : (input.pts_per_second > 0.0
            ? input.pts_per_second
            : static_cast<double>(input.expected_fps));
    if (!(units_per_second > 0.0) || !std::isfinite(units_per_second)) {
        report.failure = Failure::InvalidTimeline;
        report.timeline_subtype = TimelineSubtype::InvalidClock;
        return report;
    }

    int64_t previous_wall_qpc = input.packets.front().wall_qpc;
    double previous = use_qpc
        ? static_cast<double>(previous_wall_qpc)
        : static_cast<double>(input.packets.front().pts);
    if (!std::isfinite(previous)) {
        report.failure = Failure::InvalidTimeline;
        report.timeline_subtype = TimelineSubtype::InvalidClock;
        return report;
    }
    const double pts_units_per_second = input.pts_per_second > 0.0
        ? input.pts_per_second
        : static_cast<double>(input.expected_fps);
    if (!(pts_units_per_second > 0.0)
        || !std::isfinite(pts_units_per_second)) {
        report.failure = Failure::InvalidTimeline;
        report.timeline_subtype = TimelineSubtype::InvalidClock;
        return report;
    }
    int64_t previous_pts_value = input.packets.front().pts;
    double previous_pts = static_cast<double>(previous_pts_value);
    if (!std::isfinite(previous_pts)) {
        report.failure = Failure::InvalidTimeline;
        report.timeline_subtype = TimelineSubtype::InvalidClock;
        return report;
    }
    report.max_allowed_gap_s = std::max(0.250, 4.0 / input.expected_fps);
    report.minimum_effective_rate = input.expected_fps * 0.8;
    for (size_t index = 1; index < input.packets.size(); ++index) {
        const auto& packet = input.packets[index];
        const int64_t current_wall_qpc = packet.wall_qpc;
        const double current = use_qpc
            ? static_cast<double>(current_wall_qpc)
            : static_cast<double>(packet.pts);
        const double gap_units = current - previous;
        if (!(gap_units > 0.0) || !std::isfinite(current)) {
            report.wall_monotonic = false;
            report.timeline_subtype = TimelineSubtype::WallRegressionOrDuplicate;
            report.failure = Failure::InvalidTimeline;
            return report;
        }
        const double gap_seconds = gap_units / units_per_second;
        if (use_qpc && gap_seconds > report.largest_wall_gap_s) {
            report.largest_wall_gap_s = gap_seconds;
            report.largest_wall_gap_start_qpc = previous_wall_qpc;
            report.largest_wall_gap_end_qpc = current_wall_qpc;
        }
        previous_wall_qpc = current_wall_qpc;
        previous = current;

        const int64_t current_pts_value = packet.pts;
        const double current_pts = static_cast<double>(current_pts_value);
        const double pts_gap_units = current_pts - previous_pts;
        if (!(pts_gap_units > 0.0) || !std::isfinite(current_pts)) {
            report.pts_monotonic = false;
            report.timeline_subtype = TimelineSubtype::PtsRegressionOrDuplicate;
            report.failure = Failure::InvalidTimeline;
            return report;
        }
        const double pts_gap_seconds = pts_gap_units / pts_units_per_second;
        if (pts_gap_seconds > report.largest_pts_gap_s) {
            report.largest_pts_gap_s = pts_gap_seconds;
            report.largest_pts_gap_start = previous_pts_value;
            report.largest_pts_gap_end = current_pts_value;
        }
        if (gap_seconds > report.max_allowed_gap_s
            || pts_gap_seconds > report.max_allowed_gap_s) {
            ++report.intervals_over_gap_threshold;
        }
        previous_pts_value = current_pts_value;
        previous_pts = current_pts;
    }
    // A healthy capture must have both a monotonic wall-clock progression and
    // a monotonic media timeline. A QPC-only check can otherwise hide a
    // malformed encoder PTS stream with nominal wall timing.
    report.largest_gap_s = std::max(
        report.largest_wall_gap_s, report.largest_pts_gap_s);
    const double wall_coverage_s =
        (previous - (use_qpc
            ? static_cast<double>(input.packets.front().wall_qpc)
            : static_cast<double>(input.packets.front().pts)))
        / units_per_second;
    const double media_coverage_s =
        (previous_pts - static_cast<double>(input.packets.front().pts))
        / pts_units_per_second;
    if (!(wall_coverage_s > 0.0) || !std::isfinite(wall_coverage_s)
        || !(media_coverage_s > 0.0) || !std::isfinite(media_coverage_s)) {
        report.timeline_subtype = TimelineSubtype::NonpositiveCoverage;
        report.failure = Failure::InvalidTimeline;
        return report;
    }
    report.wall_coverage_s = wall_coverage_s;
    // During warm-up or after a restart, save the available healthy history.
    // A full-history selection must still substantiate its claimed duration.
    report.required_min_coverage_s = input.full_history
        ? std::max(0.5, report.requested_duration_s * 0.90
            - 1.0 / input.expected_fps)
        : 0.5;
    // Coverage is a media-time property. Wall-clock coverage remains useful
    // for stale-end and gap diagnostics, but must not allow a short PTS
    // history to be published as a full-duration clip.
    report.actual_coverage_s = media_coverage_s;
    const double effective_packet_rate =
        static_cast<double>(input.packets.size() - 1)
        / report.actual_coverage_s;
    if (std::isfinite(effective_packet_rate)) {
        report.effective_packet_rate = effective_packet_rate;
        report.effective_packet_rate_measured = true;
    }
    if (use_qpc && input.target_end_wall_qpc > 0
        && input.target_end_wall_qpc >= report.last_wall_qpc) {
        const double end_staleness_s = static_cast<double>(
            input.target_end_wall_qpc - report.last_wall_qpc)
            / units_per_second;
        report.end_staleness_s = end_staleness_s;
        if (end_staleness_s > 1.0) {
            report.failure = Failure::StaleEnd;
            return report;
        }
    }
    // Partial clips need at least half a second of real media. All timeline,
    // freshness, generation and cadence checks still apply.
    if (report.actual_coverage_s < report.required_min_coverage_s) {
        report.failure = Failure::InsufficientCoverage;
        return report;
    }

    // A short scheduling hiccup is recoverable.  A gap above 250 ms (or four
    // nominal frame periods for unusually low configured FPS) is evidence
    // that the selected replay cannot be published as a continuous clip.
    const double max_bounded_gap_s =
        std::max(0.250, 4.0 / input.expected_fps);
    if (report.largest_gap_s > max_bounded_gap_s) {
        report.failure = Failure::LargeTimelineGap;
        return report;
    }

    // Once the requested interval is established, a sustained rate below 80%
    // of the configured cadence is not a valid native replay. This rejects a
    // 120/144-fps request carrying only a 60-fps source while retaining the
    // observed 51-56-fps cadence range for a 60-fps request.
    if (report.effective_packet_rate < input.expected_fps * 0.8) {
        report.failure = Failure::LowEffectiveRate;
        return report;
    }
    report.accepted = true;
    report.failure = Failure::None;
    return report;
}

} // namespace fthr::replay_health
