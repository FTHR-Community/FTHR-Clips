#include "replay_encoder.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>

#include <algorithm>
#include <cctype>

namespace fthr {

const char* EncoderVendorName(EncoderVendor vendor) noexcept {
    switch (vendor) {
    case EncoderVendor::Nvidia: return "NVIDIA";
    case EncoderVendor::Amd: return "AMD";
    case EncoderVendor::Intel: return "Intel";
    case EncoderVendor::Software: return "Software";
    }
    return "Software";
}

const char* ReplayEncoderBackendName(ReplayEncoderBackend backend) noexcept {
    switch (backend) {
    case ReplayEncoderBackend::NativeNvenc: return "native-nvenc";
    case ReplayEncoderBackend::FfmpegAmf: return "ffmpeg-amf";
    case ReplayEncoderBackend::FfmpegQsv: return "ffmpeg-qsv";
    case ReplayEncoderBackend::Software: return "software-openh264";
    }
    return "software-openh264";
}

const char* ReplayStartupErrorName(ReplayStartupError error) noexcept {
    switch (error) {
    case ReplayStartupError::None: return "NONE";
    case ReplayStartupError::RequestedCodecUnsupported:
        return "REQUESTED_CODEC_UNSUPPORTED";
    case ReplayStartupError::HardwareEncoderUnavailable:
        return "HARDWARE_ENCODER_UNAVAILABLE";
    case ReplayStartupError::CaptureAdapterUnsupported:
        return "CAPTURE_ADAPTER_UNSUPPORTED";
    case ReplayStartupError::CrossAdapterPathUnavailable:
        return "CROSS_ADAPTER_PATH_UNAVAILABLE";
    case ReplayStartupError::ReplayCapacityLimited:
        return "REPLAY_CAPACITY_LIMITED";
    case ReplayStartupError::EncoderInitFailed: return "ENCODER_INIT_FAILED";
    case ReplayStartupError::CaptureTargetUnavailable:
        return "CAPTURE_TARGET_UNAVAILABLE";
    }
    return "ENCODER_INIT_FAILED";
}

ReplayStartupError ClassifyReplayInitializationFailure(
    std::string_view detail) noexcept {
    std::string normalized(detail);
    std::transform(normalized.begin(), normalized.end(), normalized.begin(),
        [](unsigned char value) {
            return static_cast<char>(std::tolower(value));
        });
    if (normalized.find("unsupported") != std::string::npos
        || normalized.find("does not support") != std::string::npos) {
        return ReplayStartupError::RequestedCodecUnsupported;
    }
    if (normalized.find("runtime unavailable") != std::string::npos
        || normalized.find("not found") != std::string::npos
        || normalized.find("failed to load") != std::string::npos) {
        return ReplayStartupError::HardwareEncoderUnavailable;
    }
    return ReplayStartupError::EncoderInitFailed;
}

ActiveReplayCapability EvaluateActiveReplayCapability(
    const WindowsReplaySelection& selection,
    const ActiveEncoderInfo& active,
    bool initialized,
    uint64_t generation) {
    ActiveReplayCapability result;
    result.requested_codec = selection.requested_codec;
    result.active_codec = active.codec;
    result.active_backend = active.backend;
    result.capture_vendor = selection.capture_vendor;
    result.encoder_vendor = active.vendor;
    result.hardware = active.hardware;
    result.same_adapter = selection.same_adapter
        && active.vendor == selection.capture_vendor;
    result.generation = generation;
    result.active_name = active.name;

    if (!selection.allowed) {
        result.error = selection.error;
        return result;
    }
    if (!initialized
        || active.hardware != (selection.backend != ReplayEncoderBackend::Software)
        || active.backend != selection.backend
        || active.vendor != selection.encoder_vendor) {
        result.error = ReplayStartupError::EncoderInitFailed;
        return result;
    }
    if (active.codec != selection.requested_codec) {
        result.error = ReplayStartupError::RequestedCodecUnsupported;
        return result;
    }
    result.initialized = true;
    return result;
}

EncoderVendor QueryD3D11DeviceVendor(ID3D11Device* device) noexcept {
    if (!device) return EncoderVendor::Software;
    IDXGIDevice* dxgi_device = nullptr;
    if (FAILED(device->QueryInterface(
            __uuidof(IDXGIDevice),
            reinterpret_cast<void**>(&dxgi_device)))) {
        return EncoderVendor::Software;
    }

    IDXGIAdapter* adapter = nullptr;
    const HRESULT adapter_result = dxgi_device->GetAdapter(&adapter);
    dxgi_device->Release();
    if (FAILED(adapter_result) || !adapter) {
        return EncoderVendor::Software;
    }

    DXGI_ADAPTER_DESC description{};
    const HRESULT description_result = adapter->GetDesc(&description);
    adapter->Release();
    return SUCCEEDED(description_result)
        ? EncoderVendorFromPciVendorId(description.VendorId)
        : EncoderVendor::Software;
}

} // namespace fthr
