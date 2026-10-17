// Bounded, same-device Desktop Duplication recovery primitives.

#pragma once
#ifndef FTHR_WINDOWS_DXGI_RECOVERY_H
#define FTHR_WINDOWS_DXGI_RECOVERY_H

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <dxgi.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <vector>
#include <string>
#include <utility>

#include "windows_monitor_resolver.h"

namespace fthr::dxgi {

// Desktop Duplication may be recreated in-place only while the capture
// contract used by the live encoder remains unchanged. A monitor moving to a
// different adapter or changing dimensions requires a fresh engine/encoder
// generation; silently reusing the old D3D11 resources would be invalid.
struct RecoveryIdentity {
    std::wstring monitor_device_path;
    monitor::AdapterLuid adapter_luid;
    uint32_t width = 0;
    uint32_t height = 0;
};

struct RecoveryObservation {
    bool running = false;
    bool monitor_resolved = false;
    RecoveryIdentity current;
    HRESULT device_removed_reason = S_OK;
};

enum class RecoveryDecision {
    RecreateDuplication,
    StopRequested,
    MonitorUnavailable,
    MonitorIdentityChanged,
    AdapterChanged,
    DimensionsChanged,
    DeviceRemoved,
};

inline const char* RecoveryDecisionName(RecoveryDecision decision) noexcept {
    switch (decision) {
    case RecoveryDecision::RecreateDuplication:
        return "RECREATE_DUPLICATION";
    case RecoveryDecision::StopRequested:
        return "STOP_REQUESTED";
    case RecoveryDecision::MonitorUnavailable:
        return "MONITOR_UNAVAILABLE";
    case RecoveryDecision::MonitorIdentityChanged:
        return "MONITOR_IDENTITY_CHANGED";
    case RecoveryDecision::AdapterChanged:
        return "ADAPTER_CHANGED";
    case RecoveryDecision::DimensionsChanged:
        return "DIMENSIONS_CHANGED";
    case RecoveryDecision::DeviceRemoved:
        return "DEVICE_REMOVED";
    }
    return "UNKNOWN";
}

inline RecoveryDecision EvaluateRecovery(
    const RecoveryIdentity& active,
    const RecoveryObservation& observation) {
    if (!observation.running) return RecoveryDecision::StopRequested;
    if (!observation.monitor_resolved) {
        return RecoveryDecision::MonitorUnavailable;
    }
    if (monitor::NormalizeMonitorDevicePath(active.monitor_device_path)
        != monitor::NormalizeMonitorDevicePath(
            observation.current.monitor_device_path)) {
        return RecoveryDecision::MonitorIdentityChanged;
    }
    if (active.adapter_luid != observation.current.adapter_luid) {
        return RecoveryDecision::AdapterChanged;
    }
    // A device-loss notification is never repaired by reopening only the
    // duplication object. Check it before dimensions so a coincident resize
    // cannot downgrade a device-removal boundary to a retry.
    if (observation.device_removed_reason != S_OK) {
        return RecoveryDecision::DeviceRemoved;
    }
    if (active.width != observation.current.width
        || active.height != observation.current.height) {
        return RecoveryDecision::DimensionsChanged;
    }
    return RecoveryDecision::RecreateDuplication;
}

enum class RecoveryAttemptResult {
    Recovered,
    RetryableFailure,
    FatalFailure,
    StopRequested,
};

// A resize observed while handling ACCESS_LOST is retryable: Windows may
// briefly report the new desktop bounds while the selected output is still
// being reattached. Identity, adapter, and device changes remain fatal and
// therefore require a fresh capture generation.
inline RecoveryAttemptResult RecoveryAttemptForDecision(
    RecoveryDecision decision) noexcept {
    switch (decision) {
    case RecoveryDecision::RecreateDuplication:
        return RecoveryAttemptResult::Recovered;
    case RecoveryDecision::StopRequested:
        return RecoveryAttemptResult::StopRequested;
    case RecoveryDecision::MonitorUnavailable:
    case RecoveryDecision::DimensionsChanged:
        return RecoveryAttemptResult::RetryableFailure;
    case RecoveryDecision::MonitorIdentityChanged:
    case RecoveryDecision::AdapterChanged:
    case RecoveryDecision::DeviceRemoved:
        return RecoveryAttemptResult::FatalFailure;
    }
    return RecoveryAttemptResult::FatalFailure;
}

enum class RecoveryOutcome {
    Recovered,
    AttemptsExhausted,
    FatalFailure,
    StopRequested,
};

inline const char* RecoveryOutcomeName(RecoveryOutcome outcome) noexcept {
    switch (outcome) {
    case RecoveryOutcome::Recovered: return "RECOVERED";
    case RecoveryOutcome::AttemptsExhausted: return "ATTEMPTS_EXHAUSTED";
    case RecoveryOutcome::FatalFailure: return "FATAL_FAILURE";
    case RecoveryOutcome::StopRequested: return "STOP_REQUESTED";
    }
    return "UNKNOWN";
}

struct RecoveryRunResult {
    RecoveryOutcome outcome = RecoveryOutcome::AttemptsExhausted;
    uint32_t attempts = 0;
};

struct TextureContract {
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t format = 0;
    uint32_t sample_count = 0;
};

inline bool IsCompatibleTexture(
    const TextureContract& expected,
    const TextureContract& actual) noexcept {
    return expected.width > 0
        && expected.height > 0
        && actual.width == expected.width
        && actual.height == expected.height
        && actual.format == expected.format
        && actual.sample_count == expected.sample_count;
}

inline bool IsValidBgraRowPitch(
    uint32_t width, uint32_t row_pitch) noexcept {
    return width > 0
        && static_cast<uint64_t>(row_pitch)
            >= static_cast<uint64_t>(width) * 4u;
}

// Pointer-only desktop updates can return the current surface without a new
// desktop presentation. Release them, but never encode them as fresh frames.
inline bool HasNewDesktopImage(int64_t last_present_qpc) noexcept {
    return last_present_qpc > 0;
}

enum class PipelineStallBoundary {
    CaptureNoFrames,
    CaptureFrameStalled,
    EncoderSubmitFailed,
    EncoderOutputStalled,
};

inline PipelineStallBoundary ClassifyPipelineStall(
    bool capture_stalled,
    bool any_frame_acquired,
    bool encoder_received_recent_submission,
    bool encoder_has_pending_work = false) noexcept {
    // A pending NVENC submission proves capture handed work to the encoder.
    // Prefer that evidence over the capture timer so driver/API stalls (such
    // as pending 8039/8040 operations) are not mislabeled as capture stalls.
    if (encoder_has_pending_work || encoder_received_recent_submission) {
        return PipelineStallBoundary::EncoderOutputStalled;
    }
    if (capture_stalled) {
        return any_frame_acquired
            ? PipelineStallBoundary::CaptureFrameStalled
            : PipelineStallBoundary::CaptureNoFrames;
    }
    return PipelineStallBoundary::EncoderSubmitFailed;
}

inline const char* PipelineStallCode(
    PipelineStallBoundary boundary) noexcept {
    switch (boundary) {
    case PipelineStallBoundary::CaptureNoFrames:
        return "CAPTURE_NO_FRAMES";
    case PipelineStallBoundary::CaptureFrameStalled:
        return "CAPTURE_FRAME_STALLED";
    case PipelineStallBoundary::EncoderSubmitFailed:
        return "ENCODER_SUBMIT_FAILED";
    case PipelineStallBoundary::EncoderOutputStalled:
        return "ENCODER_OUTPUT_STALLED";
    }
    return "CAPTURE_FRAME_STALLED";
}

// A live DXGI duplication can legitimately return S_OK pointer-only updates
// or WAIT_TIMEOUT while the desktop is static. This is a diagnostic-only
// watchdog exemption: it requires recent acquire-loop progress, at least one
// packet, no pending encoder work, and direct evidence of either condition.
// Persistent encoder resource mappings are not pending work and are excluded
// by the caller before this helper is evaluated.
inline bool IsDxgiAliveButDesktopStatic(
    bool has_packet,
    bool acquire_loop_recent,
    bool encoder_idle,
    bool wait_timeout_current,
    bool pointer_only_recent) noexcept {
    return has_packet
        && acquire_loop_recent
        && encoder_idle
        && (wait_timeout_current || pointer_only_recent);
}

// Bounded DXGI event history written by CaptureThread and read at recovery.
// The mutex protects event copies: a seqlock check after a non-atomic copy
// would still permit a C++ data race.
struct RecentDxgiEvent {
    int64_t acquire_start_qpc = 0;
    int64_t acquire_end_qpc = 0;
    int64_t last_present_qpc = 0;
    int64_t presentation_gap_qpc = 0;
    uint32_t accumulated_frames = 0;
    int32_t acquire_hresult = 0;
    int32_t release_hresult = 0;
    // Opaque per-capture-thread token. Never expose a raw COM pointer in a
    // diagnostic bundle because pointer values disclose process addresses.
    uint64_t resource_token = 0;
    uint32_t generation = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t format = 0;
    uint32_t output_index = 0xffffffffu;
    bool acquired = false;
    bool timed_out = false;
    bool pointer_only = false;
    bool pacing_accepted = false;
    bool pacing_rejected = false;
    bool release_attempted = false;
};

template <size_t Capacity = 32>
class RecentDxgiEvidence final {
    static_assert(Capacity > 0, "DXGI evidence capacity must be non-zero");

public:
    void Record(const RecentDxgiEvent& event) noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        slots_[next_ % Capacity] = event;
        ++next_;
    }

    std::vector<RecentDxgiEvent> Snapshot() const {
        std::lock_guard<std::mutex> lock(mutex_);
        std::vector<RecentDxgiEvent> result;
        result.reserve(Capacity);
        const uint64_t newest = next_;
        const uint64_t first = newest > Capacity ? newest - Capacity : 0;
        for (uint64_t sequence = first; sequence < newest; ++sequence) {
            result.push_back(slots_[sequence % Capacity]);
        }
        return result;
    }

private:
    std::array<RecentDxgiEvent, Capacity> slots_{};
    uint64_t next_ = 0;
    mutable std::mutex mutex_;
};

inline bool IsFatalDuplicationRecreateFailure(HRESULT result) noexcept {
    // ACCESS_DENIED is expected while Windows owns a secure desktop and can
    // clear without a device or monitor change. Device loss or an invalid
    // recreation contract cannot be repaired by retrying this same device.
    return result == E_INVALIDARG
        || result == DXGI_ERROR_INVALID_CALL
        || result == DXGI_ERROR_DEVICE_REMOVED
        || result == DXGI_ERROR_DEVICE_RESET;
}

inline constexpr std::array<uint32_t, 6> kRecoveryBackoffMs{
    0, 100, 250, 500, 1000, 2000};
inline constexpr uint32_t kMaxRecoveryAttempts =
    static_cast<uint32_t>(kRecoveryBackoffMs.size());

// Run the same bounded controller with real DXGI callbacks in production and
// injected callbacks in native tests. Waiting is supplied by the owner so a
// shutdown signal can interrupt backoff without a second worker thread.
template <typename RunningFn, typename WaitFn, typename AttemptFn>
RecoveryRunResult RunBoundedRecovery(
    RunningFn&& is_running,
    WaitFn&& wait,
    AttemptFn&& attempt) {
    RecoveryRunResult result;
    for (uint32_t index = 0; index < kMaxRecoveryAttempts; ++index) {
        if (!is_running()) {
            result.outcome = RecoveryOutcome::StopRequested;
            return result;
        }
        if (!wait(kRecoveryBackoffMs[index]) || !is_running()) {
            result.outcome = RecoveryOutcome::StopRequested;
            return result;
        }

        ++result.attempts;
        switch (attempt(result.attempts)) {
        case RecoveryAttemptResult::Recovered:
            result.outcome = RecoveryOutcome::Recovered;
            return result;
        case RecoveryAttemptResult::FatalFailure:
            result.outcome = RecoveryOutcome::FatalFailure;
            return result;
        case RecoveryAttemptResult::StopRequested:
            result.outcome = RecoveryOutcome::StopRequested;
            return result;
        case RecoveryAttemptResult::RetryableFailure:
            break;
        }
    }
    result.outcome = RecoveryOutcome::AttemptsExhausted;
    return result;
}

// One successful AcquireNextFrame owns both the returned resource reference
// and one duplication frame. This guard makes every early-continue/error path
// release each ownership exactly once without heap allocation in the hot path.
template <typename Resource, typename Duplication>
class FrameLease {
public:
    FrameLease(Resource* resource, Duplication* duplication) noexcept
        : resource_(resource), duplication_(duplication), owns_frame_(true) {}

    ~FrameLease() noexcept { Reset(); }

    FrameLease(const FrameLease&) = delete;
    FrameLease& operator=(const FrameLease&) = delete;

    FrameLease(FrameLease&& other) noexcept
        : resource_(std::exchange(other.resource_, nullptr)),
          duplication_(std::exchange(other.duplication_, nullptr)),
          owns_frame_(std::exchange(other.owns_frame_, false)) {}

    FrameLease& operator=(FrameLease&& other) noexcept {
        if (this != &other) {
            Reset();
            resource_ = std::exchange(other.resource_, nullptr);
            duplication_ = std::exchange(other.duplication_, nullptr);
            owns_frame_ = std::exchange(other.owns_frame_, false);
        }
        return *this;
    }

    Resource* resource() const noexcept { return resource_; }
    bool owns_frame() const noexcept { return owns_frame_; }

    void ReleaseResource() noexcept {
        if (!resource_) return;
        resource_->Release();
        resource_ = nullptr;
    }

    HRESULT ReleaseFrame() noexcept {
        if (!owns_frame_) return S_FALSE;
        owns_frame_ = false;
        return duplication_ ? duplication_->ReleaseFrame() : E_POINTER;
    }

    void Reset() noexcept {
        ReleaseResource();
        ReleaseFrame();
        duplication_ = nullptr;
    }

private:
    Resource* resource_ = nullptr;
    Duplication* duplication_ = nullptr;
    bool owns_frame_ = false;
};

} // namespace fthr::dxgi

#endif // FTHR_WINDOWS_DXGI_RECOVERY_H
