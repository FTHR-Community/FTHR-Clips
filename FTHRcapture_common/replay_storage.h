#pragma once

#include "replay_memory_budget.h"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <functional>
#include <iostream>
#include <mutex>
#include <string_view>
#include <thread>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace fthr {

enum class ReplayStorageMode { Legacy, Memory, Disk };

struct ReplayStorageConfig {
    ReplayStorageMode mode = ReplayStorageMode::Legacy;
    std::filesystem::path directory;
    uint32_t disk_mb = 4096;
    uint32_t memory_mb = 0;
    bool automatic_budget = false;
    bool allow_low_resources = false;
};

inline bool ParseReplayStorageArgs(int& argc, char** argv,
                                   ReplayStorageConfig& config, std::string& error) {
    int first = 1;
    while (first < argc && std::string_view(argv[first]).rfind("--replay-", 0) != 0)
        ++first;
    bool explicit_budget_mode = false;
    bool manual_budget_values = false;
    for (int i = first; i < argc; i += 2) {
        const std::string_view flag(argv[i]);
        if (i + 1 >= argc) {
            error = "Missing value for " + std::string(flag);
            return false;
        }
        const std::string_view value(argv[i + 1]);
        if (flag == "--replay-storage" && value == "memory") {
            config.mode = ReplayStorageMode::Memory;
        } else if (flag == "--replay-storage" && value == "disk") {
            config.mode = ReplayStorageMode::Disk;
        } else if (flag == "--replay-budget-mode" && (value == "auto" || value == "manual")) {
            explicit_budget_mode = true;
            config.automatic_budget = value == "auto";
        } else if (flag == "--replay-low-resources" && value == "attempt") {
            config.allow_low_resources = true;
        } else if (flag == "--replay-storage-path") {
#if defined(__cpp_char8_t)
            config.directory = std::filesystem::path(
                reinterpret_cast<const char8_t*>(value.data()),
                reinterpret_cast<const char8_t*>(value.data() + value.size()));
#else
            config.directory = std::filesystem::u8path(value);
#endif
        } else if (flag == "--replay-storage-mb" || flag == "--replay-memory-mb") {
            const bool disk = flag == "--replay-storage-mb";
            uint32_t mb = 0;
            const auto parsed = std::from_chars(value.data(), value.data() + value.size(), mb);
            if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()
                || mb < 64 || mb > (disk ? 1048576u : 16384u)) {
                error = "Replay budget out of range for " + std::string(flag);
                return false;
            }
            (disk ? config.disk_mb : config.memory_mb) = mb;
            manual_budget_values = true;
        } else {
            error = "Invalid replay storage option " + std::string(flag) + " " + std::string(value);
            return false;
        }
    }
    if (first < argc && config.mode == ReplayStorageMode::Legacy) {
        error = "Specify --replay-storage memory or disk";
        return false;
    }
    if (!explicit_budget_mode) {
        config.automatic_budget = config.mode != ReplayStorageMode::Legacy && !manual_budget_values;
    } else if (config.automatic_budget && manual_budget_values) {
        error = "Automatic replay budgeting cannot be combined with manual budget limits";
        return false;
    }
    if (config.mode == ReplayStorageMode::Disk && !config.directory.is_absolute()) {
        error = "Choose an absolute path for the replay storage folder";
        return false;
    }
    argc = first;
    return true;
}

inline constexpr uint64_t kReplayMiB = 1024 * 1024;
inline constexpr uint64_t kReplayChunkBytes = 16 * kReplayMiB;
inline constexpr uint64_t kReplayWriteQueueBytes = 32 * kReplayMiB;

inline uint64_t EstimateReplayRingBytes(uint32_t seconds,
                                        uint32_t bitrate_kbps, uint32_t audio_tracks) {
    const uint64_t retained_seconds = static_cast<uint64_t>(seconds) + 5;
    const uint64_t video = retained_seconds * bitrate_kbps * 125;
    const uint64_t audio = retained_seconds * audio_tracks * 48000 * 2 * sizeof(float);
    return video + video / 4 + audio;
}

// Chunks keep their full on-disk allocation charged until the final ring or
// snapshot reference closes the handle. Queued writes also reserve disk bytes.
class ReplayStorageChunk {
public:
    explicit ReplayStorageChunk(const std::filesystem::path& directory,
                                std::shared_ptr<ReplayMemoryBudget> disk)
        : disk_(std::move(disk), ReplayMemoryUse::Ring) {
#ifdef _WIN32
        static std::atomic<uint64_t> sequence{0};
        for (int attempt = 0; attempt < 8; ++attempt) {
            const auto path = directory / (L"fthr-replay-" + std::to_wstring(GetCurrentProcessId())
                + L"-" + std::to_wstring(GetTickCount64()) + L"-"
                + std::to_wstring(sequence.fetch_add(1)) + L".tmp");
            file_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
            if (valid() || GetLastError() != ERROR_FILE_EXISTS) break;
        }
#else
        std::string name = (directory / "fthr-replay-XXXXXX").native();
        const int fd = mkstemp(name.data());
        if (fd < 0) return;
        if (unlink(name.c_str()) == 0) file_ = fdopen(fd, "w+b");
        if (!file_) close(fd);
#endif
    }

    ~ReplayStorageChunk() {
#ifdef _WIN32
        if (valid()) CloseHandle(file_);
#else
        if (valid()) std::fclose(file_);
#endif
    }

    bool valid() const {
#ifdef _WIN32
        return file_ != INVALID_HANDLE_VALUE;
#else
        return file_ != nullptr;
#endif
    }

    size_t size() const { return size_; } // writer thread only

    bool Append(const uint8_t* data, size_t size, uint64_t& offset,
                ReplayMemoryReservation& reservation) {
        std::lock_guard<std::mutex> lock(mutex_);
        offset = size_;
        // Transfer the reservation even if a partial write later fails.
        disk_.TransferFrom(reservation);
        if (!Seek(offset)) return false;
#ifdef _WIN32
        DWORD written = 0;
        if (!WriteFile(file_, data, static_cast<DWORD>(size), &written, nullptr)
            || written != size) return false;
#else
        if (std::fwrite(data, 1, size, file_) != size || std::fflush(file_) != 0) return false;
#endif
        size_ += size;
        return true;
    }

    bool Read(uint64_t offset, uint8_t* data, size_t size) const {
        std::lock_guard<std::mutex> lock(mutex_);
#ifdef _WIN32
        if (!valid() || size > MAXDWORD) return false;
        // An explicit offset on this synchronous handle avoids a separate
        // SetFilePointerEx call for each replay packet.
        OVERLAPPED position{};
        position.Offset = static_cast<DWORD>(offset);
        position.OffsetHigh = static_cast<DWORD>(offset >> 32);
        DWORD read = 0;
        return ReadFile(file_, data, static_cast<DWORD>(size), &read, &position) && read == size;
#else
        return Seek(offset) && std::fread(data, 1, size, file_) == size;
#endif
    }

private:
    bool Seek(uint64_t offset) const {
#ifdef _WIN32
        LARGE_INTEGER position{};
        position.QuadPart = static_cast<LONGLONG>(offset);
        return valid() && SetFilePointerEx(file_, position, nullptr, FILE_BEGIN);
#else
        return valid() && std::fseek(file_, static_cast<long>(offset), SEEK_SET) == 0;
#endif
    }

    ReplayMemoryReservation disk_;
    mutable std::mutex mutex_;
    size_t size_ = 0;
#ifdef _WIN32
    HANDLE file_ = INVALID_HANDLE_VALUE;
#else
    std::FILE* file_ = nullptr;
#endif
};

class ReplayStoragePayload {
public:
    size_t size() const { return size_; }
    bool is_silence() const { return silence_; }

    bool ReadInto(void* output, size_t size) const {
        return size == size_ && ReadSlice(0, output, size);
    }

    bool ReadSlice(size_t offset, void* output, size_t size) const {
        if (offset > size_ || size > size_ - offset) return false;
        if (silence_) {
            std::memset(output, 0, size);
            return true;
        }
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [this] { return !pending_; });
        if (!memory_.empty()) {
            std::memcpy(output, memory_.data() + offset, size);
            return true;
        }
        return chunk_ && chunk_->Read(offset_ + offset, static_cast<uint8_t*>(output), size);
    }

    std::shared_ptr<const std::vector<uint8_t>> Read() const {
        auto data = std::make_shared<std::vector<uint8_t>>(size_);
        return ReadInto(data->data(), size_) ? std::move(data) : nullptr;
    }

private:
    friend class ReplayStorage;
    ReplayStoragePayload(size_t size, bool pending, bool silence,
                         const std::shared_ptr<ReplayMemoryBudget>& memory,
                         const std::shared_ptr<ReplayMemoryBudget>& disk)
        : size_(size), pending_(pending), silence_(silence),
          metadata_(memory, ReplayMemoryUse::Ring), staging_(memory, ReplayMemoryUse::Ring),
          disk_(disk, ReplayMemoryUse::Ring) {}

    bool Complete(const std::shared_ptr<ReplayStorageChunk>& chunk) {
        const bool written = chunk && chunk->Append(memory_.data(), size_, offset_, disk_);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (written) chunk_ = chunk;
            disk_.Shrink(disk_.bytes());
            std::vector<uint8_t>().swap(memory_);
            staging_.Shrink(staging_.bytes());
            pending_ = false;
        }
        ready_.notify_all();
        return written;
    }

    const size_t size_;
    bool pending_;
    const bool silence_;
    ReplayMemoryReservation metadata_;
    ReplayMemoryReservation staging_;
    ReplayMemoryReservation disk_;
    std::vector<uint8_t> memory_;
    std::shared_ptr<ReplayStorageChunk> chunk_;
    uint64_t offset_ = 0;
    mutable std::mutex mutex_;
    mutable std::condition_variable ready_;
};

// One store per engine. Video and all participating audio sources share the
// queue, the writer and both budgets. Snapshots pin payloads independently of
// the live ring and can finish after ring reset.
class ReplayStorage {
public:
    static std::shared_ptr<ReplayStorage> Create(
        const ReplayStorageConfig& config, std::shared_ptr<ReplayMemoryBudget> memory,
        std::string& error, std::function<void()> before_write = {}) {
        const bool disk = config.mode == ReplayStorageMode::Disk;
        try {
            if (disk) {
                std::error_code ec;
                if (!std::filesystem::is_directory(config.directory, ec)) {
                    error = "The replay storage folder is missing or inaccessible.";
                    return {};
                }
            }
            auto store = std::shared_ptr<ReplayStorage>(new ReplayStorage(config, std::move(memory), std::move(before_write)));
            if (disk) {
                ReplayStorageChunk probe(config.directory, store->disk_);
                if (!probe.valid()) {
                    error = "Could not create a temporary replay file in the selected folder.";
                    return {};
                }
                store->worker_ = std::thread([ptr = store.get()] { ptr->Run(); });
            }
            return store;
        } catch (const std::exception& ex) {
            error = "Could not initialize replay storage: " + std::string(ex.what());
            return {};
        }
    }

    ~ReplayStorage() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        ready_.notify_one();
        if (worker_.joinable()) worker_.join();
    }

    std::shared_ptr<ReplayStoragePayload> Push(const uint8_t* bytes, size_t size,
                                               bool silence = false) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_ || !healthy() || !size || (!silence && !bytes)) return {};
        if (disk_ && !silence && (size > queue_limit_ - queued_bytes_ || pending_count_ >= 2048)) {
            SetFailure("Replay disk writes could not keep up with capture.");
            return {};
        }
        try {
            const bool queued = disk_ && !silence;
            auto payload = std::shared_ptr<ReplayStoragePayload>(
                new ReplayStoragePayload(size, queued, silence, memory_, disk_));
            if (!payload->metadata_.Grow(sizeof(ReplayStoragePayload) + 64)
                || (!silence && !payload->staging_.Grow(size))) {
                SetFailure("The replay RAM budget was exhausted.");
                return {};
            }
            if (queued && !payload->disk_.Grow(size)) {
                SetFailure("The replay disk budget was exhausted, including any clips being saved.");
                return {};
            }
            if (!silence) {
                payload->memory_.assign(bytes, bytes + size);
                if (!payload->staging_.Grow(payload->memory_.capacity() - size)) {
                    SetFailure("The replay RAM budget was exhausted.");
                    return {};
                }
            }
            if (queued) {
                queue_.push_back(payload);
                queued_bytes_ += size;
                ++pending_count_;
                ready_.notify_one();
            }
            return payload;
        } catch (const std::bad_alloc&) {
            SetFailure("Could not allocate memory for replay storage.");
            return {};
        }
    }

    // A single save can pin a complete ring while capture replaces it.
    // Keep the gate tied to the task lifetime, including queued saves.
    std::shared_ptr<const bool> BeginSave() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!save_.expired()) return {};
        auto lease = std::make_shared<const bool>(true);
        save_ = lease;
        return lease;
    }

    bool healthy() const { return !failure_.load(std::memory_order_acquire); }
    const char* failure() const {
        const auto error = failure_.load(std::memory_order_acquire);
        return error ? error : "";
    }
    uint64_t disk_bytes() const { return disk_ ? disk_->Used(ReplayMemoryUse::Ring) : 0; }
    uint64_t disk_limit() const { return disk_ ? disk_->total_limit() : 0; }
    std::shared_ptr<ReplayMemoryBudget> memory_budget() const { return memory_; }

private:
    ReplayStorage(const ReplayStorageConfig& config, std::shared_ptr<ReplayMemoryBudget> memory,
                  std::function<void()> before_write)
        : directory_(config.directory), memory_(std::move(memory)),
          disk_(config.mode == ReplayStorageMode::Disk
              ? std::make_shared<ReplayMemoryBudget>(config.disk_mb * kReplayMiB,
                                                     config.disk_mb * kReplayMiB) : nullptr),
          queue_limit_(std::min(kReplayWriteQueueBytes, memory_->ring_limit() / 4)),
          before_write_(std::move(before_write)) {}

    void SetFailure(const char* detail) {
        const char* expected = nullptr;
        failure_.compare_exchange_strong(expected, detail, std::memory_order_release);
    }

    void Run() {
        std::shared_ptr<ReplayStorageChunk> chunk;
        while (true) {
            std::shared_ptr<ReplayStoragePayload> payload;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
                if (queue_.empty()) return;
                payload = std::move(queue_.front());
                queue_.pop_front();
            }
            if (healthy()) {
                try {
                    if (before_write_) before_write_();
                    if (!chunk || chunk->size() + payload->size() > kReplayChunkBytes)
                        chunk = std::make_shared<ReplayStorageChunk>(directory_, disk_);
                    if (!chunk->valid())
                        SetFailure("Could not open the next replay file in the selected folder.");
                } catch (const std::exception&) {
                    SetFailure("Could not open the next replay file in the selected folder.");
                }
            }
            if (!payload->Complete(healthy() ? chunk : nullptr)) {
                SetFailure("Could not write replay data to the selected folder.");
                chunk.reset();
            }
            {
                std::lock_guard<std::mutex> lock(mutex_);
                queued_bytes_ -= payload->size();
                --pending_count_;
            }
        }
    }

    const std::filesystem::path directory_;
    const std::shared_ptr<ReplayMemoryBudget> memory_;
    const std::shared_ptr<ReplayMemoryBudget> disk_;
    const uint64_t queue_limit_;
    const std::function<void()> before_write_; // deterministic writer tests only
    std::atomic<const char*> failure_{nullptr};
    std::mutex mutex_;
    std::condition_variable ready_;
    std::weak_ptr<const bool> save_;
    std::deque<std::shared_ptr<ReplayStoragePayload>> queue_;
    uint64_t queued_bytes_ = 0;
    size_t pending_count_ = 0;
    bool stopping_ = false;
    std::thread worker_;
};

inline std::string DescribeAutomaticReplayBudget(const ReplayStorage& storage) {
    std::string detail = "[ReplayStorage] Automatic RAM budget: "
        + std::to_string((storage.memory_budget()->total_limit() + kReplayMiB - 1) / kReplayMiB)
        + " MiB";
    if (storage.disk_limit()) {
        detail += ", disk budget: " + std::to_string(storage.disk_limit() / kReplayMiB) + " MiB";
    }
    return detail;
}

// Use the same estimate for adaptive duration and admission. No allocations or
// mutable system-memory samples: one capture generation gets one stable plan.
inline uint64_t RequiredCaptureReplayMemory(
    const ReplayStorageConfig& config, uint64_t memory_ceiling_bytes,
    uint32_t seconds, uint32_t fps, uint32_t bitrate_kbps, uint32_t audio_tracks) {
    const bool disk = config.mode == ReplayStorageMode::Disk;
    const uint64_t history = EstimateReplayRingBytes(seconds, bitrate_kbps, audio_tracks);
    const uint64_t metadata = (static_cast<uint64_t>(seconds) + 5)
        * (fps * 512ULL + audio_tracks * 100 * 512ULL);
    const uint64_t ring_ceiling = memory_ceiling_bytes * 3 / 4;
    const uint64_t staging = !disk ? 0 : (config.automatic_budget
        ? kReplayWriteQueueBytes : std::min(kReplayWriteQueueBytes, ring_ceiling / 4));
    const uint64_t required_ring_bytes = 2 * metadata + 8 * kReplayMiB + staging
        + (disk ? 0 : 2 * history);
    uint64_t required_memory_bytes = std::max(64 * kReplayMiB, (required_ring_bytes * 4 + 2) / 3);
    if (config.automatic_budget) {
        // The other quarter of the budget covers snapshot metadata and muxing scratch.
        required_memory_bytes = std::max(required_memory_bytes, 4 * (metadata + 8 * kReplayMiB));
    }
    return required_memory_bytes;
}

// Automatic RAM mode can retain less history under pressure without changing
// user preferences, quality, or explicit budgets. Keep at least five seconds;
// if even that cannot fit, retain the normal actionable admission failure.
inline uint32_t EffectiveAutomaticReplaySeconds(
    const ReplayStorageConfig& config, uint64_t memory_ceiling_bytes,
    uint32_t requested, uint32_t fps, uint32_t bitrate_kbps, uint32_t audio_tracks) {
    if (config.mode != ReplayStorageMode::Memory || !config.automatic_budget
        || config.allow_low_resources || requested < 5 || requested > 900
        || !fps || fps > 360 || !bitrate_kbps || bitrate_kbps > 200000
        || audio_tracks > 128) return requested;
    const auto fits = [&](uint32_t seconds) {
        return RequiredCaptureReplayMemory(config, memory_ceiling_bytes,
            seconds, fps, bitrate_kbps, audio_tracks) <= memory_ceiling_bytes;
    };
    if (fits(requested) || !fits(5)) return requested;
    uint32_t low = 5, high = requested;
    while (low < high) {
        const uint32_t middle = low + (high - low + 1) / 2;
        if (fits(middle)) low = middle;
        else high = middle - 1;
    }
    return low;
}

inline std::shared_ptr<ReplayStorage> CreateCaptureReplayStorage(
    ReplayStorageConfig config, uint64_t memory_ceiling_bytes, uint32_t seconds,
    uint32_t fps, uint32_t bitrate_kbps, uint32_t audio_tracks, std::string& error) {
    const bool disk = config.mode == ReplayStorageMode::Disk;
    const uint64_t history = EstimateReplayRingBytes(seconds, bitrate_kbps, audio_tracks);
    const uint64_t required_memory_bytes = RequiredCaptureReplayMemory(
        config, memory_ceiling_bytes, seconds, fps, bitrate_kbps, audio_tracks);
    // Estimates reserve peak history plus a simultaneous save, not an upfront allocation.
    // Opt-in admission retains finite runtime budgets and ordinary allocation/write errors.
    auto capacity_check = [&](bool low, const char* code, const std::string& detail) {
        if (!low) return true;
        if (!config.allow_low_resources) {
            error = detail + " Adjust replay settings or enable Attempt saves with low resources.";
            return false;
        }
        std::cout << "FTHR_STARTUP_WARNING: " << code << ": " << detail
                  << " Trying anyway; capture or saving may fail." << std::endl;
        return true;
    };
    if (memory_ceiling_bytes < 64 * kReplayMiB) {
        error = "Replay needs at least 64 MiB of available RAM budget. Close other apps and retry.";
        return {};
    }
    if (!capacity_check(required_memory_bytes > memory_ceiling_bytes, "LOW_REPLAY_RAM",
        "Replay RAM estimate: " + std::to_string((required_memory_bytes + kReplayMiB - 1) / kReplayMiB)
        + " MiB; available budget: " + std::to_string(memory_ceiling_bytes / kReplayMiB)
        + " MiB.")) return {};
    const uint64_t memory_bytes = config.automatic_budget
        ? std::min(required_memory_bytes, memory_ceiling_bytes) : memory_ceiling_bytes;
    if (disk) {
        const uint64_t required_bytes = 2 * history + 2 * kReplayChunkBytes;
        const auto required_mb = (required_bytes + kReplayMiB - 1) / kReplayMiB;
        if (config.automatic_budget) {
            config.disk_mb = static_cast<uint32_t>(std::max<uint64_t>(64, required_mb));
        }
        if (!capacity_check(required_bytes > config.disk_mb * kReplayMiB, "LOW_REPLAY_STORAGE",
            "Replay storage estimate: " + std::to_string(required_mb)
            + " MiB; configured budget: " + std::to_string(config.disk_mb) + " MiB.")) return {};
        std::error_code ec;
        const auto space = std::filesystem::space(config.directory, ec);
        if (ec) {
            error = "The replay storage folder is missing or inaccessible.";
            return {};
        }
        // Allow the writer to fill its automatic quota with space left for
        // filesystem overhead and other writes to the selected volume.
        const uint64_t required_free_bytes = config.automatic_budget
            ? config.disk_mb * kReplayMiB + kReplayChunkBytes : required_bytes;
        if (!capacity_check(space.available < required_free_bytes, "LOW_REPLAY_STORAGE",
            "Replay storage estimate: "
            + std::to_string((required_free_bytes + kReplayMiB - 1) / kReplayMiB)
            + " MiB; free on this drive: " + std::to_string(space.available / kReplayMiB)
            + " MiB.")) return {};
    }
    return ReplayStorage::Create(config,
        std::make_shared<ReplayMemoryBudget>(memory_bytes, memory_bytes * 3 / 4), error);
}

} // namespace fthr
