#pragma once

// Platform-neutral scheduling and replay policy for DXGI's sampled capture
// path. Callers supply QPC values and the result of one zero-timeout acquire;
// this header deliberately has no Windows, COM, or D3D dependency.

#include <algorithm>
#include <cstdint>
#include <limits>

namespace fthr::dxgi_replay {

enum class Observation : uint8_t {
    Image,
    PointerOnly,
    WaitTimeout,
    Failure,
};

struct AcquireSchedule {
    bool due = false;
    int64_t wait_qpc = 0;
    uint64_t tick = 0;
};

struct AcquireResult {
    uint64_t generation = 0;
    int64_t call_start_qpc = 0;
    int64_t call_end_qpc = 0;
    int64_t source_qpc = 0;
    Observation observation = Observation::Failure;
    // Required for image-bearing and pointer-only successes. Timeouts do not
    // own an acquired frame and therefore do not release one.
    bool release_succeeded = false;
    // True only after a valid image has been copied into the owned texture.
    bool cache_copy_succeeded = false;
};

struct Presentation {
    bool present = false;
    bool held = false;
    bool resampled_source_qpc = false;
    int64_t qpc = 0;
};

class Cadence {
public:
    Cadence() = default;
    Cadence(uint32_t frames_per_second, int64_t qpc_frequency) {
        Configure(frames_per_second, qpc_frequency);
    }

    void Configure(uint32_t frames_per_second, int64_t qpc_frequency) noexcept {
        frames_per_second_ = frames_per_second;
        qpc_frequency_ = qpc_frequency;
        period_qpc_ = 0;
        if (frames_per_second_ > 0 && qpc_frequency_ > 0) {
            period_qpc_ = qpc_frequency_ / static_cast<int64_t>(frames_per_second_);
            if (qpc_frequency_ % static_cast<int64_t>(frames_per_second_) != 0) {
                ++period_qpc_;
            }
            period_qpc_ = std::max<int64_t>(period_qpc_, 1);
        }
        ResetSchedule();
        Invalidate();
        last_submitted_qpc_ = 0;
    }

    // A due call consumes one tick token. Polling again before Complete() is
    // rejected, which prevents accidental catch-up or multiple acquisitions
    // for one cadence slot.
    AcquireSchedule Schedule(int64_t now_qpc) noexcept {
        if (period_qpc_ <= 0 || awaiting_completion_) {
            return {};
        }
        if (now_qpc <= 0) {
            Invalidate();
            ResetSchedule();
            return {};
        }
        if (last_schedule_qpc_ > 0 && now_qpc <= last_schedule_qpc_) {
            Invalidate();
            ResetSchedule();
            return {};
        }
        last_schedule_qpc_ = now_qpc;
        if (!deadline_initialized_) {
            next_deadline_qpc_ = now_qpc;
            deadline_initialized_ = true;
        }
        if (now_qpc < next_deadline_qpc_) {
            return {false, next_deadline_qpc_ - now_qpc, 0};
        }

        const int64_t lateness = now_qpc - next_deadline_qpc_;
        const int64_t skipped_slots = lateness / period_qpc_ + 1;
        if (skipped_slots > (std::numeric_limits<int64_t>::max()
                - next_deadline_qpc_) / period_qpc_) {
            Invalidate();
            ResetSchedule();
            return {};
        }
        next_deadline_qpc_ += skipped_slots * period_qpc_;
        if (next_tick_ == std::numeric_limits<uint64_t>::max()) {
            Invalidate();
            ResetSchedule();
            return {};
        }
        pending_tick_ = ++next_tick_;
        awaiting_completion_ = true;
        return {true, 0, pending_tick_};
    }

    Presentation Complete(uint64_t tick, const AcquireResult& result) noexcept {
        Presentation decision;
        if (!awaiting_completion_ || tick == 0 || tick != pending_tick_) {
            Invalidate();
            return decision;
        }
        awaiting_completion_ = false;
        pending_tick_ = 0;

        if (generation_set_ && result.generation != generation_) {
            SetGeneration(result.generation);
            return decision;
        }
        if (!generation_set_) {
            generation_ = result.generation;
            generation_set_ = true;
        }

        const bool success = result.observation != Observation::Failure;
        const int64_t maximum_gap = MaximumApiGapQpc();
        if (!success || result.call_start_qpc <= 0
            || result.call_end_qpc < result.call_start_qpc
            || result.call_end_qpc - result.call_start_qpc > maximum_gap
            || (last_sample_qpc_ > 0
                && result.call_end_qpc <= last_sample_qpc_)) {
            Invalidate();
            return decision;
        }

        if (last_api_completion_qpc_ > 0
            && result.call_start_qpc < last_api_completion_qpc_) {
            Invalidate();
            return decision;
        }
        const bool missed_api_gap = last_api_completion_qpc_ > 0
            && result.call_start_qpc - last_api_completion_qpc_ > maximum_gap;
        if (missed_api_gap) {
            // The old cache is no longer authorized. A real image acquired by
            // this completed call can still begin new history; held frames may
            // not bridge this gap.
            cache_seeded_ = false;
        }

        const int64_t sample_qpc = result.call_end_qpc;
        last_sample_qpc_ = sample_qpc;
        last_api_completion_qpc_ = sample_qpc;

        if (result.observation == Observation::Image) {
            if (!result.release_succeeded || !result.cache_copy_succeeded
                || result.source_qpc <= 0 || result.source_qpc > sample_qpc) {
                Invalidate();
                return decision;
            }
            cache_seeded_ = true;
            last_image_completion_qpc_ = sample_qpc;
            // LastPresentTime may legitimately predate the first sample of a
            // static desktop. This window controls only whether to preserve a
            // source timestamp or use the current sample time for presentation;
            // it is not source-freshness evidence and never rewrites source_qpc.
            const bool source_timestamp_is_old = sample_qpc - result.source_qpc
                > SourceTimestampResampleThresholdQpc();
            if (last_submitted_qpc_ > 0
                && result.source_qpc <= last_submitted_qpc_) {
                // A timestamp that would move an established presentation
                // timeline backward is only a small on-time race when it is
                // still inside the bounded presentation-resampling window.
                // Older timestamps remain a real gap and cannot be smoothed.
                if (source_timestamp_is_old
                    || sample_qpc <= last_submitted_qpc_) {
                    Invalidate();
                    return decision;
                }
                decision = {true, false, true, sample_qpc};
                return decision;
            }
            if (last_submitted_qpc_ == 0 && source_timestamp_is_old) {
                // The first acquired static image may legitimately carry an
                // old LastPresentTime. Admit the valid owned image at the
                // current sample time without changing its source timestamp.
                decision = {true, false, true, sample_qpc};
                return decision;
            }
            decision = {true, false, false, result.source_qpc};
            return decision;
        }

        if (result.observation == Observation::PointerOnly
            && !result.release_succeeded) {
            Invalidate();
            return decision;
        }
        if (last_submitted_qpc_ > 0 && sample_qpc <= last_submitted_qpc_) {
            Invalidate();
            return decision;
        }
        if ((result.observation == Observation::PointerOnly
                || result.observation == Observation::WaitTimeout)
            && cache_seeded_ && sample_qpc > last_submitted_qpc_) {
            if (!missed_api_gap && last_image_completion_qpc_ > 0
                && sample_qpc >= last_image_completion_qpc_
                && sample_qpc - last_image_completion_qpc_
                    >= HoldEligibilityQpc()) {
                decision = {true, true, false, sample_qpc};
                return decision;
            }
        }
        if (result.observation != Observation::PointerOnly
            && result.observation != Observation::WaitTimeout) {
            Invalidate();
        }
        return decision;
    }

    // Call only after the existing encoder path accepted the presentation.
    bool CommitPresentation(int64_t presentation_qpc) noexcept {
        if (presentation_qpc <= 0
            || (last_submitted_qpc_ > 0
                && presentation_qpc <= last_submitted_qpc_)) {
            Invalidate();
            return false;
        }
        last_submitted_qpc_ = presentation_qpc;
        return true;
    }

    // A capture-generation boundary disarms replay reuse but deliberately
    // retains the submitted presentation clock; the encoder epoch continues.
    void SetGeneration(uint64_t generation) noexcept {
        if (!generation_set_ || generation_ != generation) {
            generation_ = generation;
            generation_set_ = true;
            Invalidate();
        }
    }

    void Invalidate() noexcept {
        cache_seeded_ = false;
        last_api_completion_qpc_ = 0;
        last_sample_qpc_ = 0;
        last_image_completion_qpc_ = 0;
        awaiting_completion_ = false;
        pending_tick_ = 0;
    }

    int64_t PeriodQpc() const noexcept { return period_qpc_; }
    int64_t LastSubmittedQpc() const noexcept { return last_submitted_qpc_; }
    int64_t LastImageCompletionQpc() const noexcept {
        return last_image_completion_qpc_;
    }
    bool HasSeed() const noexcept { return cache_seeded_; }

private:
    int64_t MaximumApiGapQpc() const noexcept {
        if (qpc_frequency_ <= 0 || period_qpc_ <= 0) return 0;
        const int64_t quarter_second = qpc_frequency_ / 4
            + (qpc_frequency_ % 4 != 0 ? 1 : 0);
        const int64_t four_periods = period_qpc_ > std::numeric_limits<int64_t>::max() / 4
            ? std::numeric_limits<int64_t>::max()
            : period_qpc_ * 4;
        return std::max(quarter_second, four_periods);
    }

    int64_t SourceTimestampResampleThresholdQpc() const noexcept {
        if (qpc_frequency_ <= 0 || period_qpc_ <= 0) return 0;
        const int64_t one_ms = qpc_frequency_ / 1000
            + (qpc_frequency_ % 1000 != 0 ? 1 : 0);
        const int64_t two_periods = period_qpc_ > std::numeric_limits<int64_t>::max() / 2
            ? std::numeric_limits<int64_t>::max()
            : period_qpc_ * 2;
        return two_periods > std::numeric_limits<int64_t>::max() - one_ms
            ? std::numeric_limits<int64_t>::max()
            : two_periods + one_ms;
    }

    int64_t HoldEligibilityQpc() const noexcept {
        const int64_t three_periods = period_qpc_
                > std::numeric_limits<int64_t>::max() / 3
            ? std::numeric_limits<int64_t>::max()
            : period_qpc_ * 3;
        const int64_t maximum_api_gap = MaximumApiGapQpc();
        const int64_t half_api_gap = maximum_api_gap / 2
            + (maximum_api_gap % 2 != 0 ? 1 : 0);
        // This quiet qualification prevents a steady low-rate source from
        // being mistaken for a static desktop merely because it had a gap
        // between updates. It does not prove intended content is static: a
        // long-lived quiescent display (for example, a slideshow) remains
        // indistinguishable from genuinely static content.
        return std::max(three_periods, half_api_gap);
    }

    void ResetSchedule() noexcept {
        deadline_initialized_ = false;
        next_deadline_qpc_ = 0;
        last_schedule_qpc_ = 0;
        next_tick_ = 0;
        pending_tick_ = 0;
        awaiting_completion_ = false;
    }

    uint32_t frames_per_second_ = 0;
    int64_t qpc_frequency_ = 0;
    int64_t period_qpc_ = 0;
    int64_t next_deadline_qpc_ = 0;
    int64_t last_schedule_qpc_ = 0;
    int64_t last_api_completion_qpc_ = 0;
    int64_t last_sample_qpc_ = 0;
    int64_t last_image_completion_qpc_ = 0;
    int64_t last_submitted_qpc_ = 0;
    uint64_t generation_ = 0;
    uint64_t next_tick_ = 0;
    uint64_t pending_tick_ = 0;
    bool generation_set_ = false;
    bool deadline_initialized_ = false;
    bool awaiting_completion_ = false;
    bool cache_seeded_ = false;
};

} // namespace fthr::dxgi_replay
