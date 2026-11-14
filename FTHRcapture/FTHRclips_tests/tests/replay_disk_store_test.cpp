#include "replay_disk_store.h"

#include <windows.h>

#include <atomic>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

int checks = 0;

void Check(bool condition, const char* message) {
    ++checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

std::filesystem::path TestRoot() {
    wchar_t buffer[32768]{};
    const DWORD length = GetEnvironmentVariableW(
        L"FTHR_DISK_TEST_ROOT", buffer, static_cast<DWORD>(std::size(buffer)));
    if (length == 0 || length >= std::size(buffer)) return {};
    return std::filesystem::path(buffer, buffer + length);
}

std::filesystem::path MakeOwnedDirectory(const std::filesystem::path& root) {
    LARGE_INTEGER counter{};
    QueryPerformanceCounter(&counter);
    for (unsigned attempt = 0; attempt < 32; ++attempt) {
        const auto directory = root / (
            L"fthr-f14-disk-store-" + std::to_wstring(GetCurrentProcessId())
            + L"-" + std::to_wstring(counter.QuadPart)
            + L"-" + std::to_wstring(attempt));
        if (CreateDirectoryW(directory.wstring().c_str(), nullptr)) return directory;
        if (GetLastError() != ERROR_ALREADY_EXISTS) break;
    }
    return {};
}

bool WriteSentinel(const std::filesystem::path& path) {
    HANDLE handle = CreateFileW(path.wstring().c_str(), GENERIC_WRITE,
        0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return false;
    const char value[] = "keep-me";
    DWORD written = 0;
    const bool ok = WriteFile(handle, value, sizeof(value) - 1, &written, nullptr)
        && written == sizeof(value) - 1;
    CloseHandle(handle);
    return ok;
}

std::string ReadSentinel(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

fthr::replay_disk::ReplaySegment Segment(uint64_t generation,
                                         uint64_t sequence,
                                         uint8_t seed = 1) {
    using namespace fthr::replay_disk;
    ReplaySegment segment;
    segment.generation = generation;
    segment.sequence = sequence;
    segment.start_qpc = static_cast<int64_t>(sequence * 1000);
    segment.end_qpc = segment.start_qpc + 999;
    segment.codec_description = {seed, static_cast<uint8_t>(seed + 1), 0x7f};
    ReplayPacket video;
    video.stream_id = 1;
    video.kind = TrackKind::Video;
    video.pts = 100 + sequence;
    video.dts = 90 + sequence;
    video.duration = 16;
    video.qpc = segment.start_qpc;
    video.keyframe = true;
    video.bytes = {seed, 0x00, 0x01, 0x65, 0x88};
    ReplayPacket audio;
    audio.stream_id = 2;
    audio.kind = TrackKind::Audio;
    audio.pts = 200 + sequence;
    audio.dts = 200 + sequence;
    audio.duration = 20;
    audio.qpc = segment.start_qpc + 20;
    audio.bytes = {seed, 0xa1, 0xb2};
    ReplayPacket camera;
    camera.stream_id = 3;
    camera.kind = TrackKind::Camera;
    camera.pts = 300 + sequence;
    camera.dts = 300 + sequence;
    camera.duration = 16;
    camera.qpc = segment.start_qpc + 30;
    camera.bytes = {seed, 0xc1};
    segment.packets = {std::move(video), std::move(audio), std::move(camera)};
    return segment;
}

bool Equal(const fthr::replay_disk::ReplaySegment& left,
           const fthr::replay_disk::ReplaySegment& right) {
    if (left.generation != right.generation || left.sequence != right.sequence
        || left.start_qpc != right.start_qpc || left.end_qpc != right.end_qpc
        || left.codec_description != right.codec_description
        || left.packets.size() != right.packets.size()) return false;
    for (size_t index = 0; index < left.packets.size(); ++index) {
        const auto& a = left.packets[index];
        const auto& b = right.packets[index];
        if (a.stream_id != b.stream_id || a.kind != b.kind || a.pts != b.pts
            || a.dts != b.dts || a.duration != b.duration || a.qpc != b.qpc
            || a.keyframe != b.keyframe || a.bytes != b.bytes) return false;
    }
    return true;
}

fthr::replay_disk::ReplayDiskStore::Config Config(
    const std::filesystem::path& directory, uint64_t disk, uint64_t segment,
    uint64_t count, uint64_t queue_count = 2) {
    fthr::replay_disk::ReplayDiskStore::Config config;
    config.work_directory = directory;
    config.max_disk_bytes = disk;
    config.max_segment_bytes = segment;
    config.max_segments = count;
    config.max_queue_segments = queue_count;
    return config;
}

struct WriteGate {
    std::mutex mutex;
    std::condition_variable changed;
    bool entered = false;
    bool release = false;
};

std::shared_ptr<fthr::replay_disk::ReplayDiskTestHooks> GatedHooks(
    const std::shared_ptr<WriteGate>& gate,
    const std::atomic<bool>* fail_write = nullptr,
    const std::atomic<bool>* fail_read = nullptr,
    const std::shared_ptr<WriteGate>& close_gate = nullptr,
    std::atomic<int>* fail_close_count = nullptr) {
    using namespace fthr::replay_disk;
    auto hooks = std::make_shared<ReplayDiskTestHooks>();
    hooks->probe = [gate, close_gate](ReplayIoPoint point) {
        const auto& selected = point == ReplayIoPoint::BeforeClose
            ? close_gate : gate;
        if (!selected
            || (point != ReplayIoPoint::BeforeWrite
                && point != ReplayIoPoint::BeforeClose)) return;
        std::unique_lock<std::mutex> lock(selected->mutex);
        selected->entered = true;
        selected->changed.notify_all();
        selected->changed.wait(lock, [&] { return selected->release; });
    };
    hooks->fail = [fail_write, fail_read, fail_close_count](ReplayIoPoint point) {
        if (point == ReplayIoPoint::BeforeWrite && fail_write)
            return fail_write->load();
        if (point == ReplayIoPoint::BeforeRead && fail_read)
            return fail_read->load();
        if (point == ReplayIoPoint::BeforeClose && fail_close_count)
            return fail_close_count->fetch_sub(1) > 0;
        return false;
    };
    return hooks;
}

void WaitForGate(const std::shared_ptr<WriteGate>& gate) {
    std::unique_lock<std::mutex> lock(gate->mutex);
    Check(gate->changed.wait_for(lock, std::chrono::seconds(2),
                                 [&] { return gate->entered; }),
          "writer reaches the deterministic I/O gate");
}

void ReleaseGate(const std::shared_ptr<WriteGate>& gate) {
    {
        std::lock_guard<std::mutex> lock(gate->mutex);
        gate->release = true;
    }
    gate->changed.notify_all();
}

void RoundTripAndSnapshotOutlivesStore(const std::filesystem::path& directory) {
    std::string error;
    auto store = fthr::replay_disk::ReplayDiskStore::Create(
        Config(directory, 64 * 1024, 4096, 8), error);
    Check(store != nullptr && error.empty(), "store opens an existing absolute test directory");
    const uint64_t generation = store->CurrentGeneration();
    auto original = Segment(generation, 1, 0x11);
    const auto submit = store->Submit(std::move(original));
    Check(static_cast<bool>(submit), "complete segment is accepted without producer disk I/O");
    Check(store->Drain(2000), "writer drains a submitted segment");
    const auto stats = store->Stats();
    Check(stats.retained_segments == 1
              && stats.disk_file_count == 1
              && stats.disk_file_bytes == submit.logical_file_bytes,
          "shared disk accounting reflects one complete file");
    auto snapshot = store->SnapshotGeneration(generation);
    Check(snapshot.size() == 1, "snapshot leases the committed GOP");
    std::vector<fthr::replay_disk::ReplaySegment> materialized;
    Check(snapshot.Materialize(materialized, error) && materialized.size() == 1,
        "snapshot reads the complete file outside the state lock");
    Check(Equal(materialized.front(), Segment(generation, 1, 0x11)),
        "codec, A/V/camera records, timestamps, and payloads round-trip unchanged");
    store->ResetGeneration();
    store.reset();
    materialized.clear();
    Check(snapshot.Materialize(materialized, error) && materialized.size() == 1,
        "snapshot remains readable after reset and store destruction");
    Check(Equal(materialized.front(), Segment(generation, 1, 0x11)),
        "post-destruction snapshot retains exact segment metadata");
}

void CapacityRejectsAndLatches(const std::filesystem::path& directory) {
    std::string error;
    auto store = fthr::replay_disk::ReplayDiskStore::Create(
        Config(directory, 64 * 1024, 1024, 4), error);
    Check(store != nullptr, "capacity test store opens");
    auto segment = Segment(store->CurrentGeneration(), 1, 0x22);
    segment.codec_description.reserve(4096);
    const auto before = segment.packets.front().bytes;
    const auto result = store->Submit(std::move(segment));
    Check(result.status == fthr::replay_disk::SubmitStatus::QuotaExceeded,
        "vector capacity limit rejects an oversized admission");
    Check(!store->Stats().accepting, "producer quota failure latches the generation");
    Check(segment.packets.front().bytes == before && segment.codec_description.size() == 3,
        "rejected Submit leaves caller ownership intact");
}

void PinnedQuotaLatchesUntilReset(const std::filesystem::path& directory) {
    std::string error;
    auto store = fthr::replay_disk::ReplayDiskStore::Create(
        Config(directory, 1500, 4096, 4), error);
    Check(store != nullptr, "quota test store opens");
    const uint64_t generation = store->CurrentGeneration();
    auto first = Segment(generation, 1, 0x31);
    for (auto& packet : first.packets) packet.bytes.resize(200, 0x31);
    Check(static_cast<bool>(store->Submit(std::move(first))),
        "first quota segment is accepted");
    Check(store->Drain(2000), "first quota segment is written");
    auto pinned = store->SnapshotGeneration(generation);
    Check(pinned.size() == 1 && store->Stats().pinned_segments == 1,
        "active snapshot lease is visible as pinned history");
    auto second = Segment(generation, 2, 0x32);
    for (auto& packet : second.packets) packet.bytes.resize(200, 0x32);
    Check(static_cast<bool>(store->Submit(std::move(second))),
        "disk quota is reserved by the writer rather than Submit");
    Check(store->Drain(2000), "quota failure drains the accepted prefix");
    const auto failed = store->Stats();
    Check(failed.last_status == fthr::replay_disk::SubmitStatus::QuotaExceeded
              && !failed.accepting && failed.disk_file_count == 1,
          "pinned quota failure is visible and generation-fatal");
    std::vector<fthr::replay_disk::ReplaySegment> old_materialized;
    Check(pinned.Materialize(old_materialized, error),
        "pinned older GOP remains readable after quota failure");
    const uint64_t reset = store->ResetGeneration();
    Check(reset > generation && store->Stats().accepting,
        "Reset clears the quota fault latch");
    pinned = {};
    auto replacement = Segment(reset, 1, 0x33);
    Check(static_cast<bool>(store->Submit(std::move(replacement))),
        "new generation accepts its first contiguous GOP");
    Check(store->Drain(2000), "writer releases retired data and writes replacement");
    Check(store->Stats().last_status == fthr::replay_disk::SubmitStatus::Accepted
              && store->SnapshotGeneration(reset).size() == 1,
          "unpinning permits a complete suffix in the next generation");
}

void PinnedPrefixKeepsCommittedSuffix(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    std::string error;
    auto store = ReplayDiskStore::Create(Config(directory, 1700, 4096, 4), error);
    Check(store != nullptr, "suffix test store opens");
    const uint64_t generation = store->CurrentGeneration();
    auto first = Segment(generation, 1, 0x35);
    for (auto& packet : first.packets) packet.bytes.resize(200, 0x35);
    Check(static_cast<bool>(store->Submit(std::move(first))),
        "pinned prefix first GOP is accepted");
    Check(store->Drain(2000), "pinned prefix first GOP drains");
    auto pinned = store->SnapshotGeneration(generation);
    auto second = Segment(generation, 2, 0x36);
    for (auto& packet : second.packets) packet.bytes.resize(200, 0x36);
    Check(static_cast<bool>(store->Submit(std::move(second))),
        "committed suffix GOP is accepted while prefix is pinned");
    Check(store->Drain(2000), "committed suffix GOP drains");
    auto third = Segment(generation, 3, 0x37);
    for (auto& packet : third.packets) packet.bytes.resize(200, 0x37);
    Check(static_cast<bool>(store->Submit(std::move(third))),
        "next suffix GOP is accepted before writer reclamation");
    Check(store->Drain(2000), "writer reclaims only the oldest unpinned suffix");
    auto snapshot = store->SnapshotGeneration(generation);
    std::vector<ReplaySegment> materialized;
    Check(snapshot.size() == 1 && snapshot.Materialize(materialized, error)
              && materialized.size() == 1 && materialized[0].sequence == 3,
          "fresh snapshots expose only the contiguous active suffix");
    materialized.clear();
    Check(pinned.Materialize(materialized, error) && materialized.size() == 1
              && materialized[0].sequence == 1,
          "a pre-reset snapshot keeps its retired pinned prefix readable");
    pinned = {};
}

void SequenceAndResetRules(const std::filesystem::path& directory) {
    std::string error;
    auto store = fthr::replay_disk::ReplayDiskStore::Create(
        Config(directory, 64 * 1024, 4096, 4), error);
    Check(store != nullptr, "sequence test store opens");
    const uint64_t old_generation = store->CurrentGeneration();
    auto gap = Segment(old_generation, 2, 0x41);
    Check(store->Submit(std::move(gap)).status
            == fthr::replay_disk::SubmitStatus::SequenceMismatch,
        "sequence gaps are rejected without consuming a sequence");
    auto first = Segment(old_generation, 1, 0x42);
    Check(static_cast<bool>(store->Submit(std::move(first))),
        "first contiguous sequence remains accepted");
    Check(store->Drain(2000), "first sequence drains");
    auto old_snapshot = store->SnapshotGeneration(old_generation);
    const uint64_t new_generation = store->ResetGeneration();
    auto stale = Segment(old_generation, 2, 0x43);
    Check(store->Submit(std::move(stale)).status
            == fthr::replay_disk::SubmitStatus::GenerationMismatch,
        "stale generation is rejected without faulting the successor");
    auto new_gap = Segment(new_generation, 2, 0x44);
    Check(store->Submit(std::move(new_gap)).status
            == fthr::replay_disk::SubmitStatus::SequenceMismatch,
        "successor generation starts a fresh sequence");
    auto next = Segment(new_generation, 1, 0x45);
    Check(static_cast<bool>(store->Submit(std::move(next))),
        "successor accepts sequence one");
    Check(store->Drain(2000), "successor drains after reset");
    Check(old_snapshot.size() == 1
              && store->SnapshotGeneration(old_generation).empty()
              && store->SnapshotGeneration(new_generation).size() == 1,
        "fresh snapshots keep retired generations separate from the new suffix");
}

void QueueFullAndInflightSuccessAfterReset(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    auto gate = std::make_shared<WriteGate>();
    auto hooks = GatedHooks(gate);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8, 1);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "queue gate store opens");
    const uint64_t old_generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(old_generation, 1, 0x51))),
        "gated writer accepts its first segment");
    WaitForGate(gate);
    const auto inflight = store->Stats();
    Check(inflight.inflight_segments == 1
              && inflight.inflight_memory_bytes <= config.max_queue_bytes
              && inflight.queued_memory_bytes <= config.max_queue_bytes,
          "queue and in-flight RAM accounting stays within its independent limit");
    Check(!store->Drain(10), "Drain times out while the writer is paused");
    auto second = Segment(old_generation, 2, 0x52);
    Check(store->Submit(std::move(second)).status == SubmitStatus::QueueFull,
        "queue full is visible at the capture boundary");
    Check(store->Stats().last_status == SubmitStatus::QueueFull,
        "queue full latches the capture generation");
    const uint64_t new_generation = store->ResetGeneration();
    ReleaseGate(gate);
    Check(store->Drain(2000), "old in-flight success is closed before Drain returns");
    Check(store->SnapshotGeneration(new_generation).empty(),
        "old in-flight success is never published into the successor");
    auto replacement = Segment(new_generation, 1, 0x53);
    Check(static_cast<bool>(store->Submit(std::move(replacement))),
        "Reset reopens queue admission");
    Check(store->Drain(2000), "successor segment drains");
}

void InflightFailureAfterResetDoesNotFaultSuccessor(
    const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    auto gate = std::make_shared<WriteGate>();
    std::atomic<bool> fail_write{false};
    auto hooks = GatedHooks(gate, &fail_write);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "failure gate store opens");
    const uint64_t old_generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(old_generation, 1, 0x61))),
        "failure gate accepts old segment");
    WaitForGate(gate);
    const uint64_t new_generation = store->ResetGeneration();
    fail_write.store(true);
    ReleaseGate(gate);
    Check(store->Drain(2000), "old in-flight failure is drained");
    const auto after_old_failure = store->Stats();
    Check(after_old_failure.accepting && !after_old_failure.io_failure_latched,
        "old in-flight failure does not fault the successor generation");
    fail_write.store(false);
    auto replacement = Segment(new_generation, 1, 0x62);
    Check(static_cast<bool>(store->Submit(std::move(replacement))),
        "successor remains writable after old failure");
    Check(store->Drain(2000), "successor write drains");
}

void CleanupRequestAndCloseFailureAreGenerationBound(
    const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    auto write_gate = std::make_shared<WriteGate>();
    ReleaseGate(write_gate);
    auto close_gate = std::make_shared<WriteGate>();
    std::atomic<int> fail_close_count{1};
    auto hooks = GatedHooks(write_gate, nullptr, nullptr, close_gate,
                            &fail_close_count);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "cleanup gate store opens");
    const uint64_t first_generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(first_generation, 1, 0x68))),
        "cleanup gate writes its first generation");
    Check(store->Drain(2000), "cleanup gate first write drains");

    const uint64_t second_generation = store->ResetGeneration();
    WaitForGate(close_gate);
    Check(!store->Drain(10),
        "Drain reports busy while the cleanup close is blocked");
    const uint64_t third_generation = store->ResetGeneration();
    Check(third_generation > second_generation,
        "a second reset records a distinct cleanup request");
    ReleaseGate(close_gate);
    Check(store->Drain(2000),
        "new cleanup request is processed after the old close failure");
    const auto after_cleanup = store->Stats();
    Check(after_cleanup.accepting && !after_cleanup.io_failure_latched
              && after_cleanup.disk_file_count == 0,
          "old cleanup failure neither faults nor charges the successor");
    auto replacement = Segment(third_generation, 1, 0x69);
    Check(static_cast<bool>(store->Submit(std::move(replacement))),
        "successor accepts work after generation-bound cleanup");
    Check(store->Drain(2000), "successor after cleanup drains");
}

void CurrentWriteFailureLatchesUntilReset(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    auto gate = std::make_shared<WriteGate>();
    ReleaseGate(gate);
    std::atomic<bool> fail_write{true};
    auto hooks = GatedHooks(gate, &fail_write);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "current-failure store opens");
    const uint64_t generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(generation, 1, 0x6a))),
        "current-failure segment is accepted before the writer fault");
    Check(store->Drain(2000), "current-failure segment drains");
    auto failed = store->Stats();
    Check(failed.last_status == SubmitStatus::IoFailure
              && failed.io_failure_latched && !failed.accepting,
          "a current generation write error is visible and terminal");
    const uint64_t successor = store->ResetGeneration();
    fail_write.store(false);
    auto replacement = Segment(successor, 1, 0x6b);
    Check(static_cast<bool>(store->Submit(std::move(replacement))),
        "reset clears the current write fault");
    Check(store->Drain(2000), "replacement after current failure drains");
}

void ShutdownCloseKeepsDrainBusy(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    auto write_gate = std::make_shared<WriteGate>();
    ReleaseGate(write_gate);
    auto close_gate = std::make_shared<WriteGate>();
    auto hooks = GatedHooks(write_gate, nullptr, nullptr, close_gate);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "shutdown-close store opens");
    const uint64_t generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(generation, 1, 0x6c))),
        "shutdown-close segment is accepted");
    Check(store->Drain(2000), "shutdown-close segment drains");

    std::thread shutdown([&] { store->Shutdown(); });
    WaitForGate(close_gate);
    const auto during_shutdown = store->Stats();
    Check(!during_shutdown.accepting,
        "shutdown stops admission while its final close is blocked");
    Check(!store->Drain(0) && !store->Drain(10),
        "Drain remains busy during the writer-owned final close");
    ReleaseGate(close_gate);
    shutdown.join();
    Check(store->Drain(2000),
        "Drain becomes idle after Shutdown closes and releases its records");
}

void ReadHooksAndConcurrentSnapshots(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    std::atomic<bool> fail_read{false};
    auto gate = std::make_shared<WriteGate>();
    ReleaseGate(gate);
    auto hooks = GatedHooks(gate, nullptr, &fail_read);
    std::string error;
    auto config = Config(directory, 64 * 1024, 4096, 8);
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "read-hook store opens");
    const uint64_t generation = store->CurrentGeneration();
    Check(static_cast<bool>(store->Submit(Segment(generation, 1, 0x71))),
        "read-hook segment is accepted");
    Check(store->Drain(2000), "read-hook segment drains");
    auto snapshot = store->SnapshotGeneration(generation);
    fail_read.store(true);
    std::vector<ReplaySegment> failed;
    Check(!snapshot.Materialize(failed, error), "injected read failure is visible");
    fail_read.store(false);
    std::atomic<bool> first_ok{false};
    std::atomic<bool> second_ok{false};
    std::thread first([&] {
        std::vector<ReplaySegment> output;
        std::string local_error;
        first_ok.store(snapshot.Materialize(output, local_error)
                       && output.size() == 1
                       && Equal(output.front(), Segment(generation, 1, 0x71)));
    });
    std::thread second([&] {
        std::vector<ReplaySegment> output;
        std::string local_error;
        second_ok.store(snapshot.Materialize(output, local_error)
                        && output.size() == 1
                        && Equal(output.front(), Segment(generation, 1, 0x71)));
    });
    first.join();
    second.join();
    Check(first_ok.load() && second_ok.load(),
        "concurrent snapshot reads serialize per private handle");
}

void PartialTransfersAndBounds(const std::filesystem::path& directory) {
    using namespace fthr::replay_disk;
    std::atomic<int> write_calls{0};
    std::atomic<int> read_calls{0};
    std::atomic<bool> fail_write_after_first{false};
    std::atomic<bool> fail_read_after_first{false};
    auto hooks = std::make_shared<ReplayDiskTestHooks>();
    hooks->probe = [](ReplayIoPoint) {};
    hooks->fail = [&](ReplayIoPoint point) {
        if (point == ReplayIoPoint::BeforeWrite && fail_write_after_first)
            return write_calls.fetch_add(1) >= 1;
        if (point == ReplayIoPoint::BeforeRead && fail_read_after_first)
            return read_calls.fetch_add(1) >= 1;
        return false;
    };
    std::string error;
    auto config = Config(directory, 8 * 1024 * 1024, 8 * 1024 * 1024,
                         8, 4);
    config.max_queue_bytes = 16 * 1024 * 1024;
    config.test_hooks = hooks;
    auto store = ReplayDiskStore::Create(config, error);
    Check(store != nullptr, "partial-transfer store opens");
    const uint64_t generation = store->CurrentGeneration();
    auto large = Segment(generation, 1, 0x72);
    large.packets[0].bytes.resize(1'100'000, 0x72);
    large.packets[1].bytes.resize(700'000, 0x73);
    large.packets[2].bytes.resize(700'000, 0x74);
    const auto expected = large;
    fail_write_after_first.store(true);
    Check(static_cast<bool>(store->Submit(std::move(large))),
        "two-megabyte segment is admitted before partial write failure");
    Check(store->Drain(2000), "partial write failure drains visibly");
    Check(store->Stats().last_status == SubmitStatus::IoFailure,
        "write failure after the first 1MiB chunk is latched");

    const uint64_t successor = store->ResetGeneration();
    fail_write_after_first.store(false);
    write_calls.store(0);
    auto complete = expected;
    complete.generation = successor;
    Check(static_cast<bool>(store->Submit(std::move(complete))),
        "complete two-megabyte segment is accepted after reset");
    Check(store->Drain(2000), "complete two-megabyte segment drains");
    auto snapshot = store->SnapshotGeneration(successor);
    fail_read_after_first.store(true);
    read_calls.store(0);
    std::vector<ReplaySegment> output;
    Check(!snapshot.Materialize(output, error),
        "read failure after the first 1MiB chunk is visible");
    fail_read_after_first.store(false);
    read_calls.store(0);
    output.clear();
    Check(snapshot.Materialize(output, error) && output.size() == 1,
        "complete multi-chunk read succeeds after injected failure");
    auto expected_successor = expected;
    expected_successor.generation = successor;
    Check(Equal(output.front(), expected_successor),
        "partial transfer recovery preserves all metadata and payload bytes");

    auto invalid = Segment(successor, 2, 0x75);
    invalid.packets.front().keyframe = false;
    Check(store->Submit(std::move(invalid)).status == SubmitStatus::InvalidSegment,
        "a non-keyframe primary video start is rejected without latching");
    Check(store->Stats().accepting, "invalid segment leaves the generation usable");
}

}  // namespace

int RunReplayDiskStoreTests() {
    const auto root = TestRoot();
    if (root.empty()) {
        std::cerr << "FTHR_DISK_TEST_ROOT is required for replay disk tests"
                  << std::endl;
        return -1;
    }
    const auto directory = MakeOwnedDirectory(root);
    Check(!directory.empty(), "test creates only its own isolated directory");
    const auto sentinel = directory / L"preexisting-sentinel.txt";
    Check(WriteSentinel(sentinel), "test creates a preexisting sentinel file");
    RoundTripAndSnapshotOutlivesStore(directory);
    CapacityRejectsAndLatches(directory);
    PinnedQuotaLatchesUntilReset(directory);
    PinnedPrefixKeepsCommittedSuffix(directory);
    SequenceAndResetRules(directory);
    QueueFullAndInflightSuccessAfterReset(directory);
    InflightFailureAfterResetDoesNotFaultSuccessor(directory);
    CleanupRequestAndCloseFailureAreGenerationBound(directory);
    CurrentWriteFailureLatchesUntilReset(directory);
    ShutdownCloseKeepsDrainBusy(directory);
    ReadHooksAndConcurrentSnapshots(directory);
    PartialTransfersAndBounds(directory);
    Check(ReadSentinel(sentinel) == "keep-me", "preexisting file remains unchanged");
    Check(DeleteFileW(sentinel.wstring().c_str()) != FALSE,
        "test removes only its own sentinel");
    Check(RemoveDirectoryW(directory.wstring().c_str()) != FALSE,
        "test removes only its own empty directory");
    std::cout << "Replay disk store tests: " << checks << " checks passed" << std::endl;
    return checks;
}

#ifdef FTHR_REPLAY_DISK_STORE_TEST_STANDALONE
int main() { return RunReplayDiskStoreTests() < 0 ? 2 : 0; }
#endif
