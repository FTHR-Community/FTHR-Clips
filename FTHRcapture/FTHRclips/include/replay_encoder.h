// Small Windows replay-encoder seam for codec-neutral hardware replay encoding.

#pragma once
#ifndef FTHR_REPLAY_ENCODER_H
#define FTHR_REPLAY_ENCODER_H

#include "encoded_video_config.h"
#include "video_encoder.h"

#include <cstdint>
#include <atomic>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;

namespace fthr {

enum class EncoderVendor : uint32_t {
    Nvidia,
    Amd,
    Intel,
    Software,
};

enum class EncoderPreference : uint32_t {
    Auto = 0,
    Nvidia = 1,
    Amd = 2,
    Intel = 3,
    Software = 4,
};

enum class ReplayEncoderBackend : uint32_t {
    NativeNvenc,
    FfmpegAmf,
    FfmpegQsv,
    Software,
};

enum class ReplayStartupError : uint32_t {
    None,
    RequestedCodecUnsupported,
    HardwareEncoderUnavailable,
    CaptureAdapterUnsupported,
    CrossAdapterPathUnavailable,
    ReplayCapacityLimited,
    EncoderInitFailed,
    CaptureTargetUnavailable,
};

struct WindowsReplaySelection {
    EncoderVendor capture_vendor = EncoderVendor::Software;
    EncoderVendor encoder_vendor = EncoderVendor::Software;
    ReplayEncoderBackend backend = ReplayEncoderBackend::Software;
    VideoCodec requested_codec = VideoCodec::H264;
    bool same_adapter = false;
    bool allowed = false;
    ReplayStartupError error = ReplayStartupError::None;
};

struct ActiveReplayCapability {
    VideoCodec requested_codec = VideoCodec::H264;
    VideoCodec active_codec = VideoCodec::H264;
    ReplayEncoderBackend active_backend = ReplayEncoderBackend::Software;
    EncoderVendor capture_vendor = EncoderVendor::Software;
    EncoderVendor encoder_vendor = EncoderVendor::Software;
    bool hardware = false;
    bool same_adapter = false;
    bool initialized = false;
    uint64_t generation = 0;
    ReplayStartupError error = ReplayStartupError::None;
    std::string active_name;
};

struct RawReplayCapacity {
    uint64_t frame_capacity = 0;
    uint64_t capacity_milliseconds = 0;
    bool meets_requested_duration = false;
};

struct ActiveEncoderInfo {
    EncoderVendor vendor = EncoderVendor::Software;
    ReplayEncoderBackend backend = ReplayEncoderBackend::Software;
    VideoCodec codec = VideoCodec::H264;
    bool hardware = false;
    std::string name;
};

struct NvencSlotDiagnostic {
    uint32_t slot_index = 0;
    uint32_t state = 0;
    uint64_t submitted_frame = 0;
    int64_t submitted_pts = 0;
    int64_t submitted_qpc = 0;
    uint64_t generation = 0;
    uint32_t submit_stage = 0;
    uint32_t drain_stage = 0;
    bool mapped = false;
    bool completion_signaled = false;
    bool output_locked = false;
};

// Progress snapshot used by CaptureEngine's diagnostic watchdog. Aggregate
// counters stay lock-free; the bounded per-slot vector is populated only when
// a stall snapshot is requested. Backends without native resource pools keep
// the zero defaults.
struct ReplayEncoderDiagnostics {
    uint64_t input_slots_acquired = 0;
    uint64_t map_attempts = 0;
    uint64_t maps_succeeded = 0;
    uint64_t encode_attempts = 0;
    uint64_t encode_returns = 0;
    uint64_t encode_successes = 0;
    uint64_t drain_dequeues = 0;
    uint64_t completion_events = 0;
    uint64_t bitstream_lock_attempts = 0;
    uint64_t bitstream_locks = 0;
    uint64_t bitstream_unlocks = 0;
    uint64_t resources_unmapped = 0;
    uint64_t packets_produced = 0;
    uint64_t slots_recycled = 0;
    // Backend-neutral aggregate failure evidence.  NVENC-specific slot and
    // status fields below remain unchanged; other backends populate only
    // these bounded counters when the corresponding condition is observed.
    uint64_t backpressure_events = 0;
    uint64_t backend_errors = 0;
    uint64_t device_removed_errors = 0;
    uint32_t registered_resources = 0;
    uint32_t pool_capacity = 0;
    uint32_t pending_resources = 0;
    uint32_t queued_outputs = 0;
    uint32_t mapped_resources = 0;
    uint32_t locked_bitstreams = 0;
    uint32_t submit_stage = 0;
    uint32_t drain_stage = 0;
    uint32_t active_submit_slot = 0xffffffffu;
    uint32_t active_drain_slot = 0xffffffffu;
    int32_t last_nvenc_status = 0;
    std::vector<NvencSlotDiagnostic> nvenc_slots;
};

constexpr ReplayEncoderBackend SelectProductionReplayBackend(
    EncoderVendor vendor,
    VideoCodec codec) noexcept {
    if (!IsProductionReplayCodecEnabled(codec)) {
        return ReplayEncoderBackend::Software;
    }
    switch (vendor) {
    case EncoderVendor::Nvidia: return ReplayEncoderBackend::NativeNvenc;
    case EncoderVendor::Amd: return ReplayEncoderBackend::FfmpegAmf;
    case EncoderVendor::Intel: return ReplayEncoderBackend::FfmpegQsv;
    case EncoderVendor::Software:
        return ReplayEncoderBackend::Software;
    }
    return ReplayEncoderBackend::Software;
}

constexpr bool IsProductionReplayBackendEnabled(
    EncoderVendor vendor,
    VideoCodec codec) noexcept {
    return (vendor == EncoderVendor::Software && codec == VideoCodec::H264)
        || SelectProductionReplayBackend(vendor, codec)
            != ReplayEncoderBackend::Software;
}

constexpr EncoderVendor EncoderVendorFromPciVendorId(
    uint32_t vendor_id) noexcept {
    switch (vendor_id) {
    case 0x10DE: return EncoderVendor::Nvidia;
    case 0x1002: return EncoderVendor::Amd;
    case 0x8086: return EncoderVendor::Intel;
    default: return EncoderVendor::Software;
    }
}

constexpr bool ShouldAllocateRawReplayPool(
    ReplayEncoderBackend,
    bool initialized) noexcept {
    return !initialized;
}

constexpr bool IsAutomaticCrossAdapterReplayAllowed() noexcept {
    return false;
}

constexpr bool IsAutomaticRawReplayFallbackAllowed() noexcept {
    return false;
}

constexpr WindowsReplaySelection SelectWindowsReplayPolicy(
    EncoderVendor capture_vendor,
    EncoderPreference preference,
    VideoCodec requested_codec) noexcept {
    WindowsReplaySelection result;
    result.capture_vendor = capture_vendor;
    result.requested_codec = requested_codec;

    if (!IsProductionReplayCodecEnabled(requested_codec)) {
        result.error = ReplayStartupError::RequestedCodecUnsupported;
        return result;
    }

    switch (preference) {
    case EncoderPreference::Auto:
        result.encoder_vendor = capture_vendor;
        break;
    case EncoderPreference::Nvidia:
        result.encoder_vendor = EncoderVendor::Nvidia;
        break;
    case EncoderPreference::Amd:
        result.encoder_vendor = EncoderVendor::Amd;
        break;
    case EncoderPreference::Intel:
        result.encoder_vendor = EncoderVendor::Intel;
        break;
    case EncoderPreference::Software:
        result.encoder_vendor = EncoderVendor::Software;
        break;
    }
    result.backend = SelectProductionReplayBackend(
        result.encoder_vendor, requested_codec);
    result.same_adapter = result.encoder_vendor == capture_vendor
        && capture_vendor != EncoderVendor::Software;

    if (preference == EncoderPreference::Software) {
        if (requested_codec != VideoCodec::H264) {
            result.error = ReplayStartupError::RequestedCodecUnsupported;
            return result;
        }
        result.allowed = true;
        return result;
    }
    if (result.encoder_vendor == EncoderVendor::Software
        || result.backend == ReplayEncoderBackend::Software) {
        result.error = ReplayStartupError::HardwareEncoderUnavailable;
        return result;
    }
    if (!result.same_adapter
        && result.encoder_vendor != EncoderVendor::Nvidia) {
        result.error = ReplayStartupError::CrossAdapterPathUnavailable;
        return result;
    }
    // An explicit NVIDIA choice may use the already-supported CPU-input path
    // on hybrid systems. Automatic mode remains strictly same-adapter.
    if (!result.same_adapter && preference == EncoderPreference::Auto) {
        result.error = ReplayStartupError::CrossAdapterPathUnavailable;
        return result;
    }
    result.allowed = true;
    return result;
}

constexpr WindowsReplaySelection SelectWindowsReplayPolicy(
    EncoderVendor capture_vendor,
    VideoCodec requested_codec) noexcept {
    auto result = SelectWindowsReplayPolicy(
        capture_vendor, EncoderPreference::Auto, requested_codec);
    if (capture_vendor == EncoderVendor::Software
        && result.error == ReplayStartupError::HardwareEncoderUnavailable) {
        result.error = ReplayStartupError::CaptureAdapterUnsupported;
    }
    return result;
}

constexpr RawReplayCapacity CalculateRawReplayCapacity(
    uint32_t width,
    uint32_t height,
    uint32_t bytes_per_pixel,
    uint32_t effective_fps,
    uint32_t requested_seconds,
    uint32_t budget_mebibytes) noexcept {
    RawReplayCapacity result;
    if (width == 0 || height == 0 || bytes_per_pixel == 0
        || effective_fps == 0 || requested_seconds == 0) {
        return result;
    }
    const uint64_t bytes_per_frame = static_cast<uint64_t>(width)
        * static_cast<uint64_t>(height)
        * static_cast<uint64_t>(bytes_per_pixel);
    const uint64_t budget_bytes = static_cast<uint64_t>(budget_mebibytes)
        * 1024ULL * 1024ULL;
    result.frame_capacity = budget_bytes / bytes_per_frame;
    result.capacity_milliseconds = result.frame_capacity * 1000ULL
        / static_cast<uint64_t>(effective_fps);
    result.meets_requested_duration = result.capacity_milliseconds
        >= static_cast<uint64_t>(requested_seconds) * 1000ULL;
    return result;
}

class IReplayEncoder {
public:
    using PacketCallback = std::function<void(
        const uint8_t* encoded_data,
        uint32_t size,
        int64_t pts,
        bool is_keyframe,
        int64_t wall_qpc)>;

    virtual ~IReplayEncoder() = default;

    virtual bool Initialize(
        const EncoderConfig& config,
        ID3D11Device* shared_device,
        ID3D11DeviceContext* shared_context,
        PacketCallback callback,
        bool cpu_input_mode) = 0;
    virtual bool EncodeFrame(int64_t present_qpc = 0) = 0;
    virtual bool EncodeFrameCPU(
        const uint8_t* bgra_data,
        uint32_t source_stride,
        int64_t present_qpc = 0) = 0;
    // Backends that require format conversion/scaling before submission can
    // prepare the current hardware-pool frame themselves. Direct-copy
    // backends keep the default path unchanged.
    virtual bool RequiresBackendGpuPreparation() const noexcept { return false; }
    virtual bool PrepareGpuFrame(
        ID3D11Texture2D*, uint32_t = 0) { return false; }
    virtual ID3D11Texture2D* GetCurrentInputTexture() const noexcept = 0;
    virtual uint32_t GetCurrentInputSubresource() const noexcept { return 0; }

    // Flushes pending packets and releases encoder resources. The current
    // native encoder's Finalize operation already has exactly these semantics.
    virtual void Shutdown() = 0;
    virtual EncodedVideoConfig GetVideoConfig() const = 0;
    virtual bool IsVideoConfigReady() const { return true; }
    virtual ActiveEncoderInfo GetActiveEncoderInfo() const = 0;
    virtual std::string GetLastError() const { return {}; }
    virtual ReplayEncoderDiagnostics GetDiagnostics() const noexcept {
        return {};
    }
    // CaptureEngine supplies the internal capture generation for bounded
    // per-slot evidence. Other backends do not need to implement this seam.
    virtual void SetCaptureGeneration(uint64_t) noexcept {}
    virtual bool GetEncodeEpoch(
        int64_t& start_qpc, int64_t& qpc_frequency) const = 0;
    virtual bool IsInitialized() const = 0;

    // Called from the command thread when recording starts, or from capture
    // when a focus gap ends. Consume only on the encoder submission thread.
    void RequestKeyframe() noexcept {
        keyframe_requested_.store(true, std::memory_order_release);
    }

protected:
    bool ConsumeKeyframeRequest() noexcept {
        return keyframe_requested_.exchange(false, std::memory_order_acq_rel);
    }

private:
    std::atomic<bool> keyframe_requested_{false};
};

const char* EncoderVendorName(EncoderVendor vendor) noexcept;
const char* ReplayEncoderBackendName(ReplayEncoderBackend backend) noexcept;
const char* ReplayStartupErrorName(ReplayStartupError error) noexcept;
EncoderVendor QueryD3D11DeviceVendor(ID3D11Device* device) noexcept;
ReplayStartupError ClassifyReplayInitializationFailure(
    std::string_view detail) noexcept;
ActiveReplayCapability EvaluateActiveReplayCapability(
    const WindowsReplaySelection& selection,
    const ActiveEncoderInfo& active,
    bool initialized,
    uint64_t generation);

// The selected monitor's capture adapter is authoritative. NVIDIA stays on
// native NVENC; AMD uses FFmpeg AMF; Intel uses FFmpeg QSV on the same device.
std::unique_ptr<IReplayEncoder> CreateProductionReplayEncoder(
    EncoderVendor vendor,
    VideoCodec codec);

} // namespace fthr

#endif // FTHR_REPLAY_ENCODER_H
