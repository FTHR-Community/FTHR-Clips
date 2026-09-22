#include "capture_engine.h"
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

void TestMeasuredMemoryGuardPruning() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 60;
    // Set 60-second time window, but limit memory to 1 MB (1024 * 1024 bytes)
    constexpr size_t max_bytes = 1024 * 1024;
    fthr::EncodedRingBuffer ring(60'000, fps, max_bytes);
    assert(ring.MaxBytes() == max_bytes);

    // Each packet has 64 KB of payload (simulating high-bitrate video stream)
    std::vector<uint8_t> payload(64 * 1024, 0xAA);
    // Push 30 packets = 30 * 64 KB = 1920 KB (> 1 MB budget)
    for (int frame = 0; frame < 30; ++frame) {
        fthr::EncodedPacket pkt;
        pkt.data = payload;
        pkt.pts = frame;
        pkt.dts = frame;
        pkt.is_keyframe = (frame % 5 == 0);
        pkt.wall_time_ns = frame * second / fps;
        ring.Push(std::move(pkt));
        // Hard resource guard invariant: total bytes must NEVER exceed max_bytes
        assert(ring.TotalBytes() <= max_bytes);
    }

    // At 64 KB per packet, a 1 MB limit can retain at most 16 packets
    assert(ring.PacketCount() <= 16);
    assert(ring.TotalBytes() <= max_bytes);

    // Snapshot should still work correctly and align to a keyframe within retained packets
    const auto snapshot = ring.TakeSnapshot(30'000, 29 * second / fps);
    assert(!snapshot.packets.empty());
    assert(snapshot.packets.front().is_keyframe);
    // Earlier packets were pruned strictly by the measured byte limit, not duration
    assert(snapshot.packets.front().pts > 0);
}

void TestMeasuredMemoryGuardUnderBudget() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 60;
    constexpr size_t max_bytes = 10 * 1024 * 1024; // 10 MB budget
    fthr::EncodedRingBuffer ring(2'000, fps, max_bytes); // 2-second time window

    std::vector<uint8_t> payload(1024, 0xBB); // 1 KB each
    // Push 3 seconds of frames (180 frames = 180 KB, far below 10 MB)
    for (int frame = 0; frame < 180; ++frame) {
        fthr::EncodedPacket pkt;
        pkt.data = payload;
        pkt.pts = frame;
        pkt.dts = frame;
        pkt.is_keyframe = (frame % 30 == 0);
        pkt.wall_time_ns = frame * second / fps;
        ring.Push(std::move(pkt));
    }

    // Since memory was well within budget, pruning was purely duration-based
    assert(ring.TotalBytes() <= max_bytes);
    assert(ring.PacketCount() >= 115 && ring.PacketCount() <= 125);
}

void TestDynamicMemoryLimitReduction() {
    constexpr int64_t second = 1'000'000'000LL;
    constexpr int fps = 60;
    fthr::EncodedRingBuffer ring(60'000, fps, 10 * 1024 * 1024);

    std::vector<uint8_t> payload(100 * 1024, 0xCC); // 100 KB
    for (int frame = 0; frame < 20; ++frame) {
        fthr::EncodedPacket pkt;
        pkt.data = payload;
        pkt.pts = frame;
        pkt.dts = frame;
        pkt.is_keyframe = (frame % 5 == 0);
        pkt.wall_time_ns = frame * second / fps;
        ring.Push(std::move(pkt));
    }
    assert(ring.TotalBytes() == 20 * 100 * 1024);

    // Dynamically clamp memory limit to 500 KB (5 packets)
    ring.SetMaxBytes(500 * 1024);
    assert(ring.TotalBytes() <= 500 * 1024);
    assert(ring.PacketCount() == 5);
}

void TestReconfigurePreservesMemoryLimit() {
    fthr::CaptureConfig cfg;
    cfg.buffer_seconds = 30;
    cfg.fps = 60;
    cfg.max_buffer_mb = 2048;
    cfg.audio_enabled = false;

    // Verify initializing sets ring_ max_bytes
    fthr::CaptureEngine engine;
    assert(engine.Initialize(cfg));
    assert(engine.GetRingBuffer() != nullptr);
    assert(engine.GetRingBuffer()->MaxBytes() == 2048ULL * 1024ULL * 1024ULL);

    // Reconfigure codec/preset
    engine.Reconfigure(static_cast<uint32_t>(fthr::CodecPref::H264), 4);
    assert(engine.GetRingBuffer() != nullptr);
    assert(engine.GetRingBuffer()->MaxBytes() == 2048ULL * 1024ULL * 1024ULL);
}

} // namespace

int main() {
    TestFullTimestampWindowUsesPriorKeyframe();
    TestVariableTimingAndDelayedPublication();
    TestRecoveryClearsOldGeneration();
    TestMeasuredMemoryGuardPruning();
    TestMeasuredMemoryGuardUnderBudget();
    TestDynamicMemoryLimitReduction();
    TestReconfigurePreservesMemoryLimit();
    return 0;
}
