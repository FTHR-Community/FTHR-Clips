#include "replay_encoder.h"
#include "encoded_ring_buffer.h"
#include "capture_pipeline_health.h"
#include "capture_config_limits.h"
#include "frame_rate_scheduler.h"
#include "replay_health.h"
#include "replay_save_diagnostics.h"
#include "save_clip_task.h"
#include "dxgi_replay_cadence.h"
#include "encoded_replay_health.h"
#include "wgc_cadence_wait.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <iostream>
#include <limits>
#include <string>
#include <thread>
#include <vector>

namespace {

using fthr::EncoderVendor;
using fthr::EncoderPreference;
using fthr::ReplayEncoderBackend;
using fthr::ReplayStartupError;
using fthr::VideoCodec;

int policy_checks = 0;

void CheckPolicy(bool condition, const char* message) {
    ++policy_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

void WgcWaitPreservesDeadlineAndInterruption() {
    using namespace std::chrono;
    for (bool high_resolution : {false, true}) {
        fthr::WgcCadenceWait wait(high_resolution);
        std::mutex mutex;
        std::condition_variable cv;
        std::unique_lock<std::mutex> lock(mutex);
        const auto deadline = steady_clock::now() + milliseconds(5);
        wait.WaitUntil(deadline, cv, lock, [] { return false; });
        CheckPolicy(steady_clock::now() >= deadline && lock.owns_lock(),
            "WGC timer and fallback never return before a frame deadline");
        const auto start = steady_clock::now();
        wait.WaitUntil(start + seconds(5), cv, lock, [] { return true; });
        CheckPolicy(steady_clock::now() - start < seconds(1) && lock.owns_lock(),
            "WGC interrupted wait does not block for its deadline");
        std::atomic<bool> stop{false};
        std::thread stopper([&] {
            std::this_thread::sleep_for(milliseconds(20));
            { std::lock_guard<std::mutex> guard(mutex); stop.store(true); }
            cv.notify_all();
        });
        const auto stop_start = steady_clock::now();
        wait.WaitUntil(stop_start + seconds(5), cv, lock,
            [&] { return stop.load(); });
        CheckPolicy(stop.load() && steady_clock::now() - stop_start < seconds(1),
            "WGC shutdown/source-close interrupts both timer and CV waits");
        lock.unlock();
        stopper.join();
    }
}

void SameAdapterVendorMatrixIsAuthoritative() {
    const auto nvidia = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Nvidia, VideoCodec::H264);
    const auto amd = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Amd, VideoCodec::HEVC);
    const auto intel = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Intel, VideoCodec::AV1);
    CheckPolicy(nvidia.allowed
            && nvidia.backend == ReplayEncoderBackend::NativeNvenc,
        "selected NVIDIA monitor uses same-adapter NVENC");
    CheckPolicy(amd.allowed
            && amd.backend == ReplayEncoderBackend::FfmpegAmf,
        "selected AMD monitor uses same-adapter AMF");
    CheckPolicy(intel.allowed
            && intel.backend == ReplayEncoderBackend::FfmpegQsv,
        "selected Intel monitor uses same-adapter QSV");
    CheckPolicy(nvidia.same_adapter && amd.same_adapter && intel.same_adapter,
        "all alpha production selections are same-adapter");
}

void MissingRuntimeAndUnsupportedCodecStayFailed() {
    CheckPolicy(fthr::ClassifyReplayInitializationFailure(
                    "NVENC runtime unavailable")
                == ReplayStartupError::HardwareEncoderUnavailable,
        "missing NVIDIA runtime is distinct");
    CheckPolicy(fthr::ClassifyReplayInitializationFailure(
                    "AMF runtime unavailable: amfrt64.dll not found")
                == ReplayStartupError::HardwareEncoderUnavailable,
        "missing AMD runtime is distinct");
    CheckPolicy(fthr::ClassifyReplayInitializationFailure(
                    "oneVPL runtime unavailable")
                == ReplayStartupError::HardwareEncoderUnavailable,
        "missing Intel runtime is distinct");
    CheckPolicy(fthr::ClassifyReplayInitializationFailure(
                    "av1_qsv unsupported by this hardware")
                == ReplayStartupError::RequestedCodecUnsupported,
        "unsupported requested codec is distinct");
    CheckPolicy(fthr::ClassifyReplayInitializationFailure(
                    "h264_qsv unsupported on this Intel adapter")
                == ReplayStartupError::RequestedCodecUnsupported,
        "old or incapable Intel hardware is classified from capability failure, not model name");
    CheckPolicy(fthr::SelectWindowsReplayPolicy(
                    EncoderVendor::Nvidia, static_cast<VideoCodec>(99)).error
                == ReplayStartupError::RequestedCodecUnsupported,
        "unknown codec cannot silently become H.264");
}

void CaptureAdapterCannotBeStolenByAnotherVendor() {
    const auto intel = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Intel, VideoCodec::H264);
    CheckPolicy(intel.encoder_vendor == EncoderVendor::Intel
            && intel.backend == ReplayEncoderBackend::FfmpegQsv,
        "unrelated NVIDIA presence cannot steal Intel monitor selection");
    CheckPolicy(!fthr::IsAutomaticCrossAdapterReplayAllowed(),
        "cross-adapter path is disabled until explicitly qualified");
    const auto unknown = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Software, VideoCodec::H264);
    CheckPolicy(!unknown.allowed
            && unknown.error == ReplayStartupError::CaptureAdapterUnsupported,
        "unknown capture adapter refuses startup instead of enumerating vendors");
    const auto amd_on_nvidia = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Nvidia, EncoderPreference::Amd, VideoCodec::H264);
    CheckPolicy(!amd_on_nvidia.allowed
            && amd_on_nvidia.error
                == ReplayStartupError::CrossAdapterPathUnavailable,
        "requested AMF adapter cannot consume another adapter's monitor texture");
}

void ExplicitNvidiaSelectionEnablesQualifiedHybridPath() {
    const auto selected = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Intel, EncoderPreference::Nvidia, VideoCodec::HEVC);
    CheckPolicy(selected.allowed && !selected.same_adapter,
        "explicit NVIDIA selection permits the hybrid CPU-input path");
    CheckPolicy(selected.encoder_vendor == EncoderVendor::Nvidia
            && selected.backend == ReplayEncoderBackend::NativeNvenc,
        "explicit NVIDIA selection resolves to native NVENC");

    const auto unsupported = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Intel, EncoderPreference::Amd, VideoCodec::H264);
    CheckPolicy(!unsupported.allowed
            && unsupported.error == ReplayStartupError::CrossAdapterPathUnavailable,
        "unimplemented AMD cross-adapter selection stays rejected");
}

void RawCapacityNeverOverclaimsConfiguredHistory() {
    const auto capacity = fthr::CalculateRawReplayCapacity(
        1920, 1080, 4, 60, 60, 512);
    CheckPolicy(capacity.frame_capacity == 64,
        "512 MiB raw BGRA capacity uses exact whole-frame count");
    CheckPolicy(capacity.capacity_milliseconds == 1066,
        "raw capacity reports actual duration at effective FPS");
    CheckPolicy(!capacity.meets_requested_duration,
        "raw capacity cannot claim the configured 60 seconds");
    const auto invalid = fthr::CalculateRawReplayCapacity(
        0, 1080, 4, 60, 30, 512);
    CheckPolicy(invalid.frame_capacity == 0
            && !invalid.meets_requested_duration,
        "invalid raw geometry reports no replay capability");
    CheckPolicy(!fthr::IsAutomaticRawReplayFallbackAllowed(),
        "raw replay fallback is disabled for the public alpha");
}

void CaptureBitrateBoundaryMatchesStartupContract() {
    using fthr::capture_config_limits::IsValidBitrateKbps;
    CheckPolicy(!IsValidBitrateKbps(499),
        "startup rejects a bitrate below the supported lower boundary");
    CheckPolicy(IsValidBitrateKbps(500),
        "startup accepts the supported lower bitrate boundary");
    CheckPolicy(IsValidBitrateKbps(200'000),
        "startup accepts the UI's supported high bitrate boundary");
    CheckPolicy(!IsValidBitrateKbps(200'001),
        "startup rejects a bitrate above the supported high boundary");
}

void CaptureUnsignedArgumentParserRejectsMalformedValues() {
    using fthr::capture_config_limits::ParseU32;
    CheckPolicy(!ParseU32("").valid,
        "empty startup argument is rejected");
    CheckPolicy(!ParseU32("-1").valid,
        "negative startup argument is rejected");
    CheckPolicy(!ParseU32("60fps").valid,
        "trailing startup argument text is rejected");
    CheckPolicy(ParseU32("4294967295").valid
            && ParseU32("4294967295").value
                == std::numeric_limits<uint32_t>::max(),
        "UINT32_MAX is accepted without signed overflow");
    CheckPolicy(!ParseU32("4294967296").valid,
        "UINT32_MAX plus one is rejected deterministically");
}

int CountScheduledFrames(int host_fps, int target_fps, int seconds) {
    constexpr int64_t ticks_per_second = 90000000;
    fthr::FrameRateScheduler scheduler(ticks_per_second / target_fps);
    int accepted = 0;
    for (int frame = 0; frame < host_fps * seconds; ++frame) {
        const int64_t now = static_cast<int64_t>(frame)
            * ticks_per_second / host_fps;
        if (scheduler.ShouldCapture(now)) ++accepted;
    }
    return accepted;
}

void DeadlineSchedulerPreservesCadenceAcrossRefreshRates() {
    for (const int target_fps : {30, 60, 120, 144}) {
        CheckPolicy(CountScheduledFrames(240, target_fps, 2)
                == target_fps * 2,
            "240 Hz synthetic input preserves each requested 30/60/120/144 cadence");
    }
    CheckPolicy(CountScheduledFrames(90, 60, 2) == 120,
        "90 Hz input preserves a 60 fps deadline cadence");
    CheckPolicy(CountScheduledFrames(144, 60, 2) == 120,
        "144 Hz input preserves a 60 fps deadline cadence");
    CheckPolicy(CountScheduledFrames(60, 120, 2) == 120,
        "target above host refresh accepts every available frame");

    fthr::FrameRateScheduler scheduler(10);
    CheckPolicy(scheduler.ShouldCapture(0) && !scheduler.ShouldCapture(9)
            && scheduler.ShouldCapture(10),
        "deadline scheduler accepts the first frame and exact deadlines");
    CheckPolicy(scheduler.ShouldCapture(1000) && !scheduler.ShouldCapture(1001),
        "a long stall advances missed deadlines without a catch-up burst");
}

void EncodedReplayCapacityHasBoundedHeadroom() {
    for (const uint32_t fps : {30u, 60u, 120u, 144u, 165u, 240u}) {
        CheckPolicy(fthr::CalculateEncodedReplaySlotCapacity(30, fps)
                == static_cast<size_t>(35) * fps,
            "30-second replay keeps five seconds of headroom at every tested FPS");
        CheckPolicy(fthr::CalculateEncodedReplaySlotCapacity(300, fps)
                == static_cast<size_t>(305) * fps,
            "300-second replay remains bounded at every tested FPS");
    }
    CheckPolicy(fthr::CalculateEncodedReplaySlotCapacity(0, 60) == 0
            && fthr::CalculateEncodedReplaySlotCapacity(30, 0) == 0,
        "invalid replay timing cannot allocate a ring");
}

fthr::replay_health::Input ReplayHealthFixture(
    uint32_t seconds, uint32_t fps, bool full_history = true) {
    fthr::replay_health::Input input;
    input.requested_duration_seconds = seconds;
    input.expected_fps = static_cast<double>(fps);
    input.wall_qpc_frequency = 1'000'000;
    input.full_history = full_history;
    const int64_t frame_period = input.wall_qpc_frequency / fps;
    const size_t frame_count = static_cast<size_t>(seconds) * fps + 1;
    input.packets.reserve(frame_count);
    for (size_t index = 0; index < frame_count; ++index) {
        input.packets.push_back({
            1'000'000 + static_cast<int64_t>(index) * frame_period,
            static_cast<int64_t>(index),
            4});
    }
    return input;
}

fthr::replay_health::Input ReplayHealthCoverageFixture(
    uint32_t requested_seconds, double coverage_seconds, uint32_t fps,
    bool full_history = false) {
    fthr::replay_health::Input input;
    input.requested_duration_seconds = requested_seconds;
    input.expected_fps = static_cast<double>(fps);
    input.wall_qpc_frequency = 1'000'000;
    input.full_history = full_history;
    const size_t frame_count = std::max<size_t>(
        2, static_cast<size_t>(std::ceil(coverage_seconds * fps)) + 1);
    const int64_t coverage_qpc = static_cast<int64_t>(std::llround(
        coverage_seconds * input.wall_qpc_frequency));
    input.packets.reserve(frame_count);
    for (size_t index = 0; index < frame_count; ++index) {
        const auto offset = static_cast<int64_t>(
            (static_cast<long double>(coverage_qpc) * index)
            / static_cast<long double>(frame_count - 1));
        input.packets.push_back({
            1'000'000 + offset,
            static_cast<int64_t>(index),
            4});
    }
    return input;
}

fthr::replay_health::Input ReplayHealthFrameIntervalFixture(
    uint32_t requested_seconds, uint32_t fps, size_t frame_intervals,
    bool full_history = false) {
    fthr::replay_health::Input input;
    input.requested_duration_seconds = requested_seconds;
    input.expected_fps = static_cast<double>(fps);
    input.wall_qpc_frequency = 1'000'000;
    input.pts_per_second = static_cast<double>(fps);
    input.full_history = full_history;
    input.packets.reserve(frame_intervals + 1);
    for (size_t index = 0; index <= frame_intervals; ++index) {
        input.packets.push_back({
            1'000'000 + static_cast<int64_t>(std::llround(
                static_cast<double>(index) * input.wall_qpc_frequency / fps)),
            static_cast<int64_t>(index),
            4});
    }
    return input;
}

fthr::replay_health::Input ReplayHealthObservedRateFixture(
    uint32_t requested_seconds, uint32_t expected_fps, uint32_t actual_fps) {
    fthr::replay_health::Input input;
    input.requested_duration_seconds = requested_seconds;
    input.expected_fps = static_cast<double>(expected_fps);
    input.wall_qpc_frequency = 1'000'000;
    input.full_history = true;
    const size_t frame_count = static_cast<size_t>(requested_seconds)
        * actual_fps + 1;
    input.packets.reserve(frame_count);
    for (size_t index = 0; index < frame_count; ++index) {
        input.packets.push_back({
            1'000'000 + static_cast<int64_t>(std::llround(
                static_cast<double>(index) * input.wall_qpc_frequency
                    / actual_fps)),
            static_cast<int64_t>(std::llround(
                static_cast<double>(index) * expected_fps / actual_fps)),
            4});
    }
    return input;
}

void ReplayHealthRejectsSparseNativeTimeline() {
    auto sparse = ReplayHealthFixture(5, 60);
    sparse.packets = {
        {1'000'000, 0, 4},
        {2'500'000, 1, 4},
        {4'000'000, 2, 4},
        {6'000'000, 3, 4},
    };
    const auto report = fthr::replay_health::Evaluate(sparse);
    CheckPolicy(!report.accepted
            && report.failure
                == fthr::replay_health::Failure::InsufficientCoverage
            && report.packet_count == 4
            && report.largest_gap_s >= 1.5
            && report.largest_wall_gap_s >= 1.5
            && report.largest_pts_gap_s < 0.1
            && report.largest_wall_gap_start_qpc == 4'000'000
            && report.largest_wall_gap_end_qpc == 6'000'000
            && report.largest_pts_gap_start == 0
            && report.largest_pts_gap_end == 1
            && report.first_wall_qpc == 1'000'000
            && report.last_wall_qpc == 6'000'000
            && report.first_pts == 0
            && report.last_pts == 3
            && report.actual_coverage_s < 0.1
            && report.effective_packet_rate_measured
            && report.effective_packet_rate > 59.0
            && report.generation == 4,
        "sparse wall history stays rejected even when its measured media rate looks nominal");

    sparse.packets = {
        {1'000'000, 0, 4},
        {1'016'666, 1, 4},
        {1'033'332, 2, 4},
        {1'049'998, 3, 4},
    };
    const auto clustered = fthr::replay_health::Evaluate(sparse);
    CheckPolicy(!clustered.accepted
            && clustered.failure
                == fthr::replay_health::Failure::InsufficientCoverage,
        "four tightly clustered packets cannot claim full requested history");
}

void ReplayHealthAllowsHealthyAndPartialHistory() {
    for (const uint32_t fps : {30u, 60u, 120u, 144u, 165u, 240u}) {
        const auto report = fthr::replay_health::Evaluate(
            ReplayHealthFixture(5, fps));
        CheckPolicy(report.accepted && report.generation_continuous
                && report.effective_packet_rate > static_cast<double>(fps) - 1.0,
            "normal replay cadence is accepted for 30/60/120/144 fps");
    }

    const auto low_rate_120 = fthr::replay_health::Evaluate(
        ReplayHealthObservedRateFixture(5, 120, 60));
    CheckPolicy(!low_rate_120.accepted
            && low_rate_120.failure
                == fthr::replay_health::Failure::LowEffectiveRate
            && low_rate_120.effective_packet_rate_measured
            && low_rate_120.effective_packet_rate >= 59.0
            && low_rate_120.effective_packet_rate < 61.0,
        "60 fps packets cannot be published as a 120 fps replay");

    const auto low_rate_144 = fthr::replay_health::Evaluate(
        ReplayHealthObservedRateFixture(5, 144, 60));
    CheckPolicy(!low_rate_144.accepted
            && low_rate_144.failure
                == fthr::replay_health::Failure::LowEffectiveRate,
        "60 fps packets cannot be published as a 144 fps replay");

    const auto observed_51 = fthr::replay_health::Evaluate(
        ReplayHealthObservedRateFixture(5, 60, 51));
    CheckPolicy(observed_51.accepted
            && observed_51.effective_packet_rate > 50.0,
        "the observed 51 fps cadence remains acceptable for a 60 fps target");

    for (const uint32_t fps : {144u, 165u}) {
        const auto timer_limited = fthr::replay_health::Evaluate(
            ReplayHealthObservedRateFixture(10, fps, 67));
        CheckPolicy(!timer_limited.accepted
                && timer_limited.failure == fthr::replay_health::Failure::LowEffectiveRate,
            "observed 67 fps timer starvation remains rejected at 144/165 fps");
    }

    auto partial = ReplayHealthCoverageFixture(30, 0.787, 60);
    const auto partial_report = fthr::replay_health::Evaluate(partial);
    CheckPolicy(partial_report.accepted && partial_report.partial_history
            && partial_report.required_min_coverage_s == 0.5,
        "a dense 0.787-second buffer saves available history for a 30-second request");

    const auto medium_partial = fthr::replay_health::Evaluate(
        ReplayHealthCoverageFixture(30, 10.432, 60));
    CheckPolicy(medium_partial.accepted && medium_partial.partial_history,
        "a dense 10.432-second buffer saves available history for a 30-second request");

    const auto eight_tenths = fthr::replay_health::Evaluate(
        ReplayHealthCoverageFixture(30, 0.8, 60));
    CheckPolicy(eight_tenths.accepted && eight_tenths.partial_history,
        "an 0.8-second partial buffer is accepted for a 30-second request");

    for (const uint32_t fps : {30u, 60u, 120u, 144u, 165u, 240u}) {
        for (const double coverage : {0.5, 1.0, 6.125, 12.125, 23.8167, 54.0, 59.0}) {
            const auto short_clip = fthr::replay_health::Evaluate(
                ReplayHealthCoverageFixture(60, coverage, fps));
            CheckPolicy(short_clip.accepted && short_clip.partial_history,
                "a 60-second request saves healthy available history from half a second onward");
        }
        const auto too_short = fthr::replay_health::Evaluate(
            ReplayHealthFrameIntervalFixture(60, fps, fps / 2 - 1));
        CheckPolicy(!too_short.accepted
                && too_short.failure == fthr::replay_health::Failure::InsufficientCoverage,
            "less than half a second of media still reports insufficient history");
    }

    auto stale_partial = ReplayHealthCoverageFixture(60, 1.0, 60);
    stale_partial.target_end_wall_qpc = stale_partial.packets.back().wall_qpc + 1'000'001;
    CheckPolicy(fthr::replay_health::Evaluate(stale_partial).failure
            == fthr::replay_health::Failure::StaleEnd,
        "short available history must still reach the save boundary");

    auto gapped_partial = ReplayHealthCoverageFixture(60, 1.0, 60);
    for (size_t index = 30; index < gapped_partial.packets.size(); ++index) {
        gapped_partial.packets[index].wall_qpc += 500'000;
    }
    CheckPolicy(fthr::replay_health::Evaluate(gapped_partial).failure
            == fthr::replay_health::Failure::LargeTimelineGap,
        "short available history must still have a continuous timeline");

    const auto complete_partial = fthr::replay_health::Evaluate(
        ReplayHealthCoverageFixture(30, 30.0, 60));
    CheckPolicy(complete_partial.accepted && complete_partial.partial_history
            && complete_partial.actual_coverage_s >= 29.9,
        "a full-duration partial selection remains valid after recovery");

    const auto dense_partial = fthr::replay_health::Evaluate(
        ReplayHealthFixture(1, 30, false));
    CheckPolicy(dense_partial.accepted && dense_partial.partial_history,
        "dense one-second history satisfies its one-second request threshold");

    auto short_gap = ReplayHealthFixture(5, 60);
    // One missed scheduling deadline is bounded and must not reject the clip.
    for (size_t index = 61; index < short_gap.packets.size(); ++index) {
        short_gap.packets[index].wall_qpc += 2 * 16'666;
    }
    const auto short_gap_report = fthr::replay_health::Evaluate(short_gap);
    CheckPolicy(short_gap_report.accepted
            && short_gap_report.largest_gap_s < 0.250,
        "a short bounded capture gap remains acceptable");
}

void ReplayHealthRejectsGenerationChange() {
    auto input = ReplayHealthFixture(1, 60);
    input.packets[30].generation = 5;
    const auto report = fthr::replay_health::Evaluate(input);
    CheckPolicy(!report.accepted
            && report.failure
                == fthr::replay_health::Failure::GenerationDiscontinuity,
        "a replay crossing capture generations is rejected");
}

void ReplayHealthRejectsStaleSaveBoundary() {
    auto input = ReplayHealthFixture(5, 60);
    input.target_end_wall_qpc = input.packets.back().wall_qpc + 1'000'001;
    const auto report = fthr::replay_health::Evaluate(input);
    CheckPolicy(!report.accepted
            && report.failure == fthr::replay_health::Failure::StaleEnd
            && report.effective_packet_rate_measured
            && report.effective_packet_rate > 59.0,
        "a stale replay keeps its measured valid timeline rate for diagnostics");
}

void ReplayHealthReportsF7746A8BStaleSaveEvidence() {
    fthr::replay_health::Input input;
    input.requested_duration_seconds = 30;
    input.expected_fps = 60.0;
    input.wall_qpc_frequency = 10'000'000;
    input.target_end_wall_qpc = 539'577'129'004;
    input.pts_per_second = 60.0;
    input.full_history = true;
    input.packets.reserve(22);
    input.packets.push_back({539'343'227'390, 1223, 4});
    input.packets.push_back({539'344'311'448, 1229, 4});
    input.packets.push_back({539'548'385'515, 2454, 4});

    constexpr int64_t kTailQpcSpan = 5'418'597;
    for (int64_t tail_index = 1; tail_index <= 19; ++tail_index) {
        const int64_t pts_offset = tail_index <= 13
            ? tail_index * 2
            : 26 + tail_index - 13;
        input.packets.push_back({
            539'548'385'515 + kTailQpcSpan * tail_index / 19,
            2454 + pts_offset,
            4});
    }

    const auto stale_report = fthr::replay_health::Evaluate(input);
    CheckPolicy(!stale_report.accepted
            && stale_report.failure == fthr::replay_health::Failure::StaleEnd
            && stale_report.packet_count == 22
            && stale_report.full_history
            && stale_report.first_wall_qpc == 539'343'227'390
            && stale_report.last_wall_qpc == 539'553'804'112
            && stale_report.first_pts == 1223
            && stale_report.last_pts == 2486
            && std::abs(stale_report.end_staleness_s - 2.3324892) < 1e-12
            && std::abs(stale_report.wall_coverage_s - 21.0576722) < 1e-12
            && std::abs(stale_report.actual_coverage_s - 21.05) < 1e-12
            && std::abs(stale_report.largest_wall_gap_s - 20.4074067) < 1e-12
            && std::abs(stale_report.largest_pts_gap_s
                - 20.4166666667) < 1e-9
            && std::abs(stale_report.largest_gap_s - 20.4166666667) < 1e-9
            && stale_report.largest_wall_gap_start_qpc == 539'344'311'448
            && stale_report.largest_wall_gap_end_qpc == 539'548'385'515
            && stale_report.largest_pts_gap_start == 1229
            && stale_report.largest_pts_gap_end == 2454
            && stale_report.effective_packet_rate_measured
            && std::abs(stale_report.effective_packet_rate - 21.0 / 21.05)
                < 1e-12,
        "f7746a8b stale-save evidence retains its measured 21/21.05 fps rate");

    input.target_end_wall_qpc = input.packets.back().wall_qpc;
    const auto nonstale_report = fthr::replay_health::Evaluate(input);
    CheckPolicy(!nonstale_report.accepted
            && nonstale_report.failure
                == fthr::replay_health::Failure::InsufficientCoverage
            && nonstale_report.end_staleness_s == 0.0
            && nonstale_report.effective_packet_rate_measured
            && std::abs(nonstale_report.actual_coverage_s - 21.05) < 1e-12
            && std::abs(nonstale_report.largest_gap_s - 20.4166666667) < 1e-9,
        "removing stale-end evidence still rejects the replay for its short coverage and large gap");
}

void ReplayHealthLeavesRateUnmeasuredForEmptyOrInvalidTimelines() {
    fthr::replay_health::Input empty;
    empty.expected_fps = 60.0;
    const auto empty_report = fthr::replay_health::Evaluate(empty);
    CheckPolicy(!empty_report.accepted
            && empty_report.failure == fthr::replay_health::Failure::Empty
            && !empty_report.effective_packet_rate_measured
            && empty_report.effective_packet_rate == 0.0,
        "an empty packet timeline has no measured effective rate");

    auto invalid = ReplayHealthFixture(1, 60);
    invalid.packets[1].pts = invalid.packets[0].pts;
    const auto invalid_report = fthr::replay_health::Evaluate(invalid);
    CheckPolicy(!invalid_report.accepted
            && invalid_report.failure
                == fthr::replay_health::Failure::InvalidTimeline
            && !invalid_report.effective_packet_rate_measured
            && invalid_report.effective_packet_rate == 0.0,
        "a non-monotone timeline has no measured effective rate");
}

void ReplayHealthReportsExactSavePredicates() {
    for (const uint32_t fps : {60u, 120u, 144u}) {
        const auto report = fthr::replay_health::Evaluate(
            ReplayHealthFixture(2, fps));
        CheckPolicy(report.accepted
                && report.wall_coverage_s > 1.9
                && report.actual_coverage_s > 1.9
                && report.required_min_coverage_s > 1.7
                && report.max_allowed_gap_s >= 0.250
                && report.minimum_effective_rate == static_cast<double>(fps) * 0.8
                && report.wall_monotonic && report.pts_monotonic
                && report.intervals_over_gap_threshold == 0,
            "health report exposes exact coverage/rate/gap predicates at 60/120/144 fps");
    }

    // For a 30-second request at 60 fps the policy requires exactly
    // (30 * 0.90 - one frame) = 1619 frame intervals of media coverage.
    const auto before_boundary = fthr::replay_health::Evaluate(
        ReplayHealthFrameIntervalFixture(30, 60, 1'618, true));
    const auto at_boundary = fthr::replay_health::Evaluate(
        ReplayHealthFrameIntervalFixture(30, 60, 1'619, true));
    const auto after_boundary = fthr::replay_health::Evaluate(
        ReplayHealthFrameIntervalFixture(30, 60, 1'620, true));
    CheckPolicy(!before_boundary.accepted
            && before_boundary.failure
                == fthr::replay_health::Failure::InsufficientCoverage
            && at_boundary.accepted
            && after_boundary.accepted,
        "coverage is rejected just before and accepted at/after the exact minimum boundary");

    auto jittered = ReplayHealthFixture(5, 60);
    for (size_t index = 1; index + 1 < jittered.packets.size(); ++index) {
        const int64_t jitter_qpc = (index % 3 == 0) ? 2'000
            : ((index % 3 == 1) ? -1'000 : 0);
        jittered.packets[index].wall_qpc += jitter_qpc;
    }
    const auto jittered_report = fthr::replay_health::Evaluate(jittered);
    CheckPolicy(jittered_report.accepted
            && jittered_report.intervals_over_gap_threshold == 0,
        "minor legitimate wall-clock jitter remains valid replay history");

    auto wall_regression = ReplayHealthFixture(1, 60);
    wall_regression.packets[10].wall_qpc = wall_regression.packets[9].wall_qpc;
    const auto wall_report = fthr::replay_health::Evaluate(wall_regression);
    CheckPolicy(wall_report.timeline_subtype
            == fthr::replay_health::TimelineSubtype::WallRegressionOrDuplicate
            && !wall_report.wall_monotonic,
        "duplicate wall timestamps retain an exact invalid-timeline subtype");

    auto pts_regression = ReplayHealthFixture(1, 60);
    pts_regression.packets[10].pts = pts_regression.packets[9].pts;
    const auto pts_report = fthr::replay_health::Evaluate(pts_regression);
    CheckPolicy(pts_report.timeline_subtype
            == fthr::replay_health::TimelineSubtype::PtsRegressionOrDuplicate
            && !pts_report.pts_monotonic,
        "duplicate media timestamps retain an exact invalid-timeline subtype");

    auto genuine_gap = ReplayHealthFixture(2, 60);
    for (size_t index = 60; index < genuine_gap.packets.size(); ++index) {
        genuine_gap.packets[index].wall_qpc += 500'000;
    }
    const auto gap_report = fthr::replay_health::Evaluate(genuine_gap);
    CheckPolicy(gap_report.failure == fthr::replay_health::Failure::LargeTimelineGap
            && gap_report.intervals_over_gap_threshold == 1
            && gap_report.largest_gap_s > gap_report.max_allowed_gap_s,
        "genuine wall gaps report actual/required threshold predicates");
}

void ReplaySaveClassificationCoversEvidenceBoundaries() {
    using fthr::replay_health::Failure;
    using fthr::replay_health::TimelineSubtype;
    using fthr::replay_save::Classification;
    using fthr::replay_save::SnapshotEvidence;
    using fthr::replay_save::SnapshotOutcome;
    using fthr::replay_save::RuntimeEvidence;

    const auto missing_selection = fthr::replay_interval::Select(
        {{100, false}, {200, false}}, 200, 100);
    CheckPolicy(!missing_selection.valid
            && missing_selection.outcome
                == fthr::replay_interval::Selection::Outcome::MissingStartKeyframe
            && missing_selection.requested_start == 100,
        "selection reports a missing start keyframe without losing the interval");

    RuntimeEvidence startup;
    startup.now_qpc = 1'000;
    startup.qpc_frequency = 1'000;
    startup.generation_start_qpc = 900;
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::InsufficientCoverage, SnapshotEvidence{}, startup, 30.0)
                == Classification::NotReadyAfterStart,
        "short history in a young generation is classified as startup readiness");

    RuntimeEvidence retained = startup;
    retained.generation_start_qpc = 0;
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::InsufficientCoverage, SnapshotEvidence{}, retained, 30.0)
                == Classification::InsufficientRetainedHistory,
        "short history in an old/unknown generation is retained-history failure");

    SnapshotEvidence missing{SnapshotOutcome::MissingStartKeyframe};
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::Empty, missing, retained, 30.0)
                == Classification::MissingStartKeyframe,
        "missing keyframe is classified at selection boundary");

    SnapshotEvidence race{SnapshotOutcome::RetryExhaustedGenerationOrSlotRace};
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::Empty, race, retained, 30.0)
                == Classification::ReplaySnapshotRace,
        "snapshot retry exhaustion is classified as a race");

    CheckPolicy(fthr::replay_save::Classify(
                    Failure::GenerationDiscontinuity, SnapshotEvidence{},
                    retained, 30.0)
                == Classification::ReplaySnapshotRace,
        "mixed packet generations are classified at the snapshot boundary");

    CheckPolicy(fthr::replay_save::Classify(
                    Failure::LargeTimelineGap, SnapshotEvidence{}, retained, 30.0)
                == Classification::VideoTimestampGap
            && fthr::replay_save::Classify(
                    Failure::InvalidTimeline, TimelineSubtype::PtsRegressionOrDuplicate,
                    SnapshotEvidence{}, retained, 30.0)
                == Classification::VideoTimestampRegression,
        "timestamp gaps and regressions retain separate classifications");

    RuntimeEvidence capture = retained;
    capture.now_qpc = 5'000;
    capture.qpc_frequency = 1'000;
    capture.last_source_frame_qpc = 3'000;
    capture.last_encoded_output_qpc = 0;
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::StaleEnd, SnapshotEvidence{}, capture, 30.0)
                == Classification::CaptureStarvation,
        "stale source progress classifies capture starvation");

    RuntimeEvidence encoder = capture;
    encoder.last_source_frame_qpc = 5'000;
    encoder.last_encoder_submission_qpc = 4'000;
    encoder.last_encoded_output_qpc = 1'000;
    encoder.encoder_submissions = 20;
    encoder.encoded_packets = 19;
    CheckPolicy(fthr::replay_save::Classify(
                    Failure::StaleEnd, SnapshotEvidence{}, encoder, 30.0)
                == Classification::EncoderStarvation,
        "pending submissions without output classify encoder starvation");
}

void CapturePipelineHealthClassifiesBoundaries() {
    using fthr::capture_pipeline_health::Classification;
    using fthr::capture_pipeline_health::Snapshot;

    Snapshot healthy;
    healthy.expected_fps = 60.0;
    healthy.observation_interval_ms = 10'000;
    healthy.source_frames = 120;
    healthy.raw_source_frame_delta = 600;
    healthy.accepted_source_frame_delta = 600;
    healthy.encoder_submissions = 120;
    healthy.encoded_packets = 120;
    healthy.replay_ring_insertions = 120;
    healthy.source_age_ms = 100;
    healthy.encoder_submission_age_ms = 100;
    healthy.encoded_output_age_ms = 100;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(healthy)
            == Classification::Healthy,
        "active capture with accepted encoder output is healthy");

    Snapshot degraded_cadence = healthy;
    degraded_cadence.accepted_source_frame_delta = 500;
    degraded_cadence.interval_pacing_drops = 100;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(degraded_cadence)
            == Classification::DegradedCaptureCadence,
        "offered 60 fps with sustained sub-target accepted cadence is degraded");

    Snapshot high_fps_source_limited = healthy;
    high_fps_source_limited.expected_fps = 144.0;
    high_fps_source_limited.accepted_source_frame_delta = 600;
    high_fps_source_limited.raw_source_frame_delta = 600;
    high_fps_source_limited.interval_pacing_drops = 0;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(high_fps_source_limited)
            == Classification::DegradedCaptureCadence,
        "active 60 fps DXGI source below a 144 fps target is degraded without pacing drops");

    Snapshot high_fps_healthy = high_fps_source_limited;
    high_fps_healthy.accepted_source_frame_delta = 1'440;
    high_fps_healthy.raw_source_frame_delta = 1'440;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(high_fps_healthy)
            == Classification::Healthy,
        "active DXGI source meeting a 144 fps target remains healthy");

    Snapshot held_sparse_dxgi = healthy;
    held_sparse_dxgi.expected_fps = 144.0;
    held_sparse_dxgi.observation_interval_ms = 10'000;
    held_sparse_dxgi.source_frames = 100;
    held_sparse_dxgi.raw_source_frame_delta = 100;
    held_sparse_dxgi.accepted_source_frame_delta = 100;
    held_sparse_dxgi.interval_pacing_drops = 0;
    held_sparse_dxgi.encoder_submissions = 1'440;
    held_sparse_dxgi.encoded_packets = 1'440;
    held_sparse_dxgi.replay_ring_insertions = 1'440;
    held_sparse_dxgi.source_age_ms = 100;
    held_sparse_dxgi.encoder_submission_age_ms = 100;
    held_sparse_dxgi.encoded_output_age_ms = 100;
    held_sparse_dxgi.held_presentation_observations = 1'340;
    held_sparse_dxgi.api_completion_recent = true;
    held_sparse_dxgi.dxgi_replay_cache_armed = true;
    held_sparse_dxgi.acquire_loop_recent = true;
    held_sparse_dxgi.wait_timeout_current = true;
    CheckPolicy(held_sparse_dxgi.raw_source_frame_delta
                < held_sparse_dxgi.expected_fps
                    * held_sparse_dxgi.observation_interval_ms / 1000.0 * 0.10
            && fthr::capture_pipeline_health::Classify(held_sparse_dxgi)
                == Classification::DegradedCaptureCadence,
        "100 real image updates in 10s with held 144-fps output is degraded even below the raw-source 10-percent floor");

    Snapshot held_sparse_without_cache = held_sparse_dxgi;
    held_sparse_without_cache.dxgi_replay_cache_armed = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(held_sparse_without_cache)
            == Classification::DegradedCaptureCadence,
        "disarming the cache does not erase already observed held-source cadence degradation");

    Snapshot pointer_only_static = healthy;
    pointer_only_static.accepted_source_frame_delta = 0;
    pointer_only_static.raw_source_frame_delta = 0;
    pointer_only_static.source_age_ms = 100;
    pointer_only_static.encoder_submission_age_ms = 100;
    pointer_only_static.encoded_output_age_ms = 100;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(pointer_only_static)
            == Classification::Healthy,
        "pointer-only DXGI activity without image-bearing acquires remains healthy");

    Snapshot physical_burst_starvation = healthy;
    physical_burst_starvation.accepted_source_frame_delta = 98;
    physical_burst_starvation.interval_pacing_drops = 41;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(physical_burst_starvation)
            == Classification::DegradedCaptureCadence,
        "9.79 fps accepted cadence with active pacing drops is degraded");

    Snapshot recovered_cadence = healthy;
    recovered_cadence.interval_recovery_attempts = 2;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(recovered_cadence)
            == Classification::DegradedCaptureCadence,
        "repeated recovery attempts degrade an otherwise flowing interval");

    Snapshot single_recovery = healthy;
    single_recovery.interval_recovery_attempts = 1;
    single_recovery.interval_generation_resets = 1;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(single_recovery)
            == Classification::Healthy,
        "one transient recovery does not degrade the interval");

    Snapshot awaiting;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(awaiting)
            == Classification::AwaitingFirstSource,
        "capture without a source frame remains awaiting first source");

    Snapshot stale = healthy;
    stale.wgc_backend = true;
    stale.repeated_frame_observations = 300;
    stale.source_age_ms = 5'001;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(stale)
            == Classification::WgcSourceStaleCandidate,
        "quiet WGC source is classified as a stale candidate");
    // The helper returns evidence only.  A stale WGC source has no recovery
    // decision or restart side effect, because a static desktop is valid.
    CheckPolicy(
        fthr::capture_pipeline_health::ClassificationName(
            fthr::capture_pipeline_health::Classify(stale))
            == std::string("WGC_SOURCE_STALE_CANDIDATE"),
        "WGC stale classification is diagnostic-only");

    Snapshot static_dxgi = healthy;
    static_dxgi.accepted_source_frame_delta = 0;
    static_dxgi.raw_source_frame_delta = 0;
    static_dxgi.interval_pacing_drops = 0;
    static_dxgi.source_frames = 23;
    static_dxgi.encoder_submissions = 23;
    static_dxgi.encoded_packets = 23;
    static_dxgi.replay_ring_insertions = 23;
    static_dxgi.source_age_ms = 20'138;
    static_dxgi.encoder_submission_age_ms = 20'138;
    static_dxgi.encoded_output_age_ms = 20'138;
    CheckPolicy(!static_dxgi.acquire_loop_recent
            && !static_dxgi.idle_evidence && !static_dxgi.encoder_idle
            && !static_dxgi.dxgi_replay_cache_armed
            && !static_dxgi.has_pending_work,
        "DXGI idle evidence defaults to unknown rather than healthy");
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(static_dxgi)
            == Classification::CaptureSourceStallCandidate
            && std::string(fthr::capture_pipeline_health::ClassificationName(
                fthr::capture_pipeline_health::Classify(static_dxgi)))
                == "CAPTURE_SOURCE_STALL_CANDIDATE",
        "23 old source/submission/output observations without live DXGI evidence identify a source stall");

    Snapshot timeout_idle = static_dxgi;
    timeout_idle.dxgi_replay_cache_armed = true;
    timeout_idle.api_completion_recent = true;
    timeout_idle.acquire_loop_recent = true;
    timeout_idle.wait_timeout_current = true;
    timeout_idle.idle_evidence = true;
    timeout_idle.encoder_idle = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(timeout_idle)
            == Classification::DxgiImageIdle
            && std::string(fthr::capture_pipeline_health::ClassificationName(
                fthr::capture_pipeline_health::Classify(timeout_idle)))
                == "DXGI_IMAGE_IDLE"
            && !fthr::capture_pipeline_health::IsDiagnosticCandidate(
                fthr::capture_pipeline_health::Classify(timeout_idle)),
        "recent AcquireNextFrame timeout and an idle encoder classify a static image as informational");
    const auto idle_replay = fthr::replay_health::Evaluate(
        ReplayHealthCoverageFixture(30, 29.0, 60));
    CheckPolicy(fthr::capture_pipeline_health::Classify(timeout_idle)
                == Classification::DxgiImageIdle
            && idle_replay.accepted && idle_replay.partial_history,
        "healthy near-duration partial history can save during informational DXGI image idle");

    Snapshot pointer_only_idle = static_dxgi;
    pointer_only_idle.dxgi_replay_cache_armed = true;
    pointer_only_idle.api_completion_recent = true;
    pointer_only_idle.acquire_loop_recent = true;
    pointer_only_idle.pointer_only_recent = true;
    pointer_only_idle.idle_evidence = true;
    pointer_only_idle.encoder_idle = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(pointer_only_idle)
            == Classification::DxgiImageIdle,
        "recent pointer-only activity with a live acquire loop and idle encoder proves static imagery");

    Snapshot unarmed_idle = timeout_idle;
    unarmed_idle.dxgi_replay_cache_armed = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(unarmed_idle)
            == Classification::CaptureSourceStallCandidate,
        "timeout evidence without a confirmed owned DXGI cache cannot claim static-image idle");

    Snapshot held_current_output = healthy;
    held_current_output.source_age_ms = 20'138;
    held_current_output.dxgi_replay_cache_armed = true;
    held_current_output.held_presentation_observations = 600;
    held_current_output.api_completion_recent = true;
    held_current_output.acquire_loop_recent = true;
    held_current_output.wait_timeout_current = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(held_current_output)
            == Classification::DxgiImageIdle,
        "held static presentations with recent API completions remain informational while encoder output is current");

    Snapshot held_without_completion = held_current_output;
    held_without_completion.api_completion_recent = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(held_without_completion)
            == Classification::CaptureSourceStallCandidate,
        "held static output without a completed acquire call is not mistaken for live DXGI idle evidence");

    Snapshot held_without_cache = held_current_output;
    held_without_cache.dxgi_replay_cache_armed = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(held_without_cache)
            == Classification::CaptureSourceStallCandidate,
        "recent held output cannot claim static idle after its owned cache was disarmed");

    Snapshot held_pending_stale_output = held_current_output;
    held_pending_stale_output.encoded_output_age_ms = 2'001;
    held_pending_stale_output.has_pending_work = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(held_pending_stale_output)
            == Classification::EncoderOutputStallCandidate,
        "stale encoder output takes priority over held-static evidence when work remains pending");

    Snapshot timeout_without_recent_loop = timeout_idle;
    timeout_without_recent_loop.acquire_loop_recent = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(timeout_without_recent_loop)
            == Classification::CaptureSourceStallCandidate,
        "a timeout without a recently advancing acquire loop cannot exempt stale DXGI clocks");

    Snapshot old_pointer_only = pointer_only_idle;
    old_pointer_only.pointer_only_recent = false;
    old_pointer_only.idle_evidence = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(old_pointer_only)
            == Classification::CaptureSourceStallCandidate,
        "old pointer-only observations cannot exempt stale DXGI clocks");

    Snapshot encoder_state_unknown = timeout_idle;
    encoder_state_unknown.encoder_idle = false;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(encoder_state_unknown)
            == Classification::CaptureSourceStallCandidate,
        "recent DXGI evidence without positive encoder-idle diagnostics is not an idle exemption");

    Snapshot pending_encoder_work = timeout_idle;
    pending_encoder_work.encoder_idle = false;
    pending_encoder_work.has_pending_work = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(pending_encoder_work)
            == Classification::EncoderOutputStallCandidate
            && fthr::capture_pipeline_health::IsDiagnosticCandidate(
                fthr::capture_pipeline_health::Classify(pending_encoder_work)),
        "pending encoder work takes precedence over static-image evidence");

    Snapshot static_backend_failure = timeout_idle;
    static_backend_failure.backend_failed = true;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(static_backend_failure)
            == Classification::BackendFailure,
        "a real backend failure takes precedence over a static-image exemption");

    Snapshot repeated_recovery_static = static_dxgi;
    repeated_recovery_static.interval_recovery_attempts = 2;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(repeated_recovery_static)
            == Classification::DegradedCaptureCadence,
        "repeated recovery is degraded even when static DXGI ages are old");

    Snapshot single_recovery_static = static_dxgi;
    single_recovery_static.interval_recovery_attempts = 1;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(single_recovery_static)
            == Classification::CaptureSourceStallCandidate,
        "a single recovery does not supply missing DXGI-idle evidence");

    Snapshot wgc_capture_stall = static_dxgi;
    wgc_capture_stall.wgc_backend = true;
    wgc_capture_stall.repeated_frame_observations = 300;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(wgc_capture_stall)
            == Classification::CaptureToEncoderStallCandidate,
        "stalled WGC sampler with old submissions identifies capture boundary");

    Snapshot capture_stall = healthy;
    capture_stall.encoder_submissions = 0;
    capture_stall.encoded_packets = 0;
    capture_stall.replay_ring_insertions = 0;
    capture_stall.encoder_submission_age_ms = 2'001;
    capture_stall.encoded_output_age_ms = 2'001;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(capture_stall)
            == Classification::CaptureToEncoderStallCandidate
            && fthr::capture_pipeline_health::IsDiagnosticCandidate(
                fthr::capture_pipeline_health::Classify(capture_stall)),
        "source progress without encoder submissions identifies the boundary");

    Snapshot encoder_stall = healthy;
    encoder_stall.encoded_packets = 119;
    encoder_stall.replay_ring_insertions = 119;
    encoder_stall.encoded_output_age_ms = 2'001;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(encoder_stall)
            == Classification::EncoderOutputStallCandidate,
        "encoder submissions without recent output identify the boundary");

    Snapshot ring_rejection = healthy;
    ring_rejection.encoded_packets = 120;
    ring_rejection.replay_ring_insertions = 119;
    ring_rejection.replay_ring_insertion_age_ms = 100;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(ring_rejection)
            == Classification::Healthy,
        "a historical ring rejection does not stick while insertion progresses");
    ring_rejection.replay_ring_insertion_age_ms = 2'001;
    CheckPolicy(
        fthr::capture_pipeline_health::Classify(ring_rejection)
            == Classification::ReplayRingRejectionCandidate,
        "a ring that stops accepting packets identifies the boundary");
    CheckPolicy(
        fthr::capture_pipeline_health::SaturatingDifference(119, 120) == 0,
        "ring rejection accounting saturates when accepted count is ahead");
}

void EncodedRingGenerationIsolatedAcrossClear() {
    fthr::EncodedRingBuffer ring(16, 60, 1'000'000);
    const uint8_t packet = 0x01;
    ring.Push(&packet, 1, 0, true, 1'000'000);
    const auto before = ring.TakeSnapshotByTime(1, 1'000'000);
    CheckPolicy(before.packets.size() == 1
            && before.capture_generation == before.packets.front().generation,
        "snapshot exposes the packet generation before recovery");
    CheckPolicy(before.selection_outcome
            == fthr::replay_save::SnapshotOutcome::Valid
            && before.selection_attempts == 1
            && before.scanned_packet_count == 1
            && before.eligible_packet_count == 1
            && before.keyframe_count == 1
            && before.valid_starting_keyframe
            && before.ring_capacity == 16
            && before.ring_count == 1
            && before.snapshot_start_ring_head == 1
            && before.snapshot_end_ring_head == 1
            && before.snapshot_start_ring_count == 1
            && before.snapshot_end_ring_count == 1
            && before.snapshot_start_ring_generation == before.capture_generation
            && before.snapshot_end_ring_generation == before.capture_generation
            && before.oldest_wall_qpc == 1'000'000
            && before.newest_wall_qpc == 1'000'000,
        "valid snapshot exposes bounded ring and keyframe metadata");

    const uint64_t old_generation = ring.GetGeneration();
    ring.Clear();
    CheckPolicy(ring.GetGeneration() == old_generation + 1
            && ring.GetCount() == 0,
        "clearing the encoded ring advances its internal generation");
    ring.Push(&packet, 1, 0, true, 2'000'000);
    const auto after = ring.TakeSnapshotByTime(1, 2'000'000);
    CheckPolicy(after.packets.size() == 1
            && after.capture_generation == ring.GetGeneration()
            && after.packets.front().generation != before.packets.front().generation,
        "post-recovery snapshots contain only the fresh generation");

    fthr::EncodedRingBuffer cutoff_ring(16, 60, 1'000'000);
    cutoff_ring.Clear(1'500'000);
    cutoff_ring.Push(&packet, 1, 0, true, 1'400'000);
    CheckPolicy(cutoff_ring.GetCount() == 0,
        "delayed pre-recovery output at the cutoff is rejected");
    cutoff_ring.Push(&packet, 1, 1, true, 1'600'000);
    CheckPolicy(cutoff_ring.GetCount() == 1,
        "post-recovery output beyond the cutoff is accepted");
}

enum class ScriptedDxgiScenario {
    StaticDesktopAfterFiveSeconds,
    CaptureInterruptionAfterFiveSeconds,
    ContinuouslyDynamic,
};

enum class ScriptedDxgiEventKind {
    NewDesktopImage,
    PointerOnlyCompletion,
    WaitTimeout,
    CaptureInterrupted,
};

struct ScriptedDxgiEvent {
    ScriptedDxgiEventKind kind = ScriptedDxgiEventKind::WaitTimeout;
    int64_t qpc = 0;
    int64_t last_present_qpc = 0;
    uint64_t image_id = 0;
};

// This source injects QPC ticks instead of consulting QueryPerformanceCounter,
// sleeps, DXGI, or a real desktop. One source step is one 60-fps interval.
class ScriptedDxgiSource {
public:
    static constexpr int64_t kQpcFrequency = 1'000'000;
    static constexpr uint32_t kFps = 60;
    static constexpr uint32_t kDynamicSeconds = 5;
    static constexpr uint32_t kTotalSeconds = 35;
    static constexpr int64_t kQpcEpoch = 1'000'000;

    explicit ScriptedDxgiSource(ScriptedDxgiScenario scenario)
        : scenario_(scenario) {}

    bool Next(ScriptedDxgiEvent& event) {
        if (tick_ >= static_cast<uint64_t>(kTotalSeconds) * kFps) {
            return false;
        }

        event.qpc = kQpcEpoch + static_cast<int64_t>(
            tick_ * static_cast<uint64_t>(kQpcFrequency) / kFps);
        const uint64_t dynamic_ticks =
            static_cast<uint64_t>(kDynamicSeconds) * kFps;
        if (scenario_ == ScriptedDxgiScenario::ContinuouslyDynamic
            || tick_ < dynamic_ticks) {
            event.kind = ScriptedDxgiEventKind::NewDesktopImage;
            event.last_present_qpc = event.qpc;
            event.image_id = tick_;
            ++image_updates_;
        } else if (scenario_
                   == ScriptedDxgiScenario::StaticDesktopAfterFiveSeconds) {
            event.image_id = dynamic_ticks - 1;
            event.last_present_qpc = 0;
            if ((tick_ - dynamic_ticks) % kFps == 0) {
                event.kind = ScriptedDxgiEventKind::PointerOnlyCompletion;
                ++pointer_only_completions_;
            } else {
                event.kind = ScriptedDxgiEventKind::WaitTimeout;
                ++wait_timeouts_;
            }
        } else {
            event.kind = ScriptedDxgiEventKind::CaptureInterrupted;
            event.last_present_qpc = 0;
            ++interrupted_ticks_;
        }
        ++tick_;
        return true;
    }

    int64_t SaveTargetQpc() const {
        return kQpcEpoch + static_cast<int64_t>(
            static_cast<uint64_t>(kTotalSeconds) * kQpcFrequency);
    }

    uint64_t image_updates() const { return image_updates_; }
    uint64_t pointer_only_completions() const {
        return pointer_only_completions_;
    }
    uint64_t wait_timeouts() const { return wait_timeouts_; }
    uint64_t interrupted_ticks() const { return interrupted_ticks_; }

private:
    ScriptedDxgiScenario scenario_;
    uint64_t tick_ = 0;
    uint64_t image_updates_ = 0;
    uint64_t pointer_only_completions_ = 0;
    uint64_t wait_timeouts_ = 0;
    uint64_t interrupted_ticks_ = 0;
};

struct DeterministicReplayPacket {
    std::array<uint8_t, 16> bytes{};
    int64_t pts = 0;
};

// Modeled encoder only: it emits deterministic packet bytes and frame-index
// PTS for an admitted desktop presentation. This is not NVENC, the real
// CaptureEngine save path, or a muxer.
class DeterministicReplayEncoderFake {
public:
    DeterministicReplayPacket Encode(
        uint64_t image_id, int64_t presentation_qpc) {
        DeterministicReplayPacket packet;
        packet.pts = static_cast<int64_t>(
            ((presentation_qpc - ScriptedDxgiSource::kQpcEpoch)
                * ScriptedDxgiSource::kFps
                + ScriptedDxgiSource::kQpcFrequency / 2)
            / ScriptedDxgiSource::kQpcFrequency);
        packet.bytes = {
            0x46, 0x54, 0x48, 0x52,
            static_cast<uint8_t>(image_id & 0xff),
            static_cast<uint8_t>((image_id >> 8) & 0xff),
            static_cast<uint8_t>(packet.pts & 0xff),
            static_cast<uint8_t>((packet.pts >> 8) & 0xff),
            static_cast<uint8_t>((packet.pts >> 16) & 0xff),
            static_cast<uint8_t>((packet.pts >> 24) & 0xff),
            0xa5, 0x5a, 0x01, 0x00, 0x00, 0x00,
        };
        return packet;
    }
};

struct ScriptedReplayRun {
    uint64_t source_image_updates = 0;
    uint64_t pointer_only_completions = 0;
    uint64_t wait_timeouts = 0;
    uint64_t interrupted_ticks = 0;
    uint64_t encoder_submissions = 0;
    uint64_t ring_packets_published = 0;
    fthr::EncodedRingSnapshot snapshot;
    fthr::replay_health::Report health;
};

ScriptedReplayRun RunCurrentDxgiAdmissionPolicy(
    ScriptedDxgiScenario scenario) {
    constexpr uint32_t kRequestedSeconds = 30;
    constexpr size_t kReplayRingCapacity =
        static_cast<size_t>(ScriptedDxgiSource::kTotalSeconds)
        * ScriptedDxgiSource::kFps;
    fthr::EncodedRingBuffer ring(
        kReplayRingCapacity,
        ScriptedDxgiSource::kFps,
        ScriptedDxgiSource::kQpcFrequency);
    ScriptedDxgiSource source(scenario);
    DeterministicReplayEncoderFake encoder;
    fthr::FrameRateScheduler scheduler(
        ScriptedDxgiSource::kQpcFrequency / ScriptedDxgiSource::kFps);
    ScriptedDxgiEvent event;
    ScriptedReplayRun run;

    while (source.Next(event)) {
        // Match the existing DXGI path: timeout events have no frame to admit;
        // successful pointer-only completions have no new desktop image and
        // are deliberately dropped. Only a new image reaches the encoder.
        if (event.kind != ScriptedDxgiEventKind::NewDesktopImage) continue;
        if (!scheduler.ShouldCapture(event.qpc)) continue;

        const auto packet = encoder.Encode(event.image_id, event.last_present_qpc);
        ++run.encoder_submissions;
        if (ring.Push(packet.bytes.data(),
                static_cast<uint32_t>(packet.bytes.size()), packet.pts,
                packet.pts % ScriptedDxgiSource::kFps == 0,
                event.last_present_qpc)) {
            ++run.ring_packets_published;
        }
    }

    run.source_image_updates = source.image_updates();
    run.pointer_only_completions = source.pointer_only_completions();
    run.wait_timeouts = source.wait_timeouts();
    run.interrupted_ticks = source.interrupted_ticks();
    run.snapshot = ring.TakeSnapshotByTime(
        kRequestedSeconds, source.SaveTargetQpc());

    run.health = fthr::replay_health::Evaluate(
        fthr::encoded_replay_health::MakeInput(
            run.snapshot, kRequestedSeconds, ScriptedDxgiSource::kFps,
            ScriptedDxgiSource::kQpcFrequency, source.SaveTargetQpc()));
    return run;
}

bool SamePacketHistory(
    const fthr::EncodedRingSnapshot& left,
    const fthr::EncodedRingSnapshot& right) {
    if (left.packets.size() != right.packets.size()) return false;
    for (size_t i = 0; i < left.packets.size(); ++i) {
        const auto& a = left.packets[i];
        const auto& b = right.packets[i];
        if (a.pts != b.pts || a.wall_qpc != b.wall_qpc
            || a.generation != b.generation || a.data != b.data) {
            return false;
        }
    }
    return true;
}

void StaticDesktopAndCaptureInterruptionReplayBaseline() {
    const auto static_desktop = RunCurrentDxgiAdmissionPolicy(
        ScriptedDxgiScenario::StaticDesktopAfterFiveSeconds);
    const auto interruption = RunCurrentDxgiAdmissionPolicy(
        ScriptedDxgiScenario::CaptureInterruptionAfterFiveSeconds);
    const auto dynamic_control = RunCurrentDxgiAdmissionPolicy(
        ScriptedDxgiScenario::ContinuouslyDynamic);

    std::cout << "[StaticReplayBaseline] static image_updates="
              << static_desktop.source_image_updates
              << " pointer_only=" << static_desktop.pointer_only_completions
              << " timeouts=" << static_desktop.wait_timeouts
              << " encoder_submissions=" << static_desktop.encoder_submissions
              << " ring_packets=" << static_desktop.ring_packets_published
              << " snapshot_packets=" << static_desktop.snapshot.packets.size()
              << " replay_health="
              << fthr::replay_health::FailureName(static_desktop.health.failure)
              << " accepted=" << (static_desktop.health.accepted ? "true" : "false")
              << std::endl;
    std::cout << "[StaticReplayBaseline] interruption image_updates="
              << interruption.source_image_updates
              << " interrupted_ticks=" << interruption.interrupted_ticks
              << " encoder_submissions=" << interruption.encoder_submissions
              << " ring_packets=" << interruption.ring_packets_published
              << " snapshot_packets=" << interruption.snapshot.packets.size()
              << " replay_health="
              << fthr::replay_health::FailureName(interruption.health.failure)
              << " accepted=" << (interruption.health.accepted ? "true" : "false")
              << std::endl;
    std::cout << "[StaticReplayBaseline] dynamic image_updates="
              << dynamic_control.source_image_updates
              << " encoder_submissions=" << dynamic_control.encoder_submissions
              << " ring_packets=" << dynamic_control.ring_packets_published
              << " snapshot_packets=" << dynamic_control.snapshot.packets.size()
              << " replay_health="
              << fthr::replay_health::FailureName(dynamic_control.health.failure)
              << " accepted=" << (dynamic_control.health.accepted ? "true" : "false")
              << std::endl;

    CheckPolicy(static_desktop.source_image_updates == 300
            && static_desktop.pointer_only_completions > 0
            && static_desktop.wait_timeouts > 0,
        "scripted static DXGI source holds image-update count fixed while pointer-only and timeout completions continue");
    CheckPolicy(interruption.source_image_updates == 300
            && interruption.interrupted_ticks == 30 * 60
            && interruption.encoder_submissions == 300
            && interruption.ring_packets_published == 300,
        "capture interruption adds no source updates or modeled packets beyond the identical first five seconds");
    CheckPolicy(SamePacketHistory(static_desktop.snapshot,
                interruption.snapshot),
        "static desktop and interrupted history contain identical deterministic early packets");
    CheckPolicy(!interruption.health.accepted
            && interruption.health.failure
                == fthr::replay_health::Failure::StaleEnd,
        "production replay-health criteria reject the genuinely interrupted history at the save boundary");
    CheckPolicy(dynamic_control.source_image_updates == 35 * 60
            && dynamic_control.encoder_submissions == 35 * 60
            && dynamic_control.ring_packets_published == 35 * 60
            && dynamic_control.health.accepted,
        "continuously dynamic 60-fps control produces and accepts a full 30-second replay snapshot");

    // Preserve the pre-fix behavior as an explicit baseline. The production
    // cadence path is covered separately and must accept this same valid still.
    CheckPolicy(!static_desktop.health.accepted
            && static_desktop.health.failure
                == fthr::replay_health::Failure::StaleEnd,
        "legacy image-only admission reproduces static replay rejection");
}

struct CadenceStepResult {
    fthr::dxgi_replay::AcquireSchedule schedule;
    fthr::dxgi_replay::AcquireResult acquire;
    fthr::dxgi_replay::Presentation presentation;
};

CadenceStepResult CompleteCadenceStep(
    fthr::dxgi_replay::Cadence& cadence,
    int64_t call_start_qpc,
    uint64_t generation,
    fthr::dxgi_replay::Observation observation,
    int64_t source_qpc = 0,
    bool release_succeeded = false,
    bool cache_copy_succeeded = false,
    int64_t call_duration_qpc = 1) {
    CadenceStepResult step;
    step.schedule = cadence.Schedule(call_start_qpc);
    if (!step.schedule.due) return step;
    step.acquire.generation = generation;
    step.acquire.call_start_qpc = call_start_qpc;
    step.acquire.call_end_qpc = call_start_qpc + call_duration_qpc;
    step.acquire.source_qpc = source_qpc;
    step.acquire.observation = observation;
    step.acquire.release_succeeded = release_succeeded;
    step.acquire.cache_copy_succeeded = cache_copy_succeeded;
    step.presentation = cadence.Complete(step.schedule.tick, step.acquire);
    return step;
}

struct HardwarePtsPacketFake {
    std::array<uint8_t, 16> bytes{};
    int64_t pts = 0;
};

// Models HardwareEncoder::ComputePts exactly: first accepted presentation is
// PTS 0; later timestamps floor elapsed-QPC * fps / frequency and clamp only
// when the result would not advance. It is not an NVENC, mux, or save fake.
class HardwarePtsEncoderFake {
public:
    HardwarePtsEncoderFake(uint32_t fps, int64_t qpc_frequency)
        : fps_(fps), qpc_frequency_(qpc_frequency) {}

    HardwarePtsPacketFake Encode(uint64_t image_id, int64_t presentation_qpc) {
        HardwarePtsPacketFake packet;
        if (first_) {
            first_ = false;
            epoch_qpc_ = presentation_qpc;
            packet.pts = 0;
            last_raw_pts_ = 0;
        } else {
            const int64_t raw_pts = ((presentation_qpc - epoch_qpc_)
                * static_cast<int64_t>(fps_)) / qpc_frequency_;
            last_raw_pts_ = raw_pts;
            packet.pts = raw_pts;
            if (packet.pts <= last_pts_) {
                packet.pts = last_pts_ + 1;
                ++pts_clamps_;
            }
            max_pts_clamp_lead_ = std::max(
                max_pts_clamp_lead_, packet.pts - raw_pts);
        }
        last_pts_ = packet.pts;
        packet.bytes = {
            0x46, 0x54, 0x48, 0x52,
            static_cast<uint8_t>(image_id & 0xff),
            static_cast<uint8_t>((image_id >> 8) & 0xff),
            static_cast<uint8_t>(packet.pts & 0xff),
            static_cast<uint8_t>((packet.pts >> 8) & 0xff),
            static_cast<uint8_t>((packet.pts >> 16) & 0xff),
            static_cast<uint8_t>((packet.pts >> 24) & 0xff),
            0xa5, 0x5a, 0x02, 0x00, 0x00, 0x00,
        };
        return packet;
    }

    uint64_t pts_clamps() const { return pts_clamps_; }
    int64_t max_pts_clamp_lead() const { return max_pts_clamp_lead_; }

private:
    uint32_t fps_;
    int64_t qpc_frequency_;
    int64_t epoch_qpc_ = 0;
    int64_t last_pts_ = 0;
    int64_t last_raw_pts_ = 0;
    int64_t max_pts_clamp_lead_ = 0;
    uint64_t pts_clamps_ = 0;
    bool first_ = true;
};

enum class CadenceReplayScenario {
    StaticAfterFiveSeconds,
    ContinuouslyDynamic,
    InterruptedThenResume,
    RepeatedQuietMotionEpisodes,
    SparseDynamic,
    InitialStatic,
};

struct CadenceReplaySettings {
    CadenceReplayScenario scenario =
        CadenceReplayScenario::StaticAfterFiveSeconds;
    uint32_t requested_fps = 60;
    uint32_t source_fps = 60;
    uint32_t total_seconds = 35;
    uint32_t requested_duration_seconds = 30;
    uint32_t poll_fps = 500;
    int64_t poll_phase_qpc = 0;
    bool jitter_poll_phase = false;
    bool encoder_emits_packets = true;
};

struct CadenceReplayRun {
    uint64_t source_image_updates = 0;
    uint64_t pointer_only_completions = 0;
    uint64_t wait_timeouts = 0;
    uint64_t failure_observations = 0;
    uint64_t interrupted_presentations = 0;
    uint64_t interrupted_encoder_submissions = 0;
    uint64_t acquire_calls = 0;
    uint64_t completed_ticks = 0;
    uint64_t distinct_ticks = 0;
    uint64_t presentations = 0;
    uint64_t held_presentations = 0;
    uint64_t resampled_source_qpc = 0;
    uint64_t encoder_submissions = 0;
    uint64_t ring_packets_published = 0;
    uint64_t pts_clamps = 0;
    int64_t max_pts_clamp_lead = 0;
    double media_wall_coverage_divergence_s = 0.0;
    int64_t first_source_qpc = 0;
    int64_t last_source_qpc = 0;
    int64_t last_presentation_qpc = 0;
    double health_expected_fps = 0.0;
    double health_pts_per_second = 0.0;
    bool presentations_monotonic = true;
    fthr::EncodedRingSnapshot snapshot;
    fthr::replay_health::Report health;
};

CadenceReplayRun RunProductionCadenceReplay(
    const CadenceReplaySettings& settings) {
    constexpr int64_t kQpcFrequency = 1'000'000;
    constexpr int64_t kQpcEpoch = 1'000'000;
    constexpr uint64_t kGeneration = 7;
    const size_t ring_capacity = fthr::CalculateEncodedReplaySlotCapacity(
        settings.requested_duration_seconds, settings.requested_fps);
    fthr::EncodedRingBuffer ring(
        ring_capacity, settings.requested_fps, kQpcFrequency);
    // The deterministic fake packet payload is not a decodable access unit;
    // this valid stream description exists only so snapshot timing uses the
    // same frame-rate and PTS units as the fake encoder.
    fthr::EncodedVideoConfig fake_video_config;
    fake_video_config.width = 1920;
    fake_video_config.height = 1080;
    fake_video_config.frame_rate = {
        static_cast<int32_t>(settings.requested_fps), 1};
    fake_video_config.time_base = {
        1, static_cast<int32_t>(settings.requested_fps)};
    fake_video_config.codec_extradata = {0x01};
    if (!ring.SetVideoConfig(fake_video_config)) {
        std::cerr << "FAILED: deterministic fake stream metadata matches its PTS clock"
                  << std::endl;
        std::exit(1);
    }
    fthr::dxgi_replay::Cadence cadence(
        settings.requested_fps, kQpcFrequency);
    HardwarePtsEncoderFake encoder(settings.requested_fps, kQpcFrequency);
    CadenceReplayRun run;
    uint64_t previous_tick = 0;
    uint64_t previous_source_frame = std::numeric_limits<uint64_t>::max();
    uint64_t current_image_id = 0;
    const uint64_t total_polls = static_cast<uint64_t>(settings.total_seconds)
        * settings.poll_fps;
    const uint64_t first_static_second = 5;
    const uint64_t interruption_start_second = 10;
    const uint64_t interruption_end_second = 12;

    for (uint64_t poll = 0; poll < total_polls; ++poll) {
        const int64_t poll_jitter = settings.jitter_poll_phase
            ? static_cast<int64_t>((poll % 4) * 250)
            : 0;
        const int64_t now_qpc = kQpcEpoch + settings.poll_phase_qpc
            + static_cast<int64_t>(
                poll * static_cast<uint64_t>(kQpcFrequency) / settings.poll_fps)
            + poll_jitter;
        const auto schedule = cadence.Schedule(now_qpc);
        if (!schedule.due) continue;
        ++run.acquire_calls;
        ++run.completed_ticks;
        if (schedule.tick != previous_tick) ++run.distinct_ticks;
        previous_tick = schedule.tick;

        const uint64_t elapsed_qpc = static_cast<uint64_t>(
            now_qpc - kQpcEpoch);
        const uint64_t elapsed_seconds = elapsed_qpc
            / static_cast<uint64_t>(kQpcFrequency);
        const uint64_t source_frame = settings.source_fps == 0
            ? 0
            : elapsed_qpc * settings.source_fps
                / static_cast<uint64_t>(kQpcFrequency);
        const int64_t source_frame_qpc = settings.source_fps == 0
            ? kQpcEpoch
            : kQpcEpoch + static_cast<int64_t>(
                source_frame * static_cast<uint64_t>(kQpcFrequency)
                    / settings.source_fps);

        bool image_phase = false;
        bool static_phase = false;
        bool interrupted_phase = false;
        switch (settings.scenario) {
        case CadenceReplayScenario::StaticAfterFiveSeconds:
            image_phase = elapsed_qpc
                < first_static_second * static_cast<uint64_t>(kQpcFrequency);
            static_phase = !image_phase;
            break;
        case CadenceReplayScenario::ContinuouslyDynamic:
            image_phase = true;
            break;
        case CadenceReplayScenario::InterruptedThenResume:
            image_phase = elapsed_seconds < first_static_second
                || elapsed_seconds >= interruption_end_second;
            static_phase = elapsed_seconds >= first_static_second
                && elapsed_seconds < interruption_start_second;
            interrupted_phase = elapsed_seconds >= interruption_start_second
                && elapsed_seconds < interruption_end_second;
            break;
        case CadenceReplayScenario::RepeatedQuietMotionEpisodes:
            static_phase = (elapsed_seconds >= 2 && elapsed_seconds < 5)
                || (elapsed_seconds >= 7 && elapsed_seconds < 10);
            image_phase = !static_phase;
            break;
        case CadenceReplayScenario::SparseDynamic:
            image_phase = true;
            break;
        case CadenceReplayScenario::InitialStatic:
            image_phase = run.source_image_updates == 0;
            static_phase = !image_phase;
            break;
        }

        fthr::dxgi_replay::AcquireResult acquire;
        acquire.generation = kGeneration;
        acquire.call_start_qpc = now_qpc;
        acquire.call_end_qpc = now_qpc + 1;
        if (interrupted_phase) {
            acquire.observation = fthr::dxgi_replay::Observation::Failure;
            ++run.failure_observations;
        } else if (image_phase
            && source_frame != previous_source_frame) {
            acquire.observation = fthr::dxgi_replay::Observation::Image;
            acquire.release_succeeded = true;
            acquire.cache_copy_succeeded = true;
            acquire.source_qpc = source_frame_qpc;
            previous_source_frame = source_frame;
            current_image_id = source_frame;
            ++run.source_image_updates;
            if (run.first_source_qpc == 0) {
                run.first_source_qpc = acquire.source_qpc;
            }
            run.last_source_qpc = acquire.source_qpc;
        } else if (static_phase && (elapsed_qpc
                % static_cast<uint64_t>(kQpcFrequency))
                < static_cast<uint64_t>(kQpcFrequency / settings.poll_fps + 1)) {
            acquire.observation = fthr::dxgi_replay::Observation::PointerOnly;
            acquire.release_succeeded = true;
            ++run.pointer_only_completions;
        } else {
            acquire.observation = fthr::dxgi_replay::Observation::WaitTimeout;
            ++run.wait_timeouts;
        }

        const auto presentation = cadence.Complete(schedule.tick, acquire);
        if (interrupted_phase && presentation.present) {
            ++run.interrupted_presentations;
        }
        if (!presentation.present) continue;
        ++run.presentations;
        if (presentation.held) ++run.held_presentations;
        if (presentation.resampled_source_qpc) {
            ++run.resampled_source_qpc;
        }
        if (presentation.qpc <= run.last_presentation_qpc) {
            run.presentations_monotonic = false;
        }
        run.last_presentation_qpc = presentation.qpc;
        if (!settings.encoder_emits_packets) continue;

        if (interrupted_phase) ++run.interrupted_encoder_submissions;
        const auto packet = encoder.Encode(current_image_id, presentation.qpc);
        ++run.encoder_submissions;
        if (ring.Push(packet.bytes.data(),
                static_cast<uint32_t>(packet.bytes.size()), packet.pts,
                packet.pts % settings.requested_fps == 0,
                presentation.qpc)) {
            ++run.ring_packets_published;
            if (!cadence.CommitPresentation(presentation.qpc)) {
                run.presentations_monotonic = false;
            }
        }
    }

    run.pts_clamps = encoder.pts_clamps();
    run.max_pts_clamp_lead = encoder.max_pts_clamp_lead();
    const int64_t target_end_qpc = kQpcEpoch + static_cast<int64_t>(
        static_cast<uint64_t>(settings.total_seconds) * kQpcFrequency);
    run.snapshot = ring.TakeSnapshotByTime(
        settings.requested_duration_seconds, target_end_qpc);
    const auto health_input = fthr::encoded_replay_health::MakeInput(
        run.snapshot, settings.requested_duration_seconds,
        settings.requested_fps, kQpcFrequency, target_end_qpc);
    run.health_expected_fps = health_input.expected_fps;
    run.health_pts_per_second = health_input.pts_per_second;
    run.health = fthr::replay_health::Evaluate(health_input);
    if (health_input.packets.size() > 1
        && health_input.wall_qpc_frequency > 0
        && health_input.pts_per_second > 0.0) {
        const auto& first = health_input.packets.front();
        const auto& last = health_input.packets.back();
        const double wall_coverage = static_cast<double>(
            last.wall_qpc - first.wall_qpc)
            / static_cast<double>(health_input.wall_qpc_frequency);
        const double media_coverage = static_cast<double>(
            last.pts - first.pts) / health_input.pts_per_second;
        run.media_wall_coverage_divergence_s = std::abs(
            media_coverage - wall_coverage);
    }
    return run;
}

void CadenceRejectsUnseededAndInvalidImageCompletions() {
    using fthr::dxgi_replay::Cadence;
    using fthr::dxgi_replay::Observation;
    constexpr int64_t kQpcFrequency = 1'000'000;
    constexpr int64_t kEpoch = 1'000'000;
    constexpr uint64_t kGeneration = 4;
    const int64_t period_qpc = (kQpcFrequency + 59) / 60;

    Cadence unseeded(60, kQpcFrequency);
    const auto timeout = CompleteCadenceStep(
        unseeded, kEpoch, kGeneration, Observation::WaitTimeout);
    const auto pointer = CompleteCadenceStep(
        unseeded, kEpoch + period_qpc, kGeneration,
        Observation::PointerOnly, 0, true);
    CheckPolicy(timeout.schedule.due && !timeout.presentation.present
            && pointer.schedule.due && !pointer.presentation.present
            && !unseeded.HasSeed(),
        "timeout and pointer-only events cannot invent a seed image");

    Cadence failed_release(60, kQpcFrequency);
    const auto release_failure = CompleteCadenceStep(
        failed_release, kEpoch, kGeneration, Observation::Image,
        kEpoch, false, true);
    const auto no_seed_pointer = CompleteCadenceStep(
        failed_release, kEpoch + period_qpc, kGeneration,
        Observation::PointerOnly, 0, true);
    CheckPolicy(!release_failure.presentation.present
            && !failed_release.HasSeed()
            && !no_seed_pointer.presentation.present,
        "failed DXGI frame release cannot seed or later repeat an image");

    Cadence failed_copy(60, kQpcFrequency);
    const auto copy_failure = CompleteCadenceStep(
        failed_copy, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, false);
    CheckPolicy(!copy_failure.presentation.present
            && !failed_copy.HasSeed(),
        "failed owned-texture cache copy cannot create a presentation seed");

    Cadence access_lost(60, kQpcFrequency);
    const auto seed = CompleteCadenceStep(
        access_lost, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, true);
    CheckPolicy(seed.presentation.present
            && access_lost.CommitPresentation(seed.presentation.qpc),
        "successful image creates a baseline before the access-loss case");
    const auto failure = CompleteCadenceStep(
        access_lost, kEpoch + 2 * period_qpc, kGeneration,
        Observation::Failure);
    const auto after_failure = CompleteCadenceStep(
        access_lost, kEpoch + 3 * period_qpc, kGeneration,
        Observation::WaitTimeout);
    CheckPolicy(!failure.presentation.present
            && !access_lost.HasSeed()
            && !after_failure.presentation.present,
        "ACCESS_LOST/failure invalidates held imagery and cannot fill the outage");
}

void CadenceTracksQuietHoldGenerationAndQpcRaces() {
    using fthr::dxgi_replay::Cadence;
    using fthr::dxgi_replay::Observation;
    constexpr int64_t kQpcFrequency = 1'000'000;
    constexpr int64_t kEpoch = 1'000'000;
    constexpr uint64_t kGeneration = 5;
    const int64_t period_qpc = (kQpcFrequency + 59) / 60;

    Cadence cadence(60, kQpcFrequency);
    const auto seed = CompleteCadenceStep(
        cadence, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, true);
    CheckPolicy(seed.presentation.present && !seed.presentation.held
            && !seed.presentation.resampled_source_qpc
            && cadence.CommitPresentation(seed.presentation.qpc),
        "fresh dynamic image is admitted at its source QPC");

    const auto before_hold = CompleteCadenceStep(
        cadence, kEpoch + period_qpc, kGeneration,
        Observation::WaitTimeout);
    const auto at_two_periods = CompleteCadenceStep(
        cadence, kEpoch + 2 * period_qpc, kGeneration,
        Observation::WaitTimeout);
    const auto at_three_periods = CompleteCadenceStep(
        cadence, kEpoch + 3 * period_qpc, kGeneration,
        Observation::WaitTimeout);
    const auto before_quiet_qualification = CompleteCadenceStep(
        cadence, kEpoch + 7 * period_qpc, kGeneration,
        Observation::WaitTimeout);
    const auto at_quiet_qualification = CompleteCadenceStep(
        cadence, kEpoch + 8 * period_qpc, kGeneration,
        Observation::PointerOnly, 0, true);
    CheckPolicy(!before_hold.presentation.present
            && !at_two_periods.presentation.present
            && !at_three_periods.presentation.present
            && !before_quiet_qualification.presentation.present
            && at_quiet_qualification.presentation.present
            && at_quiet_qualification.presentation.held
            && at_quiet_qualification.presentation.qpc
                > seed.acquire.call_end_qpc
            && at_quiet_qualification.presentation.qpc
                - cadence.LastImageCompletionQpc() >= 125'000
            && cadence.LastImageCompletionQpc()
                == seed.acquire.call_end_qpc,
        "held output waits for the 125 ms quiet qualification measured from image completion, not stale source QPC");
    CheckPolicy(cadence.CommitPresentation(
            at_quiet_qualification.presentation.qpc),
        "quiet static presentation advances the presentation clock");

    const int64_t last_held_qpc = at_quiet_qualification.presentation.qpc;
    const int64_t raced_source_qpc = last_held_qpc - 100;
    const auto motion = CompleteCadenceStep(
        cadence, kEpoch + 9 * period_qpc, kGeneration,
        Observation::Image, raced_source_qpc, true, true);
    CheckPolicy(motion.presentation.present
            && motion.presentation.resampled_source_qpc
            && motion.presentation.qpc > last_held_qpc
            && motion.acquire.source_qpc == raced_source_qpc
            && cadence.CommitPresentation(motion.presentation.qpc),
        "a small static-to-motion QPC race preserves raw source time while resampling monotone presentation time");
    const auto stale_after_timeline = CompleteCadenceStep(
        cadence, kEpoch + 10 * period_qpc, kGeneration,
        Observation::Image, seed.acquire.source_qpc, true, true);
    CheckPolicy(!stale_after_timeline.presentation.present
            && !cadence.HasSeed(),
        "an image timestamp older than the bounded race window cannot bridge an established replay gap");

    Cadence old_seed(60, kQpcFrequency);
    const int64_t old_source = kEpoch;
    const int64_t late_acquire = kEpoch + 5 * kQpcFrequency;
    const auto old_image = CompleteCadenceStep(
        old_seed, late_acquire, kGeneration, Observation::Image,
        old_source, true, true);
    CheckPolicy(old_image.presentation.present
            && old_image.presentation.resampled_source_qpc
            && old_image.presentation.qpc == late_acquire + 1
            && old_image.acquire.source_qpc == old_source
            && old_seed.HasSeed(),
        "a valid initially acquired static image remains usable when LastPresentTime is old");

    Cadence generation_change(60, kQpcFrequency);
    const auto old_generation_image = CompleteCadenceStep(
        generation_change, kEpoch, kGeneration,
        Observation::Image, kEpoch, true, true);
    CheckPolicy(old_generation_image.presentation.present
            && generation_change.CommitPresentation(
                old_generation_image.presentation.qpc),
        "generation fixture seeds the prior replay generation");
    const auto changed_generation = CompleteCadenceStep(
        generation_change, kEpoch + period_qpc, kGeneration + 1,
        Observation::Image, kEpoch + period_qpc, true, true);
    const auto unseeded_new_generation = CompleteCadenceStep(
        generation_change, kEpoch + 2 * period_qpc, kGeneration + 1,
        Observation::WaitTimeout);
    const bool old_seed_disarmed = !generation_change.HasSeed();
    const auto new_generation_image = CompleteCadenceStep(
        generation_change, kEpoch + 3 * period_qpc, kGeneration + 1,
        Observation::Image, kEpoch + 3 * period_qpc, true, true);
    CheckPolicy(!changed_generation.presentation.present
            && old_seed_disarmed
            && !unseeded_new_generation.presentation.present
            && new_generation_image.presentation.present,
        "capture generation change disarms old texture until a new-generation image seeds it");
}

void CadenceRejectsStallsAndDoesNotBurstAfterLatePolls() {
    using fthr::dxgi_replay::Cadence;
    using fthr::dxgi_replay::Observation;
    constexpr int64_t kQpcFrequency = 1'000'000;
    constexpr int64_t kEpoch = 1'000'000;
    constexpr uint64_t kGeneration = 6;
    const int64_t period_qpc = (kQpcFrequency + 59) / 60;
    const int64_t maximum_gap_qpc = (kQpcFrequency + 3) / 4;

    Cadence slow_call(60, kQpcFrequency);
    const auto slow_seed = CompleteCadenceStep(
        slow_call, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, true);
    CheckPolicy(slow_seed.presentation.present
            && slow_call.CommitPresentation(slow_seed.presentation.qpc),
        "slow-call fixture seeds valid history");
    const auto slow_completion = CompleteCadenceStep(
        slow_call, kEpoch + period_qpc, kGeneration,
        Observation::WaitTimeout, 0, false, false,
        maximum_gap_qpc + 1);
    CheckPolicy(!slow_completion.presentation.present
            && !slow_call.HasSeed(),
        "an excessive DXGI API call duration invalidates held-frame eligibility");

    Cadence evidence_gap(60, kQpcFrequency);
    const auto evidence_seed = CompleteCadenceStep(
        evidence_gap, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, true);
    CheckPolicy(evidence_seed.presentation.present
            && evidence_gap.CommitPresentation(evidence_seed.presentation.qpc),
        "evidence-gap fixture seeds valid history");
    const int64_t late_start = kEpoch + maximum_gap_qpc + 10;
    const auto gap_observation = CompleteCadenceStep(
        evidence_gap, late_start, kGeneration, Observation::WaitTimeout);
    CheckPolicy(!gap_observation.presentation.present
            && !evidence_gap.HasSeed(),
        "a gap in timely acquire evidence cannot be repaired by later timeout frames");

    Cadence token(60, kQpcFrequency);
    const auto first_tick = token.Schedule(kEpoch);
    const auto duplicate_tick = token.Schedule(kEpoch + 1);
    fthr::dxgi_replay::AcquireResult acquired;
    acquired.generation = kGeneration;
    acquired.call_start_qpc = kEpoch;
    acquired.call_end_qpc = kEpoch + 1;
    acquired.source_qpc = kEpoch;
    acquired.observation = Observation::Image;
    acquired.release_succeeded = true;
    acquired.cache_copy_succeeded = true;
    const auto first_result = first_tick.due
        ? token.Complete(first_tick.tick, acquired)
        : fthr::dxgi_replay::Presentation{};
    CheckPolicy(first_tick.due && !duplicate_tick.due
            && first_tick.tick != 0
            && first_result.present
            && token.CommitPresentation(first_result.qpc),
        "one due schedule tick cannot trigger more than one acquisition or sample");

    Cadence late_poll(60, kQpcFrequency);
    const auto late_seed = CompleteCadenceStep(
        late_poll, kEpoch, kGeneration, Observation::Image,
        kEpoch, true, true);
    CheckPolicy(late_seed.presentation.present
            && late_poll.CommitPresentation(late_seed.presentation.qpc),
        "late-poll fixture seeds a schedule");
    const int64_t delayed_now = kEpoch + 100 * period_qpc;
    const auto delayed = CompleteCadenceStep(
        late_poll, delayed_now, kGeneration, Observation::Failure);
    const auto immediate = late_poll.Schedule(delayed_now + 1);
    CheckPolicy(delayed.schedule.due && !delayed.presentation.present
            && !immediate.due && immediate.wait_qpc > 0,
        "a late poll creates one failed tick and no catch-up burst on the next poll");
}

void CadenceReplayScenariosUseProductionSnapshotHealth() {
    CadenceReplaySettings still;
    still.scenario = CadenceReplayScenario::StaticAfterFiveSeconds;
    const auto static_replay = RunProductionCadenceReplay(still);
    std::cout << "[CadenceReplayProduction] static image_updates="
              << static_replay.source_image_updates
              << " pointer_only=" << static_replay.pointer_only_completions
              << " timeouts=" << static_replay.wait_timeouts
              << " held_presentations=" << static_replay.held_presentations
              << " modeled_packets=" << static_replay.ring_packets_published
              << " snapshot_packets=" << static_replay.snapshot.packets.size()
              << " health="
              << fthr::replay_health::FailureName(static_replay.health.failure)
              << " accepted=" << (static_replay.health.accepted ? "true" : "false")
              << std::endl;
    CheckPolicy(static_replay.source_image_updates == 300
            && static_replay.pointer_only_completions > 0
            && static_replay.wait_timeouts > 0
            && static_replay.held_presentations > 0,
        "production cadence holds the acquired static image without inventing source updates");
    CheckPolicy(static_replay.presentations_monotonic
            && static_replay.acquire_calls == static_replay.completed_ticks
            && static_replay.completed_ticks == static_replay.distinct_ticks
            && static_replay.acquire_calls
                <= static_cast<uint64_t>(still.requested_fps)
                    * still.total_seconds
            && static_replay.ring_packets_published
                == static_replay.encoder_submissions
            && static_replay.health.accepted,
        "a 5s dynamic then 30s static replay passes the unchanged production snapshot-health criteria");
    CheckPolicy(static_replay.health_expected_fps == still.requested_fps
            && static_replay.health_pts_per_second == still.requested_fps,
        "static scenario health uses the configured stream cadence and fake encoder PTS units");
    CheckPolicy(static_replay.last_source_qpc > static_replay.first_source_qpc
            && static_replay.last_presentation_qpc
                - static_replay.last_source_qpc > 29'000'000
            && static_replay.media_wall_coverage_divergence_s
                <= 1.0 / still.requested_fps + 1e-6
            && static_replay.max_pts_clamp_lead <= 1,
        "static replay reuses the unchanged source timestamp and keeps hardware-style PTS drift within one frame");

    CadenceReplaySettings dynamic;
    dynamic.scenario = CadenceReplayScenario::ContinuouslyDynamic;
    const auto dynamic_control = RunProductionCadenceReplay(dynamic);
    CheckPolicy(dynamic_control.source_image_updates == 35 * 60
            && dynamic_control.health.accepted
            && dynamic_control.health_expected_fps == dynamic.requested_fps
            && dynamic_control.health_pts_per_second == dynamic.requested_fps
            && dynamic_control.max_pts_clamp_lead <= 1
            && dynamic_control.media_wall_coverage_divergence_s
                <= 1.0 / dynamic.requested_fps + 1e-6,
        "continuously dynamic 60-fps source remains accepted with hardware-style PTS");

    CadenceReplaySettings jittered_dynamic = dynamic;
    jittered_dynamic.jitter_poll_phase = true;
    jittered_dynamic.poll_phase_qpc = 137;
    const auto jittered_control = RunProductionCadenceReplay(jittered_dynamic);
    CheckPolicy(jittered_control.health.accepted
            && jittered_control.acquire_calls
                == jittered_control.completed_ticks
            && jittered_control.completed_ticks
                == jittered_control.distinct_ticks
            && jittered_control.max_pts_clamp_lead <= 1
            && jittered_control.media_wall_coverage_divergence_s
                <= 1.0 / jittered_dynamic.requested_fps + 1e-6,
        "irregular QPC polling phases preserve one-tick admission and bounded hardware-style PTS drift");

    for (const uint32_t source_fps : {90u, 120u, 144u, 240u, 500u}) {
        CadenceReplaySettings faster_source = dynamic;
        faster_source.source_fps = source_fps;
        faster_source.jitter_poll_phase = true;
        faster_source.poll_phase_qpc = source_fps * 37;
        const auto faster = RunProductionCadenceReplay(faster_source);
        std::cout << "[CadenceReplaySourceRate] source_fps=" << source_fps
                  << " source_updates=" << faster.source_image_updates
                  << " pts_clamps=" << faster.pts_clamps
                  << " max_pts_clamp_lead=" << faster.max_pts_clamp_lead
                  << " media_wall_divergence_s="
                  << faster.media_wall_coverage_divergence_s
                  << " accepted=" << (faster.health.accepted ? "true" : "false")
                  << std::endl;
        CheckPolicy(faster.health.accepted
                && faster.health_expected_fps == faster_source.requested_fps
                && faster.health_pts_per_second == faster_source.requested_fps
                && faster.acquire_calls == faster.completed_ticks
                && faster.completed_ticks == faster.distinct_ticks
                && faster.max_pts_clamp_lead <= 1
                && faster.media_wall_coverage_divergence_s
                    <= 1.0 / faster_source.requested_fps + 1e-6,
            "dynamic 90/120/144/240/500-fps source updates keep paced replay PTS drift within one frame");
    }

    CadenceReplaySettings repeated_quiet_motion = dynamic;
    repeated_quiet_motion.scenario =
        CadenceReplayScenario::RepeatedQuietMotionEpisodes;
    repeated_quiet_motion.jitter_poll_phase = true;
    repeated_quiet_motion.poll_phase_qpc = 421;
    const auto repeated_episodes = RunProductionCadenceReplay(
        repeated_quiet_motion);
    CheckPolicy(repeated_episodes.held_presentations > 0,
        "quiet-to-motion transitions resume held output after static source periods");
    CheckPolicy(repeated_episodes.presentations_monotonic
            && repeated_episodes.health.accepted,
        "repeated quiet episodes preserve monotonic presentations and accepted replay coverage");
    CheckPolicy(repeated_episodes.max_pts_clamp_lead <= 1
            && repeated_episodes.media_wall_coverage_divergence_s
                <= 1.0 / repeated_quiet_motion.requested_fps + 1e-6,
        "several quiet-to-motion transitions prevent cumulative hardware-style PTS lead");

    CadenceReplaySettings interruption;
    interruption.scenario = CadenceReplayScenario::InterruptedThenResume;
    const auto interrupted = RunProductionCadenceReplay(interruption);
    std::cout << "[CadenceReplayProduction] interruption image_updates="
              << interrupted.source_image_updates
              << " failed_acquires=" << interrupted.failure_observations
              << " outage_presentations=" << interrupted.interrupted_presentations
              << " outage_encoder_submissions="
              << interrupted.interrupted_encoder_submissions
              << " modeled_packets=" << interrupted.ring_packets_published
              << " snapshot_packets=" << interrupted.snapshot.packets.size()
              << " largest_gap_s=" << interrupted.health.largest_gap_s
              << " health="
              << fthr::replay_health::FailureName(interrupted.health.failure)
              << " accepted=" << (interrupted.health.accepted ? "true" : "false")
              << std::endl;
    CheckPolicy(interrupted.source_image_updates > 300
            && interrupted.failure_observations > 0
            && interrupted.interrupted_presentations == 0
            && interrupted.interrupted_encoder_submissions == 0
            && interrupted.health.failure
                == fthr::replay_health::Failure::LargeTimelineGap
            && interrupted.health.largest_gap_s > 1.5,
        "resume after a real capture interruption preserves and rejects the earlier replay gap");

    CadenceReplaySettings dynamic_60_at_120;
    dynamic_60_at_120.scenario = CadenceReplayScenario::ContinuouslyDynamic;
    dynamic_60_at_120.requested_fps = 120;
    dynamic_60_at_120.source_fps = 60;
    const auto low_rate = RunProductionCadenceReplay(dynamic_60_at_120);
    CheckPolicy(low_rate.source_image_updates == 35 * 60
            && !low_rate.health.accepted
            && low_rate.health_expected_fps == dynamic_60_at_120.requested_fps
            && low_rate.health_pts_per_second == dynamic_60_at_120.requested_fps
            && low_rate.health.failure
                == fthr::replay_health::Failure::LowEffectiveRate
            && low_rate.health.effective_packet_rate < 120.0 * 0.8,
        "a genuinely moving 60-fps source cannot be repeated into a false 120-fps replay");

    CadenceReplaySettings sparse;
    sparse.scenario = CadenceReplayScenario::SparseDynamic;
    sparse.source_fps = 10;
    const auto sparse_dynamic = RunProductionCadenceReplay(sparse);
    CheckPolicy(sparse_dynamic.source_image_updates == 35 * 10
            && !sparse_dynamic.health.accepted
            && sparse_dynamic.health.failure
                == fthr::replay_health::Failure::LowEffectiveRate,
        "sparse moving 10-fps source is not mistaken for a static desktop");

    bool high_output_sparse_sources_rejected = true;
    for (const uint32_t output_fps : {120u, 144u}) {
        for (const uint32_t source_fps : {10u, 30u, 40u, 60u}) {
            CadenceReplaySettings high_output_sparse = sparse;
            high_output_sparse.requested_fps = output_fps;
            high_output_sparse.source_fps = source_fps;
            const auto measured = RunProductionCadenceReplay(
                high_output_sparse);
            const bool correct_source_count = measured.source_image_updates
                == static_cast<uint64_t>(high_output_sparse.total_seconds)
                    * source_fps;
            std::cout << "[CadenceReplaySparseRate] source_fps=" << source_fps
                      << " output_fps=" << output_fps
                      << " source_updates=" << measured.source_image_updates
                      << " expected_source_updates="
                      << (static_cast<uint64_t>(high_output_sparse.total_seconds)
                          * source_fps)
                      << " modeled_packets=" << measured.ring_packets_published
                      << " effective_rate="
                      << measured.health.effective_packet_rate
                      << " health="
                      << fthr::replay_health::FailureName(
                          measured.health.failure)
                      << " accepted="
                      << (measured.health.accepted ? "true" : "false")
                      << std::endl;
            high_output_sparse_sources_rejected =
                high_output_sparse_sources_rejected
                && correct_source_count
                && !measured.health.accepted
                && measured.health.failure
                    == fthr::replay_health::Failure::LowEffectiveRate;
        }
    }
    CheckPolicy(high_output_sparse_sources_rejected,
        "moving 10/30/40/60-fps sources cannot be cadence-held into false 120/144-fps replay rates");

    for (const uint32_t fps : {120u, 144u}) {
        CadenceReplaySettings high_fps_static;
        high_fps_static.scenario = CadenceReplayScenario::InitialStatic;
        high_fps_static.requested_fps = fps;
        high_fps_static.source_fps = 60;
        high_fps_static.total_seconds = 7;
        high_fps_static.requested_duration_seconds = 5;
        const auto high_rate_still = RunProductionCadenceReplay(
            high_fps_static);
        CheckPolicy(high_rate_still.source_image_updates == 1
                && high_rate_still.held_presentations > 0
                && high_rate_still.health.accepted
                && high_rate_still.health_expected_fps == fps
                && high_rate_still.health_pts_per_second == fps
                && high_rate_still.last_source_qpc
                    == high_rate_still.first_source_qpc
                && high_rate_still.max_pts_clamp_lead <= 1
                && high_rate_still.media_wall_coverage_divergence_s
                    <= 1.0 / fps + 1e-6,
            "a quiet seeded static image fills a valid high-rate 120/144-fps replay after settling");
    }

    CadenceReplaySettings no_encoder_output;
    no_encoder_output.scenario = CadenceReplayScenario::InitialStatic;
    no_encoder_output.total_seconds = 7;
    no_encoder_output.requested_duration_seconds = 5;
    no_encoder_output.encoder_emits_packets = false;
    const auto empty_ring = RunProductionCadenceReplay(no_encoder_output);
    CheckPolicy(empty_ring.presentations > 0
            && empty_ring.encoder_submissions == 0
            && empty_ring.ring_packets_published == 0
            && empty_ring.snapshot.packets.empty()
            && empty_ring.health.failure
                == fthr::replay_health::Failure::Empty,
        "a Cadence presentation alone is never reported as replay-ring success without encoder packets");
}

void ActiveTruthAndGenerationAreScoped() {
    const auto selected = fthr::SelectWindowsReplayPolicy(
        EncoderVendor::Amd, VideoCodec::AV1);
    const auto active = fthr::EvaluateActiveReplayCapability(
        selected,
        {EncoderVendor::Amd, ReplayEncoderBackend::FfmpegAmf,
         VideoCodec::AV1, true, "av1_amf"},
        true,
        7);
    CheckPolicy(active.initialized && active.requested_codec == VideoCodec::AV1
            && active.active_codec == VideoCodec::AV1,
        "active codec truth comes from initialized backend");
    CheckPolicy(active.active_backend == ReplayEncoderBackend::FfmpegAmf
            && active.hardware && active.same_adapter,
        "active backend and adapter truth remain explicit");
    CheckPolicy(active.generation == 7,
        "capability truth is scoped to one capture generation");

    const auto stale = fthr::EvaluateActiveReplayCapability(
        selected,
        {EncoderVendor::Amd, ReplayEncoderBackend::FfmpegAmf,
         VideoCodec::H264, true, "h264_amf"},
        true,
        8);
    CheckPolicy(!stale.initialized
            && stale.error == ReplayStartupError::RequestedCodecUnsupported,
        "a backend reporting the wrong codec is rejected");
    CheckPolicy(active.generation != stale.generation,
        "restart/topology generation invalidates old capability truth");
}

void ProductionThreeByThreeMatrixAndRegressionsHold() {
    for (const auto vendor : {
             EncoderVendor::Nvidia, EncoderVendor::Amd, EncoderVendor::Intel}) {
        for (const auto codec : {
                 VideoCodec::H264, VideoCodec::HEVC, VideoCodec::AV1}) {
            const auto selection = fthr::SelectWindowsReplayPolicy(vendor, codec);
            CheckPolicy(selection.allowed && selection.same_adapter,
                "3x3 source matrix selects a same-adapter hardware candidate");
        }
    }
    CheckPolicy(fthr::SelectWindowsReplayPolicy(
                    EncoderVendor::Nvidia, VideoCodec::AV1).backend
                == ReplayEncoderBackend::NativeNvenc,
        "NVIDIA policy regression remains protected");
    CheckPolicy(fthr::SelectWindowsReplayPolicy(
                    EncoderVendor::Amd, VideoCodec::AV1).backend
                == ReplayEncoderBackend::FfmpegAmf,
        "AMD policy regression remains protected");
    CheckPolicy(fthr::SelectWindowsReplayPolicy(
                    EncoderVendor::Intel, VideoCodec::AV1).backend
                == ReplayEncoderBackend::FfmpegQsv,
        "Intel policy regression remains protected");
}

fthr::SaveClipTask QueueTask(uint32_t id) {
    fthr::SaveClipTask task;
    task.task_id = id;
    task.output_path = L"queue-test.mp4";
    return task;
}

void SaveQueueIsBoundedAndRejectsWithoutBlocking() {
    fthr::SaveClipQueue queue;
    for (uint32_t id = 1; id <= queue.GetCapacity(); ++id) {
        CheckPolicy(queue.Push(QueueTask(id)),
            "save queue accepts work up to its documented bound");
    }
    CheckPolicy(queue.GetQueueDepth() == queue.GetCapacity(),
        "save queue exposes its full pending depth");
    CheckPolicy(!queue.Push(QueueTask(99)),
        "save queue rejects overload instead of growing without bound");

    fthr::SaveClipTask popped;
    CheckPolicy(queue.Pop(popped) && popped.task_id == 1,
        "bounded save queue preserves FIFO ownership");
    CheckPolicy(queue.Push(QueueTask(5)),
        "save queue accepts new work after one pending slot is released");
    queue.Shutdown();
    size_t drained = 0;
    while (queue.Pop(popped)) ++drained;
    CheckPolicy(drained == queue.GetCapacity(),
        "shutdown drains every accepted save task");
    CheckPolicy(!queue.Push(QueueTask(100)),
        "shutdown visibly rejects new save work");
}

void SlowSaveConsumerDoesNotStopIndependentProgress() {
    fthr::SaveClipQueue queue;
    std::atomic<bool> consumer_started{false};
    std::atomic<bool> release_consumer{false};
    std::atomic<uint64_t> independent_progress{0};
    CheckPolicy(queue.Push(QueueTask(1)),
        "slow-consumer fixture queues its first save");

    std::thread consumer([&] {
        fthr::SaveClipTask task;
        if (!queue.Pop(task)) return;
        consumer_started.store(true, std::memory_order_release);
        while (!release_consumer.load(std::memory_order_acquire)) {
            std::this_thread::yield();
        }
    });
    while (!consumer_started.load(std::memory_order_acquire)) {
        std::this_thread::yield();
    }
    std::thread progress([&] {
        while (!release_consumer.load(std::memory_order_acquire)) {
            independent_progress.fetch_add(1, std::memory_order_relaxed);
            std::this_thread::yield();
        }
    });

    CheckPolicy(queue.Push(QueueTask(2)),
        "producer remains non-blocking while the save consumer is stalled");
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    CheckPolicy(independent_progress.load(std::memory_order_relaxed) > 0,
        "independent capture-like progress continues during a slow save");
    release_consumer.store(true, std::memory_order_release);
    progress.join();
    consumer.join();
    queue.Shutdown();
    fthr::SaveClipTask pending;
    CheckPolicy(queue.Pop(pending) && pending.task_id == 2,
        "queued save remains intact after the slow consumer resumes");
    CheckPolicy(!queue.Pop(pending),
        "drained slow-consumer queue exits cleanly at shutdown");
}

void FailedSaveLeavesQueuedSuccessorAvailable() {
    fthr::SaveClipQueue queue;
    CheckPolicy(queue.Push(QueueTask(1)),
        "failure-followup fixture queues the first save");
    CheckPolicy(queue.Push(QueueTask(2)),
        "failure-followup fixture queues the successor save");

    fthr::SaveClipTask failed;
    CheckPolicy(queue.Pop(failed) && failed.task_id == 1,
        "save consumer owns the failed task independently");
    // The writer failure is simulated at the consumer boundary. Queue
    // ownership of a later request must not be lost with the failed task.
    queue.Shutdown();
    fthr::SaveClipTask successor;
    CheckPolicy(queue.Pop(successor) && successor.task_id == 2,
        "a queued save remains available after a prior save failure");
    CheckPolicy(!queue.Pop(successor),
        "failed-save followup queue drains exactly once");
}

void ShutdownDrainsNonEmptyQueueWithActiveConsumer() {
    fthr::SaveClipQueue queue;
    CheckPolicy(queue.Push(QueueTask(1)),
        "active-shutdown fixture queues its active task");
    CheckPolicy(queue.Push(QueueTask(2)),
        "active-shutdown fixture retains a pending task");
    std::atomic<bool> consumer_started{false};
    std::atomic<bool> release_consumer{false};

    std::thread consumer([&] {
        fthr::SaveClipTask task;
        if (!queue.Pop(task)) return;
        consumer_started.store(true, std::memory_order_release);
        while (!release_consumer.load(std::memory_order_acquire)) {
            std::this_thread::yield();
        }
    });
    while (!consumer_started.load(std::memory_order_acquire)) {
        std::this_thread::yield();
    }

    queue.Shutdown();
    CheckPolicy(queue.IsShutdown(),
        "shutdown is observable while a save consumer is active");
    CheckPolicy(!queue.Push(QueueTask(3)),
        "shutdown rejects a rapid save request after the active task");
    release_consumer.store(true, std::memory_order_release);
    consumer.join();

    fthr::SaveClipTask pending;
    CheckPolicy(queue.Pop(pending) && pending.task_id == 2,
        "shutdown preserves the non-empty queue for draining");
    CheckPolicy(!queue.Pop(pending),
        "shutdown reports empty only after active and pending work drain");
    queue.Shutdown();
    CheckPolicy(queue.IsShutdown(),
        "repeated shutdown remains idempotent");
}

void RapidSaveRequestsPreserveAcceptedOrder() {
    fthr::SaveClipQueue queue;
    uint32_t next_id = 1;
    for (int round = 0; round < 32; ++round) {
        for (size_t slot = 0; slot < queue.GetCapacity(); ++slot) {
            CheckPolicy(queue.Push(QueueTask(next_id)),
                "rapid save request is accepted within the queue bound");
            ++next_id;
        }
        for (uint32_t expected = next_id - 4; expected < next_id; ++expected) {
            fthr::SaveClipTask task;
            CheckPolicy(queue.Pop(task) && task.task_id == expected,
                "rapid save requests preserve FIFO order");
        }
    }
    queue.Shutdown();
    fthr::SaveClipTask task;
    CheckPolicy(!queue.Pop(task),
        "rapid request fixture shuts down with no leaked tasks");
}

} // namespace

int RunWindowsReplayPolicyTests() {
    WgcWaitPreservesDeadlineAndInterruption();
    SameAdapterVendorMatrixIsAuthoritative();
    MissingRuntimeAndUnsupportedCodecStayFailed();
    CaptureAdapterCannotBeStolenByAnotherVendor();
    ExplicitNvidiaSelectionEnablesQualifiedHybridPath();
    RawCapacityNeverOverclaimsConfiguredHistory();
    CaptureBitrateBoundaryMatchesStartupContract();
    CaptureUnsignedArgumentParserRejectsMalformedValues();
    DeadlineSchedulerPreservesCadenceAcrossRefreshRates();
    EncodedReplayCapacityHasBoundedHeadroom();
    ReplayHealthRejectsSparseNativeTimeline();
    ReplayHealthAllowsHealthyAndPartialHistory();
    ReplayHealthRejectsGenerationChange();
    ReplayHealthRejectsStaleSaveBoundary();
    ReplayHealthReportsF7746A8BStaleSaveEvidence();
    ReplayHealthLeavesRateUnmeasuredForEmptyOrInvalidTimelines();
    ReplayHealthReportsExactSavePredicates();
    ReplaySaveClassificationCoversEvidenceBoundaries();
    CapturePipelineHealthClassifiesBoundaries();
    EncodedRingGenerationIsolatedAcrossClear();
    ActiveTruthAndGenerationAreScoped();
    ProductionThreeByThreeMatrixAndRegressionsHold();
    SaveQueueIsBoundedAndRejectsWithoutBlocking();
    SlowSaveConsumerDoesNotStopIndependentProgress();
    FailedSaveLeavesQueuedSuccessorAvailable();
    ShutdownDrainsNonEmptyQueueWithActiveConsumer();
    RapidSaveRequestsPreserveAcceptedOrder();
    StaticDesktopAndCaptureInterruptionReplayBaseline();
    CadenceRejectsUnseededAndInvalidImageCompletions();
    CadenceTracksQuietHoldGenerationAndQpcRaces();
    CadenceRejectsStallsAndDoesNotBurstAfterLatePolls();
    CadenceReplayScenariosUseProductionSnapshotHealth();
    std::cout << "Windows replay policy tests: " << policy_checks
              << " checks passed" << std::endl;
    return policy_checks;
}
