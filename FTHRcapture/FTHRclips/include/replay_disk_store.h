#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace fthr::replay_disk {

enum class TrackKind : uint8_t {
    Video = 0,
    Audio = 1,
    Camera = 2,
};

struct ReplayPacket {
    uint32_t stream_id = 0;
    TrackKind kind = TrackKind::Video;
    int64_t pts = 0;
    int64_t dts = 0;
    int64_t duration = 0;
    int64_t qpc = 0;
    bool keyframe = false;
    std::vector<uint8_t> bytes;
};

struct ReplaySegment {
    uint64_t generation = 0;
    uint64_t sequence = 0;
    int64_t start_qpc = 0;
    int64_t end_qpc = 0;
    // The store treats this as opaque codec/track configuration bytes.
    std::vector<uint8_t> codec_description;
    std::vector<ReplayPacket> packets;
};

enum class SubmitStatus : uint8_t {
    Accepted,
    InvalidSegment,
    GenerationMismatch,
    QueueFull,
    QuotaExceeded,
    IoFailure,
    Stopped,
    SequenceMismatch,
};

enum class ReplayIoPoint : uint8_t {
    BeforeWrite,
    BeforeRead,
    BeforeClose,
};

// Optional deterministic hooks used by the native store tests. Production
// callers leave this unset; hooks are invoked outside the store state lock.
struct ReplayDiskTestHooks {
    std::function<void(ReplayIoPoint)> probe;
    std::function<bool(ReplayIoPoint)> fail;
};

struct SubmitResult {
    SubmitStatus status = SubmitStatus::InvalidSegment;
    uint64_t generation = 0;
    uint64_t logical_file_bytes = 0;
    std::string detail;

    explicit operator bool() const { return status == SubmitStatus::Accepted; }
};

struct ReplayDiskStats {
    uint64_t generation = 0;
    uint64_t queued_segments = 0;
    uint64_t inflight_segments = 0;
    uint64_t retained_segments = 0;
    uint64_t pinned_segments = 0;
    uint64_t queued_file_bytes = 0;
    uint64_t inflight_file_bytes = 0;
    uint64_t queued_memory_bytes = 0;
    uint64_t inflight_memory_bytes = 0;
    uint64_t retained_file_bytes = 0;
    uint64_t pinned_file_bytes = 0;
    // queued/inflight bytes describe future file demand. These are separate
    // from bytes/counts charged to the shared disk accounting lease.
    uint64_t disk_file_bytes = 0;
    uint64_t disk_file_count = 0;
    uint64_t eviction_pending_segments = 0;
    uint64_t eviction_pending_file_bytes = 0;
    uint64_t discarding_segments = 0;
    uint64_t discarding_memory_bytes = 0;
    bool accepting = false;
    bool io_failure_latched = false;
    SubmitStatus last_status = SubmitStatus::Accepted;
    std::string last_error;
};

class ReplayDiskStore final {
public:
    struct Config {
        std::filesystem::path work_directory;
        uint64_t max_disk_bytes = 512ULL * 1024ULL * 1024ULL;
        uint64_t max_segment_bytes = 64ULL * 1024ULL * 1024ULL;
        uint64_t max_queue_bytes = 128ULL * 1024ULL * 1024ULL;
        uint64_t max_queue_segments = 2;
        uint64_t max_segments = 32;
        std::shared_ptr<ReplayDiskTestHooks> test_hooks;
    };

    // The directory must already exist and be an absolute caller-selected path.
    // No recovery, enumeration, or cleanup of pre-existing files is performed.
    static std::unique_ptr<ReplayDiskStore> Create(
        Config config, std::string& error);

    ~ReplayDiskStore();

    ReplayDiskStore(const ReplayDiskStore&) = delete;
    ReplayDiskStore& operator=(const ReplayDiskStore&) = delete;

    SubmitResult Submit(ReplaySegment&& segment);

    // Atomically starts a new generation. Queued old-generation segments may
    // remain until the writer discards their vectors; an in-flight old segment
    // can never be published afterward. Fault latches are cleared here.
    uint64_t ResetGeneration();
    uint64_t CurrentGeneration() const;

    ReplayDiskStats Stats() const;

private:
    struct FileRecord;

public:

    struct Snapshot {
        uint64_t generation = 0;
        bool empty() const { return files_.empty(); }
        size_t size() const { return files_.size(); }
        bool Materialize(std::vector<ReplaySegment>& segments,
                         std::string& error) const;

    private:
        std::vector<std::shared_ptr<const FileRecord>> files_;
        friend class ReplayDiskStore;
    };

    Snapshot SnapshotGeneration(uint64_t generation = 0) const;

    // Reads and validates each complete segment outside the store state lock.
    // The snapshot keeps its file handles alive even after this store is gone.
    bool Materialize(const Snapshot& snapshot,
                     std::vector<ReplaySegment>& segments,
                     std::string& error) const;

    // Waits up to timeout_ms for accepted queue work, stale-discard work, and
    // pending retired-file closes to become idle. Drain does not stop new
    // submissions and true only means that the store is idle; callers must
    // inspect Stats() for a latched I/O fault. Shutdown stops submissions and
    // the destructor may block on Win32 I/O.
    bool Drain(uint32_t timeout_ms);
    void Shutdown();

private:
    explicit ReplayDiskStore(Config config);

    struct Accounting;
    struct DiskPermit;
    struct Pending;
    struct WriteResult;

    Config config_;
    class Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace fthr::replay_disk
