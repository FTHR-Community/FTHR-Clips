// hardware_encoder.cpp
// FTHR Capture Engine - NVENC Hardware Encoder
//
// Encoded ring buffer update:
//   HardwareEncoder is now a pure encode-only component.
//   It no longer owns an FFmpeg muxer or writes directly to a file.
//
//   Changes from the per-clip version:
//     - Initialize() takes EncoderConfig + PacketCallback, not an output path.
//       The encoder lives for the full engine lifetime, not per clip.
//     - Step 8 now extracts SPS/PPS and stores AVCC extradata in extradata_.
//       No FFmpeg format context, no file open, no avformat_write_header.
//     - RetrieveOutput() converts Annex B -> AVCC then fires packet_callback_
//       instead of calling WritePacketToMuxer.
//     - WritePacketToMuxer removed entirely.
//     - Finalize() strips FFmpeg muxer teardown (nothing to tear down).
//     - GetExtradata() returns the stored AVCC decoder config record.

#ifdef _MSC_VER
#if __has_include("pch.h")
#include "pch.h"
#elif __has_include("stdafx.h")
#include "stdafx.h"
#endif
#endif

#include "hardware_encoder.h"
#include "video_encoder.h"

#ifdef _MSC_VER
#pragma warning(push)
#pragma warning(disable: 6011)
#endif

#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <iostream>
#include <sstream>
#include <utility>

#include "nvEncodeAPI.h"

// libswscale removed: resolution scaling is now handled by NVENC natively


namespace fthr {


    // ===========================================================================
    // NVENC Error Code to String
    // ===========================================================================

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


    // ===========================================================================
    // DLL typedef
    // ===========================================================================

    typedef NVENCSTATUS(NVENCAPI* NVENCAPICREATEINSTANCE)(NV_ENCODE_API_FUNCTION_LIST*);


    // ===========================================================================
    // Annex B / AVCC helpers (file-local)
    //
    // The drain thread calls AnnexBToAvcc once per encoded packet. The original
    // version returned a fresh std::vector every call (one heap alloc per frame
    // ≈ 60/sec). The refactored version writes into a caller-provided scratch
    // vector so the storage is reused — capacity stabilises after a few NAL
    // counts and no allocation happens in the steady state.
    // ===========================================================================

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


    // ===========================================================================
    // DetectNVENC
    // ===========================================================================

    NVENCDetectionResult DetectNVENC() {
        NVENCDetectionResult result = {};
        result.available = false;

        std::cout << "[NVENC] Detecting NVIDIA hardware encoding capability..." << std::endl;

        HMODULE nvenc_dll = LoadLibraryA("nvEncodeAPI64.dll");
        if (!nvenc_dll) {
            result.error_message = "nvEncodeAPI64.dll not found - NVIDIA driver not installed or too old";
            std::cerr << "[NVENC] " << result.error_message << std::endl;
            return result;
        }

        NVENCAPICREATEINSTANCE NvEncodeAPICreateInstance =
            (NVENCAPICREATEINSTANCE)GetProcAddress(nvenc_dll, "NvEncodeAPICreateInstance");
        if (!NvEncodeAPICreateInstance) {
            result.error_message = "NvEncodeAPICreateInstance not found in DLL";
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


    // ===========================================================================
    // HardwareEncoder - Constructor
    // ===========================================================================

    HardwareEncoder::HardwareEncoder()
        : nvenc_encoder_(nullptr)
        , nvenc_session_(nullptr)
        , nvenc_dll_(nullptr)
        , d3d11_device_(nullptr)
        , d3d11_context_(nullptr)
        , cpu_input_mode_(false)
        , input_textures_(nullptr)
        , registered_resources_(nullptr)
        , cpu_input_buffers_(nullptr)
        , output_buffers_(nullptr)
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
        , initialized_(false)
        , pts_(0)
        , first_frame_(true)
        , encode_start_qpc_(0)
        , qpc_freq_(0)
        , last_frame_qpc_(0)
        , drain_thread_(nullptr)
        , drain_stop_(false)
    {
    }

    HardwareEncoder::~HardwareEncoder() {
        Finalize();
    }


    // ===========================================================================
    // Initialize
    // ===========================================================================

    bool HardwareEncoder::Initialize(const EncoderConfig&  config,
                                     ID3D11Device*         shared_device,
                                     ID3D11DeviceContext*  shared_context,
                                     PacketCallback        callback,
                                     bool                  cpu_input_mode) {
        if (initialized_) {
            std::cerr << "[HardwareEncoder] Already initialized" << std::endl;
            Finalize();
        }

        if (!callback) {
            std::cerr << "[HardwareEncoder] PacketCallback must not be null" << std::endl;
            return false;
        }

        packet_callback_ = std::move(callback);

        src_width_ = config.src_width;
        src_height_ = config.src_height;
        enc_width_ = (config.enc_width > 0) ? config.enc_width : config.src_width;
        enc_height_ = (config.enc_height > 0) ? config.enc_height : config.src_height;
        fps_ = config.fps;
        bitrate_kbps_ = config.bitrate_kbps;
        pts_ = 0;
        first_frame_ = true;
        current_buf_idx_ = 0;
        pending_count_ = 0;

        {
            LARGE_INTEGER freq;
            QueryPerformanceFrequency(&freq);
            qpc_freq_ = freq.QuadPart;
        }

        std::cout << "[HardwareEncoder] Init: "
            << src_width_ << "x" << src_height_
            << " -> " << enc_width_ << "x" << enc_height_
            << "  " << fps_ << " fps  " << bitrate_kbps_ << " kbps" << std::endl;

        if (enc_width_ == 0 || enc_height_ == 0 || fps_ == 0 || fps_ > 360) {
            std::cerr << "[HardwareEncoder] Invalid config" << std::endl;
            return false;
        }

        // ------------------------------------------------------------------
        // Step 1: Load NVENC DLL
        // ------------------------------------------------------------------
        HMODULE nvenc_dll = LoadLibraryA("nvEncodeAPI64.dll");
        if (!nvenc_dll) {
            std::cerr << "[HardwareEncoder] nvEncodeAPI64.dll not found" << std::endl;
            return false;
        }
        nvenc_dll_ = static_cast<void*>(nvenc_dll);

        NVENCAPICREATEINSTANCE NvEncodeAPICreateInstance =
            (NVENCAPICREATEINSTANCE)GetProcAddress(nvenc_dll, "NvEncodeAPICreateInstance");
        if (!NvEncodeAPICreateInstance) {
            std::cerr << "[HardwareEncoder] NvEncodeAPICreateInstance not found" << std::endl;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }

        NV_ENCODE_API_FUNCTION_LIST* nvenc_api = new NV_ENCODE_API_FUNCTION_LIST;
        memset(nvenc_api, 0, sizeof(NV_ENCODE_API_FUNCTION_LIST));
        nvenc_api->version = NV_ENCODE_API_FUNCTION_LIST_VER;

        NVENCSTATUS status = NvEncodeAPICreateInstance(nvenc_api);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[HardwareEncoder] NvEncodeAPICreateInstance: " << NvencStatusToString(status) << std::endl;
            delete nvenc_api;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        nvenc_encoder_ = nvenc_api;

        // ------------------------------------------------------------------
        // Step 2: Use shared D3D11 device from CaptureEngine.
        //
        // CaptureEngine creates the device on the NVIDIA adapter, so both
        // DXGI Desktop Duplication and NVENC encoding share one device on the
        // same physical GPU. This eliminates the second D3D11 device and the
        // cross-device (potentially cross-PCIe) texture copy that existed before.
        // ------------------------------------------------------------------
        if (!shared_device || !shared_context) {
            std::cerr << "[HardwareEncoder] shared_device/context must not be null" << std::endl;
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        d3d11_device_   = shared_device;   // non-owning
        d3d11_context_  = shared_context;  // non-owning
        cpu_input_mode_ = cpu_input_mode;
        std::cout << "[HardwareEncoder] Using shared D3D11 device ("
                  << (cpu_input_mode ? "Optimus CPU-input path" : "GPU zero-copy path")
                  << ")" << std::endl;

        // ------------------------------------------------------------------
        // Step 4: Open NVENC session
        // ------------------------------------------------------------------
        NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS session_params = { NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER };
        session_params.device = d3d11_device_;
        session_params.deviceType = NV_ENC_DEVICE_TYPE_DIRECTX;
        session_params.apiVersion = NVENCAPI_VERSION;

        status = nvenc_api->nvEncOpenEncodeSessionEx(&session_params, &nvenc_session_);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[HardwareEncoder] nvEncOpenEncodeSessionEx: " << NvencStatusToString(status) << std::endl;
            // d3d11 device/context not owned - do not Release
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }

        // ------------------------------------------------------------------
        // Step 5: Load preset config + override rate control
        // ------------------------------------------------------------------
        NV_ENC_PRESET_CONFIG preset_config = { NV_ENC_PRESET_CONFIG_VER };
        preset_config.presetCfg.version = NV_ENC_CONFIG_VER;

        status = nvenc_api->nvEncGetEncodePresetConfigEx(
            nvenc_session_,
            NV_ENC_CODEC_H264_GUID,
            NV_ENC_PRESET_P2_GUID,
            NV_ENC_TUNING_INFO_LOW_LATENCY,
            &preset_config
        );

        NV_ENC_CONFIG encode_config = {};
        if (status == NV_ENC_SUCCESS) {
            memcpy(&encode_config, &preset_config.presetCfg, sizeof(NV_ENC_CONFIG));
            std::cout << "[HardwareEncoder] Preset P2/LOW_LATENCY loaded" << std::endl;
        }
        else {
            std::cout << "[HardwareEncoder] Preset failed (" << NvencStatusToString(status)
                << ") - using manual config" << std::endl;
            memset(&encode_config, 0, sizeof(encode_config));
            encode_config.version = NV_ENC_CONFIG_VER;
            encode_config.profileGUID = NV_ENC_H264_PROFILE_MAIN_GUID;
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

        encode_config.frameIntervalP = 1;
        // IDR every 4 seconds instead of every 1 second. Keyframes are 4-10x
        // larger than P-frames and cause brief GPU spikes at each boundary.
        // 4s intervals keep the ring buffer seekable while reducing spike frequency.
        encode_config.gopLength = static_cast<uint32_t>(fps_) * 4;

        encode_config.encodeCodecConfig.h264Config.idrPeriod = static_cast<uint32_t>(fps_) * 4;
        encode_config.encodeCodecConfig.h264Config.chromaFormatIDC = 1;
        encode_config.encodeCodecConfig.h264Config.level = NV_ENC_LEVEL_AUTOSELECT;
        encode_config.encodeCodecConfig.h264Config.enableVFR = 0;
        encode_config.encodeCodecConfig.h264Config.outputPictureTimingSEI = 0;
        encode_config.encodeCodecConfig.h264Config.outputBufferingPeriodSEI = 0;

        // Suppress timing_info in SPS via VUI parameters
        NV_ENC_CONFIG_H264_VUI_PARAMETERS vui_params = {};
        vui_params.timingInfoPresentFlag = 0;
        encode_config.encodeCodecConfig.h264Config.h264VUIParameters = vui_params;

        // ------------------------------------------------------------------
        // Step 6: Initialize encoder
        // ------------------------------------------------------------------
        NV_ENC_INITIALIZE_PARAMS init_params = {};
        memset(&init_params, 0, sizeof(init_params));
        init_params.version = NV_ENC_INITIALIZE_PARAMS_VER;
        init_params.encodeConfig = &encode_config;
        init_params.encodeGUID = NV_ENC_CODEC_H264_GUID;
        init_params.presetGUID = NV_ENC_PRESET_P2_GUID;
        init_params.encodeWidth = enc_width_;
        init_params.encodeHeight = enc_height_;
        init_params.darWidth = enc_width_;
        init_params.darHeight = enc_height_;
        init_params.frameRateNum = fps_;
        init_params.frameRateDen = 1;
        init_params.enablePTD = 1;
        init_params.tuningInfo = NV_ENC_TUNING_INFO_LOW_LATENCY;

        status = nvenc_api->nvEncInitializeEncoder(nvenc_session_, &init_params);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[HardwareEncoder] nvEncInitializeEncoder: " << NvencStatusToString(status) << std::endl;
            nvenc_api->nvEncDestroyEncoder(nvenc_session_); nvenc_session_ = nullptr;
            // d3d11 device/context not owned - do not Release
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
            return false;
        }
        std::cout << "[HardwareEncoder] Encoder initialized" << std::endl;

        // ------------------------------------------------------------------
        // Step 7: Allocate input buffer pool
        //
        // GPU zero-copy path: D3D11_USAGE_DEFAULT textures registered with NVENC.
        //   CaptureEngine calls CopyResource(texture, dxgi_frame) — pure GPU op.
        //   EncodeFrame() maps the registered resource; NVENC reads from VRAM directly.
        //
        // CPU-input (Optimus) path: system-memory NVENC input buffers.
        //   CaptureEngine maps a staging texture and calls EncodeFrameCPU() with the
        //   CPU pointer. We lock the NVENC buffer, memcpy, unlock, then submit.
        //   Still hardware H.264 — only the copy touches the CPU.
        // ------------------------------------------------------------------
        // 32 slots: enough headroom for any NVENC preset pipeline depth and
        // smooths out the bursty wakeup pattern that caused 15% CPU spikes
        // (old value of 8 caused CaptureThread to batch-submit then stall).
        buffer_count_ = 32;
        output_buffers_ = new void*[buffer_count_];
        memset(output_buffers_, 0, sizeof(void*) * buffer_count_);
        slot_qpc_ = new int64_t[buffer_count_];
        memset(slot_qpc_, 0, sizeof(int64_t) * buffer_count_);

        // Helper: clean up partially-allocated output buffers then destroy encoder/dll.
        auto cleanup_and_fail = [&](uint32_t allocated_outputs) {
            for (uint32_t j = 0; j < allocated_outputs; j++) {
                if (output_buffers_[j])
                    nvenc_api->nvEncDestroyBitstreamBuffer(nvenc_session_, output_buffers_[j]);
            }
            delete[] output_buffers_; output_buffers_ = nullptr;
            delete[] slot_qpc_; slot_qpc_ = nullptr;
            nvenc_api->nvEncDestroyEncoder(nvenc_session_); nvenc_session_ = nullptr;
            delete nvenc_api; nvenc_encoder_ = nullptr;
            FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
        };

        if (!cpu_input_mode) {
            // --- GPU zero-copy path ---
            input_textures_       = new ID3D11Texture2D*[buffer_count_];
            registered_resources_ = new void*[buffer_count_];
            memset(input_textures_,       0, sizeof(ID3D11Texture2D*) * buffer_count_);
            memset(registered_resources_, 0, sizeof(void*)            * buffer_count_);

            for (uint32_t i = 0; i < buffer_count_; i++) {
                D3D11_TEXTURE2D_DESC tex_desc = {};
                tex_desc.Width            = src_width_;
                tex_desc.Height           = src_height_;
                tex_desc.MipLevels        = 1;
                tex_desc.ArraySize        = 1;
                tex_desc.Format           = DXGI_FORMAT_B8G8R8A8_UNORM;
                tex_desc.SampleDesc.Count = 1;
                tex_desc.Usage            = D3D11_USAGE_DEFAULT;
                tex_desc.BindFlags        = 0;
                tex_desc.CPUAccessFlags   = 0;
                tex_desc.MiscFlags        = 0;

                HRESULT hr = d3d11_device_->CreateTexture2D(&tex_desc, nullptr, &input_textures_[i]);
                if (FAILED(hr)) {
                    std::cerr << "[HardwareEncoder] CreateTexture2D[" << i << "] failed: 0x"
                        << std::hex << hr << std::dec << std::endl;
                    for (uint32_t j = 0; j < i; j++) {
                        if (registered_resources_[j]) nvenc_api->nvEncUnregisterResource(nvenc_session_, static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[j]));
                        if (input_textures_[j]) input_textures_[j]->Release();
                    }
                    delete[] input_textures_; delete[] registered_resources_;
                    input_textures_ = nullptr; registered_resources_ = nullptr;
                    cleanup_and_fail(i); // 0 output buffers allocated yet
                    return false;
                }

                NV_ENC_REGISTER_RESOURCE reg = { NV_ENC_REGISTER_RESOURCE_VER };
                reg.resourceType       = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX;
                reg.resourceToRegister = input_textures_[i];
                reg.width              = src_width_;
                reg.height             = src_height_;
                reg.pitch              = 0;
                reg.bufferFormat       = NV_ENC_BUFFER_FORMAT_ARGB;
                reg.bufferUsage        = NV_ENC_INPUT_IMAGE;

                status = nvenc_api->nvEncRegisterResource(nvenc_session_, &reg);
                if (status != NV_ENC_SUCCESS) {
                    std::cerr << "[HardwareEncoder] nvEncRegisterResource[" << i << "]: "
                        << NvencStatusToString(status) << std::endl;
                    input_textures_[i]->Release(); input_textures_[i] = nullptr;
                    for (uint32_t j = 0; j < i; j++) {
                        if (registered_resources_[j]) nvenc_api->nvEncUnregisterResource(nvenc_session_, static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[j]));
                        if (input_textures_[j]) input_textures_[j]->Release();
                    }
                    delete[] input_textures_; delete[] registered_resources_;
                    input_textures_ = nullptr; registered_resources_ = nullptr;
                    cleanup_and_fail(0);
                    return false;
                }
                registered_resources_[i] = reg.registeredResource;

                NV_ENC_CREATE_BITSTREAM_BUFFER create_output = { NV_ENC_CREATE_BITSTREAM_BUFFER_VER };
                status = nvenc_api->nvEncCreateBitstreamBuffer(nvenc_session_, &create_output);
                if (status != NV_ENC_SUCCESS) {
                    std::cerr << "[HardwareEncoder] nvEncCreateBitstreamBuffer[" << i << "]: "
                        << NvencStatusToString(status) << std::endl;
                    nvenc_api->nvEncUnregisterResource(nvenc_session_, static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[i]));
                    input_textures_[i]->Release();
                    for (uint32_t j = 0; j < i; j++) {
                        if (registered_resources_[j]) nvenc_api->nvEncUnregisterResource(nvenc_session_, static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[j]));
                        if (input_textures_[j]) input_textures_[j]->Release();
                        if (output_buffers_[j]) nvenc_api->nvEncDestroyBitstreamBuffer(nvenc_session_, output_buffers_[j]);
                    }
                    delete[] input_textures_; delete[] registered_resources_; delete[] output_buffers_;
                    input_textures_ = nullptr; registered_resources_ = nullptr; output_buffers_ = nullptr;
                    nvenc_api->nvEncDestroyEncoder(nvenc_session_); nvenc_session_ = nullptr;
                    delete nvenc_api; nvenc_encoder_ = nullptr;
                    FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
                    return false;
                }
                output_buffers_[i] = create_output.bitstreamBuffer;
            }
            std::cout << "[HardwareEncoder] " << buffer_count_
                      << " GPU texture slots registered with NVENC (zero-copy path)" << std::endl;
        }
        else {
            // --- CPU-input path (Optimus) ---
            cpu_input_buffers_ = new void*[buffer_count_];
            memset(cpu_input_buffers_, 0, sizeof(void*) * buffer_count_);

            for (uint32_t i = 0; i < buffer_count_; i++) {
                NV_ENC_CREATE_INPUT_BUFFER create_input = { NV_ENC_CREATE_INPUT_BUFFER_VER };
                create_input.width     = src_width_;
                create_input.height    = src_height_;
                create_input.bufferFmt = NV_ENC_BUFFER_FORMAT_ARGB; // BGRA byte order on x86

                status = nvenc_api->nvEncCreateInputBuffer(nvenc_session_, &create_input);
                if (status != NV_ENC_SUCCESS) {
                    std::cerr << "[HardwareEncoder] nvEncCreateInputBuffer[" << i << "]: "
                        << NvencStatusToString(status) << std::endl;
                    for (uint32_t j = 0; j < i; j++) {
                        if (cpu_input_buffers_[j])
                            nvenc_api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[j]));
                    }
                    delete[] cpu_input_buffers_; cpu_input_buffers_ = nullptr;
                    cleanup_and_fail(0);
                    return false;
                }
                cpu_input_buffers_[i] = create_input.inputBuffer;

                NV_ENC_CREATE_BITSTREAM_BUFFER create_output = { NV_ENC_CREATE_BITSTREAM_BUFFER_VER };
                status = nvenc_api->nvEncCreateBitstreamBuffer(nvenc_session_, &create_output);
                if (status != NV_ENC_SUCCESS) {
                    std::cerr << "[HardwareEncoder] nvEncCreateBitstreamBuffer[" << i << "]: "
                        << NvencStatusToString(status) << std::endl;
                    nvenc_api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[i]));
                    for (uint32_t j = 0; j < i; j++) {
                        if (cpu_input_buffers_[j]) nvenc_api->nvEncDestroyInputBuffer(nvenc_session_, static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[j]));
                        if (output_buffers_[j]) nvenc_api->nvEncDestroyBitstreamBuffer(nvenc_session_, output_buffers_[j]);
                    }
                    delete[] cpu_input_buffers_; delete[] output_buffers_;
                    cpu_input_buffers_ = nullptr; output_buffers_ = nullptr;
                    nvenc_api->nvEncDestroyEncoder(nvenc_session_); nvenc_session_ = nullptr;
                    delete nvenc_api; nvenc_encoder_ = nullptr;
                    FreeLibrary(nvenc_dll); nvenc_dll_ = nullptr;
                    return false;
                }
                output_buffers_[i] = create_output.bitstreamBuffer;
            }
            std::cout << "[HardwareEncoder] " << buffer_count_
                      << " CPU input buffers allocated (Optimus path)" << std::endl;
        }

        // ------------------------------------------------------------------
        // Step 8: Extract SPS/PPS -> build AVCC extradata
        // ------------------------------------------------------------------
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
            std::cout << "[HardwareEncoder] SPS/PPS retrieved (" << spspps_size << " bytes)" << std::endl;

            const uint8_t* sps_data = nullptr; int sps_size = 0;
            const uint8_t* pps_data = nullptr; int pps_size = 0;

            std::vector<NalSpan> nals;
            ParseAnnexBNals(spspps_buf, static_cast<int>(spspps_size), nals);
            for (size_t n = 0; n < nals.size(); n++) {
                const uint8_t* ptr = nals[n].first;
                int            sz = nals[n].second;
                if (sz < 1) continue;
                uint8_t nal_type = ptr[0] & 0x1F;
                if (nal_type == 7) { sps_data = ptr; sps_size = sz; }
                if (nal_type == 8) { pps_data = ptr; pps_size = sz; }
            }

            if (sps_data && sps_size >= 4 && pps_data && pps_size >= 1) {
                extradata_ = BuildAvccExtradata(sps_data, sps_size, pps_data, pps_size);
                std::cout << "[HardwareEncoder] AVCC extradata built ("
                    << extradata_.size() << " bytes)" << std::endl;
            }
            else {
                std::cerr << "[HardwareEncoder] WARNING: SPS/PPS NAL units not found in sequence header" << std::endl;
            }
        }

        // Note: resolution downscaling (src -> enc) is now handled by NVENC natively.
        // The input textures are src_width_ x src_height_; NVENC outputs enc_width_ x enc_height_.
        // No CPU swscale needed.

        avcc_buf_.reserve(static_cast<size_t>(enc_width_) * enc_height_ * 2);

        initialized_ = true;

        // Start async drain thread: submit threads enqueue slot indices, this
        // thread pops FIFO and blocks in nvEncLockBitstream until each output
        // is ready — keeping the GPU-sync wait off the CaptureThread.
        drain_stop_.store(false);
        drain_thread_ = new std::thread(&HardwareEncoder::DrainThread, this);

        std::cout << "[HardwareEncoder] Ready (async drain enabled)." << std::endl;
        return true;
    }


    // ===========================================================================
    // GetExtradata
    // ===========================================================================

    std::vector<uint8_t> HardwareEncoder::GetExtradata() const {
        return extradata_;
    }


    // ===========================================================================
    // ComputePts (private helper)
    // ===========================================================================

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


    // ===========================================================================
    // EncodeFrame (GPU zero-copy path)
    // ===========================================================================

    bool HardwareEncoder::EncodeFrame(int64_t dxgi_present_qpc) {
        if (!initialized_) {
            std::cerr << "[EncodeFrame] Not initialized" << std::endl;
            return false;
        }

        // Backpressure: if drain hasn't caught up, wait for a slot. Normally a no-op.
        WaitForFreeSlot();

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);
        uint32_t idx = current_buf_idx_ % buffer_count_;

        pts_ = ComputePts(dxgi_present_qpc);
        slot_qpc_[idx] = last_frame_qpc_;

        NV_ENC_MAP_INPUT_RESOURCE map_res = { NV_ENC_MAP_INPUT_RESOURCE_VER };
        map_res.registeredResource = static_cast<NV_ENC_REGISTERED_PTR>(registered_resources_[idx]);

        NVENCSTATUS status = api->nvEncMapInputResource(nvenc_session_, &map_res);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[EncodeFrame] nvEncMapInputResource: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        NV_ENC_PIC_PARAMS pic = {};
        pic.version         = NV_ENC_PIC_PARAMS_VER;
        pic.inputBuffer     = map_res.mappedResource;
        pic.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[idx]);
        pic.bufferFmt       = map_res.mappedBufferFmt;
        pic.inputWidth      = src_width_;
        pic.inputHeight     = src_height_;
        pic.pictureStruct   = NV_ENC_PIC_STRUCT_FRAME;
        pic.inputTimeStamp  = static_cast<uint64_t>(pts_);

        status = api->nvEncEncodePicture(nvenc_session_, &pic);

        api->nvEncUnmapInputResource(nvenc_session_, map_res.mappedResource);

        // SUCCESS and NEED_MORE_INPUT both mean "submitted OK"; the drain thread
        // will block in nvEncLockBitstream until each slot's output is ready.
        if (status != NV_ENC_SUCCESS && status != NV_ENC_ERR_NEED_MORE_INPUT) {
            std::cerr << "[EncodeFrame] nvEncEncodePicture: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        {
            std::lock_guard<std::mutex> lk(drain_mutex_);
            drain_queue_.push_back(idx);
            pending_count_++;
        }
        drain_cv_.notify_one();

        current_buf_idx_ = (current_buf_idx_ + 1) % buffer_count_;
        return true;
    }


    // ===========================================================================
    // GetCurrentInputTexture
    // ===========================================================================

    ID3D11Texture2D* HardwareEncoder::GetCurrentInputTexture() const noexcept {
        if (!input_textures_ || !initialized_) return nullptr;
        return input_textures_[current_buf_idx_ % buffer_count_];
    }


    // ===========================================================================
    // EncodeFrameCPU (Optimus / CPU-input path)
    // ===========================================================================

    bool HardwareEncoder::EncodeFrameCPU(const uint8_t* bgra_data, uint32_t src_stride,
                                         int64_t dxgi_present_qpc) {
        if (!initialized_ || !cpu_input_mode_) {
            std::cerr << "[EncodeFrameCPU] Not initialized or not in CPU input mode" << std::endl;
            return false;
        }

        WaitForFreeSlot();

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);
        uint32_t idx = current_buf_idx_ % buffer_count_;

        pts_ = ComputePts(dxgi_present_qpc);
        slot_qpc_[idx] = last_frame_qpc_;

        // Lock the NVENC system-memory input buffer.
        NV_ENC_LOCK_INPUT_BUFFER lock_input = { NV_ENC_LOCK_INPUT_BUFFER_VER };
        lock_input.inputBuffer = static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[idx]);

        NVENCSTATUS status = api->nvEncLockInputBuffer(nvenc_session_, &lock_input);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[EncodeFrameCPU] nvEncLockInputBuffer: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        // Copy BGRA frame data. Both DXGI staging and NVENC input buffers may have
        // padding (RowPitch / pitch > width*4). Handle each independently.
        uint8_t*       dst        = static_cast<uint8_t*>(lock_input.bufferDataPtr);
        const uint32_t dst_stride = lock_input.pitch;
        const uint32_t row_bytes  = src_width_ * 4;

        if (src_stride == row_bytes && dst_stride == row_bytes) {
            memcpy(dst, bgra_data, static_cast<size_t>(row_bytes) * src_height_);
        } else {
            for (uint32_t y = 0; y < src_height_; y++) {
                memcpy(dst + static_cast<size_t>(y) * dst_stride,
                       bgra_data + static_cast<size_t>(y) * src_stride,
                       row_bytes);
            }
        }

        api->nvEncUnlockInputBuffer(nvenc_session_, lock_input.inputBuffer);

        // Submit encode.
        NV_ENC_PIC_PARAMS pic = {};
        pic.version         = NV_ENC_PIC_PARAMS_VER;
        pic.inputBuffer     = static_cast<NV_ENC_INPUT_PTR>(cpu_input_buffers_[idx]);
        pic.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[idx]);
        pic.bufferFmt       = NV_ENC_BUFFER_FORMAT_ARGB;
        pic.inputWidth      = src_width_;
        pic.inputHeight     = src_height_;
        pic.pictureStruct   = NV_ENC_PIC_STRUCT_FRAME;
        pic.inputTimeStamp  = static_cast<uint64_t>(pts_);

        status = api->nvEncEncodePicture(nvenc_session_, &pic);

        if (status != NV_ENC_SUCCESS && status != NV_ENC_ERR_NEED_MORE_INPUT) {
            std::cerr << "[EncodeFrameCPU] nvEncEncodePicture: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        {
            std::lock_guard<std::mutex> lk(drain_mutex_);
            drain_queue_.push_back(idx);
            pending_count_++;
        }
        drain_cv_.notify_one();

        current_buf_idx_ = (current_buf_idx_ + 1) % buffer_count_;
        return true;
    }


    // ===========================================================================
    // RetrieveOutput (private)
    // ===========================================================================

    bool HardwareEncoder::RetrieveOutput(uint32_t buf_idx) {
        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);

        NV_ENC_LOCK_BITSTREAM lock_bs = { NV_ENC_LOCK_BITSTREAM_VER };
        lock_bs.outputBitstream = static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[buf_idx]);
        lock_bs.doNotWait = 0;

        NVENCSTATUS status = api->nvEncLockBitstream(nvenc_session_, &lock_bs);
        if (status != NV_ENC_SUCCESS) {
            std::cerr << "[RetrieveOutput] nvEncLockBitstream: " << NvencStatusToString(status) << std::endl;
            return false;
        }

        if (lock_bs.bitstreamSizeInBytes > 0 && lock_bs.bitstreamBufferPtr) {
            const uint8_t* annexb_data = static_cast<const uint8_t*>(lock_bs.bitstreamBufferPtr);
            const uint32_t annexb_size = lock_bs.bitstreamSizeInBytes;
            const bool     is_keyframe = (lock_bs.pictureType == NV_ENC_PIC_TYPE_IDR ||
                lock_bs.pictureType == NV_ENC_PIC_TYPE_I);

            const int64_t output_pts = static_cast<int64_t>(lock_bs.outputTimeStamp);

            int avcc_size = AnnexBToAvcc(annexb_data, static_cast<int>(annexb_size),
                avcc_buf_, nals_scratch_, /*skip_spspps=*/true);

            if (avcc_size > 0 && packet_callback_) {
                static int callback_count = 0;
                if (callback_count < 3) {
                    std::cout << "[HardwareEncoder] Callback #" << callback_count
                        << ": output_PTS=" << output_pts
                        << " size=" << avcc_size
                        << (is_keyframe ? " [KEYFRAME]" : "")
                        << std::endl;
                }
                callback_count++;

                packet_callback_(avcc_buf_.data(),
                    static_cast<uint32_t>(avcc_size),
                    output_pts,
                    is_keyframe,
                    slot_qpc_[buf_idx]);
            }
        }

        api->nvEncUnlockBitstream(nvenc_session_,
            static_cast<NV_ENC_OUTPUT_PTR>(output_buffers_[buf_idx]));
        return true;
    }


    // ===========================================================================
    // WaitForFreeSlot / DrainThread
    // ===========================================================================

    void HardwareEncoder::WaitForFreeSlot() {
        std::unique_lock<std::mutex> lk(drain_mutex_);
        // Leave at least one output buffer untouched by submit while drain holds it.
        slot_cv_.wait(lk, [this] {
            return pending_count_ < buffer_count_ || drain_stop_.load();
        });
    }

    void HardwareEncoder::DrainThread() {
        // SetThreadDescription for easier profiling.
        // Runs at NORMAL priority — it spends most of its time blocked inside
        // nvEncLockBitstream (a kernel wait on the GPU), not burning CPU.
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
            }

            // RetrieveOutput blocks in nvEncLockBitstream until this slot's
            // encoded output is ready, then fires packet_callback_.
            RetrieveOutput(idx);

            {
                std::lock_guard<std::mutex> lk(drain_mutex_);
                if (pending_count_ > 0) pending_count_--;
            }
            slot_cv_.notify_all();
        }
    }


    // ===========================================================================
    // Finalize
    // ===========================================================================

    void HardwareEncoder::Finalize() {
        if (!initialized_) return;

        std::cout << "[HardwareEncoder] Finalizing..." << std::endl;

        NV_ENCODE_API_FUNCTION_LIST* api = static_cast<NV_ENCODE_API_FUNCTION_LIST*>(nvenc_encoder_);

        if (nvenc_session_ && api) {
            // Submit EOS so NVENC flushes any NEED_MORE_INPUT frames still in flight.
            // Their outputs will become available to the drain thread's pending locks.
            NV_ENC_PIC_PARAMS eos = {};
            eos.version = NV_ENC_PIC_PARAMS_VER;
            eos.encodePicFlags = NV_ENC_PIC_FLAG_EOS;
            api->nvEncEncodePicture(nvenc_session_, &eos);

            // Wait for drain thread to finish every queued slot before stopping it.
            {
                std::unique_lock<std::mutex> lk(drain_mutex_);
                slot_cv_.wait(lk, [this] {
                    return drain_queue_.empty() && pending_count_ == 0;
                });
            }
        }

        // Stop the drain thread.
        drain_stop_.store(true);
        drain_cv_.notify_all();
        slot_cv_.notify_all();
        if (drain_thread_) {
            if (drain_thread_->joinable()) drain_thread_->join();
            delete drain_thread_;
            drain_thread_ = nullptr;
        }

        avcc_buf_.clear();
        avcc_buf_.shrink_to_fit();
        nals_scratch_.clear();
        nals_scratch_.shrink_to_fit();
        extradata_.clear();

        // Free input buffers and output buffers
        if (api && nvenc_session_) {
            for (uint32_t i = 0; i < buffer_count_; i++) {
                if (!cpu_input_mode_) {
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
        delete[] cpu_input_buffers_;     cpu_input_buffers_ = nullptr;
        delete[] output_buffers_;        output_buffers_ = nullptr;
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

        initialized_ = false;
        current_buf_idx_ = 0;
        pending_count_ = 0;
        pts_ = 0;
        first_frame_ = true;
        drain_stop_.store(false);
        drain_queue_.clear();

        std::cout << "[HardwareEncoder] Finalized." << std::endl;
    }


} // namespace fthr

#ifdef _MSC_VER
#pragma warning(pop)
#endif