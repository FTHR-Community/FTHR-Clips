// Write existing compressed packets to fragmented MP4 without re-encoding.
// Commit a fragment at each video keyframe so completed fragments remain
// recoverable if recording stops before the final trailer.

#pragma once
#ifndef FTHR_CONTINUOUS_RECORDING_WRITER_H
#define FTHR_CONTINUOUS_RECORDING_WRITER_H

#include "encoded_video_config.h"
#include "capture_highlights.h"
#include "audio_source_model.h"

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <map>
#include <optional>

struct AVFormatContext;
struct AVStream;

namespace fthr {

struct ContinuousRecordingAudioConfig {
    uint32_t sample_rate = 0;
    uint32_t channels = 0;
    std::vector<uint8_t> codec_extradata;
    std::string codec = "aac";

    bool valid() const noexcept {
        return sample_rate > 0 && channels > 0
            && (codec == "pcm_f32le" || (codec == "aac" && !codec_extradata.empty()));
    }
};

class ContinuousRecordingWriter {
public:
    ContinuousRecordingWriter() = default;
    ~ContinuousRecordingWriter();

    ContinuousRecordingWriter(const ContinuousRecordingWriter&) = delete;
    ContinuousRecordingWriter& operator=(const ContinuousRecordingWriter&) = delete;

    bool Start(
        const std::filesystem::path& output_path,
        const EncodedVideoConfig& video_config,
        const ContinuousRecordingAudioConfig& audio_config = {},
        uint64_t generation = 0, bool independent_sources = false);

    bool PushVideo(
        const uint8_t* data,
        uint32_t size,
        int64_t pts,
        bool is_keyframe,
        uint64_t generation = 0, int64_t timeline_100ns = 0);
    bool PushHighlight(
        const std::string& marker_id,
        int64_t pts,
        uint64_t generation = 0);
    bool PushAudio(
        const uint8_t* data,
        uint32_t size,
        int64_t pts_samples,
        int64_t duration_samples = 1024);
    bool PushSourceAudio(const AudioSourceMetadata& source, const uint8_t* data,
        uint32_t size, int64_t timeline_100ns, int64_t duration_samples);

    // Stops accepting packets, drains the bounded queue, flushes the final
    // fragment, writes the optional trailer, closes the file, and joins.
    bool Stop();

    bool IsRunning() const noexcept {
        return running_.load(std::memory_order_acquire);
    }
    bool HasFailed() const noexcept {
        return failed_.load(std::memory_order_acquire);
    }
    std::string LastError() const;

private:
    enum class PacketKind { Video, Audio, Highlight };

    struct QueuedPacket {
        PacketKind kind = PacketKind::Video;
        std::vector<uint8_t> data;
        int64_t pts = 0;
        int64_t duration = 0;
        bool is_keyframe = false;
        uint64_t generation = 0;
        std::string marker_id;
        AudioSourceMetadata source;
        int64_t timeline_100ns = 0;
    };

    static constexpr size_t kMaxQueuedBytes = 64u * 1024u * 1024u;
    static constexpr size_t kMaxQueuedMarkers = kMaxHighlightMarkers;

    bool Enqueue(QueuedPacket packet);
    void WriterThread();
    bool WritePacket(const QueuedPacket& packet);
    bool FlushFragment();
    bool AppendEligibleMarkers(int64_t last_video_pts);
    bool GenerationMatches(uint64_t packet_generation) const noexcept {
        return generation_ == 0
            ? packet_generation == 0
            : packet_generation == generation_;
    }
    void CloseContainer(bool write_trailer);
    void Fail(std::string message);
    void FailLocked(std::string message);
    bool PublishAudioIdentities(bool remove_unused);

    EncodedVideoConfig video_config_;
    ContinuousRecordingAudioConfig audio_config_;
    AVFormatContext* format_context_ = nullptr;
    AVStream* video_stream_ = nullptr;
    AVStream* audio_stream_ = nullptr;
    std::vector<AVStream*> audio_streams_;
    std::vector<std::optional<AudioSourceMetadata>> source_infos_;
    std::vector<bool> source_written_;
    std::vector<bool> source_committed_;
    std::vector<int64_t> source_first_sample_;
    std::vector<int64_t> source_last_sample_;
    std::map<std::string, size_t> source_slots_;
    std::filesystem::path output_path_;
    bool independent_sources_ = false;
    bool headers_finalized_ = false;
    int64_t first_video_timeline_100ns_ = 0;

    std::deque<QueuedPacket> queue_;
    size_t queued_bytes_ = 0;
    mutable std::mutex mutex_;
    std::condition_variable condition_;
    std::thread thread_;
    bool accepting_ = false;
    std::string last_error_;

    bool video_started_ = false;
    bool audio_started_ = false;
    int64_t first_video_pts_ = 0;
    int64_t first_audio_pts_ = 0;
    int64_t last_video_pts_ = 0;
    uint64_t generation_ = 0;
    size_t queued_marker_count_ = 0;
    std::deque<QueuedPacket> pending_markers_;
    // Lifetime admission set: queued, pending and already-written IDs all
    // count toward the bounded recording quota.
    std::vector<std::string> admitted_marker_ids_;
    uint64_t video_packets_written_ = 0;
    uint64_t audio_packets_written_ = 0;
    std::atomic<bool> running_{false};
    std::atomic<bool> failed_{false};
    std::atomic<bool> audio_enabled_{false};
};

} // namespace fthr

#endif // FTHR_CONTINUOUS_RECORDING_WRITER_H
