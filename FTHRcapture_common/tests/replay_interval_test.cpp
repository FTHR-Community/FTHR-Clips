#include "replay_interval.h"

#include <cassert>
#include <cstdint>
#include <vector>

namespace replay = fthr::replay_interval;

namespace {

constexpr int64_t kSecond = 1'000'000'000LL;

std::vector<replay::Sample> ConstantRate(
    int fps,
    int seconds,
    int keyframe_interval_frames,
    int64_t epoch = 0) {
    std::vector<replay::Sample> samples;
    for (int frame = 0; frame <= fps * seconds; ++frame) {
        samples.push_back({
            epoch + frame * kSecond / fps,
            frame % keyframe_interval_frames == 0,
        });
    }
    return samples;
}

void AssertFullWindow(
    const std::vector<replay::Sample>& samples,
    int64_t target_end,
    int64_t duration,
    int64_t frame_tolerance) {
    const auto selection = replay::Select(samples, target_end, duration);
    assert(selection.valid);
    assert(selection.full_history);
    assert(samples[selection.decode_start].keyframe);
    assert(samples[selection.decode_start].timestamp <= selection.requested_start);
    assert(samples[selection.presentation_start].timestamp >= selection.requested_start);
    assert(samples[selection.presentation_start].timestamp - selection.requested_start
           <= frame_tolerance);
    assert(selection.media_end <= target_end);
    assert(target_end - selection.media_end <= frame_tolerance);
}

void TestNormalDurationsAndFrameRates() {
    for (const int fps : {30, 60, 120}) {
        const auto samples = ConstantRate(fps, 120, fps * 2);
        AssertFullWindow(samples, 60 * kSecond, 30 * kSecond, kSecond / fps);
    }

    const auto long_samples = ConstantRate(60, 120, 120);
    AssertFullWindow(long_samples, 120 * kSecond, 60 * kSecond, kSecond / 60);
    AssertFullWindow(long_samples, 77 * kSecond, 17 * kSecond, kSecond / 60);
}

void TestKeyframeBoundaries() {
    const int fps = 60;
    const auto long_gop = ConstantRate(fps, 70, fps * 4);

    const auto exact = replay::Select(
        long_gop, 60 * kSecond, 28 * kSecond);  // start = 32s keyframe
    assert(long_gop[exact.decode_start].timestamp == 32 * kSecond);

    const auto after = replay::Select(
        long_gop, 60 * kSecond, 28 * kSecond - kSecond / fps);
    assert(long_gop[after.decode_start].timestamp == 32 * kSecond);
    assert(long_gop[after.decode_start].timestamp < after.requested_start);

    const auto before_next = replay::Select(
        long_gop, 60 * kSecond, 24 * kSecond + kSecond / fps);
    assert(long_gop[before_next.decode_start].timestamp == 32 * kSecond);
    assert(long_gop[before_next.decode_start].timestamp < before_next.requested_start);

    const auto short_gop = ConstantRate(fps, 70, fps / 2);
    AssertFullWindow(short_gop, 60 * kSecond, 30 * kSecond, kSecond / fps);
}

void TestInsufficientAndResetHistory() {
    const auto five_seconds = ConstantRate(60, 5, 120);
    const auto partial = replay::Select(
        five_seconds, 5 * kSecond, 30 * kSecond);
    assert(partial.valid);
    assert(!partial.full_history);
    assert(partial.decode_start == partial.presentation_start);
    assert(five_seconds[partial.decode_start].keyframe);

    const auto startup = ConstantRate(60, 1, 120);
    const auto early = replay::Select(startup, kSecond, 30 * kSecond);
    assert(early.valid);
    assert(!early.full_history);

    const auto post_recovery = ConstantRate(60, 5, 120, 100 * kSecond);
    const auto recovered = replay::Select(
        post_recovery, 105 * kSecond, 30 * kSecond);
    assert(recovered.valid);
    assert(!recovered.full_history);
    assert(recovered.requested_start == 75 * kSecond);
    assert(post_recovery[recovered.decode_start].timestamp >= 100 * kSecond);
}

void TestIrregularTimestampsAndNonZeroEpoch() {
    std::vector<replay::Sample> variable;
    int64_t timestamp = 50 * kSecond;
    for (int i = 0; i < 2400; ++i) {
        const int64_t delta = (i % 11 == 0) ? kSecond / 30 : kSecond / 60;
        timestamp += delta;
        variable.push_back({timestamp, i % 120 == 0});
    }
    const int64_t target = variable.back().timestamp;
    AssertFullWindow(variable, target, 30 * kSecond, kSecond / 30);

    auto dropped = ConstantRate(60, 65, 120, 200 * kSecond);
    dropped.erase(dropped.begin() + 59 * 60 + 17);
    AssertFullWindow(
        dropped, 260 * kSecond, 30 * kSecond, 2 * kSecond / 60);

    // Encoder publication may be delayed, but capture timestamps remain the
    // authority. A packet after the target must not move the save end.
    auto delayed = ConstantRate(60, 61, 120, 300 * kSecond);
    const auto selection = replay::Select(
        delayed, 360 * kSecond, 30 * kSecond);
    assert(selection.media_end == 360 * kSecond);
    assert(delayed[selection.end + 1].timestamp > 360 * kSecond);
}

void TestRingAndSaveBoundaries() {
    const auto exact = ConstantRate(60, 30, 120);
    const auto exact_selection = replay::Select(
        exact, 30 * kSecond, 30 * kSecond);
    assert(exact_selection.valid);
    assert(exact_selection.full_history);

    const auto slightly_more = ConstantRate(60, 31, 120);
    AssertFullWindow(
        slightly_more, 31 * kSecond, 30 * kSecond, kSecond / 60);

    // Represents the ordered live portion of a wrapped ring.
    const auto wrapped = ConstantRate(60, 40, 120, 500 * kSecond);
    AssertFullWindow(
        wrapped, 540 * kSecond, 30 * kSecond, kSecond / 60);

    // Simulate a packet arriving during admission: target_end still bounds it.
    auto arriving = ConstantRate(60, 31, 120);
    arriving.push_back({31 * kSecond + kSecond / 60, false});
    const auto bounded = replay::Select(
        arriving, 31 * kSecond, 30 * kSecond);
    assert(bounded.end + 1 < arriving.size());
    assert(bounded.media_end == 31 * kSecond);
}

void TestRapidSavesAreIndependentAndNonMutating() {
    const auto samples = ConstantRate(60, 65, 120);
    const auto original = samples;
    const auto first = replay::Select(samples, 60 * kSecond, 30 * kSecond);
    const auto second = replay::Select(samples, 61 * kSecond, 30 * kSecond);

    assert(first.valid && second.valid);
    assert(first.end < second.end);
    assert(samples.size() == original.size());
    for (size_t i = 0; i < samples.size(); ++i) {
        assert(samples[i].timestamp == original[i].timestamp);
        assert(samples[i].keyframe == original[i].keyframe);
    }
}

void TestInvalidInputsAndPresentationOffset() {
    assert(!replay::Select(std::vector<replay::Sample>{}, 10, 5).valid);

    const std::vector<replay::Sample> no_keyframe{
        {0, false}, {1, false}, {2, false},
    };
    assert(!replay::Select(no_keyframe, 2, 1).valid);

    // Exact AUDIT-042 reproduction after adding the missing pre-roll keyframe:
    // physical PTS 140..385, visible interval starts at PTS 236 and is 5s.
    const int64_t offset = replay::PresentationStartPts(
        385, 30, 5, true, 140);
    assert(offset == 236);
    assert(static_cast<double>(385 - offset + 1) / 30 == 5.0);
    assert(replay::PresentationStartPts(25, 30, 30, false, 7) == 7);
}

void TestThirtyMinuteReplayBufferStressAndSelection() {
    constexpr int fps = 60;
    constexpr int duration_seconds = 1800; // 30 minutes
    const auto samples = ConstantRate(fps, duration_seconds + 5, fps * 2);
    AssertFullWindow(
        samples,
        static_cast<int64_t>(duration_seconds) * kSecond,
        static_cast<int64_t>(duration_seconds) * kSecond,
        kSecond / fps);
}

} // namespace

int main() {
    TestNormalDurationsAndFrameRates();
    TestThirtyMinuteReplayBufferStressAndSelection();
    TestKeyframeBoundaries();
    TestInsufficientAndResetHistory();
    TestIrregularTimestampsAndNonZeroEpoch();
    TestRingAndSaveBoundaries();
    TestRapidSavesAreIndependentAndNonMutating();
    TestInvalidInputsAndPresentationOffset();
    return 0;
}
