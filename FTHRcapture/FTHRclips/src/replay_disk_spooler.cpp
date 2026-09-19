#include "replay_disk_spooler.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <algorithm>
#include <fstream>
#include <iostream>

namespace fthr {

ReplayDiskSpooler::ReplayDiskSpooler(
    std::filesystem::path temp_dir,
    uint32_t segment_duration_seconds,
    uint32_t retention_seconds)
    : temp_dir_(std::move(temp_dir)),
      segment_duration_seconds_(segment_duration_seconds),
      retention_seconds_(retention_seconds) {
    std::error_code ec;
    std::filesystem::create_directories(temp_dir_, ec);
    std::cout << "[ReplayDiskSpooler] Initialized: temp_dir=" << temp_dir_.u8string()
              << " segment_duration=" << segment_duration_seconds_
              << "s retention=" << retention_seconds_ << "s" << std::endl;
}

ReplayDiskSpooler::~ReplayDiskSpooler() {
    Stop();
    Cleanup();
}

bool ReplayDiskSpooler::Start(
    const EncodedVideoConfig& video_config,
    const ContinuousRecordingAudioConfig& audio_config) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (active_) return true;

    video_config_ = video_config;
    audio_config_ = audio_config;

    std::error_code ec;
    std::filesystem::create_directories(temp_dir_, ec);

    current_segment_path_ = temp_dir_ / (L"replay_seg_" + std::to_wstring(GetTickCount64()) + L"_" + std::to_wstring(++segment_counter_) + L".mp4");
    current_writer_ = std::make_unique<ContinuousRecordingWriter>();
    if (!current_writer_->Start(current_segment_path_, video_config_, audio_config_)) {
        std::cerr << "[ReplayDiskSpooler] Failed to start first segment: "
                  << current_writer_->LastError() << std::endl;
        current_writer_.reset();
        return false;
    }

    current_segment_start_time_ = std::chrono::steady_clock::now();
    current_segment_start_pts_ = 0;
    current_segment_last_pts_ = 0;
    active_ = true;
    std::cout << "[ReplayDiskSpooler] Started spooling to " << current_segment_path_.u8string() << std::endl;
    return true;
}

void ReplayDiskSpooler::PushVideo(
    const uint8_t* data,
    uint32_t size,
    int64_t pts,
    bool is_keyframe) {
    std::unique_ptr<ContinuousRecordingWriter> old_writer;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!active_ || !current_writer_) return;

        if (current_segment_start_pts_ == 0 && pts > 0) {
            current_segment_start_pts_ = pts;
        }
        current_segment_last_pts_ = pts;

        const auto now = std::chrono::steady_clock::now();
        const double elapsed_s = std::chrono::duration_cast<std::chrono::duration<double>>(now - current_segment_start_time_).count();

        // Rotate segment when target segment duration is reached and current packet is a keyframe
        if (elapsed_s >= static_cast<double>(segment_duration_seconds_) && is_keyframe) {
            old_writer = RotateSegmentLocked();
        }

        if (current_writer_) {
            current_writer_->PushVideo(data, size, pts, is_keyframe);
        }
    }

    if (old_writer) {
        std::thread([w = std::move(old_writer)]() mutable {
            w->Stop();
        }).detach();
    }
}

void ReplayDiskSpooler::PushAudio(
    const uint8_t* data,
    uint32_t size,
    int64_t pts_samples,
    int64_t duration_samples) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!active_ || !current_writer_) return;

    current_writer_->PushAudio(data, size, pts_samples, duration_samples);
}

std::unique_ptr<ContinuousRecordingWriter> ReplayDiskSpooler::RotateSegmentLocked() {
    if (!current_writer_) return nullptr;

    const auto now = std::chrono::steady_clock::now();
    const double duration = std::chrono::duration_cast<std::chrono::duration<double>>(now - current_segment_start_time_).count();

    std::unique_ptr<ContinuousRecordingWriter> old_writer = std::move(current_writer_);

    ReplaySegment seg;
    seg.path = current_segment_path_;
    seg.start_pts = current_segment_start_pts_;
    seg.end_pts = current_segment_last_pts_;
    seg.start_time = current_segment_start_time_;
    seg.end_time = now;
    seg.duration_seconds = duration;
    completed_segments_.push_back(std::move(seg));
    std::cout << "[ReplayDiskSpooler] Segment finished: " << current_segment_path_.u8string()
              << " (duration: " << duration << "s)" << std::endl;

    PruneOldSegmentsLocked();

    current_segment_path_ = temp_dir_ / (L"replay_seg_" + std::to_wstring(GetTickCount64()) + L"_" + std::to_wstring(++segment_counter_) + L".mp4");
    current_writer_ = std::make_unique<ContinuousRecordingWriter>();
    if (!current_writer_->Start(current_segment_path_, video_config_, audio_config_)) {
        std::cerr << "[ReplayDiskSpooler] Failed to start next segment: "
                  << current_writer_->LastError() << std::endl;
        current_writer_.reset();
        return old_writer;
    }
    current_segment_start_time_ = std::chrono::steady_clock::now();
    current_segment_start_pts_ = 0;
    current_segment_last_pts_ = 0;

    return old_writer;
}

void ReplayDiskSpooler::PruneOldSegmentsLocked() {
    double total_retained = 0.0;
    // Iterate from newest to oldest to calculate total available history
    for (auto it = completed_segments_.rbegin(); it != completed_segments_.rend(); ++it) {
        total_retained += it->duration_seconds;
    }

    // Keep enough segments to cover retention_seconds + one extra segment margin
    const double max_allowed = static_cast<double>(retention_seconds_ + segment_duration_seconds_);
    while (completed_segments_.size() > 1 && total_retained > max_allowed) {
        const auto& oldest = completed_segments_.front();
        total_retained -= oldest.duration_seconds;
        std::error_code ec;
        std::filesystem::remove(oldest.path, ec);
        std::cout << "[ReplayDiskSpooler] Pruned expired segment: " << oldest.path.u8string() << std::endl;
        completed_segments_.pop_front();
    }
}

std::wstring ReplayDiskSpooler::FindFfmpegExe() const {
    wchar_t module_path[MAX_PATH] = {0};
    GetModuleFileNameW(nullptr, module_path, MAX_PATH);
    std::filesystem::path exe_dir = std::filesystem::path(module_path).parent_path();

    if (std::filesystem::is_regular_file(exe_dir / L"ffmpeg.exe")) {
        return (exe_dir / L"ffmpeg.exe").wstring();
    }
    std::filesystem::path tp_bin = exe_dir / L"third_party" / L"ffmpeg" / L"bin" / L"ffmpeg.exe";
    if (std::filesystem::is_regular_file(tp_bin)) {
        return tp_bin.wstring();
    }
    std::filesystem::path repo_bin = exe_dir / L".." / L".." / L"third_party" / L"ffmpeg" / L"bin" / L"ffmpeg.exe";
    std::error_code ec;
    if (std::filesystem::is_regular_file(repo_bin, ec)) {
        return std::filesystem::canonical(repo_bin).wstring();
    }
    return L"ffmpeg.exe";
}

bool ReplayDiskSpooler::SaveClip(
    const std::wstring& output_path,
    uint32_t requested_duration_seconds) {

    std::vector<ReplaySegment> to_merge;
    std::filesystem::path concat_list;
    double accumulated_duration = 0.0;
    std::wstring ffmpeg;
    std::unique_ptr<ContinuousRecordingWriter> writer_to_finish;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!active_) {
            std::cerr << "[ReplayDiskSpooler] SaveClip called but spooler is not active" << std::endl;
            return false;
        }

        // Rotate current segment so active footage is captured into completed_segments_
        writer_to_finish = RotateSegmentLocked();
    } // MUTEX RELEASED: audio and video threads can continue writing into new segment

    // Stop active writer outside the mutex so capture and audio threads are never blocked
    if (writer_to_finish) {
        writer_to_finish->Stop();
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (completed_segments_.empty()) {
            std::cerr << "[ReplayDiskSpooler] No segments available to save" << std::endl;
            return false;
        }

        // Collect valid segments from newest to oldest up to requested_duration_seconds
        for (auto it = completed_segments_.rbegin(); it != completed_segments_.rend(); ++it) {
            std::error_code ec;
            if (std::filesystem::exists(it->path, ec) && std::filesystem::file_size(it->path, ec) > 0) {
                to_merge.push_back(*it);
                accumulated_duration += it->duration_seconds;
                if (accumulated_duration >= static_cast<double>(requested_duration_seconds)) {
                    break;
                }
            }
        }
        std::reverse(to_merge.begin(), to_merge.end());

        if (to_merge.empty()) {
            std::cerr << "[ReplayDiskSpooler] No valid non-empty segments found to save" << std::endl;
            return false;
        }

        concat_list = temp_dir_ / (L"concat_list_" + std::to_wstring(GetTickCount64()) + L"_" + std::to_wstring(++segment_counter_) + L".txt");
        {
            std::ofstream list_out(concat_list);
            for (const auto& seg : to_merge) {
                std::string p = seg.path.generic_u8string();
                list_out << "file '" << p << "'\n";
            }
        }

        ffmpeg = FindFfmpegExe();
    } // MUTEX RELEASED: CaptureThread & AudioCapture are completely free while FFmpeg runs

    const std::wstring fps_str = std::to_wstring(video_config_.frame_rate.numerator)
        + L"/" + std::to_wstring(video_config_.frame_rate.denominator > 0 ? video_config_.frame_rate.denominator : 1);
    const std::wstring bps_str = std::to_wstring(static_cast<uint64_t>(video_config_.bitrate_kbps) * 1000);

    std::wstring cmd = L"\"" + ffmpeg + L"\" -y -v error -nostdin -f concat -safe 0 -i \""
        + concat_list.wstring() + L"\"";
    if (accumulated_duration > static_cast<double>(requested_duration_seconds) + 1.0) {
        const double cut_from_start = accumulated_duration - static_cast<double>(requested_duration_seconds);
        cmd += L" -ss " + std::to_wstring(cut_from_start);
    }
    cmd += L" -map 0:v -map 0:a? -c copy -avoid_negative_ts make_zero -movflags +use_metadata_tags";
    cmd += L" -metadata fthr_frame_rate=" + fps_str;
    cmd += L" -metadata fthr_video_bitrate_bps=" + bps_str;
    cmd += L" -f mp4 \"" + output_path + L"\"";

    std::wcout << L"[ReplayDiskSpooler] Running concat: " << cmd << std::endl;

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi{};

    std::vector<wchar_t> cmd_buf(cmd.begin(), cmd.end());
    cmd_buf.push_back(0);

    if (!CreateProcessW(nullptr, cmd_buf.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        std::cerr << "[ReplayDiskSpooler] Failed to spawn FFmpeg: " << GetLastError() << std::endl;
        std::error_code ec;
        std::filesystem::remove(concat_list, ec);
        return false;
    }

    const DWORD wait_timeout_ms = std::max<DWORD>(60000, requested_duration_seconds * 250);
    const DWORD wait_result = WaitForSingleObject(pi.hProcess, wait_timeout_ms);

    if (wait_result == WAIT_TIMEOUT) {
        std::cerr << "[ReplayDiskSpooler] FFmpeg concat timed out after "
                  << (wait_timeout_ms / 1000) << "s - terminating process" << std::endl;
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        std::error_code ec;
        std::filesystem::remove(concat_list, ec);
        std::filesystem::remove(output_path, ec);
        return false;
    }

    DWORD exit_code = 1;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    std::error_code ec;
    std::filesystem::remove(concat_list, ec);

    if (exit_code != 0) {
        std::cerr << "[ReplayDiskSpooler] FFmpeg concat failed with exit code: " << exit_code << std::endl;
        std::filesystem::remove(output_path, ec);
        return false;
    }

    if (!std::filesystem::exists(output_path, ec) || std::filesystem::file_size(output_path, ec) == 0) {
        std::cerr << "[ReplayDiskSpooler] Output file missing or 0 bytes after FFmpeg concat" << std::endl;
        std::filesystem::remove(output_path, ec);
        return false;
    }

    std::wcout << L"[ReplayDiskSpooler] Successfully saved clip: " << output_path << std::endl;
    return true;
}

void ReplayDiskSpooler::Stop() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!active_) return;
    active_ = false;
    if (current_writer_) {
        current_writer_->Stop();
        current_writer_.reset();
    }
    std::cout << "[ReplayDiskSpooler] Stopped." << std::endl;
}

void ReplayDiskSpooler::Cleanup() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::error_code ec;
    for (const auto& seg : completed_segments_) {
        std::filesystem::remove(seg.path, ec);
    }
    completed_segments_.clear();
    if (!current_segment_path_.empty()) {
        std::filesystem::remove(current_segment_path_, ec);
        current_segment_path_.clear();
    }
    std::cout << "[ReplayDiskSpooler] Cleaned up temporary segment files." << std::endl;
}

bool ReplayDiskSpooler::IsActive() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return active_;
}

} // namespace fthr
