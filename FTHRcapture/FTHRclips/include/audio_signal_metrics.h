// Bounded, in-memory signal health metrics for one audio pipeline stage.
//
// Metrics are aggregated once per submitted chunk.  No samples are retained,
// written to disk, or exposed outside the diagnostic snapshot.  Every value
// that can be read by another thread is represented by an atomic counter or an
// atomic IEEE-754 bit pattern so a capture thread never needs a diagnostic
// mutex in its hot path.

#pragma once
#ifndef FTHR_AUDIO_SIGNAL_METRICS_H
#define FTHR_AUDIO_SIGNAL_METRICS_H

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace fthr {

struct AudioSignalMetricsSnapshot {
    uint64_t chunks = 0;
    uint64_t frames = 0;
    uint64_t samples = 0;
    uint64_t finite_samples = 0;
    uint32_t channels = 0;
    double peak = 0.0;
    double rms = 0.0;
    double dc_offset = 0.0;
    uint64_t clipping_samples = 0;
    double clipping_ratio = 0.0;
    uint64_t nan_samples = 0;
    uint64_t inf_samples = 0;
    uint64_t near_silence_samples = 0;
    double near_silence_ratio = 0.0;
    std::array<double, 2> channel_rms{{0.0, 0.0}};
};

// One instance belongs to one logical stage (for example converter input or
// encoder input).  It is safe for a producer to call Observe while another
// thread calls Snapshot.  The counters are lifetime counters and therefore
// remain bounded in memory regardless of capture duration.
class AudioSignalMetrics {
public:
    static constexpr float kNearSilenceThreshold = 0.0001f;

    AudioSignalMetrics() { Reset(); }

    void Observe(const float* interleaved, uint32_t frames, uint32_t channels) {
        if (!interleaved || frames == 0 || channels == 0) return;

        chunks_.fetch_add(1, std::memory_order_relaxed);
        frames_.fetch_add(frames, std::memory_order_relaxed);
        const uint64_t sample_count = static_cast<uint64_t>(frames) * channels;
        samples_.fetch_add(sample_count, std::memory_order_relaxed);
        channels_.store(channels, std::memory_order_relaxed);

        uint64_t finite = 0;
        uint64_t clipped = 0;
        uint64_t nan = 0;
        uint64_t inf = 0;
        uint64_t near_silence = 0;
        double sum = 0.0;
        double sumsq = 0.0;
        double maximum = 0.0;
        std::array<uint64_t, 2> channel_finite{{0, 0}};
        std::array<double, 2> channel_sum{{0.0, 0.0}};
        std::array<double, 2> channel_sumsq{{0.0, 0.0}};

        for (uint32_t frame = 0; frame < frames; ++frame) {
            for (uint32_t channel = 0; channel < channels; ++channel) {
                const float value = interleaved[
                    static_cast<size_t>(frame) * channels + channel];
                if (std::isnan(value)) {
                    ++nan;
                    continue;
                }
                if (std::isinf(value)) {
                    ++inf;
                    ++clipped;
                    continue;
                }
                const double sample = static_cast<double>(value);
                const double absolute = std::abs(sample);
                ++finite;
                if (absolute >= 1.0) ++clipped;
                if (absolute <= kNearSilenceThreshold) ++near_silence;
                maximum = std::max(maximum, absolute);
                sum += sample;
                sumsq += sample * sample;
                if (channel < 2) {
                    ++channel_finite[channel];
                    channel_sum[channel] += sample;
                    channel_sumsq[channel] += sample * sample;
                }
            }
        }

        finite_samples_.fetch_add(finite, std::memory_order_relaxed);
        clipping_samples_.fetch_add(clipped, std::memory_order_relaxed);
        nan_samples_.fetch_add(nan, std::memory_order_relaxed);
        inf_samples_.fetch_add(inf, std::memory_order_relaxed);
        near_silence_samples_.fetch_add(near_silence, std::memory_order_relaxed);
        AddDouble(sum_, sum);
        AddDouble(sum_squares_, sumsq);
        MaxDouble(peak_, maximum);
        for (size_t channel = 0; channel < 2; ++channel) {
            if (channel_finite[channel] == 0) continue;
            channel_finite_samples_[channel].fetch_add(
                channel_finite[channel], std::memory_order_relaxed);
            AddDouble(channel_sum_[channel], channel_sum[channel]);
            AddDouble(channel_sum_squares_[channel], channel_sumsq[channel]);
        }
    }

    AudioSignalMetricsSnapshot Snapshot() const {
        AudioSignalMetricsSnapshot result;
        result.chunks = chunks_.load(std::memory_order_relaxed);
        result.frames = frames_.load(std::memory_order_relaxed);
        result.samples = samples_.load(std::memory_order_relaxed);
        result.finite_samples = finite_samples_.load(std::memory_order_relaxed);
        result.channels = channels_.load(std::memory_order_relaxed);
        result.clipping_samples = clipping_samples_.load(std::memory_order_relaxed);
        result.nan_samples = nan_samples_.load(std::memory_order_relaxed);
        result.inf_samples = inf_samples_.load(std::memory_order_relaxed);
        result.near_silence_samples = near_silence_samples_.load(
            std::memory_order_relaxed);
        const uint64_t finite = result.finite_samples;
        if (finite != 0) {
            result.rms = std::sqrt(std::max(0.0,
                LoadDouble(sum_squares_) / static_cast<double>(finite)));
            result.dc_offset = LoadDouble(sum_) / static_cast<double>(finite);
        }
        result.peak = LoadDouble(peak_);
        if (result.samples != 0) {
            result.clipping_ratio = static_cast<double>(result.clipping_samples)
                / static_cast<double>(result.samples);
            result.near_silence_ratio = static_cast<double>(
                result.near_silence_samples) / static_cast<double>(result.samples);
        }
        for (size_t channel = 0; channel < 2; ++channel) {
            const uint64_t count = channel_finite_samples_[channel].load(
                std::memory_order_relaxed);
            if (count != 0) {
                result.channel_rms[channel] = std::sqrt(std::max(0.0,
                    LoadDouble(channel_sum_squares_[channel])
                    / static_cast<double>(count)));
            }
        }
        return result;
    }

    void Reset() {
        chunks_.store(0, std::memory_order_relaxed);
        frames_.store(0, std::memory_order_relaxed);
        samples_.store(0, std::memory_order_relaxed);
        finite_samples_.store(0, std::memory_order_relaxed);
        channels_.store(0, std::memory_order_relaxed);
        clipping_samples_.store(0, std::memory_order_relaxed);
        nan_samples_.store(0, std::memory_order_relaxed);
        inf_samples_.store(0, std::memory_order_relaxed);
        near_silence_samples_.store(0, std::memory_order_relaxed);
        sum_.store(DoubleBits(0.0), std::memory_order_relaxed);
        sum_squares_.store(DoubleBits(0.0), std::memory_order_relaxed);
        peak_.store(DoubleBits(0.0), std::memory_order_relaxed);
        for (size_t channel = 0; channel < 2; ++channel) {
            channel_finite_samples_[channel].store(0, std::memory_order_relaxed);
            channel_sum_[channel].store(DoubleBits(0.0), std::memory_order_relaxed);
            channel_sum_squares_[channel].store(DoubleBits(0.0),
                std::memory_order_relaxed);
        }
    }

private:
    static uint64_t DoubleBits(double value) {
        uint64_t bits = 0;
        std::memcpy(&bits, &value, sizeof(bits));
        return bits;
    }

    static double BitsDouble(uint64_t bits) {
        double value = 0.0;
        std::memcpy(&value, &bits, sizeof(value));
        return value;
    }

    static void AddDouble(std::atomic<uint64_t>& target, double value) {
        if (value == 0.0) return;
        uint64_t expected = target.load(std::memory_order_relaxed);
        for (;;) {
            const double sum = BitsDouble(expected) + value;
            const uint64_t desired = DoubleBits(sum);
            if (target.compare_exchange_weak(expected, desired,
                    std::memory_order_relaxed, std::memory_order_relaxed)) return;
        }
    }

    static void MaxDouble(std::atomic<uint64_t>& target, double value) {
        uint64_t expected = target.load(std::memory_order_relaxed);
        for (;;) {
            if (BitsDouble(expected) >= value) return;
            const uint64_t desired = DoubleBits(value);
            if (target.compare_exchange_weak(expected, desired,
                    std::memory_order_relaxed, std::memory_order_relaxed)) return;
        }
    }

    static double LoadDouble(const std::atomic<uint64_t>& target) {
        return BitsDouble(target.load(std::memory_order_relaxed));
    }

    std::atomic<uint64_t> chunks_{0};
    std::atomic<uint64_t> frames_{0};
    std::atomic<uint64_t> samples_{0};
    std::atomic<uint64_t> finite_samples_{0};
    std::atomic<uint32_t> channels_{0};
    std::atomic<uint64_t> clipping_samples_{0};
    std::atomic<uint64_t> nan_samples_{0};
    std::atomic<uint64_t> inf_samples_{0};
    std::atomic<uint64_t> near_silence_samples_{0};
    std::atomic<uint64_t> sum_{DoubleBits(0.0)};
    std::atomic<uint64_t> sum_squares_{DoubleBits(0.0)};
    std::atomic<uint64_t> peak_{DoubleBits(0.0)};
    std::array<std::atomic<uint64_t>, 2> channel_finite_samples_{};
    std::array<std::atomic<uint64_t>, 2> channel_sum_{};
    std::array<std::atomic<uint64_t>, 2> channel_sum_squares_{};
};

}  // namespace fthr

#endif  // FTHR_AUDIO_SIGNAL_METRICS_H
