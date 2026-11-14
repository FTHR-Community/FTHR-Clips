#include "capture_timeline.h"
#include "encoded_ring_buffer.h"
#include "save_clip_task.h"

#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>

int RunCaptureTimelineTests() {
    int checks = 0;
    const auto check = [&checks](bool condition, const char* message) {
        ++checks;
        if (!condition) {
            std::cerr << "FAILED capture timeline: " << message << std::endl;
            std::exit(1);
        }
    };
    fthr::EncodedRingBuffer ring(512, 60, 600);
    fthr::EncodedVideoConfig config;
    config.width = 1920; config.height = 1080;
    config.frame_rate = {60, 1}; config.time_base = {1, 60};
    check(ring.SetVideoConfig(config), "configure original presentation clock");
    const uint8_t byte = 1;
    for (int64_t frame = 0; frame <= 180; ++frame)
        check(ring.Push(&byte, 1, frame, frame % 30 == 0, 1000 + frame * 10), "publish video");
    auto first = ring.TakeSnapshotByTime(1, 2200);
    auto second = ring.TakeSnapshotByTime(1, 2500);
    check(fthr::IsValidCaptureTimeline(first.capture_timeline), "valid original generation provenance");
    check(first.capture_timeline.generation_id == second.capture_timeline.generation_id,
          "overlap saves share source generation UUID");
    check(first.capture_timeline.start_tick == 61 && first.capture_timeline.end_tick == 121,
          "first interval preserves original PTS with exclusive end");
    check(second.capture_timeline.start_tick == 91 && second.capture_timeline.end_tick == 151,
          "second interval overlaps in original presentation coordinates");
    check(second.capture_timeline.time_base_num == 1 && second.capture_timeline.time_base_den == 60,
          "rational clock retained");
    const auto original = fthr::SerializeCaptureTimeline(first.capture_timeline);
    fthr::SaveClipTask queued;
    queued.encoded_snapshot = std::move(first);
    ring.Clear();
    check(ring.SetVideoConfig(config), "configure recovered generation");
    check(ring.Push(&byte, 1, 200, true, 4000), "publish recovered video");
    const auto recovered = ring.TakeSnapshotByTime(1, 4000);
    check(fthr::IsValidCaptureTimeline(recovered.capture_timeline)
              && recovered.capture_timeline.generation_id != second.capture_timeline.generation_id,
          "clear rotates UUID even when encoder PTS do not reset");
    check(fthr::SerializeCaptureTimeline(queued.encoded_snapshot.capture_timeline) == original,
          "queued task preserves original identity after recovery");
    const auto rational = fthr::MakeCaptureTimeline(
        second.capture_timeline.generation_id, 10, 20, 1001, 60000);
    check(fthr::SerializeCaptureTimeline(rational) ==
          "{\"version\":1,\"generation_id\":\"" + rational.generation_id
          + "\",\"start_tick\":10,\"end_tick\":21,\"time_base_num\":1001,\"time_base_den\":60000}",
          "strict compact six-field serialization");
    check(fthr::SerializeCaptureTimeline({}).empty(), "missing provenance omitted");
    check(!fthr::IsValidCaptureTimeline({"not-a-uuid", 1, 2, 1, 60}), "invalid UUID refused");
    check(!fthr::IsValidCaptureTimeline({rational.generation_id, 2, 2, 1, 60}), "empty range refused");
    check(!fthr::IsValidCaptureTimeline({rational.generation_id, 1, 2, 1, 0}), "invalid timebase refused");
    check(!fthr::IsValidCaptureTimeline(fthr::MakeCaptureTimeline(
        rational.generation_id, 1, std::numeric_limits<int64_t>::max(), 1, 60)), "exclusive end overflow refused");
    check(fthr::NewCaptureGenerationId() != rational.generation_id, "new capture has independent UUID");
    std::cout << "Capture timeline tests: " << checks << " checks passed" << std::endl;
    return checks;
}
