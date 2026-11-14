#pragma once

#include <cstdint>

namespace fthr::wgc {

enum class UpdateIntervalStatus : uint8_t {
    InvalidFps,
    SdkUnavailable,
    OsUnavailable,
    AvailabilityCheckFailed,
    DefaultReadFailed,
    SetFailed,
    ReadbackFailed,
    ReadbackMismatch,
    AlreadyConfigured,
    Applied,
};

struct UpdateIntervalResult {
    UpdateIntervalStatus status = UpdateIntervalStatus::InvalidFps;
    int64_t requested_100ns = -1;
    int64_t default_100ns = -1;
    int64_t effective_100ns = -1;
    int32_t hresult = 0;
};

// Let Windows publish fresh frames when available. The two-slot frame pool
// and the capture thread's configured pace bound FTHR's consumption.
inline constexpr int64_t kUnthrottledUpdateInterval100ns = 0;

constexpr bool ValidUpdateIntervalFps(uint32_t fps) noexcept {
    constexpr uint32_t kMaxCaptureFps = 360; // Windows engine startup limit.
    return fps > 0 && fps <= kMaxCaptureFps;
}

constexpr const char* UpdateIntervalStatusName(UpdateIntervalStatus status) noexcept {
    switch (status) {
    case UpdateIntervalStatus::InvalidFps: return "invalid_fps";
    case UpdateIntervalStatus::SdkUnavailable: return "sdk_unavailable";
    case UpdateIntervalStatus::OsUnavailable: return "os_unavailable";
    case UpdateIntervalStatus::AvailabilityCheckFailed: return "availability_check_failed";
    case UpdateIntervalStatus::DefaultReadFailed: return "default_read_failed";
    case UpdateIntervalStatus::SetFailed: return "set_failed";
    case UpdateIntervalStatus::ReadbackFailed: return "readback_failed";
    case UpdateIntervalStatus::ReadbackMismatch: return "readback_mismatch";
    case UpdateIntervalStatus::AlreadyConfigured: return "already_configured";
    case UpdateIntervalStatus::Applied: return "applied";
    }
    return "unknown";
}

// Operations return zero on success or the failed HRESULT. This policy is
// optional and never controls whether the capture session starts.
template<class Query, class Read, class Write>
UpdateIntervalResult ConfigureUpdateInterval(
    uint32_t fps, Query query, Read read, Write write) noexcept {
    UpdateIntervalResult result;
    if (!ValidUpdateIntervalFps(fps)) return result;
    result.requested_100ns = kUnthrottledUpdateInterval100ns;

    bool present = false;
    result.hresult = query(present);
    if (result.hresult != 0 || !present) {
        result.status = result.hresult ? UpdateIntervalStatus::AvailabilityCheckFailed
                                       : UpdateIntervalStatus::OsUnavailable;
        return result;
    }

    result.hresult = read(result.default_100ns);
    if (result.hresult != 0) {
        result.status = UpdateIntervalStatus::DefaultReadFailed;
        return result;
    }
    if (result.default_100ns == result.requested_100ns) {
        result.status = UpdateIntervalStatus::AlreadyConfigured;
        result.effective_100ns = result.default_100ns;
        return result;
    }

    const int32_t set_error = write(result.requested_100ns);
    const int32_t read_error = read(result.effective_100ns);
    if (set_error != 0) {
        result.status = UpdateIntervalStatus::SetFailed;
        result.hresult = set_error;
    } else if (read_error != 0) {
        result.status = UpdateIntervalStatus::ReadbackFailed;
        result.hresult = read_error;
    } else {
        result.status = result.effective_100ns == result.requested_100ns
            ? UpdateIntervalStatus::Applied : UpdateIntervalStatus::ReadbackMismatch;
    }
    return result;
}

} // namespace fthr::wgc
