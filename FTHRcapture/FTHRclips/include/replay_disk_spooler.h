#pragma once
#ifndef FTHR_REPLAY_DISK_SPOOLER_H
#define FTHR_REPLAY_DISK_SPOOLER_H

#include <chrono>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "continuous_recording_writer.h"
#include "encoded_video_config.h"

namespace fthr {

struct ReplaySegment {
    std::filesystem::path path;
    int64_t start_pts = 0;
    int64_t end_pts = 0;
    std::chrono::steady_clock::time_point start_time;
    std::chrono::steady_clock::time_point end_time;
    double duration_seconds = 0.0;
};

class ReplayDiskSpooler {
public:
    ReplayDiskSpooler(
        std::filesystem::path temp_dir,
        uint32_t segment_duration_seconds = 600,
        uint32_t retention_seconds = 1800);
    ~ReplayDiskSpooler();

    ReplayDiskSpooler(const ReplayDiskSpooler&) = delete;
    ReplayDiskSpooler& operator=(const ReplayDiskSpooler&) = delete;

    bool Start(
        const EncodedVideoConfig& video_config,
        const ContinuousRecordingAudioConfig& audio_config);

    void PushVideo(
        const uint8_t* data,
        uint32_t size,
        int64_t pts,
        bool is_keyframe);

    void PushAudio(
        const uint8_t* data,
        uint32_t size,
        int64_t pts_samples,
        int64_t duration_samples = 1024);

    bool SaveClip(
        const std::wstring& output_path,
        uint32_t requested_duration_seconds);

    void Stop();
    void Cleanup();

    bool IsActive() const;
private:
    std::unique_ptr<ContinuousRecordingWriter> RotateSegmentLocked();
    void PruneOldSegmentsLocked();
    std::wstring FindFfmpegExe() const;

    std::filesystem::path temp_dir_;
    uint32_t segment_duration_seconds_ = 600;
    uint32_t retention_seconds_ = 1800;

    EncodedVideoConfig video_config_;
    ContinuousRecordingAudioConfig audio_config_;

    mutable std::mutex mutex_;
    bool active_ = false;

    std::unique_ptr<ContinuousRecordingWriter> current_writer_;
    std::filesystem::path current_segment_path_;
    int64_t current_segment_start_pts_ = 0;
    int64_t current_segment_last_pts_ = 0;
    std::chrono::steady_clock::time_point current_segment_start_time_;
    uint64_t segment_counter_ = 0;

    std::deque<ReplaySegment> completed_segments_;
    std::thread finishing_thread_;
};

} // namespace fthr

#endif // FTHR_REPLAY_DISK_SPOOLER_H
