#include "encoded_ring_buffer.h"

#include <cassert>
#include <chrono>
#include <cstdint>

namespace {

void PushRegressionSequence(fthr::EncodedRingBuffer& ring) {
    constexpr uint8_t byte = 0x01;
    constexpr int64_t fps = 30;
    for (int64_t frame = 0; frame < 361; ++frame) {
        ring.Push(
            &byte,
            1,
            frame * 7 / 6,
            frame % (fps * 4) == 0,
            frame * 7);
    }
}

void TestExactAudit042Regression() {
    constexpr uint32_t fps = 30;
    constexpr int64_t qpc_frequency = 180;
    fthr::EncodedRingBuffer ring(1000, fps, qpc_frequency);
    fthr::EncodedVideoConfig config;
    config.width = 1920;
    config.height = 1080;
    config.frame_rate = {30, 1};
    config.time_base = {1, 30};
    assert(ring.SetVideoConfig(config));
    PushRegressionSequence(ring);

    // Frame 330 begins at QPC 2310. The save boundary is its nominal end.
    constexpr int64_t save_qpc = 2316;
    assert(ring.WaitUntilPublished(save_qpc, std::chrono::milliseconds(1)));
    const auto snapshot = ring.TakeSnapshotByTime(5, save_qpc);

    assert(snapshot.full_history);
    assert(snapshot.packets.size() == 211);
    assert(snapshot.packets.front().is_keyframe);
    assert(snapshot.packets.front().pts == 140);
    assert(snapshot.packets.back().pts == 385);
    assert(snapshot.presentation_start_pts == 236);
    assert(static_cast<double>(
        snapshot.packets.back().pts - snapshot.presentation_start_pts + 1) / fps
        == 5.0);
    assert(snapshot.packets.front().pts < snapshot.presentation_start_pts);
    assert(snapshot.media_end_qpc_s <= snapshot.presentation_end_qpc_s);
}

void TestInsufficientHistoryAndRecoveryReset() {
    constexpr uint32_t fps = 60;
    constexpr int64_t qpc_frequency = 6000;
    constexpr uint8_t byte = 0x01;
    fthr::EncodedRingBuffer ring(1000, fps, qpc_frequency);

    for (int64_t frame = 0; frame <= 5 * fps; ++frame) {
        ring.Push(&byte, 1, frame, frame % (fps * 2) == 0, frame * 100);
    }
    auto partial = ring.TakeSnapshotByTime(30, 5 * qpc_frequency);
    assert(!partial.full_history);
    assert(!partial.packets.empty());
    assert(partial.presentation_start_pts == partial.packets.front().pts);

    ring.Clear();
    assert(ring.GetCount() == 0);
    for (int64_t frame = 0; frame <= fps; ++frame) {
        ring.Push(&byte, 1, frame, frame == 0, 100 * qpc_frequency + frame * 100);
    }
    const auto recovered = ring.TakeSnapshotByTime(30, 101 * qpc_frequency);
    assert(!recovered.full_history);
    assert(recovered.packets.front().wall_qpc >= 100 * qpc_frequency);
}

void TestThirtyMinuteReplayBufferStress() {
    constexpr uint32_t fps = 60;
    constexpr int64_t qpc_frequency = 6000;
    constexpr uint8_t byte = 0x01;
    // Capacity for 30 minutes (1800s * 60fps = 108,000 frames)
    fthr::EncodedRingBuffer ring(110000, fps, qpc_frequency);
    fthr::EncodedVideoConfig config;
    config.width = 1920;
    config.height = 1080;
    config.frame_rate = {60, 1};
    config.time_base = {1, 60};
    assert(ring.SetVideoConfig(config));

    constexpr int64_t total_frames = 1800 * fps;
    for (int64_t frame = 0; frame <= total_frames; ++frame) {
        ring.Push(
            &byte,
            1,
            frame,
            frame % (fps * 2) == 0, // keyframe every 2 seconds
            frame * 100);
    }
    const auto snapshot = ring.TakeSnapshotByTime(1800, total_frames * 100);
    assert(snapshot.full_history);
    assert(!snapshot.packets.empty());
    assert(snapshot.packets.front().is_keyframe);
}

} // namespace

int main() {
    TestExactAudit042Regression();
    TestInsufficientHistoryAndRecoveryReset();
    TestThirtyMinuteReplayBufferStress();
    return 0;
}
