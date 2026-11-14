#include "replay_disk_store.h"

#define NOMINMAX
#include <windows.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <list>
#include <limits>
#include <mutex>
#include <random>
#include <sstream>
#include <thread>
#include <utility>

namespace fthr::replay_disk {
namespace {

constexpr uint32_t kWireVersion = 1;
constexpr size_t kMagicBytes = 8;
constexpr size_t kFixedHeaderBytes = 56;
constexpr size_t kPacketHeaderBytes = 48;
constexpr size_t kMaxCodecDescriptionBytes = 1024 * 1024;
constexpr size_t kMaxPackets = 1'000'000;
constexpr wchar_t kFilePrefix[] = L"fthr-replay-v1-";
const uint8_t kMagic[kMagicBytes] = {'F', 'T', 'H', 'R', 'D', 'S', '0', '1'};

bool AddChecked(uint64_t left, uint64_t right, uint64_t* result) {
    if (right > std::numeric_limits<uint64_t>::max() - left) return false;
    *result = left + right;
    return true;
}

bool IsTrackKindValid(TrackKind kind) {
    return kind == TrackKind::Video || kind == TrackKind::Audio
        || kind == TrackKind::Camera;
}

bool IsAbsoluteDirectory(const std::filesystem::path& directory,
                         std::string& error) {
    if (directory.empty() || !directory.is_absolute()) {
        error = "work_directory must be an absolute path";
        return false;
    }
    const std::wstring path = directory.wstring();
    const DWORD attributes = GetFileAttributesW(path.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        error = "work_directory does not exist";
        return false;
    }
    if ((attributes & FILE_ATTRIBUTE_DIRECTORY) == 0) {
        error = "work_directory is not a directory";
        return false;
    }
    return true;
}

void AppendU32(std::vector<uint8_t>& output, uint32_t value) {
    output.push_back(static_cast<uint8_t>(value));
    output.push_back(static_cast<uint8_t>(value >> 8));
    output.push_back(static_cast<uint8_t>(value >> 16));
    output.push_back(static_cast<uint8_t>(value >> 24));
}

void AppendU64(std::vector<uint8_t>& output, uint64_t value) {
    for (unsigned shift = 0; shift < 64; shift += 8)
        output.push_back(static_cast<uint8_t>(value >> shift));
}

bool ReadU32(const std::vector<uint8_t>& bytes, size_t& offset,
             uint32_t* value) {
    if (offset > bytes.size() || bytes.size() - offset < 4) return false;
    *value = static_cast<uint32_t>(bytes[offset])
        | (static_cast<uint32_t>(bytes[offset + 1]) << 8)
        | (static_cast<uint32_t>(bytes[offset + 2]) << 16)
        | (static_cast<uint32_t>(bytes[offset + 3]) << 24);
    offset += 4;
    return true;
}

bool ReadU64(const std::vector<uint8_t>& bytes, size_t& offset,
             uint64_t* value) {
    if (offset > bytes.size() || bytes.size() - offset < 8) return false;
    *value = 0;
    for (unsigned shift = 0; shift < 64; shift += 8)
        *value |= static_cast<uint64_t>(bytes[offset++]) << shift;
    return true;
}

std::string Win32Error(DWORD error) {
    std::ostringstream stream;
    stream << "Win32 error " << error;
    return stream.str();
}

std::wstring MakeCandidateName(uint64_t serial) {
    LARGE_INTEGER counter{};
    QueryPerformanceCounter(&counter);
    std::random_device random;
    const uint64_t random_value = (static_cast<uint64_t>(random()) << 32)
        ^ static_cast<uint64_t>(random());
    wchar_t buffer[128]{};
    swprintf_s(buffer, L"%s%08lx-%016llx-%016llx-%016llx.seg", kFilePrefix,
               static_cast<unsigned long>(GetCurrentProcessId()),
               static_cast<unsigned long long>(counter.QuadPart),
               static_cast<unsigned long long>(serial),
               static_cast<unsigned long long>(random_value));
    return buffer;
}

bool InvokeHook(const std::shared_ptr<const ReplayDiskTestHooks>& hooks,
                ReplayIoPoint point) {
    if (!hooks) return false;
    if (hooks->probe) hooks->probe(point);
    return hooks->fail && hooks->fail(point);
}

struct SegmentSizing {
    uint64_t logical_bytes = 0;
    uint64_t admission_bytes = 0;
};

bool SizeSegment(const ReplaySegment& segment, SegmentSizing* sizing,
                 std::string& error) {
    if (segment.generation == 0 || segment.sequence == 0) {
        error = "generation and sequence must be nonzero";
        return false;
    }
    if (segment.start_qpc > segment.end_qpc) {
        error = "segment QPC range is inverted";
        return false;
    }
    if (segment.codec_description.size() > kMaxCodecDescriptionBytes
        || segment.codec_description.capacity() > kMaxCodecDescriptionBytes) {
        error = "codec description exceeds the bounded limit";
        return false;
    }
    if (segment.packets.empty() || segment.packets.size() > kMaxPackets
        || segment.packets.capacity() > kMaxPackets) {
        error = "segment must contain a bounded packet list";
        return false;
    }

    uint64_t logical = kFixedHeaderBytes;
    uint64_t admission = sizeof(ReplaySegment);
    if (segment.packets.capacity() > std::numeric_limits<uint64_t>::max()
            / sizeof(ReplayPacket)
        || !AddChecked(admission,
                       static_cast<uint64_t>(segment.packets.capacity())
                           * sizeof(ReplayPacket),
                       &admission)
        || !AddChecked(admission, segment.codec_description.capacity(), &admission)) {
        error = "segment vector capacity overflows";
        return false;
    }
    if (!AddChecked(logical, segment.codec_description.size(), &logical)) {
        error = "segment size overflows";
        return false;
    }
    uint32_t first_video_stream = 0;
    bool saw_video = false;
    for (const ReplayPacket& packet : segment.packets) {
        if (!IsTrackKindValid(packet.kind) || packet.stream_id == 0) {
            error = "packet has an invalid track kind or stream id";
            return false;
        }
        if (packet.bytes.empty()) {
            error = "packet payload must not be empty";
            return false;
        }
        if (packet.kind == TrackKind::Video && !saw_video) {
            saw_video = true;
            first_video_stream = packet.stream_id;
            if (!packet.keyframe) {
                error = "primary video stream must begin with a keyframe";
                return false;
            }
        }
        if (!AddChecked(logical, kPacketHeaderBytes, &logical)
            || !AddChecked(logical, packet.bytes.size(), &logical)
            || !AddChecked(admission, kPacketHeaderBytes, &admission)
            || !AddChecked(admission, packet.bytes.capacity(), &admission)) {
            error = "packet size overflows";
            return false;
        }
        if (packet.bytes.size() > std::numeric_limits<uint32_t>::max()
            || packet.bytes.capacity() > std::numeric_limits<uint32_t>::max()) {
            error = "packet payload is too large";
            return false;
        }
    }
    if (!saw_video || first_video_stream == 0) {
        error = "segment has no primary video stream";
        return false;
    }
    if (!AddChecked(admission, logical, &admission)) {
        error = "serialization scratch size overflows";
        return false;
    }
    sizing->logical_bytes = logical;
    sizing->admission_bytes = admission;
    return true;
}

bool SerializeSegment(const ReplaySegment& segment,
                      std::vector<uint8_t>& bytes, std::string& error) {
    SegmentSizing sizing;
    if (!SizeSegment(segment, &sizing, error)) return false;
    if (sizing.logical_bytes > std::numeric_limits<size_t>::max()) {
        error = "serialized segment is too large";
        return false;
    }
    bytes.clear();
    bytes.reserve(static_cast<size_t>(sizing.logical_bytes));
    bytes.insert(bytes.end(), std::begin(kMagic), std::end(kMagic));
    AppendU32(bytes, kWireVersion);
    AppendU32(bytes, static_cast<uint32_t>(kFixedHeaderBytes));
    AppendU64(bytes, segment.generation);
    AppendU64(bytes, segment.sequence);
    AppendU64(bytes, static_cast<uint64_t>(segment.start_qpc));
    AppendU64(bytes, static_cast<uint64_t>(segment.end_qpc));
    AppendU32(bytes, static_cast<uint32_t>(segment.codec_description.size()));
    AppendU32(bytes, static_cast<uint32_t>(segment.packets.size()));
    bytes.insert(bytes.end(), segment.codec_description.begin(),
                 segment.codec_description.end());
    for (const ReplayPacket& packet : segment.packets) {
        AppendU32(bytes, packet.stream_id);
        bytes.push_back(static_cast<uint8_t>(packet.kind));
        bytes.insert(bytes.end(), 3, 0);
        AppendU64(bytes, static_cast<uint64_t>(packet.pts));
        AppendU64(bytes, static_cast<uint64_t>(packet.dts));
        AppendU64(bytes, static_cast<uint64_t>(packet.duration));
        AppendU64(bytes, static_cast<uint64_t>(packet.qpc));
        bytes.push_back(packet.keyframe ? 1 : 0);
        bytes.insert(bytes.end(), 3, 0);
        AppendU32(bytes, static_cast<uint32_t>(packet.bytes.size()));
        bytes.insert(bytes.end(), packet.bytes.begin(), packet.bytes.end());
    }
    if (bytes.size() != sizing.logical_bytes) {
        error = "internal segment serialization size mismatch";
        return false;
    }
    return true;
}

bool DeserializeSegment(const std::vector<uint8_t>& bytes,
                        ReplaySegment& segment, std::string& error) {
    if (bytes.size() < kFixedHeaderBytes
        || !std::equal(std::begin(kMagic), std::end(kMagic), bytes.begin())) {
        error = "segment file header is invalid";
        return false;
    }
    size_t offset = kMagicBytes;
    uint32_t version = 0;
    uint32_t header_bytes = 0;
    uint64_t generation = 0;
    uint64_t sequence = 0;
    uint64_t start_qpc = 0;
    uint64_t end_qpc = 0;
    uint32_t codec_bytes = 0;
    uint32_t packet_count = 0;
    if (!ReadU32(bytes, offset, &version)
        || !ReadU32(bytes, offset, &header_bytes)
        || !ReadU64(bytes, offset, &generation)
        || !ReadU64(bytes, offset, &sequence)
        || !ReadU64(bytes, offset, &start_qpc)
        || !ReadU64(bytes, offset, &end_qpc)
        || !ReadU32(bytes, offset, &codec_bytes)
        || !ReadU32(bytes, offset, &packet_count)
        || version != kWireVersion || header_bytes != kFixedHeaderBytes
        || codec_bytes > kMaxCodecDescriptionBytes
        || packet_count == 0 || packet_count > kMaxPackets
        || offset > bytes.size() || bytes.size() - offset < codec_bytes) {
        error = "segment file metadata is invalid";
        return false;
    }
    const size_t codec_offset = offset;
    offset += codec_bytes;
    if (packet_count > (bytes.size() - offset) / kPacketHeaderBytes) {
        error = "segment packet count exceeds remaining file bounds";
        return false;
    }
    segment = {};
    segment.generation = generation;
    segment.sequence = sequence;
    segment.start_qpc = static_cast<int64_t>(start_qpc);
    segment.end_qpc = static_cast<int64_t>(end_qpc);
    segment.codec_description.assign(bytes.begin() + codec_offset,
                                     bytes.begin() + offset);
    segment.packets.reserve(packet_count);
    for (uint32_t index = 0; index < packet_count; ++index) {
        if (offset > bytes.size() || bytes.size() - offset < kPacketHeaderBytes) {
            error = "segment packet header is truncated";
            return false;
        }
        ReplayPacket packet;
        uint32_t kind = 0;
        uint64_t pts = 0;
        uint64_t dts = 0;
        uint64_t duration = 0;
        uint64_t qpc = 0;
        uint32_t payload_bytes = 0;
        if (!ReadU32(bytes, offset, &packet.stream_id)) {
            error = "segment packet stream id is truncated";
            return false;
        }
        kind = bytes[offset++];
        offset += 3;
        if (!ReadU64(bytes, offset, &pts)
            || !ReadU64(bytes, offset, &dts)
            || !ReadU64(bytes, offset, &duration)
            || !ReadU64(bytes, offset, &qpc)) {
            error = "segment packet timing is truncated";
            return false;
        }
        packet.keyframe = bytes[offset++] != 0;
        offset += 3;
        if (!ReadU32(bytes, offset, &payload_bytes)
            || kind > static_cast<uint32_t>(TrackKind::Camera)
            || packet.stream_id == 0 || payload_bytes == 0
            || offset > bytes.size() || bytes.size() - offset < payload_bytes) {
            error = "segment packet metadata is invalid";
            return false;
        }
        packet.kind = static_cast<TrackKind>(kind);
        packet.pts = static_cast<int64_t>(pts);
        packet.dts = static_cast<int64_t>(dts);
        packet.duration = static_cast<int64_t>(duration);
        packet.qpc = static_cast<int64_t>(qpc);
        packet.bytes.assign(bytes.begin() + offset,
                            bytes.begin() + offset + payload_bytes);
        offset += payload_bytes;
        segment.packets.push_back(std::move(packet));
    }
    if (offset != bytes.size()) {
        error = "segment file has trailing bytes";
        return false;
    }
    SegmentSizing sizing;
    return SizeSegment(segment, &sizing, error) && sizing.logical_bytes == bytes.size();
}

bool WriteAll(HANDLE handle, const uint8_t* data, size_t size,
              const std::shared_ptr<const ReplayDiskTestHooks>& hooks,
              std::string& error) {
    while (size > 0) {
        if (InvokeHook(hooks, ReplayIoPoint::BeforeWrite)) {
            error = "injected write failure";
            return false;
        }
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(size, 1U << 20));
        DWORD written = 0;
        if (!WriteFile(handle, data, chunk, &written, nullptr)
            || written != chunk) {
            error = Win32Error(GetLastError());
            return false;
        }
        data += written;
        size -= written;
    }
    return true;
}

bool ReadAll(HANDLE handle, uint64_t expected_bytes,
             const std::shared_ptr<const ReplayDiskTestHooks>& hooks,
             std::vector<uint8_t>& bytes, std::string& error) {
    if (expected_bytes > std::numeric_limits<size_t>::max()) {
        error = "segment file is too large to materialize";
        return false;
    }
    bytes.assign(static_cast<size_t>(expected_bytes), 0);
    size_t offset = 0;
    while (offset < bytes.size()) {
        if (InvokeHook(hooks, ReplayIoPoint::BeforeRead)) {
            error = "injected read failure";
            return false;
        }
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(
            bytes.size() - offset, 1U << 20));
        DWORD read = 0;
        if (!ReadFile(handle, bytes.data() + offset, chunk, &read, nullptr)
            || read == 0) {
            error = Win32Error(GetLastError());
            return false;
        }
        offset += read;
    }
    return true;
}

struct UniqueHandle {
    HANDLE value = INVALID_HANDLE_VALUE;
    ~UniqueHandle() {
        if (value != INVALID_HANDLE_VALUE) CloseHandle(value);
    }
    HANDLE release() {
        const HANDLE result = value;
        value = INVALID_HANDLE_VALUE;
        return result;
    }
};

}  // namespace

struct ReplayDiskStore::Accounting {
    explicit Accounting(uint64_t max_bytes, uint64_t max_files)
        : max_bytes(max_bytes), max_files(max_files) {}

    bool TryCharge(uint64_t bytes) {
        std::lock_guard<std::mutex> lock(mutex);
        if (bytes > max_bytes - charged_bytes || charged_files >= max_files)
            return false;
        charged_bytes += bytes;
        ++charged_files;
        return true;
    }

    void Release(uint64_t bytes) {
        std::lock_guard<std::mutex> lock(mutex);
        if (charged_bytes >= bytes) charged_bytes -= bytes;
        else charged_bytes = 0;
        if (charged_files > 0) --charged_files;
    }

    void Snapshot(uint64_t* bytes, uint64_t* files) const {
        std::lock_guard<std::mutex> lock(mutex);
        *bytes = charged_bytes;
        *files = charged_files;
    }

    const uint64_t max_bytes;
    const uint64_t max_files;
    mutable std::mutex mutex;
    uint64_t charged_bytes = 0;
    uint64_t charged_files = 0;
};

struct ReplayDiskStore::DiskPermit {
    std::shared_ptr<Accounting> accounting;
    uint64_t bytes = 0;
    bool charged = false;

    ~DiskPermit() {
        if (charged && accounting) accounting->Release(bytes);
    }
};

struct ReplayDiskStore::FileRecord {
    HANDLE handle = INVALID_HANDLE_VALUE;
    uint64_t generation = 0;
    uint64_t sequence = 0;
    uint64_t logical_file_bytes = 0;
    std::shared_ptr<const ReplayDiskTestHooks> hooks;
    std::unique_ptr<DiskPermit> permit;
    mutable std::mutex io_mutex;
    bool closing = false;  // protected by the store state mutex

    bool Close() {
        std::lock_guard<std::mutex> lock(io_mutex);
        if (handle == INVALID_HANDLE_VALUE) {
            if (permit) permit.reset();
            return true;
        }
        bool forced_failure = false;
        try {
            forced_failure = InvokeHook(hooks, ReplayIoPoint::BeforeClose);
        } catch (...) {
            forced_failure = true;
        }
        // A test-injected close failure models a real close that did not
        // happen. Keep both the handle and accounting lease so a later
        // cleanup pass can retry without making quota lie.
        if (forced_failure) return false;
        const BOOL closed = CloseHandle(handle);
        if (closed == FALSE) return false;
        handle = INVALID_HANDLE_VALUE;
        permit.reset();
        return true;
    }

    ~FileRecord() {
        (void)Close();
        // If the OS or an injected hook prevents closing, do not let the
        // member permit destructor release bytes while the handle is alive.
        // The conservative leak is process-boundary cleanup rather than an
        // incorrect quota release.
        if (handle != INVALID_HANDLE_VALUE) (void)permit.release();
    }
};

struct ReplayDiskStore::Pending {
    ReplaySegment segment;
    SegmentSizing sizing;
};

struct ReplayDiskStore::WriteResult {
    std::shared_ptr<FileRecord> file;
    SubmitStatus status = SubmitStatus::IoFailure;
    std::string error;
};

class ReplayDiskStore::Impl {
public:
    explicit Impl(Config config)
        : config(std::move(config)),
          hooks(this->config.test_hooks),
          accounting(std::make_shared<Accounting>(
              this->config.max_disk_bytes, this->config.max_segments)) {}

    Config config;
    std::shared_ptr<const ReplayDiskTestHooks> hooks;
    std::shared_ptr<Accounting> accounting;
    mutable std::mutex mutex;
    std::condition_variable wake;
    std::condition_variable drained;
    std::deque<Pending> queue;
    std::list<std::shared_ptr<FileRecord>> retained;
    std::list<std::shared_ptr<FileRecord>> retired;
    std::thread writer;
    uint64_t generation = 1;
    uint64_t last_sequence = 0;
    uint64_t reserved_count = 0;
    uint64_t reserved_memory_bytes = 0;
    uint64_t queued_file_bytes = 0;
    uint64_t inflight_file_bytes = 0;
    uint64_t queued_memory_bytes = 0;
    uint64_t inflight_memory_bytes = 0;
    uint64_t inflight_count = 0;
    uint64_t discarding_segments = 0;
    uint64_t discarding_memory_bytes = 0;
    uint64_t serial = 0;
    bool accepting = true;
    bool stopping = false;
    bool fault_latched = false;
    bool io_failure = false;
    bool cleanup_requested = false;
    bool cleanup_inflight = false;
    uint64_t next_cleanup_token = 0;
    uint64_t cleanup_requested_token = 0;
    uint64_t cleanup_inflight_token = 0;
    uint64_t cleanup_inflight_generation = 0;
    SubmitStatus last_status = SubmitStatus::Accepted;
    std::string last_error;

    void LatchLocked(SubmitStatus status, const std::string& error,
                     std::deque<Pending>& discard) {
        accepting = false;
        fault_latched = true;
        io_failure = status == SubmitStatus::IoFailure;
        last_status = status;
        last_error = error;
        discard.swap(queue);
        queued_file_bytes = 0;
        queued_memory_bytes = 0;
        for (const Pending& pending : discard) {
            ++discarding_segments;
            discarding_memory_bytes += pending.sizing.admission_bytes;
        }
    }

    enum class CloseAttempt : uint8_t { None, Closed, Failed };

    CloseAttempt CloseOneRetired(std::string& error) {
        std::shared_ptr<FileRecord> candidate;
        {
            std::lock_guard<std::mutex> lock(mutex);
            for (const auto& file : retired) {
                if (!file->closing && file.use_count() == 1) {
                    candidate = file;
                    file->closing = true;
                    break;
                }
            }
        }
        if (!candidate) return CloseAttempt::None;
        if (!candidate->Close()) {
            error = "replay segment close failed";
            std::lock_guard<std::mutex> lock(mutex);
            candidate->closing = false;
            return CloseAttempt::Failed;
        }
        {
            std::lock_guard<std::mutex> lock(mutex);
            for (auto iterator = retired.begin(); iterator != retired.end();
                 ++iterator) {
                if (iterator->get() == candidate.get()) {
                    retired.erase(iterator);
                    break;
                }
            }
        }
        return CloseAttempt::Closed;
    }

    CloseAttempt CloseOneActiveCandidate(std::string& error) {
        std::shared_ptr<FileRecord> candidate;
        {
            std::lock_guard<std::mutex> lock(mutex);
            // The active history is a contiguous suffix. Pinned prefixes are
            // moved as list nodes, without allocation, until the first
            // unpinned GOP is found.
            while (!retained.empty() && retained.front().use_count() > 1)
                retired.splice(retired.end(), retained, retained.begin());
            if (!retained.empty() && !retained.front()->closing) {
                candidate = retained.front();
                candidate->closing = true;
            }
        }
        if (!candidate) return CloseAttempt::None;
        if (!candidate->Close()) {
            error = "replay segment close failed";
            std::lock_guard<std::mutex> lock(mutex);
            candidate->closing = false;
            return CloseAttempt::Failed;
        }
        {
            std::lock_guard<std::mutex> lock(mutex);
            for (auto iterator = retained.begin(); iterator != retained.end();
                 ++iterator) {
                if (iterator->get() == candidate.get()) {
                    retained.erase(iterator);
                    return CloseAttempt::Closed;
                }
            }
            // Reset may splice the node while the handle is being closed.
            for (auto iterator = retired.begin(); iterator != retired.end();
                 ++iterator) {
                if (iterator->get() == candidate.get()) {
                    retired.erase(iterator);
                    return CloseAttempt::Closed;
                }
            }
        }
        return CloseAttempt::Closed;
    }

    std::unique_ptr<DiskPermit> TryReserve(uint64_t logical_bytes) {
        auto permit = std::make_unique<DiskPermit>();
        permit->accounting = accounting;
        permit->bytes = logical_bytes;
        if (!accounting->TryCharge(logical_bytes)) return nullptr;
        permit->charged = true;
        return permit;
    }

    std::unique_ptr<DiskPermit> AcquireDiskPermit(
        uint64_t logical_bytes, uint64_t pending_generation,
        SubmitStatus& status, std::string& error) {
        for (;;) {
            const CloseAttempt retired_close = CloseOneRetired(error);
            if (retired_close == CloseAttempt::Failed) {
                    status = SubmitStatus::IoFailure;
                    return nullptr;
            }
            if (retired_close == CloseAttempt::Closed)
                continue;

            auto permit = TryReserve(logical_bytes);
            if (permit) return permit;

            {
                std::lock_guard<std::mutex> lock(mutex);
                if (pending_generation != generation) {
                    status = SubmitStatus::GenerationMismatch;
                    error = "segment generation changed while reserving disk space";
                    return nullptr;
                }
            }
            const CloseAttempt active_close = CloseOneActiveCandidate(error);
            if (active_close == CloseAttempt::None) {
                status = SubmitStatus::QuotaExceeded;
                error = "max_disk_bytes or max_segments has no reclaimable GOP";
                return nullptr;
            }
            if (active_close == CloseAttempt::Failed) {
                status = SubmitStatus::IoFailure;
                return nullptr;
            }
        }
    }

    WriteResult Write(Pending&& pending) {
        WriteResult result;
        try {
            std::vector<uint8_t> bytes;
            if (!SerializeSegment(pending.segment, bytes, result.error)) {
                result.status = SubmitStatus::InvalidSegment;
                return result;
            }
            auto permit = AcquireDiskPermit(pending.sizing.logical_bytes,
                                             pending.segment.generation,
                                             result.status, result.error);
            if (!permit) return result;

            UniqueHandle handle;
            std::wstring path;
            for (unsigned attempt = 0; attempt != 32; ++attempt) {
                path = config.work_directory / MakeCandidateName(++serial);
                handle.value = CreateFileW(path.c_str(),
                    GENERIC_READ | GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                    FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE,
                    nullptr);
                if (handle.value != INVALID_HANDLE_VALUE) break;
                if (GetLastError() != ERROR_FILE_EXISTS
                    && GetLastError() != ERROR_ALREADY_EXISTS) {
                    result.error = Win32Error(GetLastError());
                    return result;
                }
            }
            if (handle.value == INVALID_HANDLE_VALUE) {
                result.error = "could not allocate a unique replay segment file";
                return result;
            }
            if (!WriteAll(handle.value, bytes.data(), bytes.size(), hooks,
                          result.error)
                || !FlushFileBuffers(handle.value)) {
                if (result.error.empty()) result.error = Win32Error(GetLastError());
                return result;
            }
            auto file = std::make_shared<FileRecord>();
            file->handle = handle.release();
            file->generation = pending.segment.generation;
            file->sequence = pending.segment.sequence;
            file->logical_file_bytes = bytes.size();
            file->hooks = hooks;
            file->permit = std::move(permit);
            result.file = std::move(file);
            result.status = SubmitStatus::Accepted;
            return result;
        } catch (const std::exception&) {
            result.status = SubmitStatus::IoFailure;
            if (result.error.empty()) result.error = "replay write threw an exception";
            return result;
        } catch (...) {
            result.status = SubmitStatus::IoFailure;
            if (result.error.empty()) result.error = "replay write threw an unknown exception";
            return result;
        }
    }

    void FinishDiscard(std::deque<Pending>& discard) {
        const uint64_t count = discard.size();
        uint64_t memory = 0;
        for (const Pending& pending : discard)
            memory += pending.sizing.admission_bytes;
        discard.clear();
        std::lock_guard<std::mutex> lock(mutex);
        discarding_segments -= count;
        discarding_memory_bytes -= memory;
        reserved_count -= count;
        reserved_memory_bytes -= memory;
    }

    void FinishPending(uint64_t file_bytes, uint64_t memory_bytes) {
        std::lock_guard<std::mutex> lock(mutex);
        --inflight_count;
        inflight_file_bytes -= file_bytes;
        inflight_memory_bytes -= memory_bytes;
        --reserved_count;
        reserved_memory_bytes -= memory_bytes;
        if (queue.empty() && inflight_count == 0 && discarding_segments == 0
            && !cleanup_requested && !cleanup_inflight) drained.notify_all();
    }

    void RunCleanup(uint64_t cleanup_token, uint64_t cleanup_generation) {
        std::string error;
        bool ok = true;
        try {
            for (;;) {
                const CloseAttempt outcome = CloseOneRetired(error);
                if (outcome == CloseAttempt::None) break;
                if (outcome == CloseAttempt::Failed) {
                    ok = false;
                    break;
                }
            }
        } catch (...) {
            ok = false;
            error = "retired replay cleanup threw an exception";
        }
        std::deque<Pending> discard;
        {
            std::lock_guard<std::mutex> lock(mutex);
            cleanup_inflight = false;
            const bool same_request = cleanup_requested_token == cleanup_token;
            if (same_request) cleanup_requested = false;
            const bool same_generation = generation == cleanup_generation;
            if (!ok && !error.empty() && !stopping && same_request
                && same_generation)
                LatchLocked(SubmitStatus::IoFailure, error, discard);
        }
        FinishDiscard(discard);
        drained.notify_all();
    }

    void Run() {
        for (;;) {
            Pending pending;
            bool have_pending = false;
            bool do_cleanup = false;
            {
                std::unique_lock<std::mutex> lock(mutex);
                wake.wait(lock, [this] {
                    return stopping || !queue.empty()
                        || (cleanup_requested && !cleanup_inflight);
                });
                if (!queue.empty()) {
                    pending = std::move(queue.front());
                    queue.pop_front();
                    queued_file_bytes -= pending.sizing.logical_bytes;
                    queued_memory_bytes -= pending.sizing.admission_bytes;
                    inflight_file_bytes += pending.sizing.logical_bytes;
                    inflight_memory_bytes += pending.sizing.admission_bytes;
                    ++inflight_count;
                    have_pending = true;
                } else if (cleanup_requested && !cleanup_inflight) {
                    cleanup_inflight = true;
                    cleanup_inflight_token = cleanup_requested_token;
                    cleanup_inflight_generation = generation;
                    do_cleanup = true;
                } else if (stopping) {
                    break;
                }
            }
            if (do_cleanup) {
                uint64_t cleanup_token = 0;
                uint64_t cleanup_generation = 0;
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    cleanup_token = cleanup_inflight_token;
                    cleanup_generation = cleanup_inflight_generation;
                }
                RunCleanup(cleanup_token, cleanup_generation);
                continue;
            }
            if (!have_pending) continue;

            const uint64_t pending_generation = pending.segment.generation;
            const uint64_t pending_file_bytes = pending.sizing.logical_bytes;
            const uint64_t pending_memory_bytes = pending.sizing.admission_bytes;
            WriteResult result;
            try {
                Pending local = std::move(pending);
                bool stale = false;
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    stale = pending_generation != generation;
                }
                if (stale) {
                    result.status = SubmitStatus::GenerationMismatch;
                    result.error = "queued segment belongs to an old generation";
                } else {
                    result = Write(std::move(local));
                }
            } catch (...) {
                result.status = SubmitStatus::IoFailure;
                result.error = "replay worker caught an unexpected exception";
            }

            std::shared_ptr<FileRecord> discard_file;
            std::deque<Pending> discard;
            bool published = false;
            {
                std::lock_guard<std::mutex> lock(mutex);
                const bool own_generation = pending_generation == generation;
                if (result.file && result.status == SubmitStatus::Accepted
                    && own_generation && !fault_latched) {
                    try {
                        retained.push_back(std::move(result.file));
                        published = true;
                    } catch (...) {
                        result.status = SubmitStatus::IoFailure;
                        result.error = "replay history publication threw an exception";
                    }
                }
                if (!published && result.file)
                    discard_file = std::move(result.file);
                if (!published && own_generation
                    && (result.status == SubmitStatus::QuotaExceeded
                        || result.status == SubmitStatus::IoFailure)) {
                    LatchLocked(result.status,
                                result.error.empty() ? "replay write failed" : result.error,
                                discard);
                }
            }
            discard_file.reset();
            FinishDiscard(discard);
            FinishPending(pending_file_bytes, pending_memory_bytes);
        }

        std::list<std::shared_ptr<FileRecord>> release;
        {
            std::lock_guard<std::mutex> lock(mutex);
            release.splice(release.end(), retained);
            release.splice(release.end(), retired);
            // Final writer-owned closes are still asynchronous from the
            // store lock's point of view. Keep Drain() busy until the close
            // loop and the final reference drops have completed.
            cleanup_inflight = true;
            cleanup_requested = false;
        }
        // A snapshot may still own a handle after the store stops. Only the
        // writer-owned reference may close a file here; the saveworker-owned
        // snapshot reference closes its file later.
        for (auto& file : release) {
            if (file.use_count() == 1) (void)file->Close();
        }
        // Drop writer references outside the state lock: this can run the
        // FileRecord destructor and therefore perform a final CloseHandle.
        release.clear();
        {
            std::lock_guard<std::mutex> lock(mutex);
            cleanup_inflight = false;
            cleanup_requested = false;
        }
        drained.notify_all();
    }
};

std::unique_ptr<ReplayDiskStore> ReplayDiskStore::Create(
    Config config, std::string& error) {
    if (config.max_disk_bytes == 0 || config.max_segment_bytes == 0
        || config.max_queue_bytes == 0 || config.max_queue_segments == 0
        || config.max_segments == 0) {
        error = "replay disk limits must be nonzero";
        return nullptr;
    }
    if (config.max_segment_bytes > config.max_queue_bytes) {
        error = "max_segment_bytes exceeds max_queue_bytes";
        return nullptr;
    }
    if (!IsAbsoluteDirectory(config.work_directory, error)) return nullptr;
    try {
        auto store = std::unique_ptr<ReplayDiskStore>(
            new ReplayDiskStore(std::move(config)));
        store->impl_->writer = std::thread(
            [impl = store->impl_.get()] { impl->Run(); });
        return store;
    } catch (const std::exception&) {
        error = "could not start replay disk writer";
        return nullptr;
    } catch (...) {
        error = "could not start replay disk writer";
        return nullptr;
    }
}

ReplayDiskStore::ReplayDiskStore(Config config)
    : config_(std::move(config)), impl_(std::make_unique<Impl>(config_)) {}

ReplayDiskStore::~ReplayDiskStore() {
    Shutdown();
}

SubmitResult ReplayDiskStore::Submit(ReplaySegment&& segment) {
    SegmentSizing sizing;
    std::string error;
    if (!SizeSegment(segment, &sizing, error))
        return {SubmitStatus::InvalidSegment, segment.generation, 0, error};

    SubmitResult result;
    bool accepted = false;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (segment.generation != impl_->generation) {
            result = {SubmitStatus::GenerationMismatch, segment.generation,
                      sizing.logical_bytes, "segment generation is not current"};
        } else if (!impl_->accepting || impl_->fault_latched) {
            result = {impl_->stopping ? SubmitStatus::Stopped : impl_->last_status,
                      segment.generation, sizing.logical_bytes, impl_->last_error};
        } else if (sizing.admission_bytes > config_.max_segment_bytes) {
            result = {SubmitStatus::QuotaExceeded, segment.generation,
                      sizing.logical_bytes,
                      "segment vector capacities exceed max_segment_bytes"};
            impl_->accepting = false;
            impl_->fault_latched = true;
            impl_->last_status = result.status;
            impl_->last_error = result.detail;
        } else if (impl_->last_sequence == std::numeric_limits<uint64_t>::max()
                   || segment.sequence != impl_->last_sequence + 1) {
            result = {SubmitStatus::SequenceMismatch, segment.generation,
                      sizing.logical_bytes,
                      "segment sequence is not the next contiguous sequence"};
        } else if (impl_->reserved_count >= config_.max_queue_segments
                   || sizing.admission_bytes > config_.max_queue_bytes
                   || impl_->reserved_memory_bytes > config_.max_queue_bytes
                   || sizing.admission_bytes
                        > config_.max_queue_bytes - impl_->reserved_memory_bytes) {
            result = {SubmitStatus::QueueFull, segment.generation,
                      sizing.logical_bytes,
                      "queued and in-flight segment RAM limits are full"};
            impl_->accepting = false;
            impl_->fault_latched = true;
            impl_->last_status = result.status;
            impl_->last_error = result.detail;
        } else {
            try {
                const uint64_t accepted_sequence = segment.sequence;
                // Allocate the queue node before moving caller-owned vectors.
                // A deque allocation failure therefore leaves the rejected
                // segment untouched and consumes no admission counters.
                impl_->queue.emplace_back();
                Pending& slot = impl_->queue.back();
                slot.sizing = sizing;
                slot.segment = std::move(segment);
                ++impl_->reserved_count;
                impl_->reserved_memory_bytes += sizing.admission_bytes;
                impl_->queued_file_bytes += sizing.logical_bytes;
                impl_->queued_memory_bytes += sizing.admission_bytes;
                impl_->last_sequence = accepted_sequence;
                impl_->last_status = SubmitStatus::Accepted;
                impl_->last_error.clear();
                result = {SubmitStatus::Accepted, impl_->generation,
                          sizing.logical_bytes, {}};
                accepted = true;
            } catch (...) {
                result = {SubmitStatus::QuotaExceeded, segment.generation,
                          sizing.logical_bytes, "segment queue allocation failed"};
                impl_->accepting = false;
                impl_->fault_latched = true;
                impl_->last_status = result.status;
                impl_->last_error = result.detail;
            }
        }
    }
    if (accepted) impl_->wake.notify_one();
    return result;
}

uint64_t ReplayDiskStore::ResetGeneration() {
    uint64_t result_generation = 0;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (impl_->stopping
            || impl_->generation == std::numeric_limits<uint64_t>::max())
            return impl_->generation;
        ++impl_->generation;
        result_generation = impl_->generation;
        impl_->last_sequence = 0;
        impl_->fault_latched = false;
        impl_->io_failure = false;
        impl_->accepting = true;
        impl_->last_status = SubmitStatus::Accepted;
        impl_->last_error.clear();
        impl_->retired.splice(impl_->retired.end(), impl_->retained);
        if (impl_->next_cleanup_token == std::numeric_limits<uint64_t>::max())
            impl_->next_cleanup_token = 1;
        else
            ++impl_->next_cleanup_token;
        impl_->cleanup_requested_token = impl_->next_cleanup_token;
        impl_->cleanup_requested = true;
    }
    impl_->wake.notify_one();
    return result_generation;
}

uint64_t ReplayDiskStore::CurrentGeneration() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->generation;
}

ReplayDiskStats ReplayDiskStore::Stats() const {
    ReplayDiskStats stats;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        stats.generation = impl_->generation;
        stats.queued_segments = impl_->queue.size();
        stats.inflight_segments = impl_->inflight_count;
        stats.retained_segments = impl_->retained.size();
        stats.queued_file_bytes = impl_->queued_file_bytes;
        stats.inflight_file_bytes = impl_->inflight_file_bytes;
        stats.queued_memory_bytes = impl_->queued_memory_bytes;
        stats.inflight_memory_bytes = impl_->inflight_memory_bytes;
        stats.discarding_segments = impl_->discarding_segments;
        stats.discarding_memory_bytes = impl_->discarding_memory_bytes;
        for (const auto& file : impl_->retained)
            stats.retained_file_bytes += file->logical_file_bytes;
        auto count_pinned = [&stats](const auto& files) {
            for (const auto& file : files) {
                if (file.use_count() > 1) {
                    ++stats.pinned_segments;
                    if (stats.pinned_file_bytes <=
                        std::numeric_limits<uint64_t>::max()
                            - file->logical_file_bytes) {
                        stats.pinned_file_bytes += file->logical_file_bytes;
                    } else {
                        stats.pinned_file_bytes =
                            std::numeric_limits<uint64_t>::max();
                    }
                }
            }
        };
        count_pinned(impl_->retained);
        count_pinned(impl_->retired);
        stats.accepting = impl_->accepting;
        stats.io_failure_latched = impl_->io_failure;
        stats.last_status = impl_->last_status;
        stats.last_error = impl_->last_error;
    }
    impl_->accounting->Snapshot(&stats.disk_file_bytes, &stats.disk_file_count);
    return stats;
}

ReplayDiskStore::Snapshot ReplayDiskStore::SnapshotGeneration(uint64_t generation) const {
    Snapshot snapshot;
    std::lock_guard<std::mutex> lock(impl_->mutex);
    snapshot.generation = generation == 0 ? impl_->generation : generation;
    for (const auto& file : impl_->retained) {
        if (file->generation == snapshot.generation && !file->closing)
            snapshot.files_.push_back(file);
    }
    // Retired records are leased only by snapshots created before reset.
    // A fresh snapshot must expose the current retained suffix exclusively.
    return snapshot;
}

bool ReplayDiskStore::Materialize(const Snapshot& snapshot,
                                  std::vector<ReplaySegment>& segments,
                                  std::string& error) const {
    return snapshot.Materialize(segments, error);
}

bool ReplayDiskStore::Snapshot::Materialize(
    std::vector<ReplaySegment>& segments, std::string& error) const {
    try {
        segments.clear();
        segments.reserve(files_.size());
        for (const auto& file : files_) {
            std::lock_guard<std::mutex> file_lock(file->io_mutex);
            LARGE_INTEGER zero{};
            if (SetFilePointerEx(file->handle, zero, nullptr, FILE_BEGIN) == FALSE) {
                error = Win32Error(GetLastError());
                return false;
            }
            std::vector<uint8_t> bytes;
            if (!ReadAll(file->handle, file->logical_file_bytes, file->hooks,
                         bytes, error)) return false;
            ReplaySegment segment;
            if (!DeserializeSegment(bytes, segment, error)
                || segment.generation != file->generation
                || segment.sequence != file->sequence) {
                if (error.empty()) error = "segment metadata does not match its lease";
                return false;
            }
            segments.push_back(std::move(segment));
        }
        return true;
    } catch (const std::exception&) {
        error = "snapshot materialization threw an exception";
        return false;
    } catch (...) {
        error = "snapshot materialization threw an unknown exception";
        return false;
    }
}

bool ReplayDiskStore::Drain(uint32_t timeout_ms) {
    std::unique_lock<std::mutex> lock(impl_->mutex);
    return impl_->drained.wait_for(lock, std::chrono::milliseconds(timeout_ms),
        [this] {
            return impl_->queue.empty() && impl_->inflight_count == 0
                && impl_->discarding_segments == 0
                && !impl_->cleanup_requested && !impl_->cleanup_inflight;
        });
}

void ReplayDiskStore::Shutdown() {
    if (!impl_) return;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (!impl_->stopping) {
            impl_->accepting = false;
            impl_->stopping = true;
            impl_->last_status = SubmitStatus::Stopped;
            impl_->last_error = "replay disk store is shutting down";
        }
    }
    impl_->wake.notify_one();
    if (impl_->writer.joinable()) impl_->writer.join();
}

}  // namespace fthr::replay_disk
