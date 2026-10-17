#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>

namespace fthr {

// Optional compact clock for video-only focus-gated generations. Audio-enabled
// generations keep raw QPC until every source/correlation boundary can share
// compact coordinates. See ShouldUseCompactReplayPauseClock.
class ReplayPauseClock {
public:
    explicit ReplayPauseClock(uint32_t retention_seconds)
        : retention_(static_cast<int64_t>(retention_seconds + 10) * 10'000'000) {}

    void Observe(bool paused, int64_t now) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (paused && !pause_start_) pause_start_ = now;
        if (!paused && pause_start_) {
            spans_.push_back({pause_start_, std::max(pause_start_, now)});
            pause_start_ = 0;
        }
        const auto active_now = MapLocked(now);
        while (!spans_.empty()
                && MapLocked(spans_.front().end) < active_now - retention_) {
            removed_ += spans_.front().end - spans_.front().start;
            spans_.pop_front();
        }
    }

    int64_t Map(int64_t timestamp) const {
        std::lock_guard<std::mutex> lock(mutex_);
        return MapLocked(timestamp);
    }

    int64_t MapQpc(int64_t timestamp, int64_t frequency) const {
        return Scale(Map(Scale(timestamp, 10'000'000, frequency)),
                     frequency, 10'000'000);
    }

    // Drop whole AAC packets touching a pause, including buffered encoder
    // output delivered after focus returns. Keep the remaining samples aligned
    // to video on the same compact clock.
    std::optional<int64_t> MapAudio(int64_t raw_origin, int64_t replay_origin,
                                    int64_t pts, int64_t duration, uint32_t rate) const {
        const auto start = raw_origin + Scale(pts, 10'000'000, rate);
        const auto end = raw_origin + Scale(pts + duration, 10'000'000, rate);
        std::lock_guard<std::mutex> lock(mutex_);
        if (pause_start_ && end > pause_start_) return std::nullopt;
        for (const auto& span : spans_) {
            if (start < span.end && end > span.start) return std::nullopt;
        }
        return Scale(MapLocked(start) - replay_origin, rate, 10'000'000);
    }

    static int64_t Scale(int64_t value, int64_t numerator, int64_t denominator) {
        return denominator > 0
            ? static_cast<int64_t>(std::llround(
                static_cast<long double>(value) * numerator / denominator)) : 0;
    }

private:
    struct Span { int64_t start, end; };
    int64_t MapLocked(int64_t timestamp) const {
        auto removed = removed_;
        for (const auto& span : spans_) {
            removed += std::clamp(timestamp - span.start, int64_t{0}, span.end - span.start);
        }
        if (pause_start_) removed += std::max(int64_t{0}, timestamp - pause_start_);
        return timestamp - removed;
    }
    int64_t retention_;
    int64_t removed_ = 0;
    int64_t pause_start_ = 0;
    std::deque<Span> spans_;
    mutable std::mutex mutex_;
};

} // namespace fthr
