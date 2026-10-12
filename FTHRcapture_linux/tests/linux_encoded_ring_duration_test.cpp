#include "ring_buffer.h"

#include <cassert>
#include <chrono>
#include <cstdint>

namespace {

fthr::EncodedPacket Packet(
    int64_t pts,
    int64_t wall_time_ns,
    bool keyframe) {
    return {{0x01}, pts, pts, keyframe, wall_time_ns};
}

void TestFullTimestampWindowUsesPriorKeyframe() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 60;
    fthr::EncodedRingBuffer ring(40'000, fps);

    for (int64_t frame = 0; frame <= 60 * fps; ++frame) {
        ring.Push(Packet(
            frame,
            frame * second / fps,
            frame % (fps * 2) == 0));
    }

    const auto snapshot = ring.TakeSnapshot(30'000, 60 * second);
    assert(snapshot.full_history);
    assert(!snapshot.packets.empty());
    assert(snapshot.packets.front().is_keyframe);
    assert(snapshot.packets.front().wall_time_ns <= 30 * second);
    assert(snapshot.presentation_start_pts == 30 * fps + 1);
    assert(snapshot.packets.back().pts - snapshot.presentation_start_pts + 1
           == 30 * fps);
}

void TestVariableTimingAndDelayedPublication() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 60;
    fthr::EncodedRingBuffer ring(70'000, fps);
    int64_t wall = 0;
    int64_t pts = 0;
    for (int frame = 0; frame < 3000; ++frame) {
        wall += frame % 13 == 0 ? second / 30 : second / 60;
        pts = wall * fps / second;
        ring.Push(Packet(pts, wall, frame % (fps * 2) == 0));
    }

    const int64_t target = wall - second;
    const auto snapshot = ring.TakeSnapshot(30'000, target);
    assert(snapshot.full_history);
    assert(snapshot.packets.back().wall_time_ns <= target);
    assert(snapshot.presentation_end_ns == target);
    assert(snapshot.packets.back().pts - snapshot.presentation_start_pts + 1
           == 30 * fps);
}

void TestRecoveryClearsOldGeneration() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    fthr::EncodedRingBuffer ring(40'000, fps);
    for (int frame = 0; frame <= 30 * fps; ++frame)
        ring.Push(Packet(frame, frame * second / fps, frame % 60 == 0));

    ring.Clear();
    for (int frame = 0; frame <= 5 * fps; ++frame) {
        ring.Push(Packet(
            frame,
            100 * second + frame * second / fps,
            frame % 60 == 0));
    }

    const auto snapshot = ring.TakeSnapshot(30'000, 105 * second);
    assert(!snapshot.full_history);
    assert(snapshot.packets.front().wall_time_ns >= 100 * second);
    assert(snapshot.presentation_start_pts == snapshot.packets.front().pts);
}

void TestSnapshotIgnoresOtherGenerations() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    fthr::EncodedRingBuffer ring(40'000, fps);
    for (int frame = 0; frame <= 10 * fps; ++frame) {
        auto packet = Packet(frame, frame * second / fps, frame % 60 == 0);
        packet.generation = 1;
        ring.Push(packet);
    }
    for (int frame = 0; frame <= 3 * fps; ++frame) {
        auto packet = Packet(frame, 10 * second + frame * second / fps, frame % 60 == 0);
        packet.generation = 2;
        ring.Push(packet);
    }

    // Generation 2 only: three seconds of history, starting at its keyframe.
    const auto newer = ring.TakeSnapshot(30'000, 13 * second, 2);
    assert(!newer.full_history);
    assert(!newer.packets.empty());
    assert(newer.packets.front().generation == 2);
    assert(newer.packets.front().wall_time_ns >= 10 * second);

    // Unknown generation yields nothing rather than mixing extradata.
    const auto none = ring.TakeSnapshot(30'000, 13 * second, 7);
    assert(none.packets.empty());

    // 0 keeps the historical behaviour.
    const auto all = ring.TakeSnapshot(30'000, 13 * second, 0);
    assert(all.packets.front().generation == 1);
}

} // namespace

int main() {
    TestFullTimestampWindowUsesPriorKeyframe();
    TestVariableTimingAndDelayedPublication();
    TestRecoveryClearsOldGeneration();
    TestSnapshotIgnoresOtherGenerations();
    return 0;
}
