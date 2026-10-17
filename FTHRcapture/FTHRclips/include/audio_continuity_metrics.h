// Bounded, lock-free diagnostics for audio timeline continuity repairs.
//
// This type records only aggregate counts and bounded histograms.  It never
// retains PCM or emits one record per packet, so it is safe to update from a
// capture thread and snapshot from the diagnostics thread.

#pragma once
#ifndef FTHR_AUDIO_CONTINUITY_METRICS_H
#define FTHR_AUDIO_CONTINUITY_METRICS_H

#include <array>
#include <atomic>
#include <cstdint>

namespace fthr {

struct AudioContinuityMetricsSnapshot {
    uint64_t inserted_frames = 0;
    uint64_t skipped_frames = 0;
    uint64_t inserted_events = 0;
    uint64_t skipped_events = 0;
    uint32_t max_inserted_frames = 0;
    uint32_t max_skipped_frames = 0;
    std::array<uint64_t, 6> inserted_bucket_counts{};
    std::array<uint64_t, 6> skipped_bucket_counts{};

    uint64_t drift_correction_events = 0;
    int64_t drift_correction_total_samples = 0;
    int64_t max_positive_drift_correction_samples = 0;
    int64_t max_negative_drift_correction_samples = 0;
    uint64_t max_abs_drift_correction_samples = 0;
    std::array<uint64_t, 6> drift_bucket_counts{};
};

class AudioContinuityMetrics {
public:
    // Stable bucket labels used by JSON emitters and tests.  The ranges are
    // inclusive: 1, 2-4, 5-16, 17-48, 49-240, and >240 frames/samples.
    static constexpr size_t kBucketCount = 6;

    void ObserveInserted(uint32_t frames) {
        if (frames == 0) return;
        inserted_frames_.fetch_add(frames, std::memory_order_relaxed);
        inserted_events_.fetch_add(1, std::memory_order_relaxed);
        Max(inserted_max_, frames);
        inserted_buckets_[Bucket(frames)].fetch_add(1, std::memory_order_relaxed);
    }

    void ObserveSkipped(uint32_t frames) {
        if (frames == 0) return;
        skipped_frames_.fetch_add(frames, std::memory_order_relaxed);
        skipped_events_.fetch_add(1, std::memory_order_relaxed);
        Max(skipped_max_, frames);
        skipped_buckets_[Bucket(frames)].fetch_add(1, std::memory_order_relaxed);
    }

    void ObserveDriftCorrection(int32_t sample_delta) {
        if (sample_delta == 0) return;
        drift_events_.fetch_add(1, std::memory_order_relaxed);
        AddSigned(drift_total_, sample_delta);
        const int64_t delta = sample_delta;
        if (delta > 0) MaxSigned(drift_max_positive_, delta);
        if (delta < 0) MinSigned(drift_max_negative_, delta);
        const uint64_t absolute = delta < 0
            ? static_cast<uint64_t>(-(delta + 1)) + 1
            : static_cast<uint64_t>(delta);
        Max(drift_max_abs_, absolute);
        drift_buckets_[Bucket(absolute)].fetch_add(1, std::memory_order_relaxed);
    }

    AudioContinuityMetricsSnapshot Snapshot() const {
        AudioContinuityMetricsSnapshot result;
        result.inserted_frames = inserted_frames_.load(std::memory_order_relaxed);
        result.skipped_frames = skipped_frames_.load(std::memory_order_relaxed);
        result.inserted_events = inserted_events_.load(std::memory_order_relaxed);
        result.skipped_events = skipped_events_.load(std::memory_order_relaxed);
        result.max_inserted_frames = inserted_max_.load(std::memory_order_relaxed);
        result.max_skipped_frames = skipped_max_.load(std::memory_order_relaxed);
        result.drift_correction_events = drift_events_.load(std::memory_order_relaxed);
        result.drift_correction_total_samples = drift_total_.load(std::memory_order_relaxed);
        result.max_positive_drift_correction_samples =
            drift_max_positive_.load(std::memory_order_relaxed);
        result.max_negative_drift_correction_samples =
            drift_max_negative_.load(std::memory_order_relaxed);
        result.max_abs_drift_correction_samples =
            drift_max_abs_.load(std::memory_order_relaxed);
        for (size_t i = 0; i < kBucketCount; ++i) {
            result.inserted_bucket_counts[i] =
                inserted_buckets_[i].load(std::memory_order_relaxed);
            result.skipped_bucket_counts[i] =
                skipped_buckets_[i].load(std::memory_order_relaxed);
            result.drift_bucket_counts[i] =
                drift_buckets_[i].load(std::memory_order_relaxed);
        }
        return result;
    }

    void Reset() {
        inserted_frames_.store(0, std::memory_order_relaxed);
        skipped_frames_.store(0, std::memory_order_relaxed);
        inserted_events_.store(0, std::memory_order_relaxed);
        skipped_events_.store(0, std::memory_order_relaxed);
        inserted_max_.store(0, std::memory_order_relaxed);
        skipped_max_.store(0, std::memory_order_relaxed);
        drift_events_.store(0, std::memory_order_relaxed);
        drift_total_.store(0, std::memory_order_relaxed);
        drift_max_positive_.store(0, std::memory_order_relaxed);
        drift_max_negative_.store(0, std::memory_order_relaxed);
        drift_max_abs_.store(0, std::memory_order_relaxed);
        for (size_t i = 0; i < kBucketCount; ++i) {
            inserted_buckets_[i].store(0, std::memory_order_relaxed);
            skipped_buckets_[i].store(0, std::memory_order_relaxed);
            drift_buckets_[i].store(0, std::memory_order_relaxed);
        }
    }

private:
    static size_t Bucket(uint64_t value) {
        if (value <= 1) return 0;
        if (value <= 4) return 1;
        if (value <= 16) return 2;
        if (value <= 48) return 3;
        if (value <= 240) return 4;
        return 5;
    }

    static void Max(std::atomic<uint64_t>& target, uint64_t value) {
        uint64_t current = target.load(std::memory_order_relaxed);
        while (value > current
                && !target.compare_exchange_weak(current, value,
                    std::memory_order_relaxed, std::memory_order_relaxed)) {}
    }

    static void MaxSigned(std::atomic<int64_t>& target, int64_t value) {
        int64_t current = target.load(std::memory_order_relaxed);
        while (value > current
                && !target.compare_exchange_weak(current, value,
                    std::memory_order_relaxed, std::memory_order_relaxed)) {}
    }

    static void MinSigned(std::atomic<int64_t>& target, int64_t value) {
        int64_t current = target.load(std::memory_order_relaxed);
        while (value < current
                && !target.compare_exchange_weak(current, value,
                    std::memory_order_relaxed, std::memory_order_relaxed)) {}
    }

    static void AddSigned(std::atomic<int64_t>& target, int64_t value) {
        target.fetch_add(value, std::memory_order_relaxed);
    }

    std::atomic<uint64_t> inserted_frames_{0};
    std::atomic<uint64_t> skipped_frames_{0};
    std::atomic<uint64_t> inserted_events_{0};
    std::atomic<uint64_t> skipped_events_{0};
    std::atomic<uint64_t> inserted_max_{0};
    std::atomic<uint64_t> skipped_max_{0};
    std::array<std::atomic<uint64_t>, kBucketCount> inserted_buckets_{};
    std::array<std::atomic<uint64_t>, kBucketCount> skipped_buckets_{};
    std::atomic<uint64_t> drift_events_{0};
    std::atomic<int64_t> drift_total_{0};
    std::atomic<int64_t> drift_max_positive_{0};
    std::atomic<int64_t> drift_max_negative_{0};
    std::atomic<uint64_t> drift_max_abs_{0};
    std::array<std::atomic<uint64_t>, kBucketCount> drift_buckets_{};
};

}  // namespace fthr

#endif  // FTHR_AUDIO_CONTINUITY_METRICS_H
