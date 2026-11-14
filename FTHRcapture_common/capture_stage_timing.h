#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>

namespace fthr {

struct CaptureStagePercentiles {
    uint64_t samples = 0;
    int64_t p95_us = 0;
    int64_t p99_us = 0;
};

// Bounded interval histogram. A producer increments one bucket per sample,
// and the diagnostic thread takes each count exactly once.
class CaptureStageTiming {
public:
    using Clock = std::chrono::steady_clock;

    void RecordSince(Clock::time_point start) noexcept {
        Record(std::chrono::duration_cast<std::chrono::microseconds>(
            Clock::now() - start).count());
    }

    void Record(int64_t elapsed_us) noexcept {
        const std::size_t index = elapsed_us <= 0 ? 0
            : elapsed_us <= kFineBuckets * kFineStep
                ? static_cast<std::size_t>((elapsed_us - 1) / kFineStep)
                : elapsed_us <= MaxUs()
                    ? kFineBuckets + static_cast<std::size_t>(
                        (elapsed_us - kFineBuckets * kFineStep - 1) / kCoarseStep)
                    : kBuckets - 1;
        counts_[index].fetch_add(1, std::memory_order_relaxed);
    }

    CaptureStagePercentiles Take() noexcept {
        std::array<uint64_t, kBuckets> counts{};
        CaptureStagePercentiles result;
        for (std::size_t i = 0; i < counts.size(); ++i) {
            counts[i] = counts_[i].exchange(0, std::memory_order_relaxed);
            result.samples += counts[i];
        }
        if (!result.samples) return result;
        const uint64_t rank95 = result.samples - result.samples / 20;
        const uint64_t rank99 = result.samples - result.samples / 100;
        uint64_t cumulative = 0;
        for (std::size_t i = 0; i < counts.size(); ++i) {
            cumulative += counts[i];
            if (cumulative < rank95) continue;
            if (result.p95_us == 0) result.p95_us = BucketUpperUs(i);
            if (cumulative >= rank99) {
                result.p99_us = BucketUpperUs(i);
                break;
            }
        }
        return result;
    }

private:
    static constexpr int64_t kFineStep = 250;
    static constexpr int64_t kFineBuckets = 64;
    static constexpr int64_t kCoarseStep = 1000;
    static constexpr std::size_t kBuckets = 128;

    static constexpr int64_t MaxUs() noexcept {
        return kFineBuckets * kFineStep + (kBuckets - kFineBuckets - 1) * kCoarseStep;
    }

    static constexpr int64_t BucketUpperUs(std::size_t index) noexcept {
        if (index == kBuckets - 1) return -1; // above the bounded histogram range
        if (index < kFineBuckets) return (index + 1) * kFineStep;
        return kFineBuckets * kFineStep + (index - kFineBuckets + 1) * kCoarseStep;
    }

    std::array<std::atomic<uint64_t>, kBuckets> counts_{};
};

} // namespace fthr
