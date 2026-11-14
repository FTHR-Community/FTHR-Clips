// Persistent NVENC encoding with packet callbacks and codec configuration.
// Convert H.264 Annex B to AVCC; retain HEVC Annex B and AV1 low-overhead OBUs
// for the pinned MP4 muxer. CaptureEngine owns storage and file output.

#ifdef _MSC_VER
#if __has_include("pch.h")
#include "pch.h"
#elif __has_include("stdafx.h")
#include "stdafx.h"
#endif
#endif

#include "hardware_encoder.h"
#include "nvenc_codec_config.h"
#include "nvenc_buffer_policy.h"
#include "video_encoder.h"
#include "windows_native_error.h"

#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable: 6011)
#endif

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <chrono>
#include <iostream>
#include <sstream>
#include <utility>

#include "nvEncodeAPI.h"

extern "C" {
#include <libswscale/swscale.h>
}


namespace fthr {

namespace {

bool SameComObject(IUnknown* left, IUnknown* right) noexcept {
    if (!left || !right) return false;
    IUnknown* left_identity = nullptr;
    IUnknown* right_identity = nullptr;
    const HRESULT left_hr = left->QueryInterface(
        __uuidof(IUnknown), reinterpret_cast<void**>(&left_identity));
    const HRESULT right_hr = right->QueryInterface(
        __uuidof(IUnknown), reinterpret_cast<void**>(&right_identity));
    const bool same = SUCCEEDED(left_hr)
        && SUCCEEDED(right_hr)
        && left_identity == right_identity;
    if (left_identity) left_identity->Release();
    if (right_identity) right_identity->Release();
    return same;
}

} // namespace


    // NVENC Error Code to String

    const char* NvencStatusToString(NVENCSTATUS status) {
        switch (status) {
        case NV_ENC_SUCCESS:                        return "SUCCESS";
        case NV_ENC_ERR_NO_ENCODE_DEVICE:           return "NO_ENCODE_DEVICE (No NVENC-capable GPU)";
        case NV_ENC_ERR_UNSUPPORTED_DEVICE:         return "UNSUPPORTED_DEVICE (GPU too old)";
        case NV_ENC_ERR_INVALID_ENCODERDEVICE:      return "INVALID_ENCODERDEVICE";
        case NV_ENC_ERR_INVALID_DEVICE:             return "INVALID_DEVICE";
        case NV_ENC_ERR_DEVICE_NOT_EXIST:           return "DEVICE_NOT_EXIST";
        case NV_ENC_ERR_INVALID_PTR:                return "INVALID_PTR (NULL pointer)";
        case NV_ENC_ERR_INVALID_EVENT:              return "INVALID_EVENT";
        case NV_ENC_ERR_INVALID_PARAM:              return "INVALID_PARAM (Invalid parameter in config)";
        case NV_ENC_ERR_INVALID_CALL:               return "INVALID_CALL (Invalid API call sequence)";
        case NV_ENC_ERR_OUT_OF_MEMORY:              return "OUT_OF_MEMORY";
        case NV_ENC_ERR_ENCODER_NOT_INITIALIZED:    return "ENCODER_NOT_INITIALIZED";
        case NV_ENC_ERR_UNSUPPORTED_PARAM:          return "UNSUPPORTED_PARAM";
        case NV_ENC_ERR_LOCK_BUSY:                  return "LOCK_BUSY";
        case NV_ENC_ERR_NOT_ENOUGH_BUFFER:          return "NOT_ENOUGH_BUFFER";
        case NV_ENC_ERR_INVALID_VERSION:            return "INVALID_VERSION (Wrong struct version)";
        case NV_ENC_ERR_MAP_FAILED:                 return "MAP_FAILED";
        case NV_ENC_ERR_NEED_MORE_INPUT:            return "NEED_MORE_INPUT";
        case NV_ENC_ERR_ENCODER_BUSY:               return "ENCODER_BUSY";
        case NV_ENC_ERR_EVENT_NOT_REGISTERD:        return "EVENT_NOT_REGISTERED";
        case NV_ENC_ERR_GENERIC:                    return "GENERIC_ERROR";
        case NV_ENC_ERR_INCOMPATIBLE_CLIENT_KEY:    return "INCOMPATIBLE_CLIENT_KEY";
        case NV_ENC_ERR_UNIMPLEMENTED:              return "UNIMPLEMENTED";
        case NV_ENC_ERR_RESOURCE_REGISTER_FAILED:   return "RESOURCE_REGISTER_FAILED";
        case NV_ENC_ERR_RESOURCE_NOT_REGISTERED:    return "RESOURCE_NOT_REGISTERED";
        case NV_ENC_ERR_RESOURCE_NOT_MAPPED:        return "RESOURCE_NOT_MAPPED";
        default:                                     return "UNKNOWN_ERROR";
        }
    }

    static const GUID& NvencPresetGuid(uint32_t preset) noexcept {
        switch (preset) {
        case 1: return NV_ENC_PRESET_P1_GUID;
        case 2: return NV_ENC_PRESET_P2_GUID;
        case 3: return NV_ENC_PRESET_P3_GUID;
        case 5: return NV_ENC_PRESET_P5_GUID;
        case 6: return NV_ENC_PRESET_P6_GUID;
        case 7: return NV_ENC_PRESET_P7_GUID;
        case 4:
        default: return NV_ENC_PRESET_P4_GUID;
        }
    }


    // DLL typedef

    typedef NVENCSTATUS(NVENCAPI* NVENCAPICREATEINSTANCE)(NV_ENCODE_API_FUNCTION_LIST*);


    // Reuse caller-owned Annex B conversion scratch storage on the drain thread
    // to avoid allocating a new vector for every encoded packet.

    using NalSpan = std::pair<const uint8_t*, int>;

    static void ParseAnnexBNals(const uint8_t* data, int size,
        std::vector<NalSpan>& out_nals)
    {
        out_nals.clear();
        int i = 0;

        while (i < size) {
            int nal_start = -1;
            for (; i < size - 2; i++) {
                if (data[i] == 0 && data[i + 1] == 0) {
                    if (data[i + 2] == 1) {
                        nal_start = i + 3;
                        i += 3;
                        break;
                    }
                    if (i + 3 < size && data[i + 2] == 0 && data[i + 3] == 1) {
                        nal_start = i + 4;
                        i += 4;
                        break;
                    }
                }
            }
            if (nal_start < 0) break;

            int nal_end = size;
            for (int j = nal_start; j < size - 2; j++) {
                if (data[j] == 0 && data[j + 1] == 0 &&
                    (data[j + 2] == 1 ||
                        (j + 3 < size && data[j + 2] == 0 && data[j + 3] == 1)))
                {
                    nal_end = j;
                    break;
                }
            }

            if (nal_end > nal_start) {
                out_nals.push_back({ data + nal_start, nal_end - nal_start });
            }
            i = nal_end;
        }
    }


    static int AnnexBToAvcc(const uint8_t* in, int in_size,
        std::vector<uint8_t>& out,
        std::vector<NalSpan>& nals_scratch,
        bool skip_spspps = true)
    {
        ParseAnnexBNals(in, in_size, nals_scratch);

        int total = 0;
        for (size_t n = 0; n < nals_scratch.size(); n++) {
            const uint8_t* ptr = nals_scratch[n].first;
            int            sz = nals_scratch[n].second;
            if (skip_spspps && sz > 0) {
                uint8_t nal_type = ptr[0] & 0x1F;
                if (nal_type == 7 || nal_type == 8) continue;
            }
            total += 4 + sz;
        }

        out.resize(static_cast<size_t>(total));
        uint8_t* dst = out.data();

        for (size_t n = 0; n < nals_scratch.size(); n++) {
            const uint8_t* ptr = nals_scratch[n].first;
            int            sz = nals_scratch[n].second;
            if (skip_spspps && sz > 0) {
                uint8_t nal_type = ptr[0] & 0x1F;
                if (nal_type == 7 || nal_type == 8) continue;
            }
            uint32_t len = static_cast<uint32_t>(sz);
            dst[0] = (len >> 24) & 0xFF;
            dst[1] = (len >> 16) & 0xFF;
            dst[2] = (len >> 8) & 0xFF;
            dst[3] = len & 0xFF;
            dst += 4;
            memcpy(dst, ptr, sz);
            dst += sz;
        }

        return total;
    }


    static std::vector<uint8_t>
        BuildAvccExtradata(const uint8_t* sps, int sps_size,
            const uint8_t* pps, int pps_size)
    {
        std::vector<uint8_t> extra;
        if (sps_size < 4 || pps_size < 1) return extra;

        extra.push_back(1);
        extra.push_back(sps[1]);
        extra.push_back(sps[2]);
        extra.push_back(sps[3]);
        extra.push_back(0xFF);
        extra.push_back(0xE1);

        extra.push_back(static_cast<uint8_t>((sps_size >> 8) & 0xFF));
        extra.push_back(static_cast<uint8_t>(sps_size & 0xFF));
        extra.insert(extra.end(), sps, sps + sps_size);

        extra.push_back(1);
        extra.push_back(static_cast<uint8_t>((pps_size >> 8) & 0xFF));
        extra.push_back(static_cast<uint8_t>(pps_size & 0xFF));
        extra.insert(extra.end(), pps, pps + pps_size);

        return extra;
    }



    NVENCDetectionResult DetectNVENC() {
        NVENCDetectionResult result = {};
        result.available = false;

        std::cout << "[NVENC] Detecting NVIDIA hardware encoding capability..." << std::endl;

        HMODULE nvenc_dll = LoadLibraryA("nvEncodeAPI64.dll");
        if (!nvenc_dll) {
            const DWORD native_error = ::GetLastError();
            result.error_message =
                "NVENC runtime load failed. "
                + diagnostics::FormatWin32Failure(
                    "LoadLibraryA(nvEncodeAPI64.dll)", native_error);
            std::cerr << "[NVENC] " << result.error_message << std::endl;
            return result;
        }

        NVENCAPICREATEINSTANCE NvEncodeAPICreateInstance =
            (NVENCAPICREATEINSTANCE)GetProcAddress(nvenc_dll, "NvEncodeAPICreateInstance");
        if (!NvEncodeAPICreateInstance) {
            const DWORD native_error = ::GetLastError();
            result.error_message =
                "NVENC entry point lookup failed. "
                + diagnostics::FormatWin32Failure(
                    "GetProcAddress(NvEncodeAPICreateInstance)", native_error);
            FreeLibrary(nvenc_dll);
            return result;
        }

        NV_ENCODE_API_FUNCTION_LIST nvenc_api = { NV_ENCODE_API_FUNCTION_LIST_VER };
        NVENCSTATUS status = NvEncodeAPICreateInstance(&nvenc_api);
        if (status != NV_ENC_SUCCESS) {
            std::ostringstream oss;
            oss << "NvEncodeAPICreateInstance failed with status " << status;
            result.error_message = oss.str();
            FreeLibrary(nvenc_dll);
            return result;
        }

        IDXGIFactory1* dxgi_factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&dxgi_factory);
        if (FAILED(hr)) {
            result.error_message = "Failed to create DXGI factory";
            FreeLibrary(nvenc_dll);
            return result;
        }

        IDXGIAdapter1* nvidia_adapter = nullptr;
        IDXGIAdapter1* adapter = nullptr;
        UINT adapter_index = 0;
        while (dxgi_factory->EnumAdapters1(adapter_index, &adapter) != DXGI_ERROR_NOT_FOUND) {
            DXGI_ADAPTER_DESC1 desc;
            adapter->GetDesc1(&desc);
            char adapter_name[256] = {};
            WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1,
                adapter_name, sizeof(adapter_name) - 1, nullptr, nullptr);
            std::cout << "[NVENC]   Adapter " << adapter_index << ": " << adapter_name << std::endl;
            if (desc.VendorId == 0x10DE) {
                nvidia_adapter = adapter;
                nvidia_adapter->AddRef();
                break;
            }
            adapter->Release();
            adapter_index++;
        }
        dxgi_factory->Release();

        if (!nvidia_adapter) {
            result.error_message = "No NVIDIA GPU found";
            FreeLibrary(nvenc_dll);
            return result;
        }

        ID3D11Device* temp_device = nullptr;
        ID3D11DeviceContext* temp_context = nullptr;
        D3D_FEATURE_LEVEL feature_level;
        hr = D3D11CreateDevice(nvidia_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
            nullptr, 0, D3D11_SDK_VERSION, &temp_device, &feature_level, &temp_context);
        nvidia_adapter->Release();

        if (FAILED(hr)) {
            result.error_message = "Failed to create D3D11 device on NVIDIA GPU";
            FreeLibrary(nvenc_dll);
            return result;
        }

        NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS session_params = { NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER };
        session_params.device = temp_device;
        session_params.deviceType = NV_ENC_DEVICE_TYPE_DIRECTX;
        session_params.apiVersion = NVENCAPI_VERSION;

        void* nvenc_session = nullptr;
        status = nvenc_api.nvEncOpenEncodeSessionEx(&session_params, &nvenc_session);
        if (status != NV_ENC_SUCCESS) {
            std::ostringstream oss;
            oss << "nvEncOpenEncodeSessionEx failed: " << status;
            result.error_message = oss.str();
            temp_context->Release(); temp_device->Release();
            FreeLibrary(nvenc_dll);
            return result;
        }

        uint32_t guid_count = 0;
        nvenc_api.nvEncGetEncodeGUIDCount(nvenc_session, &guid_count);

        if (guid_count > 0) {
            GUID* encode_guids = new GUID[guid_count];
            uint32_t guids_retrieved = 0;
            nvenc_api.nvEncGetEncodeGUIDs(nvenc_session, encode_guids, guid_count, &guids_retrieved);

            for (uint32_t i = 0; i < guids_retrieved; i++) {
                if (memcmp(&encode_guids[i], &NV_ENC_CODEC_H264_GUID, sizeof(GUID)) == 0)
                    result.h264_supported = true;
                if (memcmp(&encode_guids[i], &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)) == 0)
                    result.hevc_supported = true;
                if (memcmp(&encode_guids[i], &NV_ENC_CODEC_AV1_GUID, sizeof(GUID)) == 0)
                    result.av1_supported = true;
            }
            delete[] encode_guids;
        }

        if (result.h264_supported) {
            NV_ENC_CAPS_PARAM caps_param = { NV_ENC_CAPS_PARAM_VER };
            int caps_value = 0;
            caps_param.capsToQuery = NV_ENC_CAPS_WIDTH_MAX;
            if (nvenc_api.nvEncGetEncodeCaps(nvenc_session, NV_ENC_CODEC_H264_GUID,
                &caps_param, &caps_value) == NV_ENC_SUCCESS)
                result.max_encode_width = static_cast<uint32_t>(caps_value);
            caps_param.capsToQuery = NV_ENC_CAPS_HEIGHT_MAX;
            if (nvenc_api.nvEncGetEncodeCaps(nvenc_session, NV_ENC_CODEC_H264_GUID,
                &caps_param, &caps_value) == NV_ENC_SUCCESS)
                result.max_encode_height = static_cast<uint32_t>(caps_value);
            result.max_encode_sessions = 3;
        }

        IDXGIDevice* dxgi_device = nullptr;
        if (SUCCEEDED(temp_device->QueryInterface(__uuidof(IDXGIDevice), (void**)&dxgi_device))) {
            IDXGIAdapter* adapter_for_name = nullptr;
            if (SUCCEEDED(dxgi_device->GetAdapter(&adapter_for_name))) {
                DXGI_ADAPTER_DESC desc = {};
                if (SUCCEEDED(adapter_for_name->GetDesc(&desc))) {
                    char buf[256] = {};
                    WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1,
                        buf, sizeof(buf) - 1, nullptr, nullptr);
                    result.gpu_name = buf;
                }
                adapter_for_name->Release();
            }
            dxgi_device->Release();
        }

        nvenc_api.nvEncDestroyEncoder(nvenc_session);
        temp_context->Release();
        temp_device->Release();
        FreeLibrary(nvenc_dll);

        result.available = true;
        result.driver_version = 0;
        std::cout << "[NVENC] Detection complete - hardware encoding available ("
            << result.gpu_name << ")" << std::endl;
        return result;
    }


    // HardwareEncoder - Constructor

    HardwareEncoder::HardwareEncoder(VideoCodec codec)
        : nvenc_encoder_(nullptr)
        , nvenc_session_(nullptr)
        , nvenc_dll_(nullptr)
        , d3d11_device_(nullptr)
        , d3d11_context_(nullptr)
        , video_device_(nullptr)
        , video_context_(nullptr)
        , video_processor_enumerator_(nullptr)
        , video_processor_(nullptr)
        , gpu_scale_texture_(nullptr)
        , gpu_scale_output_view_(nullptr)
        , cpu_upload_texture_(nullptr)
        , cpu_upload_view_(nullptr)
        , gpu_scale_geometry_{}
        , input_width_(0)
        , input_height_(0)
        , gpu_scale_required_(false)
        , gpu_frame_prepared_(false)
        , cpu_input_mode_(false)
        , input_textures_(nullptr)
        , registered_resources_(nullptr)
        , mapped_input_resources_(nullptr)
        , input_slot_lifecycle_(nullptr)
        , cpu_input_buffers_(nullptr)
        , output_buffers_(nullptr)
        , completion_events_(nullptr)
        , slot_qpc_(nullptr)
        , buffer_count_(0)
        , current_buf_idx_(0)
        , pending_count_(0)
        , src_width_(0)
        , src_height_(0)
        , enc_width_(0)
        , enc_height_(0)
        , fps_(60)
        , bitrate_kbps_(16000)
        , codec_(codec)
        , initialized_(false)
        , pts_(0)
        , last_forced_idr_pts_(-1)
        , first_frame_(true)
        , encode_start_qpc_(0)
        , qpc_freq_(0)
        , last_frame_qpc_(0)
        , drain_thread_(nullptr)
        , drain_stop_(false)
        , drain_failed_(false)
        , callback_log_count_(0)
    {
    }

    HardwareEncoder::~HardwareEncoder() {
        Finalize();
    }



    bool HardwareEncoder::Initialize(const EncoderConfig&  config,
                                     ID3D11Device*         shared_device,
                                     ID3D11DeviceContext*  shared_context,
                                     PacketCallback        callback,
                                     bool                  cpu_input_mode) {
        last_error_ = "NVENC encoder initialization failed";
        if (initialized_) {
            std::cerr << "[HardwareEncoder] Already initialized" << std::endl;
            Finalize();
        }

        if (!callback) {
            last_error_ = "NVENC packet callback is missing";
            std::cerr << "[HardwareEncoder] PacketCallback must not be null" << std::endl;
            return false;
        }

        const auto* codec_selection = GetNvencCodecSelection(codec_);
        if (!codec_selection) {
            last_error_ = "requested codec is unsupported by the NVENC backend";
            std::cerr << "[HardwareEncoder] Unsupported codec selection" << std::endl;
            return false;
        }

        packet_callback_ = std::move(callback);

        src_width_ = config.src_width;
        src_height_ = config.src_height;
        enc_width_ = (config.enc_width > 0) ? config.enc_width : config.src_width;
        enc_height_ = (config.enc_height > 0) ? config.enc_height : config.src_height;
        if (!BuildCaptureScaleGeometry(
                src_width_, src_height_, enc_width_, enc_height_,
                config.scaling_mode == 1, gpu_scale_geometry_)) {
            last_error_ = "invalid NVENC source/target scaling geometry";
            return false;
        }
        enc_width_ = gpu_scale_geometry_.target_width;
        enc_height_ = gpu_scale_geometry_.target_height;
        hdr10_ = config.hdr10;
        if (hdr10_ && (src_width_ != enc_width_ || src_height_ != enc_height_)) {
            last_error_ = "NVENC HDR input must already have the encoded dimensions";
            return false;
        }
        input_width_ = enc_width_;
        input_height_ = enc_height_;
        gpu_scale_required_ = false;
        gpu_frame_prepared_ = false;
        fps_ = config.fps;
        bitrate_kbps_ = config.bitrate_kbps;
        pts_ = 0;
        last_forced_idr_pts_ = -1;
        first_frame_ = true;
        current_buf_idx_ = 0;
        pending_count_ = 0;
        drain_failed_.store(false);
        diag_input_slots_acquired_.store(0);
        diag_map_attempts_.store(0);
        diag_maps_succeeded_.store(0);
        diag_encode_attempts_.store(0);
        diag_encode_returns_.store(0);
        diag_encode_successes_.store(0);
        diag_drain_dequeues_.store(0);
        diag_completion_events_.store(0);
        diag_bitstream_lock_attempts_.store(0);
        diag_bitstream_locks_.store(0);
        diag_bitstream_unlocks_.store(0);
        diag_resources_unmapped_.store(0);
        diag_packets_produced_.store(0);
        diag_slots_recycled_.store(0);
        diag_mapped_resources_.store(0);
        diag_locked_bitstreams_.store(0);
        diag_pending_resources_.store(0);
        diag_queued_outputs_.store(0);
        diag_submit_stage_.store(0);
        diag_drain_stage_.store(0);
        drain_timings_.Take();
        diag_submit_slot_.store(0xffffffffu);
        diag_drain_slot_.store(0xffffffffu);
        diag_last_nvenc_status_.store(0);

        {
            LARGE_INTEGER freq;
            QueryPerformanceFrequency(&freq);
            qpc_freq_ = freq.QuadPart;
        }

        std::cout << "[HardwareEncoder] Init: "
            << src_width_ << "x" << src_height_
            << " -> " << enc_width_ << "x" << enc_height_
            << "  " << fps_ << " fps  " << bitrate_kbps_ << " kbps  "
            << VideoCodecName(codec_) << std::endl;

        if (enc_width_ == 0 || enc_height_ == 0 || fps_ == 0 || fps_ > 360) {
            last_error_ = "invalid NVENC dimensions or frame rate";
            std::cerr << "[HardwareEncoder] Invalid config" << std::endl;
            return false;
        }

        HMODULE nvenc_dll = LoadLibraryA("nvEncodeAPI64.dll");
        if (!nvenc_dll) {
            const DWORD native_error = ::GetLastError();
            last_error_ = "NVENC runtime unavailable. "
                + diagnostics::FormatWin32Failure(
                    "LoadLibraryA(nvEncodeAPI64.dll)", native_error);
            std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
            return false;
        }
        nvenc_dll_ = static_cast<void*>(nvenc_dll);

        NVENCAPICREATEINSTANCE NvEncodeAPICreateInstance =
            (NVENCAPICREATEINSTANCE)GetProcAddress(nvenc_dll, "NvEncodeAPICreateInstance");
        if (!NvEncodeAPICreateInstance) {
            const DWORD native_error = ::GetLastError();
            last_error_ = "NVENC runtime unavailable. "
                + diagnostics::FormatWin32Failure(
                    "GetProcAddress(NvEncodeAPICreateInstance)", native_error);
            std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }

        NV_ENCODE_API_FUNCTION_LIST* nvenc_api = new NV_ENCODE_API_FUNCTION_LIST;
        memset(nvenc_api, 0, sizeof(NV_ENCODE_API_FUNCTION_LIST));
        nvenc_api->version = NV_ENCODE_API_FUNCTION_LIST_VER;

        NVENCSTATUS status = NvEncodeAPICreateInstance(nvenc_api);
        if (status != NV_ENC_SUCCESS) {
            last_error_ = std::string("NVENC API initialization failed: ")
                + NvencStatusToString(status);
            std::cerr << "[HardwareEncoder] NvEncodeAPICreateInstance: " << NvencStatusToString(status) << std::endl;
            delete nvenc_api;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        nvenc_encoder_ = nvenc_api;

        // Share CaptureEngine's D3D11 device so same-adapter encoding avoids
        // cross-device texture transfers.
        if (!shared_device || !shared_context) {
            last_error_ = "NVENC same-adapter D3D11 device/context is unavailable";
            std::cerr << "[HardwareEncoder] shared_device/context must not be null" << std::endl;
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        d3d11_device_   = shared_device;   // non-owning
        d3d11_context_  = shared_context;  // non-owning
        cpu_input_mode_ = cpu_input_mode;
        // NVENC is an encoder, not a source-size converter. Always supply a
        // target-sized surface, including STRETCH and upscaling a small window.
        // Preserve the CPU bilinear scaler's wider downsampling filter on hybrid
        // inputs. FIT may shrink the picture even when the target canvas grows.
        const auto& destination = gpu_scale_geometry_.destination;
        const bool hybrid_downscale = cpu_input_mode_ &&
            (destination.width < src_width_ || destination.height < src_height_);
        gpu_scale_required_ = !hybrid_downscale &&
            (src_width_ != enc_width_ || src_height_ != enc_height_);
        std::cout << "[HardwareEncoder] Using shared D3D11 device ("
                  << (cpu_input_mode ? "Optimus CPU-input path" : "GPU zero-copy path")
                  << ")" << std::endl;

        NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS session_params = { NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER };
        session_params.device = d3d11_device_;
        session_params.deviceType = NV_ENC_DEVICE_TYPE_DIRECTX;
        session_params.apiVersion = NVENCAPI_VERSION;

        status = nvenc_api->nvEncOpenEncodeSessionEx(&session_params, &nvenc_session_);
        if (status != NV_ENC_SUCCESS) {
            last_error_ = std::string("NVENC session initialization failed: ")
                + NvencStatusToString(status);
            std::cerr << "[HardwareEncoder] nvEncOpenEncodeSessionEx: " << NvencStatusToString(status) << std::endl;
            // d3d11 device/context not owned - do not Release
            ReleaseGpuScalePipeline();
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }

        auto close_uninitialized_session = [&]() {
            ReleaseGpuScalePipeline();
            if (nvenc_session_) {
                nvenc_api->nvEncDestroyEncoder(nvenc_session_);
                nvenc_session_ = nullptr;
            }
            delete nvenc_api;
            nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll);
            nvenc_dll_ = nullptr;
        };

        if (gpu_scale_required_) {
            std::string scale_error;
            if (!InitializeGpuScalePipeline(scale_error)) {
                if (!cpu_input_mode_) {
                    last_error_ = "NVIDIA GPU scaling initialization failed: "
                        + scale_error;
                    close_uninitialized_session();
                    return false;
                }
                std::cout << "[HardwareEncoder] Hybrid GPU scaling unavailable: "
                          << scale_error << "; using CPU bilinear scaling" << std::endl;
                gpu_scale_required_ = false;
            } else if (cpu_upload_texture_) {
                cpu_input_mode_ = false;
            }
        }

        // Validate support on the exact D3D11 device/session used for capture.
        // Vendor ID, GPU model and FFmpeg encoder lists are not capability proof.
        uint32_t guid_count = 0;
        status = nvenc_api->nvEncGetEncodeGUIDCount(nvenc_session_, &guid_count);
        std::vector<GUID> encode_guids(guid_count);
        uint32_t guids_retrieved = 0;
        if (status != NV_ENC_SUCCESS || guid_count == 0
            || nvenc_api->nvEncGetEncodeGUIDs(
                   nvenc_session_, encode_guids.data(), guid_count,
                   &guids_retrieved) != NV_ENC_SUCCESS
            || !IsNvencCodecSupported(
                   codec_, encode_guids.data(), guids_retrieved)) {
            last_error_ = std::string("requested ") + VideoCodecName(codec_)
                + " codec is unsupported by this NVENC session";
            std::cerr << "[HardwareEncoder] Requested " << VideoCodecName(codec_)
                      << " is not supported by this NVENC session" << std::endl;
            close_uninitialized_session();
            return false;
        }

        const auto input_format = hdr10_
            ? NV_ENC_BUFFER_FORMAT_YUV420_10BIT : NV_ENC_BUFFER_FORMAT_ARGB;
        if (hdr10_) {
            NV_ENC_CAPS_PARAM caps{NV_ENC_CAPS_PARAM_VER};
            caps.capsToQuery = NV_ENC_CAPS_SUPPORT_10BIT_ENCODE;
            int supported = 0;
            if (nvenc_api->nvEncGetEncodeCaps(nvenc_session_,
                    codec_selection->encode_guid, &caps, &supported) != NV_ENC_SUCCESS
                || !supported) {
                last_error_ = "NVENC session does not support 10-bit encoding for this codec";
                close_uninitialized_session();
                return false;
            }
        }
        uint32_t input_format_count = 0;
        status = nvenc_api->nvEncGetInputFormatCount(
            nvenc_session_, codec_selection->encode_guid, &input_format_count);
        std::vector<NV_ENC_BUFFER_FORMAT> input_formats(input_format_count);
        uint32_t formats_retrieved = 0;
        if (status != NV_ENC_SUCCESS || input_format_count == 0
            || nvenc_api->nvEncGetInputFormats(
                   nvenc_session_, codec_selection->encode_guid,
                   input_formats.data(), input_format_count,
                   &formats_retrieved) != NV_ENC_SUCCESS
            || !IsNvencInputFormatSupported(
                   input_format,
                   input_formats.data(), formats_retrieved)) {
            last_error_ = std::string("requested ") + VideoCodecName(codec_)
                + (hdr10_ ? " codec does not support P010 input"
                          : " codec does not support ARGB input");
            std::cerr << "[HardwareEncoder] Requested " << VideoCodecName(codec_)
                      << " does not support the required input format"
                      << std::endl;
            close_uninitialized_session();
            return false;
        }

        NV_ENC_PRESET_CONFIG preset_config = { NV_ENC_PRESET_CONFIG_VER };
        preset_config.presetCfg.version = NV_ENC_CONFIG_VER;

        const uint32_t selected_preset = std::max<uint32_t>(
            1, std::min<uint32_t>(7, config.hardware_preset));
        const GUID& preset_guid = NvencPresetGuid(selected_preset);
        status = nvenc_api->nvEncGetEncodePresetConfigEx(
            nvenc_session_,
            codec_selection->encode_guid,
            preset_guid,
            NV_ENC_TUNING_INFO_LOW_LATENCY,
            &preset_config
        );

        NV_ENC_CONFIG encode_config = {};
        if (status == NV_ENC_SUCCESS) {
            memcpy(&encode_config, &preset_config.presetCfg, sizeof(NV_ENC_CONFIG));
            std::cout << "[HardwareEncoder] Preset P" << selected_preset
                      << "/LOW_LATENCY loaded" << std::endl;
        }
        else {
            std::cout << "[HardwareEncoder] Preset failed (" << NvencStatusToString(status)
                << ") - using manual config" << std::endl;
            memset(&encode_config, 0, sizeof(encode_config));
            encode_config.version = NV_ENC_CONFIG_VER;
        }

        encode_config.rcParams.version = NV_ENC_RC_PARAMS_VER;
        encode_config.rcParams.rateControlMode = NV_ENC_PARAMS_RC_VBR;
        encode_config.rcParams.averageBitRate = bitrate_kbps_ * 1000;
        encode_config.rcParams.maxBitRate = bitrate_kbps_ * 1500;  // 1.5x headroom for action scenes
        encode_config.rcParams.vbvBufferSize = bitrate_kbps_ * 1000 * 2;
        encode_config.rcParams.vbvInitialDelay = bitrate_kbps_ * 1000;
        // Disable lookahead: with lookahead on, NVENC buffers N frames before
        // emitting output for frame 0. If buffer_count_ < lookahead depth the
        // drain thread blocks in nvEncLockBitstream while CaptureThread fills all
        // slots and also blocks in WaitForFreeSlot — deadlock, 0 frames captured.
        encode_config.rcParams.enableLookahead = 0;

        // Preserve the existing one-media-second keyframe bound and no-B-frame
        // policy while selecting only the small codec-specific config union.
        if (!ConfigureNvencCodec(codec_, fps_, encode_config, hdr10_)) {
            last_error_ = "requested NVENC codec configuration is unsupported";
            std::cerr << "[HardwareEncoder] Could not configure requested codec"
                      << std::endl;
            close_uninitialized_session();
            return false;
        }

        NV_ENC_INITIALIZE_PARAMS init_params = {};
        memset(&init_params, 0, sizeof(init_params));
        init_params.version = NV_ENC_INITIALIZE_PARAMS_VER;
        init_params.encodeConfig = &encode_config;
        init_params.encodeGUID = codec_selection->encode_guid;
        init_params.presetGUID = preset_guid;
        init_params.encodeWidth = enc_width_;
        init_params.encodeHeight = enc_height_;
        init_params.darWidth = enc_width_;
        init_params.darHeight = enc_height_;
        init_params.frameRateNum = fps_;
        init_params.frameRateDen = 1;
        init_params.enablePTD = 1;
        // FTHR drains output from a dedicated worker thread. On Windows that
        // architecture must use NVENC asynchronous mode and wait for the
        // registered per-output completion event before LockBitstream.
        init_params.enableEncodeAsync = 1;
        init_params.tuningInfo = NV_ENC_TUNING_INFO_LOW_LATENCY;

        status = nvenc_api->nvEncInitializeEncoder(nvenc_session_, &init_params);
        if (status != NV_ENC_SUCCESS) {
            last_error_ = std::string("NVENC encoder initialization failed: ")
                + NvencStatusToString(status);
            std::cerr << "[HardwareEncoder] nvEncInitializeEncoder: " << NvencStatusToString(status) << std::endl;
            nvenc_api->nvEncDestroyEncoder(nvenc_session_); nvenc_session_ = nullptr;
            // d3d11 device/context not owned - do not Release
            ReleaseGpuScalePipeline();
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        std::cout << "[HardwareEncoder] " << VideoCodecName(codec_)
                  << " encoder initialized" << std::endl;

        // Allocate registered D3D11 textures for GPU input or system-memory buffers
        // for legacy CPU input. Size these expensive raw surfaces to the actual
        // encoder pipeline, independently of the encoded replay's storage mode.
        // A fixed 32-slot BGRA pool used ~253 MiB at 1080p before driver outputs.
        buffer_count_ = NvencAsyncBufferCount(
            encode_config.frameIntervalP,
            encode_config.rcParams.enableLookahead != 0,
            encode_config.rcParams.lookaheadDepth);
        output_buffers_ = new void*[buffer_count_];
        memset(output_buffers_, 0, sizeof(void*) * buffer_count_);
        slot_qpc_ = new int64_t[buffer_count_];
        memset(slot_qpc_, 0, sizeof(int64_t) * buffer_count_);
        diagnostic_slot_frame_ = std::make_unique<
            std::atomic<uint64_t>[]>(buffer_count_);
        diagnostic_slot_pts_ = std::make_unique<
            std::atomic<int64_t>[]>(buffer_count_);
        diagnostic_slot_qpc_ = std::make_unique<
            std::atomic<int64_t>[]>(buffer_count_);
        diagnostic_slot_generation_ = std::make_unique<
            std::atomic<uint64_t>[]>(buffer_count_);
        for (uint32_t i = 0; i < buffer_count_; ++i) {
            diagnostic_slot_frame_[i].store(0, std::memory_order_relaxed);
            diagnostic_slot_pts_[i].store(0, std::memory_order_relaxed);
            diagnostic_slot_qpc_[i].store(0, std::memory_order_relaxed);
            diagnostic_slot_generation_[i].store(0, std::memory_order_relaxed);
        }
        diagnostic_generation_.store(0, std::memory_order_release);

        // Helper: clean up partially-allocated output buffers then destroy encoder/dll.
        auto cleanup_and_fail = [&](uint32_t allocated_outputs) {
            ReleaseGpuScalePipeline();
            for (uint32_t j = 0; j < allocated_outputs; j++) {
                if (output_buffers_[j])
                    nvenc_api->nvEncDestroyBitstreamBuffer(nvenc_session_, output_buffers_[j]);
            }
            delete[] output_buffers_; output_buffers_ = nullptr;
            delete[] slot_qpc_; slot_qpc_ = nullptr;
            const auto close_status = nvenc_api->nvEncDestroyEncoder(nvenc_session_);
            nvenc_session_ = nullptr;
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return close_status;
        };

        auto allocate_gpu_input_buffers = [&]() -> bool {
            input_textures_       = new ID3D11Texture2D*[buffer_count_];
            registered_resources_ = new void*[buffer_count_];
            memset(input_textures_,       0, sizeof(ID3D11Texture2D*) * buffer_count_);
            memset(registered_resources_, 0, sizeof(void*)            * buffer_count_);

            for (uint32_t i = 0; i < buffer_count_; i++) {
                D3D11_TEXTURE2D_DESC tex_desc = {};
                tex_desc.Width            = input_width_;
                tex_desc.Height           = input_height_;
                tex_desc.MipLevels        = 1;
                tex_desc.ArraySize        = 1;
                tex_desc.Format           = hdr10_ ? DXGI_FORMAT_P010 : DXGI_FORMAT_B8G8R8A8_UNORM;
                tex_desc.SampleDesc.Count = 1;
                tex_desc.Usage            = D3D11_USAGE_DEFAULT;
                tex_desc.BindFlags        = 0;
                tex_desc.CPUAccessFlags   = 0;
                tex_desc.MiscFlags        = 0;

                HRESULT hr = d3d11_device_->CreateTexture2D(&tex_desc, nullptr, &input_textures_[i]);
                if (FAILED(hr)) {
                    last_error_ = diagnostics::FormatHResultFailure(
                        "ID3D11Device::CreateTexture2D(NVENC input)", hr);
                    return false;
                }

                NV_ENC_REGISTER_RESOURCE reg = { NV_ENC_REGISTER_RESOURCE_VER };
                reg.resourceType       = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX;
                reg.resourceToRegister = input_textures_[i];
                reg.width              = input_width_;
                reg.height             = input_height_;
                reg.pitch              = 0;
                reg.bufferFormat       = input_format;
                reg.bufferUsage        = NV_ENC_INPUT_IMAGE;

                status = nvenc_api->nvEncRegisterResource(nvenc_session_, &reg);
                if (status != NV_ENC_SUCCESS) {
                    last_error_ = std::string("nvEncRegisterResource failed: ")
                        + NvencStatusToString(status);
                    return false;
                }
                registered_resources_[i] = reg.registeredResource;

                NV_ENC_CREATE_BITSTREAM_BUFFER create_output = { NV_ENC_CREATE_BITSTREAM_BUFFER_VER };
                status = nvenc_api->nvEncCreateBitstreamBuffer(nvenc_session_, &create_output);
                if (status != NV_ENC_SUCCESS) {
                    last_error_ = std::string("nvEncCreateBitstreamBuffer failed: ")
                        + NvencStatusToString(status);
                    return false;
                }
                output_buffers_[i] = create_output.bitstreamBuffer;
            }
            std::cout << "[HardwareEncoder] " << buffer_count_
                      << " GPU texture slots registered with NVENC" << std::endl;
            mapped_input_resources_ = new void*[buffer_count_];
            memset(mapped_input_resources_, 0, sizeof(void*) * buffer_count_);
            input_slot_lifecycle_ = new NvencInputSlotLifecycle[buffer_count_];
            return true;
        };

        if (!cpu_input_mode_ && !allocate_gpu_input_buffers()) {
            const char* rollback_operation = nullptr;
            NVENCSTATUS rollback_status = NV_ENC_SUCCESS;
            const auto record_rollback_error = [&](const char* operation, NVENCSTATUS result) {
                if (!rollback_operation) {
                    rollback_operation = operation;
                    rollback_status = result;
                }
            };
            const auto release_gpu_handles = [&] {
                for (uint32_t i = 0; i < buffer_count_; ++i) {
                    if (registered_resources_[i]) {
                        const auto result = nvenc_api->nvEncUnregisterResource(nvenc_session_,
                            static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[i]));
                        if (result == NV_ENC_SUCCESS) registered_resources_[i] = nullptr;
                        else record_rollback_error("nvEncUnregisterResource", result);
                    }
                    if (output_buffers_[i]) {
                        const auto result = nvenc_api->nvEncDestroyBitstreamBuffer(
                            nvenc_session_, output_buffers_[i]);
                        if (result == NV_ENC_SUCCESS) output_buffers_[i] = nullptr;
                        else record_rollback_error("nvEncDestroyBitstreamBuffer", result);
                    }
                }
            };
            release_gpu_handles();
            if (rollback_operation) {
                last_error_ = std::string("NVENC GPU input rollback failed at ")
                    + rollback_operation + ": " + NvencStatusToString(rollback_status);
                // Retry only handles that failed to release before aborting the
                // session. Keep the textures alive until the session is closed.
                release_gpu_handles();
                const auto close_status = cleanup_and_fail(0);
                if (close_status != NV_ENC_SUCCESS) {
                    last_error_ += std::string("; nvEncDestroyEncoder failed: ")
                        + NvencStatusToString(close_status);
                }
                std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
            }
            for (uint32_t i = 0; i < buffer_count_; ++i) {
                if (input_textures_[i]) input_textures_[i]->Release();
            }
            delete[] input_textures_; input_textures_ = nullptr;
            delete[] registered_resources_; registered_resources_ = nullptr;
            if (rollback_operation) return false;
            if (!cpu_upload_texture_) {
                std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
                cleanup_and_fail(0);
                return false;
            }
            std::cout << "[HardwareEncoder] Hybrid GPU input unavailable: "
                      << last_error_ << "; using CPU bilinear scaling" << std::endl;
            ReleaseGpuScalePipeline();
            gpu_scale_required_ = false;
            cpu_input_mode_ = true;
        }

        if (cpu_input_mode_) {
            // CPU-input path (Optimus)
            cpu_input_buffers_ = new void*[buffer_count_];
            memset(cpu_input_buffers_, 0, sizeof(void*) * buffer_count_);

            for (uint32_t i = 0; i < buffer_count_; i++) {
                NV_ENC_CREATE_INPUT_BUFFER create_input = { NV_ENC_CREATE_INPUT_BUFFER_VER };
                create_input.width     = enc_width_;
                create_input.height    = enc_height_;
                create_input.bufferFmt = input_format;

                status = nvenc_api->nvEncCreateInputBuffer(nvenc_session_, &create_input);
                if (status != NV_ENC_SUCCESS) {
                    last_error_ = std::string("nvEncCreateInputBuffer failed: ")
                        + NvencStatusToString(status);
                    std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
                    for (uint32_t j = 0; j < i; j++) {
                        if (cpu_input_buffers_[j])
                            nvenc_api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[j]));
                    }
                    delete[] cpu_input_buffers_; cpu_input_buffers_ = nullptr;
                    cleanup_and_fail(i);
                    return false;
                }
                cpu_input_buffers_[i] = create_input.inputBuffer;

                NV_ENC_CREATE_BITSTREAM_BUFFER create_output = { NV_ENC_CREATE_BITSTREAM_BUFFER_VER };
                status = nvenc_api->nvEncCreateBitstreamBuffer(nvenc_session_, &create_output);
                if (status != NV_ENC_SUCCESS) {
                    last_error_ = std::string("nvEncCreateBitstreamBuffer failed: ")
                        + NvencStatusToString(status);
                    std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
                    for (uint32_t j = 0; j <= i; j++) {
                        if (cpu_input_buffers_[j]) nvenc_api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[j]));
                    }
                    delete[] cpu_input_buffers_; cpu_input_buffers_ = nullptr;
                    cleanup_and_fail(i);
                    return false;
                }
                output_buffers_[i] = create_output.bitstreamBuffer;
            }
            std::cout << "[HardwareEncoder] " << buffer_count_
                      << " CPU input buffers allocated (Optimus path)" << std::endl;
        }
        std::cout << "[HardwareEncoder] Async I/O pool: slots=" << buffer_count_
                  << " input=" << input_width_ << 'x' << input_height_
                  << " mode=" << (cpu_input_mode_ ? "cpu" : "gpu")
                  << " (raw encoder surfaces; independent of replay storage)"
                  << std::endl;
        if (cpu_input_mode) {
            const bool scales = src_width_ != enc_width_ || src_height_ != enc_height_;
            std::cout << "[HardwareEncoder] Hybrid frame preparation: "
                      << (cpu_upload_texture_ ? "NVIDIA GPU scaling"
                          : scales ? "CPU bilinear scaling" : "direct CPU copy")
                      << std::endl;
        }

        // Windows NVENC asynchronous operation requires one registered event
        // for every output buffer. The drain thread waits for the matching
        // event before touching that output or its mapped input resource.
        completion_events_ = new void*[buffer_count_];
        memset(completion_events_, 0, sizeof(void*) * buffer_count_);
        for (uint32_t i = 0; i < buffer_count_; ++i) {
            HANDLE event_handle = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            if (!event_handle) {
                last_error_ = "could not allocate NVENC completion event";
                initialized_ = true;
                Finalize();
                return false;
            }
            NV_ENC_EVENT_PARAMS event_params = { NV_ENC_EVENT_PARAMS_VER };
            event_params.completionEvent = event_handle;
            status = nvenc_api->nvEncRegisterAsyncEvent(
                nvenc_session_, &event_params);
            if (status != NV_ENC_SUCCESS) {
                CloseHandle(event_handle);
                last_error_ = std::string("nvEncRegisterAsyncEvent failed: ")
                    + NvencStatusToString(status);
                initialized_ = true;
                Finalize();
                return false;
            }
            completion_events_[i] = event_handle;
        }
        std::cout << "[HardwareEncoder] " << buffer_count_
                  << " NVENC completion events registered (Windows async mode)"
                  << std::endl;

        // Extract codec configuration for the MP4 muxer.
        std::vector<uint8_t> spspps_vec(NV_MAX_SEQ_HDR_LEN, 0);
        uint8_t* spspps_buf = spspps_vec.data();
        uint32_t  spspps_size = 0;
        NV_ENC_SEQUENCE_PARAM_PAYLOAD spspps_payload = { NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER };
        spspps_payload.spsppsBuffer = spspps_buf;
        spspps_payload.inBufferSize = NV_MAX_SEQ_HDR_LEN;
        spspps_payload.outSPSPPSPayloadSize = &spspps_size;

        status = nvenc_api->nvEncGetSequenceParams(nvenc_session_, &spspps_payload);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[HardwareEncoder] nvEncGetSequenceParams failed ("
                << NvencStatusToString(status) << ") - extradata unavailable" << std::endl;
        }
        else {
            std::cout << "[HardwareEncoder] Sequence header retrieved ("
                      << spspps_size << " bytes)" << std::endl;

            if (codec_ == VideoCodec::H264) {
                const uint8_t* sps_data = nullptr; int sps_size = 0;
                const uint8_t* pps_data = nullptr; int pps_size = 0;

                std::vector<NalSpan> nals;
                ParseAnnexBNals(spspps_buf, static_cast<int>(spspps_size), nals);
                for (const auto& nal : nals) {
                    const uint8_t* ptr = nal.first;
                    const int sz = nal.second;
                    if (sz < 1) continue;
                    const uint8_t nal_type = ptr[0] & 0x1F;
                    if (nal_type == 7) { sps_data = ptr; sps_size = sz; }
                    if (nal_type == 8) { pps_data = ptr; pps_size = sz; }
                }

                if (sps_data && sps_size >= 4 && pps_data && pps_size >= 1) {
                    extradata_ = BuildAvccExtradata(
                        sps_data, sps_size, pps_data, pps_size);
                }
            } else if (spspps_size > 0) {
                // HEVC remains Annex B; AV1 is configured for low-overhead OBU.
                // The pinned FFmpeg MP4 muxer builds hvcC/av1C from these bytes.
                extradata_.assign(spspps_buf, spspps_buf + spspps_size);
            }
        }

        if (extradata_.empty()) {
            std::cerr << "[HardwareEncoder] "
                      << (codec_ == VideoCodec::H264 ? "WARNING: " : "")
                      << "No usable " << VideoCodecName(codec_)
                      << " decoder configuration was produced" << std::endl;
            if (codec_ != VideoCodec::H264) {
                last_error_ = std::string("NVENC ") + VideoCodecName(codec_)
                    + " decoder configuration is unavailable";
                initialized_ = true;
                Finalize();
                return false;
            }
        } else {
            std::cout << "[HardwareEncoder] " << VideoCodecName(codec_)
                      << " decoder configuration ready (" << extradata_.size()
                      << " bytes)" << std::endl;
        }

        // Grow conversion scratch to encoded packet sizes on demand. Reserving
        // two bytes per raw pixel eagerly reserved 16 MiB for 4K H.264 capture.

        initialized_ = true;

        // Start async drain thread: submit threads enqueue slot indices, this
        // thread waits for completion and performs bounded output polling while
        // keeping the GPU-sync work off the CaptureThread.
        drain_stop_.store(false);
        drain_failed_.store(false);
        drain_thread_ = new std::thread(&HardwareEncoder::DrainThread, this);

        last_error_.clear();
        std::cout << "[HardwareEncoder] Ready (async drain enabled)." << std::endl;
        return true;
    }


    EncodedVideoConfig HardwareEncoder::GetVideoConfig() const {
        return BuildNvencVideoConfig(
            codec_, enc_width_, enc_height_, fps_, bitrate_kbps_, extradata_, hdr10_);
    }

    ActiveEncoderInfo HardwareEncoder::GetActiveEncoderInfo() const {
        const auto* selection = GetNvencCodecSelection(codec_);
        return {
            EncoderVendor::Nvidia,
            ReplayEncoderBackend::NativeNvenc,
            codec_,
            true,
            selection ? selection->active_codec_name : "nvenc_unknown"};
    }

    ReplayEncoderDiagnostics HardwareEncoder::GetDiagnostics() const noexcept {
        ReplayEncoderDiagnostics result;
        result.input_slots_acquired = diag_input_slots_acquired_.load();
        result.map_attempts = diag_map_attempts_.load();
        result.maps_succeeded = diag_maps_succeeded_.load();
        result.encode_attempts = diag_encode_attempts_.load();
        result.encode_returns = diag_encode_returns_.load();
        result.encode_successes = diag_encode_successes_.load();
        result.drain_dequeues = diag_drain_dequeues_.load();
        result.completion_events = diag_completion_events_.load();
        result.bitstream_lock_attempts = diag_bitstream_lock_attempts_.load();
        result.bitstream_locks = diag_bitstream_locks_.load();
        result.bitstream_unlocks = diag_bitstream_unlocks_.load();
        result.resources_unmapped = diag_resources_unmapped_.load();
        result.packets_produced = diag_packets_produced_.load();
        result.slots_recycled = diag_slots_recycled_.load();
        result.registered_resources =
            initialized_ && !cpu_input_mode_ ? buffer_count_ : 0;
        result.pool_capacity = buffer_count_;
        result.pending_resources = diag_pending_resources_.load();
        result.queued_outputs = diag_queued_outputs_.load();
        result.mapped_resources = diag_mapped_resources_.load();
        result.locked_bitstreams = diag_locked_bitstreams_.load();
        result.submit_stage = diag_submit_stage_.load();
        result.drain_stage = diag_drain_stage_.load();
        result.active_submit_slot = diag_submit_slot_.load(std::memory_order_acquire);
        result.active_drain_slot = diag_drain_slot_.load(std::memory_order_acquire);
        result.last_nvenc_status = diag_last_nvenc_status_.load();
        try {
            result.nvenc_slots.reserve(buffer_count_);
            for (uint32_t index = 0; index < buffer_count_; ++index) {
                const auto state = input_slot_lifecycle_
                    ? input_slot_lifecycle_[index].state()
                    : NvencInputSlotState::Available;
                const auto state_value = static_cast<uint32_t>(state);
                result.nvenc_slots.push_back({
                    index,
                    state_value,
                    diagnostic_slot_frame_
                        ? diagnostic_slot_frame_[index].load(
                            std::memory_order_acquire) : 0,
                    diagnostic_slot_pts_
                        ? diagnostic_slot_pts_[index].load(
                            std::memory_order_acquire) : 0,
                    diagnostic_slot_qpc_
                        ? diagnostic_slot_qpc_[index].load(
                            std::memory_order_acquire) : 0,
                    diagnostic_slot_generation_
                        ? diagnostic_slot_generation_[index].load(
                            std::memory_order_acquire) : 0,
                    result.active_submit_slot == index
                        ? result.submit_stage : 0,
                    result.active_drain_slot == index
                        ? result.drain_stage : 0,
                    state != NvencInputSlotState::Available,
                    state >= NvencInputSlotState::CompletionSignaled,
                    state == NvencInputSlotState::OutputLocked,
                });
            }
        } catch (...) {
            // Diagnostics must never turn a capture watchdog into a process
            // termination if a one-time reporting allocation fails.
            result.nvenc_slots.clear();
        }
        return result;
    }

    // ComputePts (private helper)

    int64_t HardwareEncoder::ComputePts(int64_t dxgi_present_qpc) {
        int64_t frame_qpc;
        if (dxgi_present_qpc > 0) {
            frame_qpc = dxgi_present_qpc;
        } else {
            LARGE_INTEGER qpc_now;
            QueryPerformanceCounter(&qpc_now);
            frame_qpc = qpc_now.QuadPart;
        }

        int64_t time_pts;
        if (first_frame_) {
            first_frame_ = false;
            encode_start_qpc_ = frame_qpc;
            time_pts = 0;
        } else {
            const int64_t elapsed = frame_qpc - encode_start_qpc_;
            time_pts = (elapsed * static_cast<int64_t>(fps_)) / qpc_freq_;
            if (time_pts <= pts_) time_pts = pts_ + 1;
        }
        last_frame_qpc_ = frame_qpc;
        pts_ = time_pts;
        return time_pts;
    }


    // EncodeFrame (registered GPU textures)

    bool HardwareEncoder::EncodeFrame(int64_t dxgi_present_qpc) {
        if (!initialized_ || cpu_input_mode_) {
            std::cerr << "[EncodeFrame] GPU input is unavailable" << std::endl;
            return false;
        }
        if (gpu_scale_required_ && !gpu_frame_prepared_) {
            last_error_ = "NVIDIA scaling frame was submitted without GPU preparation";
            std::cerr << "[EncodeFrame] " << last_error_ << std::endl;
            return false;
        }
        gpu_frame_prepared_ = false;

        // Backpressure: the preceding submission prepares this slot before exposing
        // its texture to CaptureEngine. Keep this wait as an invariant guard.
        diag_submit_stage_.store(1);
        if (!WaitForFreeSlot()) {
            diag_submit_stage_.store(0);
            return false;
        }
        diag_input_slots_acquired_.fetch_add(1);

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);
        uint32_t idx = current_buf_idx_ % buffer_count_;

        pts_ = ComputePts(dxgi_present_qpc);
        slot_qpc_[idx] = last_frame_qpc_;
        if (diagnostic_slot_frame_) {
            diagnostic_slot_frame_[idx].store(
                diag_input_slots_acquired_.load(std::memory_order_relaxed),
                std::memory_order_release);
            diagnostic_slot_pts_[idx].store(pts_, std::memory_order_release);
            diagnostic_slot_qpc_[idx].store(
                last_frame_qpc_, std::memory_order_release);
        }

        NV_ENC_MAP_INPUT_RESOURCE map_res = { NV_ENC_MAP_INPUT_RESOURCE_VER };
        map_res.registeredResource = static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[idx]);

        diag_submit_stage_.store(2);
        diag_map_attempts_.fetch_add(1);
        NVENCSTATUS status = api->nvEncMapInputResource(nvenc_session_, &map_res);
        diag_last_nvenc_status_.store(static_cast<int32_t>(status));
        if (status != NV_ENC_SUCCESS) {
            diag_submit_stage_.store(0);
            std::cerr << "[EncodeFrame] nvEncMapInputResource: " << NvencStatusToString(status) << std::endl;
            return false;
        }
        diag_maps_succeeded_.fetch_add(1);
        diag_mapped_resources_.fetch_add(1);
        if (!input_slot_lifecycle_[idx].OnMapped()) {
            api->nvEncUnmapInputResource(nvenc_session_, map_res.mappedResource);
            diag_resources_unmapped_.fetch_add(1);
            diag_mapped_resources_.fetch_sub(1);
            diag_submit_stage_.store(0);
            last_error_ = "NVENC input slot was reused before its prior output completed";
            std::cerr << "[EncodeFrame] " << last_error_ << std::endl;
            return false;
        }
        mapped_input_resources_[idx] = map_res.mappedResource;

        NV_ENC_PIC_PARAMS pic = {};
        pic.version         = NV_ENC_PIC_PARAMS_VER;
        pic.inputBuffer     = map_res.mappedResource;
        pic.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[idx]);
        pic.bufferFmt       = map_res.mappedBufferFmt;
        pic.inputWidth      = input_width_;
        pic.inputHeight     = input_height_;
        pic.pictureStruct   = NV_ENC_PIC_STRUCT_FRAME;
        pic.inputTimeStamp  = static_cast<uint64_t>(pts_);
        pic.completionEvent = completion_events_[idx];
        const bool force_idr = ConsumeKeyframeRequest() || last_forced_idr_pts_ < 0
            || pts_ - last_forced_idr_pts_ >= static_cast<int64_t>(fps_);
        if (force_idr) pic.encodePicFlags |= NV_ENC_PIC_FLAG_FORCEIDR;

        diag_submit_stage_.store(3);
        diag_encode_attempts_.fetch_add(1);
        status = api->nvEncEncodePicture(nvenc_session_, &pic);
        diag_encode_returns_.fetch_add(1);
        diag_last_nvenc_status_.store(static_cast<int32_t>(status));

        // SUCCESS and NEED_MORE_INPUT both mean "submitted OK"; the drain thread
        // will block in nvEncLockBitstream until each slot's output is ready.
        if (status != NV_ENC_SUCCESS && status != NV_ENC_ERR_NEED_MORE_INPUT) {
            const NVENCSTATUS unmap_status = api->nvEncUnmapInputResource(
                nvenc_session_, map_res.mappedResource);
            diag_last_nvenc_status_.store(static_cast<int32_t>(unmap_status));
            if (unmap_status == NV_ENC_SUCCESS) {
                mapped_input_resources_[idx] = nullptr;
                input_slot_lifecycle_[idx].OnRejectedSubmissionUnmapped();
                diag_resources_unmapped_.fetch_add(1);
                diag_mapped_resources_.fetch_sub(1);
            }
            diag_submit_stage_.store(0);
            std::cerr << "[EncodeFrame] nvEncEncodePicture: " << NvencStatusToString(status) << std::endl;
            return false;
        }
        if (!input_slot_lifecycle_[idx].OnSubmitted()) {
            diag_submit_stage_.store(0);
            last_error_ = "NVENC input slot lifecycle rejected a successful submission";
            std::cerr << "[EncodeFrame] " << last_error_ << std::endl;
            return false;
        }
        diag_encode_successes_.fetch_add(1);
        if (force_idr) last_forced_idr_pts_ = pts_;

        {
            std::lock_guard<std::mutex> lk(drain_mutex_);
            drain_queue_.push_back(idx);
            pending_count_++;
            diag_pending_resources_.fetch_add(1);
            diag_queued_outputs_.fetch_add(1);
        }
        drain_cv_.notify_one();

        diag_submit_stage_.store(4);
        current_buf_idx_ = (current_buf_idx_ + 1) % buffer_count_;
        // The hybrid upload prepares its next slot on entry to EncodeFrameCPU.
        // Report the current successful submission before waiting on that slot.
        if (!cpu_upload_texture_ && !PrepareCurrentGpuInputSlot()) {
            diag_submit_stage_.store(0);
            return false;
        }
        diag_submit_stage_.store(0);
        return true;
    }


    // GetCurrentInputTexture

    bool HardwareEncoder::RequiresBackendGpuPreparation() const noexcept {
        return gpu_scale_required_ && !cpu_upload_texture_;
    }

    bool HardwareEncoder::PrepareGpuFrame(
        ID3D11Texture2D* source,
        uint32_t source_subresource) {
        gpu_frame_prepared_ = false;
        if (!gpu_scale_required_) {
            last_error_ = "NVIDIA GPU preparation was requested for a non-scaled path";
            return false;
        }
        if (!initialized_ || !source || !d3d11_device_ || !d3d11_context_
            || !video_device_ || !video_context_ || !video_processor_
            || !video_processor_enumerator_ || !gpu_scale_texture_
            || !gpu_scale_output_view_ || !input_textures_
            || !input_slot_lifecycle_) {
            last_error_ = "NVIDIA scaling GPU conversion resources are unavailable";
            return false;
        }
        const uint32_t slot = current_buf_idx_ % buffer_count_;
        if (!input_slot_lifecycle_[slot].is_available()) {
            last_error_ = "NVIDIA scaling input slot is still pending output";
            return false;
        }
        ID3D11Device* source_device = nullptr;
        source->GetDevice(&source_device);
        const bool same_device = SameComObject(source_device, d3d11_device_);
        if (source_device) source_device->Release();
        if (!same_device) {
            last_error_ = "NVIDIA scaling source texture belongs to a different D3D11 device";
            return false;
        }

        D3D11_TEXTURE2D_DESC source_description{};
        source->GetDesc(&source_description);
        const uint32_t mip_levels = std::max(1U, source_description.MipLevels);
        const uint32_t array_size = std::max(1U, source_description.ArraySize);
        if (source_description.Width != src_width_
            || source_description.Height != src_height_
            || source_description.Format != DXGI_FORMAT_B8G8R8A8_UNORM
            || source_subresource >= mip_levels * array_size) {
            last_error_ = "NVIDIA scaling source texture geometry, format, or subresource is invalid";
            return false;
        }

        D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
        input_description.FourCC = 0;
        input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
        input_description.Texture2D.MipSlice = source_subresource % mip_levels;
        input_description.Texture2D.ArraySlice = source_subresource / mip_levels;
        ID3D11VideoProcessorInputView* input_view = nullptr;
        HRESULT result = video_device_->CreateVideoProcessorInputView(
            source, video_processor_enumerator_, &input_description, &input_view);
        if (FAILED(result) || !input_view) {
            last_error_ = diagnostics::FormatHResultFailure(
                "ID3D11VideoDevice::CreateVideoProcessorInputView(NVIDIA scaling)",
                result);
            return false;
        }

        const bool scaled = ScaleIntoCurrentGpuInput(input_view);
        input_view->Release();
        return scaled;
    }

    bool HardwareEncoder::ScaleIntoCurrentGpuInput(
        ID3D11VideoProcessorInputView* source_view) {
        gpu_frame_prepared_ = false;
        D3D11_VIDEO_PROCESSOR_STREAM stream{};
        stream.Enable = TRUE;
        stream.OutputIndex = 0;
        stream.pInputSurface = source_view;
        const HRESULT result = video_context_->VideoProcessorBlt(
            video_processor_, gpu_scale_output_view_, 0, 1, &stream);
        if (FAILED(result)) {
            last_error_ = diagnostics::FormatHResultFailure(
                "ID3D11VideoContext::VideoProcessorBlt(NVIDIA scaling)", result);
            return false;
        }

        const uint32_t slot = current_buf_idx_ % buffer_count_;
        d3d11_context_->CopySubresourceRegion(
            input_textures_[slot], 0, 0, 0, 0,
            gpu_scale_texture_, 0, nullptr);
        const HRESULT removed = d3d11_device_->GetDeviceRemovedReason();
        if (FAILED(removed)) {
            last_error_ = diagnostics::FormatHResultFailure(
                "ID3D11Device::GetDeviceRemovedReason(NVIDIA scaling)", removed);
            return false;
        }
        gpu_frame_prepared_ = true;
        return true;
    }

    ID3D11Texture2D* HardwareEncoder::GetCurrentInputTexture() const noexcept {
        if (!input_textures_ || !initialized_) return nullptr;
        const uint32_t idx = current_buf_idx_ % buffer_count_;
        if (input_slot_lifecycle_ && !input_slot_lifecycle_[idx].is_available())
            return nullptr;
        return input_textures_[idx];
    }


    // EncodeFrameCPU (Optimus / CPU-input path)

    bool HardwareEncoder::EncodeFrameCPU(const uint8_t* bgra_data, uint32_t src_stride,
                                         int64_t dxgi_present_qpc) {
        if (!initialized_ || (!cpu_input_mode_ && !cpu_upload_texture_)) {
            std::cerr << "[EncodeFrameCPU] Not initialized or not in CPU input mode" << std::endl;
            return false;
        }

        const uint32_t bytes_per_pixel = hdr10_ ? 2 : 4;
        if (!bgra_data || src_stride < src_width_ * bytes_per_pixel) {
            last_error_ = "NVENC CPU input has incomplete rows";
            return false;
        }

        if (cpu_upload_texture_) {
            const bool slot_ready = PrepareCurrentGpuInputSlot();
            diag_submit_stage_.store(0);
            if (!slot_ready) return false;

            // UpdateSubresource consumes the caller's CPU data before returning.
            // The upload, scale and encoder-input copy use the same immediate context.
            d3d11_context_->UpdateSubresource(
                cpu_upload_texture_, 0, nullptr, bgra_data, src_stride, 0);
            return ScaleIntoCurrentGpuInput(cpu_upload_view_)
                && EncodeFrame(dxgi_present_qpc);
        }

        const bool scale = src_width_ != enc_width_ || src_height_ != enc_height_;
        if (scale && !cpu_scale_context_) {
            const auto& destination = gpu_scale_geometry_.destination;
            cpu_scale_context_ = sws_getContext(
                src_width_, src_height_, AV_PIX_FMT_BGRA,
                destination.width, destination.height, AV_PIX_FMT_BGRA,
                SWS_BILINEAR, nullptr, nullptr, nullptr);
            if (!cpu_scale_context_) {
                last_error_ = "Could not initialize the hybrid recording scaler";
                return false;
            }
        }

        diag_submit_stage_.store(1);
        if (!WaitForFreeSlot()) {
            diag_submit_stage_.store(0);
            return false;
        }
        diag_input_slots_acquired_.fetch_add(1);

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);
        uint32_t idx = current_buf_idx_ % buffer_count_;

        pts_ = ComputePts(dxgi_present_qpc);
        slot_qpc_[idx] = last_frame_qpc_;
        if (diagnostic_slot_frame_) {
            diagnostic_slot_frame_[idx].store(
                diag_input_slots_acquired_.load(std::memory_order_relaxed),
                std::memory_order_release);
            diagnostic_slot_pts_[idx].store(pts_, std::memory_order_release);
            diagnostic_slot_qpc_[idx].store(
                last_frame_qpc_, std::memory_order_release);
        }

        // Lock the NVENC system-memory input buffer.
        NV_ENC_LOCK_INPUT_BUFFER lock_input = { NV_ENC_LOCK_INPUT_BUFFER_VER };
        lock_input.inputBuffer = static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[idx]);

        NVENCSTATUS status = api->nvEncLockInputBuffer(nvenc_session_, &lock_input);
        if (status != NV_ENC_SUCCESS) {
            last_error_ = std::string("NVENC input buffer lock failed: ")
                + NvencStatusToString(status);
            diag_submit_stage_.store(0);
            std::cerr << "[EncodeFrameCPU] nvEncLockInputBuffer: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        // Copy BGRA frame data. Both DXGI staging and NVENC input buffers may have
        // padding (RowPitch / pitch > width*4). Handle each independently.
        uint8_t*       dst        = static_cast<uint8_t*>(lock_input.bufferDataPtr);
        const uint32_t dst_stride = lock_input.pitch;
        const uint32_t row_bytes  = enc_width_ * bytes_per_pixel;
        const uint32_t rows = hdr10_ ? enc_height_ + enc_height_ / 2 : enc_height_;

        if (!dst || dst_stride < row_bytes) {
            api->nvEncUnlockInputBuffer(nvenc_session_, lock_input.inputBuffer);
            last_error_ = "NVENC input buffer pitch is smaller than the encoded frame";
            diag_submit_stage_.store(0);
            return false;
        }

        if (scale) {
            const auto& destination = gpu_scale_geometry_.destination;
            // Clear letterbox bars on each reused NVENC surface.
            if (destination.width != enc_width_ || destination.height != enc_height_)
                memset(dst, 0, static_cast<size_t>(dst_stride) * enc_height_);
            const uint8_t* source_planes[] = {bgra_data, nullptr, nullptr, nullptr};
            const int source_strides[] = {static_cast<int>(src_stride), 0, 0, 0};
            uint8_t* target_planes[] = {
                dst + static_cast<size_t>(destination.top) * dst_stride
                    + destination.left * 4, nullptr, nullptr, nullptr};
            const int target_strides[] = {static_cast<int>(dst_stride), 0, 0, 0};
            if (sws_scale(cpu_scale_context_, source_planes, source_strides,
                    0, src_height_, target_planes, target_strides)
                != static_cast<int>(destination.height)) {
                api->nvEncUnlockInputBuffer(nvenc_session_, lock_input.inputBuffer);
                last_error_ = "Hybrid recording frame scaling failed";
                diag_submit_stage_.store(0);
                return false;
            }
        } else if (src_stride == row_bytes && dst_stride == row_bytes) {
            memcpy(dst, bgra_data, static_cast<size_t>(row_bytes) * rows);
        } else {
            for (uint32_t y = 0; y < rows; y++) {
                memcpy(dst + static_cast<size_t>(y) * dst_stride,
                       bgra_data + static_cast<size_t>(y) * src_stride,
                       row_bytes);
            }
        }

        status = api->nvEncUnlockInputBuffer(nvenc_session_, lock_input.inputBuffer);
        if (status != NV_ENC_SUCCESS) {
            last_error_ = std::string("NVENC input buffer unlock failed: ")
                + NvencStatusToString(status);
            diag_submit_stage_.store(0);
            return false;
        }

        // Submit encode.
        NV_ENC_PIC_PARAMS pic = {};
        pic.version         = NV_ENC_PIC_PARAMS_VER;
        pic.inputBuffer     = static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[idx]);
        pic.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[idx]);
        pic.bufferFmt       = hdr10_ ? NV_ENC_BUFFER_FORMAT_YUV420_10BIT
                                    : NV_ENC_BUFFER_FORMAT_ARGB;
        pic.inputWidth      = enc_width_;
        pic.inputHeight     = enc_height_;
        pic.pictureStruct   = NV_ENC_PIC_STRUCT_FRAME;
        pic.inputTimeStamp  = static_cast<uint64_t>(pts_);
        pic.completionEvent = completion_events_[idx];
        const bool force_idr = ConsumeKeyframeRequest() || last_forced_idr_pts_ < 0
            || pts_ - last_forced_idr_pts_ >= static_cast<int64_t>(fps_);
        if (force_idr) pic.encodePicFlags |= NV_ENC_PIC_FLAG_FORCEIDR;

        diag_submit_stage_.store(3);
        diag_encode_attempts_.fetch_add(1);
        status = api->nvEncEncodePicture(nvenc_session_, &pic);
        diag_encode_returns_.fetch_add(1);
        diag_last_nvenc_status_.store(static_cast<int32_t>(status));

        if (status != NV_ENC_SUCCESS && status != NV_ENC_ERR_NEED_MORE_INPUT) {
            diag_submit_stage_.store(0);
            std::cerr << "[EncodeFrameCPU] nvEncEncodePicture: " << NvencStatusToString(status) << std::endl;
            return false;
        }
        diag_encode_successes_.fetch_add(1);
        if (force_idr) last_forced_idr_pts_ = pts_;

        {
            std::lock_guard<std::mutex> lk(drain_mutex_);
            drain_queue_.push_back(idx);
            pending_count_++;
            diag_pending_resources_.fetch_add(1);
            diag_queued_outputs_.fetch_add(1);
        }
        drain_cv_.notify_one();

        diag_submit_stage_.store(4);
        current_buf_idx_ = (current_buf_idx_ + 1) % buffer_count_;
        diag_submit_stage_.store(0);
        return true;
    }


    // RetrieveOutput (private)

    bool HardwareEncoder::RetrieveOutput(uint32_t buf_idx) {
        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);

        diag_drain_stage_.store(6);
        const DWORD wait_result = WaitForSingleObject(
            static_cast<HANDLE>(completion_events_[buf_idx]), 5000);
        if (wait_result != WAIT_OBJECT_0) {
            diag_drain_stage_.store(0);
            last_error_ = wait_result == WAIT_TIMEOUT
                ? "NVENC completion event timed out"
                : "NVENC completion event wait failed";
            std::cerr << "[RetrieveOutput] " << last_error_ << std::endl;
            return false;
        }
        diag_completion_events_.fetch_add(1);
        if (!cpu_input_mode_
            && !input_slot_lifecycle_[buf_idx].OnCompletionSignaled()) {
            diag_drain_stage_.store(0);
            last_error_ = "NVENC completion arrived in an invalid input-slot state";
            std::cerr << "[RetrieveOutput] " << last_error_ << std::endl;
            return false;
        }

        NV_ENC_LOCK_BITSTREAM lock_bs = { NV_ENC_LOCK_BITSTREAM_VER };
        lock_bs.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[buf_idx]);
        lock_bs.doNotWait = 1;

        diag_drain_stage_.store(9);
        constexpr ULONGLONG kBitstreamLockTimeoutMs = 2000;
        const ULONGLONG lock_deadline = GetTickCount64() + kBitstreamLockTimeoutMs;
        NVENCSTATUS status = NV_ENC_ERR_LOCK_BUSY;
        do {
            diag_bitstream_lock_attempts_.fetch_add(1);
            status = api->nvEncLockBitstream(nvenc_session_, &lock_bs);
            diag_last_nvenc_status_.store(static_cast<int32_t>(status));
            if (status != NV_ENC_ERR_LOCK_BUSY) break;
            if (drain_stop_.load() || GetTickCount64() >= lock_deadline) break;
            Sleep(1);
        } while (true);

        if (status != NV_ENC_SUCCESS) {
            diag_drain_stage_.store(0);
            last_error_ = status == NV_ENC_ERR_LOCK_BUSY
                ? "NVENC bitstream remained busy after its completion event"
                : std::string("NVENC bitstream lock failed: ")
                    + NvencStatusToString(status);
            std::cerr << "[RetrieveOutput] nvEncLockBitstream: " << NvencStatusToString(status) << std::endl;
            return false;
        }
        diag_bitstream_locks_.fetch_add(1);
        diag_locked_bitstreams_.fetch_add(1);
        if (!cpu_input_mode_ && !input_slot_lifecycle_[buf_idx].OnOutputLocked()) {
            api->nvEncUnlockBitstream(nvenc_session_,
                static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[buf_idx]));
            diag_bitstream_unlocks_.fetch_add(1);
            diag_locked_bitstreams_.fetch_sub(1);
            diag_drain_stage_.store(0);
            last_error_ = "NVENC output completed in an invalid input-slot state";
            std::cerr << "[RetrieveOutput] " << last_error_ << std::endl;
            return false;
        }

        if (lock_bs.bitstreamSizeInBytes > 0 && lock_bs.bitstreamBufferPtr) {
            const uint8_t* packet_data =
                static_cast<const uint8_t*>(lock_bs.bitstreamBufferPtr);
            uint32_t packet_size = lock_bs.bitstreamSizeInBytes;
            const bool     is_keyframe = (lock_bs.pictureType == NV_ENC_PIC_TYPE_IDR ||
                lock_bs.pictureType == NV_ENC_PIC_TYPE_I);

            const int64_t output_pts = static_cast<int64_t>(lock_bs.outputTimeStamp);

            if (codec_ == VideoCodec::H264) {
                const int avcc_size = AnnexBToAvcc(
                    packet_data, static_cast<int>(packet_size),
                    avcc_buf_, nals_scratch_, /*skip_spspps=*/true);
                packet_data = avcc_buf_.data();
                packet_size = avcc_size > 0
                    ? static_cast<uint32_t>(avcc_size)
                    : 0;
            }

            if (packet_size > 0 && packet_callback_) {
                diag_drain_stage_.store(7);
                if (callback_log_count_ < 3) {
                    std::cout << "[HardwareEncoder] Callback #" << callback_log_count_
                        << ": output_PTS=" << output_pts
                        << " size=" << packet_size
                        << (is_keyframe ? " [KEYFRAME]" : "")
                        << std::endl;
                }
                callback_log_count_++;

                packet_callback_(packet_data,
                    packet_size,
                    output_pts,
                    is_keyframe,
                    slot_qpc_[buf_idx]);
                diag_packets_produced_.fetch_add(1);
            }
        }

        diag_drain_stage_.store(8);
        status = api->nvEncUnlockBitstream(nvenc_session_,
            static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[buf_idx]));
        diag_last_nvenc_status_.store(static_cast<int32_t>(status));
        if (status == NV_ENC_SUCCESS) {
            diag_bitstream_unlocks_.fetch_add(1);
            diag_locked_bitstreams_.fetch_sub(1);
        }

        if (status == NV_ENC_SUCCESS && !cpu_input_mode_
            && !input_slot_lifecycle_[buf_idx].OnOutputConsumed()) {
            status = NV_ENC_ERR_INVALID_CALL;
            last_error_ = "NVENC output unlock completed in an invalid input-slot state";
            std::cerr << "[RetrieveOutput] " << last_error_ << std::endl;
        }
        diag_drain_stage_.store(0);
        return status == NV_ENC_SUCCESS;
    }


    // WaitForFreeSlot / DrainThread

    bool HardwareEncoder::WaitForFreeSlot() {
        std::unique_lock<std::mutex> lk(drain_mutex_);
        // Leave at least one output buffer untouched by submit while drain holds it.
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(500);
        const auto available = [this] {
            return pending_count_ < buffer_count_
                || drain_stop_.load(std::memory_order_acquire)
                || drain_failed_.load(std::memory_order_acquire);
        };
        while (!available()) {
            if (slot_cv_.wait_until(lk, deadline) == std::cv_status::timeout)
                break;
            if (std::chrono::steady_clock::now() >= deadline && !available())
                break;
        }
        if (!available()) {
            last_error_ = "NVENC input slot wait timed out while drain made no bounded progress";
            std::cerr << "[HardwareEncoder] " << last_error_ << std::endl;
            return false;
        }
        return !drain_stop_.load() && !drain_failed_.load();
    }

    bool HardwareEncoder::PrepareCurrentGpuInputSlot() {
        if (cpu_input_mode_) return true;

        diag_submit_stage_.store(1);
        if (!WaitForFreeSlot()) return false;

        const uint32_t idx = current_buf_idx_ % buffer_count_;
        NvencInputSlotLifecycle& lifecycle = input_slot_lifecycle_[idx];
        if (lifecycle.is_ready_to_unmap()) {
            void* mapped_input = mapped_input_resources_[idx];
            if (!mapped_input) {
                last_error_ = "NVENC completed input slot lost its mapped resource";
                std::cerr << "[PrepareCurrentGpuInputSlot] " << last_error_ << std::endl;
                return false;
            }

            diag_submit_stage_.store(10);
            NV_ENCODE_API_FUNCTION_LIST* api =
                static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);
            const NVENCSTATUS status = api->nvEncUnmapInputResource(
                nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(mapped_input));
            diag_last_nvenc_status_.store(static_cast<int32_t>(status));
            if (status != NV_ENC_SUCCESS) {
                last_error_ = std::string("NVENC input unmap failed: ")
                    + NvencStatusToString(status);
                std::cerr << "[PrepareCurrentGpuInputSlot] " << last_error_ << std::endl;
                return false;
            }
            mapped_input_resources_[idx] = nullptr;
            if (!lifecycle.OnCompletedInputUnmapped()) {
                last_error_ = "NVENC input slot rejected its completed unmap";
                std::cerr << "[PrepareCurrentGpuInputSlot] " << last_error_ << std::endl;
                return false;
            }
            diag_resources_unmapped_.fetch_add(1);
            diag_mapped_resources_.fetch_sub(1);
            diag_slots_recycled_.fetch_add(1);
        }

        if (!lifecycle.is_available()) {
            last_error_ = "NVENC next input slot is not available for capture";
            std::cerr << "[PrepareCurrentGpuInputSlot] " << last_error_ << std::endl;
            return false;
        }
        return true;
    }

    void HardwareEncoder::DrainThread() {
        // SetThreadDescription for easier profiling.
        // Runs at NORMAL priority and sleeps between non-blocking lock attempts.
        // The completion wait and lock polling have bounded policy timeouts;
        // an individual vendor API call remains synchronous and is not
        // claimed to be interruptible by this watchdog.
        while (true) {
            uint32_t idx;
            {
                std::unique_lock<std::mutex> lk(drain_mutex_);
                drain_cv_.wait(lk, [this] {
                    return !drain_queue_.empty() || drain_stop_.load();
                });
                if (drain_queue_.empty()) {
                    // woken by stop with nothing left to do
                    return;
                }
                idx = drain_queue_.front();
                drain_queue_.pop_front();
                diag_queued_outputs_.fetch_sub(1);
            }
            diag_drain_stage_.store(5);
            diag_drain_dequeues_.fetch_add(1);

            const auto drain_started = CaptureStageTiming::Clock::now();
            const bool output_ok = RetrieveOutput(idx);
            drain_timings_.RecordSince(drain_started);

            {
                std::lock_guard<std::mutex> lk(drain_mutex_);
                if (pending_count_ > 0) pending_count_--;
                if (diag_pending_resources_.load() > 0)
                    diag_pending_resources_.fetch_sub(1);
            }
            if (!output_ok) {
                drain_failed_.store(true);
            } else if (cpu_input_mode_) {
                diag_slots_recycled_.fetch_add(1);
            }
            diag_drain_stage_.store(0);
            slot_cv_.notify_all();
            if (!output_ok) return;
        }
    }


    // Finalize

    bool HardwareEncoder::InitializeGpuScalePipeline(std::string& error) {
        if (!gpu_scale_required_ || !d3d11_device_ || !d3d11_context_) {
            return true;
        }
        HRESULT result = d3d11_device_->QueryInterface(
            __uuidof(ID3D11VideoDevice),
            reinterpret_cast<void**>(&video_device_));
        if (FAILED(result) || !video_device_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11Device::QueryInterface(ID3D11VideoDevice)", result);
            ReleaseGpuScalePipeline();
            return false;
        }
        result = d3d11_context_->QueryInterface(
            __uuidof(ID3D11VideoContext),
            reinterpret_cast<void**>(&video_context_));
        if (FAILED(result) || !video_context_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11DeviceContext::QueryInterface(ID3D11VideoContext)",
                result);
            ReleaseGpuScalePipeline();
            return false;
        }

        D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
        content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        content.InputFrameRate = {fps_, 1};
        content.InputWidth = src_width_;
        content.InputHeight = src_height_;
        content.OutputFrameRate = {fps_, 1};
        content.OutputWidth = enc_width_;
        content.OutputHeight = enc_height_;
        content.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;
        result = video_device_->CreateVideoProcessorEnumerator(
            &content, &video_processor_enumerator_);
        if (FAILED(result) || !video_processor_enumerator_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoDevice::CreateVideoProcessorEnumerator(NVIDIA scaling)",
                result);
            ReleaseGpuScalePipeline();
            return false;
        }

        UINT input_support = 0;
        result = video_processor_enumerator_->CheckVideoProcessorFormat(
            DXGI_FORMAT_B8G8R8A8_UNORM, &input_support);
        if (FAILED(result)) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,input)",
                result);
            ReleaseGpuScalePipeline();
            return false;
        }
        if ((input_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT) == 0) {
            error = "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,input) reports unsupported format";
            ReleaseGpuScalePipeline();
            return false;
        }

        UINT output_support = 0;
        result = video_processor_enumerator_->CheckVideoProcessorFormat(
            DXGI_FORMAT_B8G8R8A8_UNORM, &output_support);
        if (FAILED(result)) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,output)",
                result);
            ReleaseGpuScalePipeline();
            return false;
        }
        if ((output_support & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT) == 0) {
            error = "ID3D11VideoProcessorEnumerator::CheckVideoProcessorFormat(BGRA,output) reports unsupported format";
            ReleaseGpuScalePipeline();
            return false;
        }

        result = video_device_->CreateVideoProcessor(
            video_processor_enumerator_, 0, &video_processor_);
        if (FAILED(result) || !video_processor_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoDevice::CreateVideoProcessor(NVIDIA scaling)", result);
            ReleaseGpuScalePipeline();
            return false;
        }

        D3D11_TEXTURE2D_DESC converter_description{};
        converter_description.Width = enc_width_;
        converter_description.Height = enc_height_;
        converter_description.MipLevels = 1;
        converter_description.ArraySize = 1;
        converter_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        converter_description.SampleDesc.Count = 1;
        converter_description.Usage = D3D11_USAGE_DEFAULT;
        converter_description.BindFlags = D3D11_BIND_RENDER_TARGET;
        result = d3d11_device_->CreateTexture2D(
            &converter_description, nullptr, &gpu_scale_texture_);
        if (FAILED(result) || !gpu_scale_texture_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11Device::CreateTexture2D(NVIDIA scaling converter)", result);
            ReleaseGpuScalePipeline();
            return false;
        }

        D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC output_description{};
        output_description.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
        output_description.Texture2D.MipSlice = 0;
        result = video_device_->CreateVideoProcessorOutputView(
            gpu_scale_texture_, video_processor_enumerator_,
            &output_description, &gpu_scale_output_view_);
        if (FAILED(result) || !gpu_scale_output_view_) {
            error = diagnostics::FormatHResultFailure(
                "ID3D11VideoDevice::CreateVideoProcessorOutputView(NVIDIA scaling)",
                result);
            ReleaseGpuScalePipeline();
            return false;
        }

        RECT source_rect{0, 0, static_cast<LONG>(src_width_),
            static_cast<LONG>(src_height_)};
        RECT destination_rect{
            static_cast<LONG>(gpu_scale_geometry_.destination.left),
            static_cast<LONG>(gpu_scale_geometry_.destination.top),
            static_cast<LONG>(gpu_scale_geometry_.destination.left
                + gpu_scale_geometry_.destination.width),
            static_cast<LONG>(gpu_scale_geometry_.destination.top
                + gpu_scale_geometry_.destination.height)};
        RECT target_rect{0, 0, static_cast<LONG>(enc_width_),
            static_cast<LONG>(enc_height_)};
        video_context_->VideoProcessorSetStreamFrameFormat(
            video_processor_, 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
        video_context_->VideoProcessorSetStreamSourceRect(
            video_processor_, 0, TRUE, &source_rect);
        video_context_->VideoProcessorSetStreamDestRect(
            video_processor_, 0, TRUE, &destination_rect);
        video_context_->VideoProcessorSetOutputTargetRect(
            video_processor_, TRUE, &target_rect);
        D3D11_VIDEO_COLOR background{};
        background.RGBA = {0.f, 0.f, 0.f, 1.f};
        video_context_->VideoProcessorSetOutputBackgroundColor(
            video_processor_, FALSE, &background);
        video_context_->VideoProcessorSetStreamAutoProcessingMode(
            video_processor_, 0, FALSE);

        if (cpu_input_mode_) {
            D3D11_TEXTURE2D_DESC upload = converter_description;
            upload.Width = src_width_;
            upload.Height = src_height_;
            upload.BindFlags = 0;
            result = d3d11_device_->CreateTexture2D(
                &upload, nullptr, &cpu_upload_texture_);
            if (FAILED(result) || !cpu_upload_texture_) {
                error = diagnostics::FormatHResultFailure(
                    "ID3D11Device::CreateTexture2D(hybrid NVIDIA upload)", result);
                ReleaseGpuScalePipeline();
                return false;
            }

            D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC input_description{};
            input_description.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
            result = video_device_->CreateVideoProcessorInputView(
                cpu_upload_texture_, video_processor_enumerator_,
                &input_description, &cpu_upload_view_);
            if (FAILED(result) || !cpu_upload_view_) {
                error = diagnostics::FormatHResultFailure(
                    "ID3D11VideoDevice::CreateVideoProcessorInputView(hybrid NVIDIA upload)",
                    result);
                ReleaseGpuScalePipeline();
                return false;
            }

            // NVENC may use the encoder's D3D11 context while draining output.
            ID3D11Multithread* multithread = nullptr;
            result = d3d11_context_->QueryInterface(
                __uuidof(ID3D11Multithread), reinterpret_cast<void**>(&multithread));
            if (FAILED(result) || !multithread) {
                error = diagnostics::FormatHResultFailure(
                    "ID3D11DeviceContext::QueryInterface(ID3D11Multithread)", result);
                ReleaseGpuScalePipeline();
                return false;
            }
            multithread->SetMultithreadProtected(TRUE);
            multithread->Release();

            // Both textures are full-range SDR BGRA. Keep the RGB conversion
            // to coded YUV in NVENC, as in the CPU input path.
            D3D11_VIDEO_PROCESSOR_COLOR_SPACE rgb{};
            video_context_->VideoProcessorSetStreamColorSpace(video_processor_, 0, &rgb);
            video_context_->VideoProcessorSetOutputColorSpace(video_processor_, &rgb);
        }
        return true;
    }

    void HardwareEncoder::ReleaseGpuScalePipeline() noexcept {
        if (cpu_upload_view_) cpu_upload_view_->Release();
        if (cpu_upload_texture_) cpu_upload_texture_->Release();
        if (gpu_scale_output_view_) gpu_scale_output_view_->Release();
        if (gpu_scale_texture_) gpu_scale_texture_->Release();
        if (video_processor_) video_processor_->Release();
        if (video_processor_enumerator_) video_processor_enumerator_->Release();
        if (video_context_) video_context_->Release();
        if (video_device_) video_device_->Release();
        cpu_upload_view_ = nullptr;
        cpu_upload_texture_ = nullptr;
        gpu_scale_output_view_ = nullptr;
        gpu_scale_texture_ = nullptr;
        video_processor_ = nullptr;
        video_processor_enumerator_ = nullptr;
        video_context_ = nullptr;
        video_device_ = nullptr;
        gpu_frame_prepared_ = false;
    }

    void HardwareEncoder::Finalize() {
        sws_freeContext(cpu_scale_context_);
        cpu_scale_context_ = nullptr;
        if (!initialized_) {
            ReleaseGpuScalePipeline();
            return;
        }

        std::cout << "[HardwareEncoder] Finalizing..." << std::endl;

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);

        if (nvenc_session_ && api) {
            // Submit EOS so NVENC flushes any NEED_MORE_INPUT frames still in flight.
            // Their outputs will become available to the drain thread's pending locks.
            NV_ENC_PIC_PARAMS eos = {};
            eos.version = NV_ENC_PIC_PARAMS_VER;
            eos.encodePicFlags = NV_ENC_PIC_FLAG_EOS;
            if (completion_events_ && buffer_count_ > 0)
                eos.completionEvent = completion_events_[current_buf_idx_ % buffer_count_];
            api->nvEncEncodePicture(nvenc_session_, &eos);

            // Wait for drain thread to finish every queued slot before stopping it.
            {
                std::unique_lock<std::mutex> lk(drain_mutex_);
                slot_cv_.wait(lk, [this] {
                    return (drain_queue_.empty() && pending_count_ == 0)
                        || drain_failed_.load();
                });
            }
        }

        drain_stop_.store(true);
        drain_cv_.notify_all();
        slot_cv_.notify_all();
        if (drain_thread_) {
            if (drain_thread_->joinable()) drain_thread_->join();
            delete drain_thread_;
            drain_thread_ = nullptr;
        }

        std::cout << "[HardwareEncoder] Final accounting: submit="
                  << diag_encode_successes_.load()
                  << " completion=" << diag_completion_events_.load()
                  << " lock=" << diag_bitstream_locks_.load()
                  << " unlock=" << diag_bitstream_unlocks_.load()
                  << " unmap=" << diag_resources_unmapped_.load()
                  << " recycled=" << diag_slots_recycled_.load()
                  << " mapped_now=" << diag_mapped_resources_.load()
                  << " locked_now=" << diag_locked_bitstreams_.load()
                  << " pending=" << diag_pending_resources_.load()
                  << " queued=" << diag_queued_outputs_.load()
                  << std::endl;

        avcc_buf_.clear();
        avcc_buf_.shrink_to_fit();
        nals_scratch_.clear();
        nals_scratch_.shrink_to_fit();
        extradata_.clear();

        // Free input buffers and output buffers
        if (api && nvenc_session_) {
            for (uint32_t i = 0; i < buffer_count_; i++) {
                if (completion_events_ && completion_events_[i]) {
                    NV_ENC_EVENT_PARAMS event_params = { NV_ENC_EVENT_PARAMS_VER };
                    event_params.completionEvent = completion_events_[i];
                    api->nvEncUnregisterAsyncEvent(nvenc_session_, &event_params);
                    CloseHandle(static_cast<HANDLE>(completion_events_[i]));
                    completion_events_[i] = nullptr;
                }
                if (!cpu_input_mode_) {
                    if (mapped_input_resources_ && mapped_input_resources_[i]) {
                        api->nvEncUnmapInputResource(
                            nvenc_session_,
                            static_cast<NV_ENC_INPUT_PTR>(mapped_input_resources_[i]));
                        mapped_input_resources_[i] = nullptr;
                    }
                    if (input_slot_lifecycle_)
                        input_slot_lifecycle_[i].ResetForShutdown();
                    if (registered_resources_[i])
                        api->nvEncUnregisterResource(nvenc_session_, static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[i]));
                    if (input_textures_[i])
                        input_textures_[i]->Release();
                } else {
                    if (cpu_input_buffers_[i])
                        api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[i]));
                }
                if (output_buffers_[i])
                    api->nvEncDestroyBitstreamBuffer(nvenc_session_, output_buffers_[i]);
            }
        }
        delete[] registered_resources_; registered_resources_ = nullptr;
        delete[] input_textures_;        input_textures_ = nullptr;
        delete[] mapped_input_resources_; mapped_input_resources_ = nullptr;
        delete[] input_slot_lifecycle_; input_slot_lifecycle_ = nullptr;
        delete[] cpu_input_buffers_;     cpu_input_buffers_ = nullptr;
        delete[] output_buffers_;        output_buffers_ = nullptr;
        delete[] completion_events_;      completion_events_ = nullptr;
        delete[] slot_qpc_;              slot_qpc_ = nullptr;

        if (nvenc_session_ && api) {
            api->nvEncDestroyEncoder(nvenc_session_);
            nvenc_session_ = nullptr;
        }

        // d3d11 device/context are non-owning (shared from CaptureEngine) — do NOT Release
        d3d11_device_  = nullptr;
        d3d11_context_ = nullptr;

        delete api;
        nvenc_encoder_ = nullptr;

        if (nvenc_dll_) {
            FreeLibrary(static_cast<HMODULE>(nvenc_dll_));
            nvenc_dll_ = nullptr;
        }

        ReleaseGpuScalePipeline();

        initialized_ = false;
        current_buf_idx_ = 0;
        pending_count_ = 0;
        pts_ = 0;
        last_forced_idr_pts_ = -1;
        first_frame_ = true;
        drain_stop_.store(false);
        drain_failed_.store(false);
        drain_queue_.clear();
        callback_log_count_ = 0;

        std::cout << "[HardwareEncoder] Finalized." << std::endl;
    }


} // namespace fthr

#ifdef _MSC_VER
#pragma warning(pop)
#endif
