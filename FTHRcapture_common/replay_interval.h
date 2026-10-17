#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace fthr::replay_interval {

struct Sample {
    int64_t timestamp = 0;
    bool keyframe = false;
};

struct Selection {
    bool valid = false;
    bool full_history = false;
    size_t decode_start = 0;
    size_t presentation_start = 0;
    size_t end = 0;
    int64_t requested_start = 0;
    int64_t target_end = 0;
    int64_t media_end = 0;
    enum class Outcome {
        Valid,
        EmptyInput,
        NoPacketAtTarget,
        MissingStartKeyframe,
    };
    Outcome outcome = Outcome::EmptyInput;
};

struct FixedRateSelection {
    uint64_t decode_start = 0;
    size_t frame_count = 0;
    bool full_history = false;
};

// Legacy raw-frame fallback only: its encoder emits exactly one CFR packet per
// selected BGRA frame and the ring has no per-frame clock. This count model is
// therefore an output-duration guarantee, not a claim about retained wall time.
inline FixedRateSelection SelectFixedRateFrames(
    uint64_t published_head,
    size_t available_frames,
    size_t requested_frames,
    size_t overwrite_guard_frames) {
    FixedRateSelection result;
    const size_t safe_frames = available_frames > overwrite_guard_frames
        ? available_frames - overwrite_guard_frames
        : available_frames;
    result.frame_count = safe_frames < requested_frames
        ? safe_frames
        : requested_frames;
    result.full_history = result.frame_count == requested_frames;
    const uint64_t distance = static_cast<uint64_t>(result.frame_count)
        + static_cast<uint64_t>(overwrite_guard_frames);
    result.decode_start = published_head >= distance
        ? published_head - distance
        : 0;
    return result;
}

inline Selection Select(
    const std::vector<Sample>& samples,
    int64_t target_end,
    int64_t requested_duration) {
    Selection result;
    if (samples.empty() || requested_duration <= 0) {
        result.outcome = Selection::Outcome::EmptyInput;
        return result;
    }

    size_t end = samples.size();
    while (end > 0 && samples[end - 1].timestamp > target_end) --end;
    if (end == 0) {
        result.outcome = Selection::Outcome::NoPacketAtTarget;
        return result;
    }
    --end;

    const int64_t requested_start = target_end - requested_duration;
    result.requested_start = requested_start;
    result.target_end = target_end;
    size_t keyframe_before = samples.size();
    size_t first_keyframe = samples.size();
    for (size_t i = 0; i <= end; ++i) {
        if (!samples[i].keyframe) continue;
        if (first_keyframe == samples.size()) first_keyframe = i;
        if (samples[i].timestamp <= requested_start) keyframe_before = i;
    }
    if (first_keyframe == samples.size()) {
        result.outcome = Selection::Outcome::MissingStartKeyframe;
        return result;
    }

    result.valid = true;
    result.outcome = Selection::Outcome::Valid;
    result.end = end;
    result.media_end = samples[end].timestamp;
    result.full_history = samples.front().timestamp <= requested_start
        && keyframe_before != samples.size();

    if (!result.full_history) {
        result.decode_start = first_keyframe;
        result.presentation_start = first_keyframe;
        return result;
    }

    result.decode_start = keyframe_before;
    result.presentation_start = keyframe_before;
    while (result.presentation_start < end
           && samples[result.presentation_start].timestamp < requested_start) {
        ++result.presentation_start;
    }
    return result;
}

inline int64_t PresentationStartPts(
    int64_t newest_pts,
    int64_t requested_duration_ticks,
    bool full_history,
    int64_t decode_start_pts) {
    if (!full_history || requested_duration_ticks <= 0)
        return decode_start_pts;
    return newest_pts + 1 - requested_duration_ticks;
}

inline int64_t PresentationStartPts(
    int64_t newest_pts,
    int64_t ticks_per_second,
    int64_t requested_seconds,
    bool full_history,
    int64_t decode_start_pts) {
    return PresentationStartPts(
        newest_pts,
        ticks_per_second * requested_seconds,
        full_history,
        decode_start_pts);
}

} // namespace fthr::replay_interval
