#include "encoded_ring_buffer.h"
#include "save_clip_task.h"
#include "replay_storage.h"

#include <atomic>
#include <array>
#include <cstdlib>
#include <future>
#include <iostream>
#include <numeric>
#include <thread>
#include <type_traits>
#include <vector>

namespace {
int checks = 0;
void Check(bool condition) {
    ++checks;
    if (!condition) std::abort();
}
using fthr::ReplayMemoryUse;
constexpr uint64_t kMiB = 1024 * 1024;

void AdmissionPreservesRequestedSettings() {
    const auto normal = fthr::PlanReplayMemory(60, 60, 16000, 2048, 8ULL * 1024 * kMiB);
    Check(normal.fits && normal.required_bytes <= normal.total_bytes);
    Check(normal.ring_bytes * 2 == normal.total_bytes);
    const auto long_high = fthr::PlanReplayMemory(900, 360, 200000, 2048, 8ULL * 1024 * kMiB);
    Check(!long_high.fits && long_high.required_bytes > long_high.total_bytes);
    const auto low_ram = fthr::PlanReplayMemory(60, 60, 16000, 2048, 64 * kMiB);
    Check(!low_ram.fits && low_ram.total_bytes == 32 * kMiB);
    Check(!fthr::PlanReplayMemory(0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff,
                                 UINT64_MAX).fits);
    Check(fthr::PlanReplayMemory(30, 360, 300, 2048, UINT64_MAX).fits);
}

void ReservationsAreAtomicAndProtectCapture() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(100, 60);
    fthr::ReplayMemoryReservation save(budget, ReplayMemoryUse::Save);
    Check(save.Grow(40));
    Check(!save.Grow(1));
    fthr::ReplayMemoryReservation capture(budget, ReplayMemoryUse::Ring);
    Check(capture.Grow(60));
    Check(!capture.Grow(1));
    auto moved = std::move(save);
    Check(save.bytes() == 0 && moved.bytes() == 40);
    moved = {};
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
    capture = {};

    std::atomic<int> acquired{0};
    std::atomic<int> started{0};
    std::atomic<bool> release{false};
    std::vector<std::thread> threads;
    for (int i = 0; i < 8; ++i) threads.emplace_back([&] {
        fthr::ReplayMemoryReservation lease(budget, ReplayMemoryUse::Save);
        if (lease.Grow(10)) acquired.fetch_add(1);
        started.fetch_add(1);
        while (!release.load()) std::this_thread::yield();
    });
    while (started.load() != 8) std::this_thread::yield();
    Check(acquired.load() == 4 && budget->Used(ReplayMemoryUse::Save) == 40);
    release.store(true);
    for (auto& thread : threads) thread.join();
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
}

void Fill(fthr::EncodedRingBuffer& ring, size_t bytes = 1024, uint8_t value = 0x5a) {
    std::vector<uint8_t> packet(bytes, value);
    for (int i = 0; i < 8; ++i)
        Check(ring.Push(packet.data(), static_cast<uint32_t>(packet.size()), i,
                        i % 2 == 0, (i + 1) * 1000));
}

void SavesRetainAdmissionUntilPayloadIsReleased() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(1024 * kMiB, 512 * kMiB);
    fthr::EncodedRingBuffer ring(8, 1, 1000, budget);
    Fill(ring);
    auto reference = ring.TakeSnapshotByTime(6, 8000);
    Check(reference.packets.size() >= 6);
    const uint64_t one_save = budget->Used(ReplayMemoryUse::Save);
    Check(one_save >= 6 * 1024);
    reference = {};
    Check(budget->Used(ReplayMemoryUse::Save) == 0);

    // Constrain saves to exactly one complete snapshot plus selection scratch.
    const uint64_t ring_limit = 32 * kMiB;
    budget = std::make_shared<fthr::ReplayMemoryBudget>(ring_limit + one_save + 2048, ring_limit);
    {
        fthr::EncodedRingBuffer bounded(8, 1, 1000, budget);
        Fill(bounded);
        auto first = bounded.TakeSnapshotByTime(6, 8000);
        Check(!first.packets.empty());
        const uint64_t held = budget->Used(ReplayMemoryUse::Save);
        auto rejected = bounded.TakeSnapshotByTime(6, 8000);
        Check(rejected.selection_outcome == fthr::replay_save::SnapshotOutcome::MemoryBudgetExceeded);
        Check(rejected.packets.empty() && budget->Used(ReplayMemoryUse::Save) == held);
        Check(!bounded.HasMemoryFailure());

        fthr::SaveClipQueue queue;
        fthr::SaveClipTask task;
        task.encoded_snapshot = std::move(first);
        Check(queue.Push(std::move(task)));
        Check(budget->Used(ReplayMemoryUse::Save) == held);
        fthr::SaveClipTask active;
        Check(queue.Pop(active));
        Check(budget->Used(ReplayMemoryUse::Save) == held);
        Fill(bounded, 1024, 0x33); // Writer-owned data survives overwrite.
        Check(active.encoded_snapshot.packets.front().data.front() == 0x5a);
        active = {};
        Check(budget->Used(ReplayMemoryUse::Save) == 0);
        auto next = bounded.TakeSnapshotByTime(6, 8000);
        Check(!next.packets.empty());
        queue.Shutdown();
        task.encoded_snapshot = std::move(next);
        Check(!queue.Push(std::move(task)));
        task = {};
        Check(budget->Used(ReplayMemoryUse::Save) == 0);
    }
    Check(budget->Used(ReplayMemoryUse::Ring) == 0);
}

void GrowthFailurePreservesPublishedPacketsAndClearReleasesStorage() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * kMiB, kMiB);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    const uint8_t small_packet[] = {1, 2, 3};
    Check(ring.Push(small_packet, 3, 1, true, 1000));
    const uint64_t allocated = budget->Used(ReplayMemoryUse::Ring);
    auto saved = ring.TakeSnapshotByTime(1, 1000);
    Check(saved.packets.size() == 1);
    std::vector<uint8_t> huge(kMiB, 0x7f);
    Check(!ring.Push(huge.data(), static_cast<uint32_t>(huge.size()), 2, true, 2000));
    Check(ring.HasMemoryFailure() && ring.GetPushCount() == 1);
    Check(budget->Used(ReplayMemoryUse::Ring) == allocated);
    auto retained = ring.TakeSnapshotByTime(1, 1000);
    Check(retained.packets.size() == 1 && retained.packets.front().data == saved.packets.front().data);
    retained = {};
    ring.Clear(1500);
    Check(!ring.HasMemoryFailure() && ring.GetCount() == 0);
    Check(budget->Used(ReplayMemoryUse::Ring) == allocated - 3);
    Check(saved.packets.front().data.size() == 3);
    Check(!ring.Push(small_packet, 3, 1, true, 1000)); // Old generation stays rejected.
    Check(!ring.HasMemoryFailure());
    Check(ring.Push(small_packet, 3, 2, true, 2000));
    saved = {};
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
}

void ActualSlotCapacityRemainsChargedAfterSmallerOverwrite() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * kMiB, kMiB);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    std::vector<uint8_t> packet(4096, 0x42);
    Check(ring.Push(packet.data(), 4096, 1, true, 1000));
    const auto held = budget->Used(ReplayMemoryUse::Ring);
    Check(ring.Push(packet.data(), 1, 2, true, 2000));
    Check(budget->Used(ReplayMemoryUse::Ring) == held);
    ring.Clear();
    Check(budget->Used(ReplayMemoryUse::Ring) == held - 4096);
}

void RotatingPeaksDoNotPoisonRetainedSlotCapacity() {
    constexpr size_t slots = 4;
    constexpr uint32_t small_packet = 1024, large = 128 * 1024;
    const uint64_t metadata = slots * (sizeof(fthr::EncodedRingPacket)
        + sizeof(std::atomic<uint32_t>) + sizeof(std::mutex));
    const uint64_t limit = metadata + 2 * large + 3 * small_packet;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * limit, limit);
    fthr::EncodedRingBuffer ring(slots, 60, 1000, budget);
    Check(budget->Used(ReplayMemoryUse::Ring) == metadata);
    std::array<uint32_t, slots> live_sizes{};
    std::vector<uint8_t> packet(large, 0x5a);
    for (uint64_t position = 0; position < 404; ++position) {
        const bool peak = position >= slots && (position - slots) % (slots + 1) == 0;
        const uint32_t bytes = peak ? large : small_packet;
        const uint64_t live = std::accumulate(
            live_sizes.begin(), live_sizes.end(), uint64_t{0});
        Check(metadata + live + bytes <= limit); // Live data plus transient fits.
        Check(ring.Push(packet.data(), bytes, static_cast<int64_t>(position), peak,
                        static_cast<int64_t>(position + 1) * 1000));
        live_sizes[static_cast<size_t>(position % slots)] = bytes;
        Check(budget->Used(ReplayMemoryUse::Ring) <= limit);
        Check(budget->Used(ReplayMemoryUse::Save) == 0);
    }
    Check(ring.GetPushCount() == 404 && ring.GetCount() == slots);
    Check(!ring.HasMemoryFailure());
}

void SlackAtAllowanceKeepsReusableStorage() {
    constexpr uint64_t limit = 8192;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * limit, limit);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    std::vector<uint8_t> packet(2048, 0x42);
    Check(ring.Push(packet.data(), 2048, 1, true, 1000));
    const auto held = budget->Used(ReplayMemoryUse::Ring);
    Check(ring.Push(packet.data(), 1024, 2, true, 2000)); // Slack equals limit / 8.
    Check(budget->Used(ReplayMemoryUse::Ring) == held);
    Check(ring.GetPushCount() == 2 && !ring.HasMemoryFailure());
}

void OptionalCompactionAdmissionFailureKeepsCaptureHealthy() {
    constexpr uint64_t limit = 8192;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * limit, limit);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    std::vector<uint8_t> packet(4096, 0x37);
    Check(ring.Push(packet.data(), 4096, 1, true, 1000));
    const auto held = budget->Used(ReplayMemoryUse::Ring);
    {
        fthr::ReplayMemoryReservation pressure(budget, ReplayMemoryUse::Ring);
        const uint64_t remaining = limit - held;
        Check(pressure.Grow(remaining));
        std::vector<uint8_t> pressure_payload(static_cast<size_t>(remaining), 0x11);
        Check(pressure_payload.capacity() == remaining);
        Check(budget->Used(ReplayMemoryUse::Ring) == limit);
        Check(ring.Push(packet.data(), 16, 2, true, 2000));
        Check(budget->Used(ReplayMemoryUse::Ring) == limit);
        Check(!ring.HasMemoryFailure() && ring.GetPushCount() == 2);
        auto latest = ring.TakeSnapshotByTime(1, 2000);
        Check(latest.packets.size() == 1);
        Check(latest.packets.front().data == std::vector<uint8_t>(16, 0x37));
    } // Free pressure payload before its reservation returns admission.
    Check(budget->Used(ReplayMemoryUse::Ring) == held); // Capacity was retained.
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
}

void CompactionPreservesIndependentSnapshot() {
    constexpr uint64_t limit = 8192;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * limit, limit);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    std::vector<uint8_t> original(2048, 0x42);
    Check(ring.Push(original.data(), 2048, 1, true, 1000));
    const auto held_ring = budget->Used(ReplayMemoryUse::Ring);
    auto saved = ring.TakeSnapshotByTime(1, 1000);
    Check(saved.packets.size() == 1);
    const auto held_save = budget->Used(ReplayMemoryUse::Save);
    std::vector<uint8_t> small_packet(64, 0x7b);
    Check(ring.Push(small_packet.data(), 64, 2, true, 2000));
    Check(budget->Used(ReplayMemoryUse::Ring) == held_ring - 2048 + 64);
    Check(budget->Used(ReplayMemoryUse::Save) == held_save);
    Check(saved.packets.front().data == original);
    Check(saved.packets.front().pts == 1 && saved.packets.front().wall_qpc == 1000);
    Check(saved.packets.front().absolute_position == 0 && saved.packets.front().is_keyframe);
    auto latest = ring.TakeSnapshotByTime(1, 2000);
    Check(latest.packets.size() == 1 && latest.packets.front().data == small_packet);
    Check(latest.packets.front().pts == 2 && latest.packets.front().wall_qpc == 2000);
    Check(latest.packets.front().absolute_position == 1 && latest.packets.front().is_keyframe);
    Check(latest.packets.front().generation == saved.packets.front().generation);
    Check(!ring.HasMemoryFailure());
}

void ZeroSizedOverwriteReleasesExcessStorage() {
    constexpr uint64_t limit = 8192;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(2 * limit, limit);
    fthr::EncodedRingBuffer ring(1, 1, 1000, budget);
    const auto metadata = budget->Used(ReplayMemoryUse::Ring);
    std::vector<uint8_t> packet(2048, 0x42);
    Check(ring.Push(packet.data(), 2048, 1, true, 1000));
    fthr::PublishedVideoPoint published;
    Check(ring.Push(nullptr, 0, 2, true, 2000, &published));
    Check(budget->Used(ReplayMemoryUse::Ring) == metadata);
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
    Check(published.pts == 2 && published.generation == ring.GetGeneration());
    Check(ring.GetPushCount() == 2 && ring.GetCount() == 1 && !ring.HasMemoryFailure());
}

void SeveralSavesDrainAfterRingDestruction() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(64 * 1024, 32 * 1024);
    fthr::SaveClipQueue queue;
    uint64_t one_save = 0;
    {
        fthr::EncodedRingBuffer ring(8, 1, 1000, budget);
        Fill(ring, 256);
        for (uint32_t id = 1; id <= queue.GetCapacity(); ++id) {
            fthr::SaveClipTask task;
            task.task_id = id;
            task.encoded_snapshot = ring.TakeSnapshotByTime(6, 8000);
            Check(task.encoded_snapshot.packets.size() == 8);
            if (id == 1) one_save = budget->Used(ReplayMemoryUse::Save);
            Check(queue.Push(std::move(task)));
            Check(budget->Used(ReplayMemoryUse::Save) == one_save * id);
        }
        fthr::SaveClipTask overflow;
        overflow.encoded_snapshot = ring.TakeSnapshotByTime(6, 8000);
        Check(!queue.Push(std::move(overflow)));
        overflow = {};
        Check(budget->Used(ReplayMemoryUse::Save) == one_save * queue.GetCapacity());
    }
    Check(budget->Used(ReplayMemoryUse::Ring) == 0);
    queue.Shutdown();
    for (uint32_t id = 1; id <= queue.GetCapacity(); ++id) {
        fthr::SaveClipTask task;
        Check(queue.Pop(task) && task.task_id == id);
        Check(task.encoded_snapshot.packets.front().data.front() == 0x5a);
    }
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
    fthr::SaveClipTask empty;
    Check(!queue.Pop(empty));
}

void EarlySnapshotReturnsDoNotRetainAdmission() {
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(64 * 1024, 32 * 1024);
    fthr::EncodedRingBuffer ring(8, 1, 1000, budget);
    auto empty = ring.TakeSnapshotByTime(1, 1000);
    Check(empty.packets.empty());
    empty = {};
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
    const uint8_t packet = 1;
    Check(ring.Push(&packet, 1, 1, false, 1000));
    auto no_keyframe = ring.TakeSnapshotByTime(1, 1000);
    Check(no_keyframe.packets.empty());
    no_keyframe = {};
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
}
std::filesystem::path NewReplayTestDirectory() {
    const auto suffix = std::chrono::steady_clock::now().time_since_epoch().count();
    const auto path = std::filesystem::temp_directory_path()
        / std::filesystem::u8path("fthr-replay-\xc3\xa9-" + std::to_string(suffix));
    Check(std::filesystem::create_directories(path));
    return path;
}

void RemoveReplayTestDirectory(const std::filesystem::path& path) {
    Check(std::filesystem::is_empty(path));
    Check(std::filesystem::remove(path));
}

void StorageArgsPreservePositionals() {
    auto parse = [](std::vector<std::string> args, fthr::ReplayStorageConfig& config,
                    std::string& error, int expected_positionals) {
        std::vector<char*> argv;
        for (auto& arg : args) argv.push_back(arg.data());
        int argc = static_cast<int>(argv.size());
        const bool ok = fthr::ParseReplayStorageArgs(argc, argv.data(), config, error);
        if (ok) Check(argc == expected_positionals);
        return ok;
    };
    const auto folder = NewReplayTestDirectory();
    const auto utf8_path = folder.u8string();
    const std::string path(utf8_path.begin(), utf8_path.end());
    fthr::ReplayStorageConfig cfg;
    std::string error;
    Check(parse({"engine", "60", "30", "--replay-storage", "disk",
                 "--replay-storage-path", path, "--replay-storage-mb", "128",
                 "--replay-memory-mb", "64"}, cfg, error, 3));
    Check(cfg.mode == fthr::ReplayStorageMode::Disk && cfg.directory == folder);
    Check(cfg.disk_mb == 128 && cfg.memory_mb == 64 && !cfg.automatic_budget);
    cfg = {};
    Check(parse({"engine", "60", "30", "--replay-storage", "memory",
                 "--replay-budget-mode", "auto"}, cfg, error, 3));
    Check(cfg.mode == fthr::ReplayStorageMode::Memory && cfg.automatic_budget);
    Check(!cfg.allow_low_resources);
    cfg = {};
    Check(parse({"engine", "--replay-storage", "memory", "--replay-low-resources", "attempt"}, cfg, error, 1));
    Check(cfg.allow_low_resources && cfg.automatic_budget);
    Check(!parse({"engine", "--replay-storage", "memory", "--replay-low-resources", "true"}, cfg, error, 0));
    cfg = {};
    Check(parse({"engine", "--replay-storage", "disk",
                 "--replay-storage-path", path}, cfg, error, 1));
    Check(cfg.automatic_budget);
    cfg = {};
    Check(!parse({"engine", "--replay-storage", "memory", "--replay-budget-mode", "auto",
                  "--replay-memory-mb", "64"}, cfg, error, 0));
    cfg = {};
    Check(!parse({"engine", "--replay-storage"}, cfg, error, 0));
    Check(!parse({"engine", "--replay-storage", "disk"}, cfg, error, 0));
    Check(!parse({"engine", "--replay-storage", "memory", "--replay-memory-mb", "64abc"}, cfg, error, 0));
    Check(!parse({"engine", "--replay-storage", "ram"}, cfg, error, 0));
    RemoveReplayTestDirectory(folder);
}

void AutomaticBudgetUsesCaptureRequirements() {
    using fthr::kReplayMiB;
    fthr::ReplayStorageConfig config;
    config.mode = fthr::ReplayStorageMode::Memory;
    std::string error;
    Check(!fthr::CreateCaptureReplayStorage(config, 2048 * kReplayMiB, 62, 60, 25000, 34, error));
    Check(error.find("3203 MiB") != std::string::npos);
    Check(error.find("available budget: 2048 MiB") != std::string::npos);

    config.automatic_budget = true;
    error.clear();
    auto memory = fthr::CreateCaptureReplayStorage(config, 8192 * kReplayMiB, 62, 60, 25000, 34, error);
    Check(memory && error.empty());
    Check(memory->memory_budget()->total_limit() > 3202 * kReplayMiB);
    Check(memory->memory_budget()->total_limit() <= 3203 * kReplayMiB);
    Check(!fthr::CreateCaptureReplayStorage(config, 3202 * kReplayMiB, 62, 60, 25000, 34, error));
    Check(error.find("3203 MiB") != std::string::npos);
    Check(error.find("Increase its RAM budget") == std::string::npos);

    const auto folder = NewReplayTestDirectory();
    config.mode = fthr::ReplayStorageMode::Disk;
    config.directory = folder;
    config.disk_mb = 64;
    error.clear();
    auto disk = fthr::CreateCaptureReplayStorage(config, 2048 * kReplayMiB, 10, 60, 25000, 1, error);
    Check(disk && error.empty());
    Check(disk->memory_budget()->total_limit() == 64 * kReplayMiB);
    disk.reset();
    config.automatic_budget = false;
    Check(!fthr::CreateCaptureReplayStorage(config, 2048 * kReplayMiB, 10, 60, 25000, 1, error));
    Check(error.find("configured budget: 64 MiB") != std::string::npos);
    RemoveReplayTestDirectory(folder);
}

void LowResourceAdmissionStaysBounded() {
    using namespace fthr;
    Check(ReplayAvailableMemoryBudget(1024 * kMiB) == 768 * kMiB);
    Check(ReplayAvailableMemoryBudget(512 * kMiB) == 256 * kMiB);
    Check(ReplayAvailableMemoryBudget(0) == 0);
    ReplayStorageConfig config;
    config.mode = ReplayStorageMode::Memory;
    config.automatic_budget = true;
    std::string error;
    Check(!CreateCaptureReplayStorage(config, 128 * kMiB, 60, 60, 25000, 2, error));
    config.allow_low_resources = true;
    error.clear();
    auto memory = CreateCaptureReplayStorage(config, 128 * kMiB, 60, 60, 25000, 2, error);
    Check(memory && error.empty());
    Check(memory->memory_budget()->total_limit() == 128 * kMiB);
    Check(!memory->memory_budget()->TryAcquire(ReplayMemoryUse::Ring, 129 * kMiB));
    Check(!CreateCaptureReplayStorage(config, 32 * kMiB, 60, 60, 25000, 2, error));
    const auto folder = NewReplayTestDirectory();
    config.mode = ReplayStorageMode::Disk;
    config.directory = folder;
    config.automatic_budget = false;
    config.disk_mb = 64;
    error.clear();
    auto disk = CreateCaptureReplayStorage(config, 128 * kMiB, 60, 60, 25000, 2, error);
    Check(disk && error.empty() && disk->disk_limit() == 64 * kMiB);
    disk.reset();
    config.directory = folder / "missing";
    Check(!CreateCaptureReplayStorage(config, 128 * kMiB, 60, 60, 25000, 2, error));
    RemoveReplayTestDirectory(folder);
}

void SnapshotsPinStorageAcrossRingReset(fthr::ReplayStorageMode mode) {
    const auto folder = NewReplayTestDirectory();
    fthr::ReplayStorageConfig config;
    config.mode = mode;
    config.directory = folder;
    const auto budget = std::make_shared<fthr::ReplayMemoryBudget>(128 * 1024, 96 * 1024);
    std::string error;
    auto store = fthr::ReplayStorage::Create(config, budget, error);
    Check(store && error.empty());
    auto save = store->BeginSave();
    Check(save && !store->BeginSave());
    save.reset();
    Check(bool(store->BeginSave()));

    auto ring = std::make_unique<fthr::EncodedRingBuffer>(5, 1, 1000, budget, store);
    fthr::EncodedVideoConfig video_config;
    video_config.time_base = {1, 1};
    Check(ring->SetVideoConfig(video_config));
    for (uint8_t i = 1; i <= 5; ++i)
        Check(ring->Push(&i, 1, i, i == 1 || i == 4, i * 1000));
    const std::string marker_id = "00000000-0000-4000-8000-000000000001";
    Check(ring->MarkHighlight(marker_id, 4000));
    auto snapshot = ring->TakeSnapshotByTime(2, 5000);
    Check(snapshot.full_history);
    Check(snapshot.packets.front().pts == 1 && snapshot.presentation_start_pts == 4);
    Check(snapshot.highlights.size() == 1 && snapshot.highlights.front().marker_id == marker_id);
    Check(snapshot.packets.front().data.empty());
    Check(snapshot.packets.front().byte_size() == 1);
    for (uint8_t i = 6; i <= 9; ++i)
        Check(ring->Push(&i, 1, i, i == 6, i * 1000));
    ring->Clear();
    Check(ring->GetCount() == 0 && ring->GetGeneration() != snapshot.capture_generation);
    ring.reset();
    store.reset();
    for (size_t i = 0; i < snapshot.packets.size(); ++i) {
        uint8_t output = 0;
        Check(snapshot.packets[i].ReadInto(&output) && output == i + 1);
    }
    Check(budget->Used(ReplayMemoryUse::Ring) != 0);
    snapshot = {};
    Check(budget->Used(ReplayMemoryUse::Ring) == 0);
    Check(budget->Used(ReplayMemoryUse::Save) == 0);
    RemoveReplayTestDirectory(folder);
}

void AudioReplayPreservesSamplesAndTiming(fthr::ReplayStorageMode mode) {
    const auto folder = NewReplayTestDirectory();
    fthr::ReplayStorageConfig config;
    config.mode = mode;
    config.directory = folder;
    std::string error;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(128 * 1024, 96 * 1024);
    auto store = fthr::ReplayStorage::Create(config, budget, error);
    Check(bool(store));
    const std::vector<uint8_t> pcm(128, 0x3a);
    const std::vector<uint8_t> silence(pcm.size());
    fthr::EncodedAudioSnapshot snapshot;
    {
        fthr::EncodedAudioPacketRing ring(
            {"20000000-0000-4000-8000-000000000001"}, 42,
            {48000, 2, "flt", "pcm_f32le"}, 5, store);
        Check(ring.Push({pcm, 0, 16}));
        Check(ring.Push({silence, 16, 16}));
        Check(ring.Push({silence, 32, 16}));
        snapshot = ring.TakeSnapshot(0, 48);
        Check(snapshot.valid() && snapshot.generation == 42);
        Check(snapshot.first_pts_samples == 0 && snapshot.last_pts_samples == 48);
        Check(snapshot.packets.size() == 2 && snapshot.packets[1].duration_samples == 32);
    }
    store.reset();
    Check(snapshot.packets[0].data.empty() && snapshot.packets[0].replay_storage);
    std::vector<uint8_t> output(pcm.size());
    Check(snapshot.packets[0].replay_storage->ReadInto(output.data(), output.size()));
    Check(output == pcm);
    output.resize(2 * pcm.size(), 0xff);
    Check(snapshot.packets[1].replay_storage->ReadInto(output.data(), output.size()));
    Check(std::all_of(output.begin(), output.end(), [](uint8_t sample) { return sample == 0; }));
    snapshot = {};
    Check(budget->Used(ReplayMemoryUse::Ring) == 0 && budget->Used(ReplayMemoryUse::Save) == 0);
    RemoveReplayTestDirectory(folder);
}

void DiskQuotaIncludesPinnedPackets() {
    const auto folder = NewReplayTestDirectory();
    fthr::ReplayStorageConfig config;
    config.mode = fthr::ReplayStorageMode::Disk;
    config.disk_mb = 1;
    config.directory = folder;
    std::string error;
    const auto budget = std::make_shared<fthr::ReplayMemoryBudget>(16 * fthr::kReplayMiB, 12 * fthr::kReplayMiB);
    auto store = fthr::ReplayStorage::Create(config, budget, error);
    Check(bool(store));
    std::vector<uint8_t> packet(700 * 1024, 0x3a);
    auto retained = store->Push(packet.data(), packet.size());
    Check(bool(retained));
    auto snapshot = retained;
    retained.reset();
    uint8_t sample = 0;
    Check(snapshot->ReadSlice(123, &sample, 1) && sample == 0x3a);
    Check(store->disk_bytes() == packet.size());
    Check(!store->Push(packet.data(), packet.size()));
    Check(!store->healthy() && std::string_view(store->failure()).find("disk budget") != std::string_view::npos);
    store.reset();
    Check(snapshot->ReadSlice(123, &sample, 1) && sample == 0x3a);
    snapshot.reset();
    RemoveReplayTestDirectory(folder);
}

void DiskQueueIncludesInFlightMemory() {
    const auto folder = NewReplayTestDirectory();
    fthr::ReplayStorageConfig config;
    config.mode = fthr::ReplayStorageMode::Disk;
    config.directory = folder;
    std::string error;
    auto budget = std::make_shared<fthr::ReplayMemoryBudget>(32768, 16384);
    std::promise<void> entering_writer, release_writer;
    auto entered = entering_writer.get_future();
    auto release = release_writer.get_future();
    auto store = fthr::ReplayStorage::Create(config, budget, error, [&] {
        entering_writer.set_value();
        release.wait();
    });
    Check(bool(store));
    std::array<uint8_t, 3072> packet{};
    auto pending = store->Push(packet.data(), packet.size());
    Check(bool(pending));
    Check(entered.wait_for(std::chrono::seconds(5)) == std::future_status::ready);
    Check(store->disk_bytes() == packet.size());
    Check(budget->Used(ReplayMemoryUse::Ring) >= packet.size());
    Check(!store->Push(packet.data(), packet.size()));
    Check(std::string_view(store->failure()).find("keep up") != std::string_view::npos);
    release_writer.set_value();
    Check(!pending->ReadInto(packet.data(), packet.size()));
    pending.reset();
    store.reset();
    Check(budget->Used(ReplayMemoryUse::Ring) == 0);
    RemoveReplayTestDirectory(folder);
}

void MissingDiskDirectoryFailsStartupAndCapture() {
    const auto folder = NewReplayTestDirectory();
    fthr::ReplayStorageConfig config;
    config.mode = fthr::ReplayStorageMode::Disk;
    config.directory = folder / "missing";
    std::string error;
    const auto budget = std::make_shared<fthr::ReplayMemoryBudget>(32768, 16384);
    Check(!fthr::ReplayStorage::Create(config, budget, error) && !error.empty());
    config.directory = folder;
    auto store = fthr::ReplayStorage::Create(config, budget, error);
    Check(bool(store));
    RemoveReplayTestDirectory(folder);
    uint8_t byte = 1;
    auto packet = store->Push(&byte, 1);
    Check(packet && !packet->ReadInto(&byte, 1));
    Check(!store->healthy());
    store.reset();
}
} // namespace

int RunReplayMemoryBudgetTests() {
    static_assert(!std::is_copy_constructible<fthr::EncodedRingSnapshot>::value,
                  "A copied snapshot must never reuse the original memory admission");
    AdmissionPreservesRequestedSettings();
    LowResourceAdmissionStaysBounded();
    ReservationsAreAtomicAndProtectCapture();
    SavesRetainAdmissionUntilPayloadIsReleased();
    GrowthFailurePreservesPublishedPacketsAndClearReleasesStorage();
    ActualSlotCapacityRemainsChargedAfterSmallerOverwrite();
    RotatingPeaksDoNotPoisonRetainedSlotCapacity();
    SlackAtAllowanceKeepsReusableStorage();
    OptionalCompactionAdmissionFailureKeepsCaptureHealthy();
    CompactionPreservesIndependentSnapshot();
    ZeroSizedOverwriteReleasesExcessStorage();
    SeveralSavesDrainAfterRingDestruction();
    EarlySnapshotReturnsDoNotRetainAdmission();
    StorageArgsPreservePositionals();
    AutomaticBudgetUsesCaptureRequirements();
    SnapshotsPinStorageAcrossRingReset(fthr::ReplayStorageMode::Memory);
    SnapshotsPinStorageAcrossRingReset(fthr::ReplayStorageMode::Disk);
    AudioReplayPreservesSamplesAndTiming(fthr::ReplayStorageMode::Memory);
    AudioReplayPreservesSamplesAndTiming(fthr::ReplayStorageMode::Disk);
    DiskQuotaIncludesPinnedPackets();
    DiskQueueIncludesInFlightMemory();
    MissingDiskDirectoryFailsStartupAndCapture();
    std::cout << "Replay memory tests: " << checks << " checks passed\n";
    return checks;
}

#ifdef FTHR_REPLAY_MEMORY_TEST_STANDALONE
int main() { RunReplayMemoryBudgetTests(); }
#endif
