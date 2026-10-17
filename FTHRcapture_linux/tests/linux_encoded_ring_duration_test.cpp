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

fthr::EncodedPacket SizedPacket(int64_t pts, int64_t wall_time_ns,
                                bool keyframe, size_t bytes) {
    fthr::EncodedPacket packet = Packet(pts, wall_time_ns, keyframe);
    packet.data.assign(bytes, 0x01);
    return packet;
}

void TestByteBudgetDropsWholeGops() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    constexpr int gop = 60;                     // keyframe every 2 s
    constexpr size_t key_bytes = 50'000;
    constexpr size_t delta_bytes = 5'000;
    constexpr size_t gop_bytes = key_bytes + (gop - 1) * delta_bytes;  // 345 kB
    constexpr size_t budget = 10 * gop_bytes + gop_bytes / 2;
    // 905 s window: only the byte budget can bind here.
    fthr::EncodedRingBuffer ring(905'000, fps, budget);

    size_t pushed = 0;
    for (int frame = 0; frame < 60 * fps; ++frame) {
        const bool key = frame % gop == 0;
        const size_t bytes = key ? key_bytes : delta_bytes;
        ring.Push(SizedPacket(frame, frame * second / fps, key, bytes));
        pushed += bytes;
        // Never over budget once a second GOP exists, and never empty.
        if (frame >= gop) assert(ring.ByteCount() <= budget);
        assert(ring.PacketCount() > 0);
    }
    assert(pushed > budget);
    assert(ring.BudgetTrimCount() > 0);
    // The budget holds ten whole GOPs plus the GOP being filled, at most.
    assert(ring.ByteCount() <= budget);
    assert(ring.ByteCount() >= 10 * gop_bytes);
    assert(ring.SpanNs() < 22 * second);

    // Whatever was dropped, a long save still starts on a keyframe and ends
    // at the newest packet; it reports the shorter history honestly.
    const auto snapshot = ring.TakeSnapshot(300'000, 0);
    assert(!snapshot.packets.empty());
    assert(snapshot.packets.front().is_keyframe);
    assert(!snapshot.full_history);
    assert(snapshot.packets.back().pts == 60 * fps - 1);
    assert(snapshot.presentation_start_pts == snapshot.packets.front().pts);
}

void TestByteBudgetNeverDropsTheCurrentGop() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    // One enormous GOP exceeds the budget on its own: it must survive whole,
    // since dropping any of it would leave nothing decodable.
    fthr::EncodedRingBuffer ring(905'000, fps, 1000);
    for (int frame = 0; frame < 90; ++frame)
        ring.Push(SizedPacket(frame, frame * second / fps, frame == 0, 100));
    assert(ring.PacketCount() == 90);
    assert(ring.BudgetTrimCount() == 0);

    // The next keyframe makes the old GOP droppable.
    ring.Push(SizedPacket(90, 90 * second / fps, true, 100));
    assert(ring.PacketCount() == 1);
    assert(ring.ByteCount() == 100);
    assert(ring.BudgetTrimCount() == 1);
    const auto snapshot = ring.TakeSnapshot(30'000, 0);
    assert(snapshot.packets.size() == 1 && snapshot.packets.front().is_keyframe);
}

void TestNoBudgetAndClear() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 30;
    fthr::EncodedRingBuffer unbounded(905'000, fps);
    for (int frame = 0; frame < 600; ++frame)
        unbounded.Push(SizedPacket(frame, frame * second / fps, frame % 60 == 0, 10'000));
    assert(unbounded.ByteCount() == 600u * 10'000u);
    assert(unbounded.BudgetTrimCount() == 0);

    // The time trim keeps the byte count exact too.
    fthr::EncodedRingBuffer timed(1'000, fps);
    for (int frame = 0; frame < 300; ++frame)
        timed.Push(SizedPacket(frame, frame * second / fps, frame % 60 == 0, 7));
    assert(timed.ByteCount() == timed.PacketCount() * 7);

    unbounded.Clear();
    assert(unbounded.ByteCount() == 0 && unbounded.PacketCount() == 0);
    assert(unbounded.SpanNs() == 0);
}

} // namespace

int main() {
    TestByteBudgetDropsWholeGops();
    TestByteBudgetNeverDropsTheCurrentGop();
    TestNoBudgetAndClear();
    TestFullTimestampWindowUsesPriorKeyframe();
    TestVariableTimingAndDelayedPublication();
    TestRecoveryClearsOldGeneration();
    TestSnapshotIgnoresOtherGenerations();
    return 0;
}
