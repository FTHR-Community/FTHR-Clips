// FTHR Capture Engine - Implementation

// Must be defined before ANY include that pulls in windows.h (including winrt/base.h)
// to prevent the min/max macros from being defined and stomping std::min/std::max.
#ifndef NOMINMAX
#define NOMINMAX
#endif
// WGC/DXGI capture feeds a compressed replay ring; the save worker muxes
// packet snapshots. WASAPI feeds lossless float packetizers and disk audio rings.
// See capture_engine.h for backend selection and thread ownership.

// Windows Graphics Capture (WGC) includes
// Must come before other Windows headers to avoid redefinition conflicts.
// Requires C++17 (/std:c++17) and windowsapp.lib.
#pragma comment(lib, "windowsapp")

#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Metadata.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <Windows.Graphics.Capture.Interop.h>

#include "capture_engine.h"
#include "audio_source_handler.h"
#include "capture_focus_policy.h"
#include "wgc_frame_lease.h"
#include "wgc_cadence_wait.h"
#include "wgc_readback_queue.h"
#include "wgc_update_interval_policy.h"
#include "wgc_window_lifecycle.h"
#include "hardware_encoder.h"
#include "encoded_video_config_ffmpeg.h"
#include "video_encoder.h"
#include "save_clip_task.h"
#include "frame_pacing_trace.h"
#include "shared_memory.h"
#include "audio_capture.h"
#include "audio_ring_buffer.h"
#include "audio_encoder.h"
#include "clip_audio_manifest.h"
#include "transactional_save.h"
#include "windows_capture_border_policy.h"
#include "windows_dxgi_recovery.h"
#include "windows_native_error.h"
#include "replay_interval.h"
#include "replay_health.h"
#include "encoded_replay_health.h"
#include "replay_save_diagnostics.h"
#include "capture_pipeline_health.h"
#include "frame_rate_scheduler.h"
#include "capture_scale_geometry.h"
#include "bgra_frame_scaler.h"
#include "hdr_frame_converter.h"
#include <dxgi1_6.h>
#include <iostream>
#include <chrono>
#include <cstring>
#include <cwctype>
#include <algorithm>
#include <cmath>
#include <numeric>
#include <iomanip>
#include <sstream>
#include <filesystem>
#include <limits>
#include <new>
#include <type_traits>
#include <utility>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/dict.h>
#include <libavutil/mathematics.h>
#include <libavutil/mem.h>
#include <libavutil/opt.h>
#include <libavutil/imgutils.h>
}


namespace fthr {

    namespace {

    struct SaveSelectionTrace {
        bool enabled;
        uint32_t task_id;
        int64_t request_qpc;
        int64_t replay_target_qpc = 0;
        int64_t publication_begin_qpc = 0;
        int64_t publication_end_qpc = 0;
        int64_t storage_lease_end_qpc = 0;
        int64_t snapshot_begin_qpc = 0;
        int64_t snapshot_end_qpc = 0;
        uint64_t snapshot_packets = 0;
        uint32_t snapshot_attempts = 0;
        EncodedSnapshotLockTiming lock_timing;
        bool publication_reached = false;
        bool queued = false;

        ~SaveSelectionTrace() {
            if (!enabled) return;
            frame_pacing_trace::Emit("native_save_selection", task_id,
                frame_pacing_trace::Qpc(), [this](std::ostream& line) {
                    line << ",\"request_qpc\":" << request_qpc
                         << ",\"replay_target_qpc\":" << replay_target_qpc
                         << ",\"publication_begin_qpc\":" << publication_begin_qpc
                         << ",\"publication_end_qpc\":" << publication_end_qpc
                         << ",\"publication_reached\":" << (publication_reached ? "true" : "false")
                         << ",\"storage_lease_end_qpc\":" << storage_lease_end_qpc
                         << ",\"snapshot_begin_qpc\":" << snapshot_begin_qpc
                         << ",\"snapshot_end_qpc\":" << snapshot_end_qpc
                         << ",\"snapshot_packets\":" << snapshot_packets
                         << ",\"snapshot_attempts\":" << snapshot_attempts
                         << ",\"ring_lock_acquisitions\":" << lock_timing.acquisitions
                         << ",\"ring_lock_wait_ns\":" << lock_timing.wait_ns
                         << ",\"ring_lock_hold_ns\":" << lock_timing.hold_ns
                         << ",\"queued\":" << (queued ? "true" : "false");
                });
        }
    };

    struct SavePacketTiming {
        uint64_t packets = 0;
        uint64_t bytes = 0;
        int64_t allocation_ticks = 0;
        int64_t read_copy_ticks = 0;
        int64_t write_ticks = 0;

        void Fields(std::ostream& line, const char* stream) const {
            line << ",\"" << stream << "\":{\"packets\":" << packets
                 << ",\"bytes\":" << bytes
                 << ",\"allocation_ticks\":" << allocation_ticks
                 << ",\"read_copy_ticks\":" << read_copy_ticks
                 << ",\"write_ticks\":" << write_ticks << "}";
        }
    };

    }  // namespace

    struct CaptureEngine::SaveWorkerTiming {
        int64_t worker_begin_qpc = 0;
        int64_t mux_begin_qpc = 0;
        int64_t mux_end_qpc = 0;
        int64_t packet_loop_begin_qpc = 0;
        int64_t packet_loop_end_qpc = 0;
        int64_t trailer_begin_qpc = 0;
        int64_t trailer_end_qpc = 0;
        int64_t close_begin_qpc = 0;
        int64_t close_end_qpc = 0;
        int64_t writer_complete_qpc = 0;
        int64_t transaction_end_qpc = 0;
        SavePacketTiming video;
        SavePacketTiming audio;

        void Emit(const SaveClipTask& task, bool committed, int64_t response_qpc) const {
            frame_pacing_trace::Emit("native_save_worker", task.task_id,
                frame_pacing_trace::Qpc(), [&](std::ostream& line) {
                    line << ",\"queue_enqueued_qpc\":" << task.trace_enqueued_qpc
                         << ",\"queue_dequeued_qpc\":" << task.trace_dequeued_qpc
                         << ",\"worker_begin_qpc\":" << worker_begin_qpc
                         << ",\"mux_begin_qpc\":" << mux_begin_qpc
                         << ",\"mux_end_qpc\":" << mux_end_qpc
                         << ",\"packet_loop_begin_qpc\":" << packet_loop_begin_qpc
                         << ",\"packet_loop_end_qpc\":" << packet_loop_end_qpc
                         << ",\"trailer_begin_qpc\":" << trailer_begin_qpc
                         << ",\"trailer_end_qpc\":" << trailer_end_qpc
                         << ",\"close_begin_qpc\":" << close_begin_qpc
                         << ",\"close_end_qpc\":" << close_end_qpc
                         << ",\"writer_complete_qpc\":" << writer_complete_qpc
                         << ",\"transaction_end_qpc\":" << transaction_end_qpc
                         << ",\"base_commit_qpc\":" << (committed ? transaction_end_qpc : 0)
                         << ",\"clip_saved_response_qpc\":" << response_qpc
                         << ",\"committed\":" << (committed ? "true" : "false");
                    video.Fields(line, "video");
                    audio.Fields(line, "audio");
                });
        }
    };

    // WGCState — WinRT types confined here so the header stays WinRT-free

    struct CaptureEngine::WGCState {
        winrt::Windows::Graphics::Capture::GraphicsCaptureItem          item{ nullptr };
        winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool   frame_pool{ nullptr };
        winrt::Windows::Graphics::Capture::GraphicsCaptureSession        session{ nullptr };
        winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice  winrt_device{ nullptr };
        winrt::event_token                                                item_closed_token{};
        bool                                                              item_closed_registered = false;
        bool                                                              monitor_item = false;
        std::shared_ptr<wgc::ItemClosedSignal>                            close_signal;
        uint32_t                                                          pool_width = 0;
        uint32_t                                                          pool_height = 0;
        int32_t                                                           last_client_width = 0;
        int32_t                                                           last_client_height = 0;
        bool                                                              initial_owned_image_pending = false;
        wgc::OwnedCanvasNormalizer                                         owned_canvas;
        std::unique_ptr<HdrFrameConverter> hdr_canvas;
        std::unique_ptr<HdrFrameConverter> color_converter;
        winrt::com_ptr<ID3D11Texture2D> hdr_staging;

        // Both slots may straddle a mode change. Decide protected geometry
        // once per tick using the newest available frame.
        auto NextFrame(bool newest) {
            auto frame = frame_pool.TryGetNextFrame();
            if (newest && frame) {
                if (auto next = frame_pool.TryGetNextFrame()) {
                    frame.Close();
                    return next;
                }
            }
            return frame;
        }

        ID3D11Texture2D* CanvasTexture() const {
            return hdr_canvas ? hdr_canvas->Texture() : owned_canvas.Texture();
        }

        bool CopyToCanvas(ID3D11DeviceContext* context, ID3D11Texture2D* source,
                          uint32_t content_width, uint32_t content_height,
                          bool fit, std::string& error) {
            if (!hdr_canvas) {
                return owned_canvas.Copy(context, source, content_width, content_height,
                                         pool_width, pool_height, fit, error);
            }
            D3D11_TEXTURE2D_DESC canvas{};
            hdr_canvas->Texture()->GetDesc(&canvas);
            const HRESULT hr = hdr_canvas->Convert(context, source,
                {0, 0, content_width, content_height},
                {0, 0, canvas.Width, canvas.Height}, 1.0f);
            if (SUCCEEDED(hr)) return true;
            error = diagnostics::FormatHResultFailure("scRGB canvas copy", hr);
            return false;
        }
    };

    struct CaptureEngine::MinecraftJavaState {
        // Shared with FTHRGameSource. The producer publishes only complete
        // frames, and either process may drop a frame while the slot is busy.
        struct Header {
            volatile LONG state;
            volatile LONG error;
            volatile LONG stop;
            LONG reserved;
        };
        static_assert(sizeof(Header) == 16);
        static constexpr LONG kEmpty = 0;
        static constexpr LONG kReady = 2;
        static constexpr LONG kReading = 3;

        winrt::handle mapping;
        winrt::handle frame_event;
        winrt::handle process;
        Header* header = nullptr;
        winrt::com_ptr<ID3D11Texture2D> upload;

        ~MinecraftJavaState() {
            if (process && WaitForSingleObject(process.get(), 0) == WAIT_TIMEOUT) {
                InterlockedExchange(&header->stop, 1);
                if (WaitForSingleObject(process.get(), 750) == WAIT_TIMEOUT) {
                    TerminateProcess(process.get(), ERROR_CANCELLED);
                    WaitForSingleObject(process.get(), 250);
                }
            }
            if (header) UnmapViewOfFile(header);
        }

        bool Start(HWND hwnd, uint32_t width, uint32_t height, uint32_t fps,
                   bool cursor, std::string& error) {
            const auto fail = [&error](const char* operation) {
                error = std::string(operation) + " failed (Windows error "
                    + std::to_string(GetLastError()) + ").";
                return false;
            };
            std::wstring exe_path(32768, L'\0');
            const DWORD length = GetModuleFileNameW(nullptr, exe_path.data(),
                                                     static_cast<DWORD>(exe_path.size()));
            if (!length || length == exe_path.size()) return fail("GetModuleFileNameW");
            exe_path.resize(length);
            const auto helper_path = std::filesystem::path(exe_path).parent_path()
                / L"game-source" / L"FTHRGameSource.exe";
            if (GetFileAttributesW(helper_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
                error = "The Minecraft Java Game Capture component is missing. "
                        "Repair or reinstall the FTHR Clips package.";
                return false;
            }

            const auto bytes = sizeof(Header) + static_cast<uint64_t>(width) * height * 4;
            SECURITY_ATTRIBUTES inherited{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
            mapping.attach(CreateFileMappingW(INVALID_HANDLE_VALUE, &inherited,
                PAGE_READWRITE, static_cast<DWORD>(bytes >> 32),
                static_cast<DWORD>(bytes), nullptr));
            if (!mapping) return fail("CreateFileMappingW");
            frame_event.attach(CreateEventW(&inherited, FALSE, FALSE, nullptr));
            if (!frame_event) return fail("CreateEventW");
            winrt::handle parent;
            parent.attach(OpenProcess(SYNCHRONIZE, TRUE, GetCurrentProcessId()));
            if (!parent) return fail("OpenProcess");
            header = static_cast<Header*>(MapViewOfFile(mapping.get(), FILE_MAP_WRITE, 0, 0, 0));
            if (!header) return fail("MapViewOfFile");

            SIZE_T list_bytes = 0;
            InitializeProcThreadAttributeList(nullptr, 1, 0, &list_bytes);
            if (!list_bytes) return fail("InitializeProcThreadAttributeList sizing");
            std::vector<uintptr_t> list_storage((list_bytes + sizeof(uintptr_t) - 1)
                / sizeof(uintptr_t));
            auto* list = reinterpret_cast<PPROC_THREAD_ATTRIBUTE_LIST>(list_storage.data());
            if (!InitializeProcThreadAttributeList(list, 1, 0, &list_bytes)) {
                return fail("InitializeProcThreadAttributeList");
            }
            const std::unique_ptr<std::remove_pointer_t<PPROC_THREAD_ATTRIBUTE_LIST>,
                decltype(&DeleteProcThreadAttributeList)> attributes(
                    list, &DeleteProcThreadAttributeList);
            HANDLE handles[] = {mapping.get(), frame_event.get(), parent.get()};
            if (!UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                           handles, sizeof(handles), nullptr, nullptr)) {
                return fail("UpdateProcThreadAttribute");
            }

            std::wostringstream command;
            command << L'"' << helper_path.native() << L"\" "
                    << reinterpret_cast<uintptr_t>(hwnd) << L' '
                    << reinterpret_cast<uintptr_t>(mapping.get()) << L' '
                    << reinterpret_cast<uintptr_t>(frame_event.get()) << L' '
                    << reinterpret_cast<uintptr_t>(parent.get()) << L' '
                    << width << L' ' << height << L' ' << fps << L' ' << (cursor ? 1 : 0);
            auto arguments = command.str();
            STARTUPINFOEXW startup{};
            startup.StartupInfo.cb = sizeof(startup);
            startup.lpAttributeList = list;
            PROCESS_INFORMATION child{};
            if (!CreateProcessW(helper_path.c_str(), arguments.data(), nullptr, nullptr,
                    TRUE, EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW, nullptr,
                    helper_path.parent_path().c_str(), &startup.StartupInfo, &child)) {
                return fail("CreateProcessW for Minecraft Java Game Capture");
            }
            process.attach(child.hProcess);
            CloseHandle(child.hThread);
            return true;
        }

        bool Ready() const {
            return InterlockedCompareExchange(&header->state, kReady, kReady) == kReady;
        }

        const uint8_t* TakeFrame() const {
            if (InterlockedCompareExchange(&header->state, kReading, kReady) != kReady) {
                return nullptr;
            }
            return reinterpret_cast<const uint8_t*>(header + 1);
        }

        void ReleaseFrame() const {
            InterlockedExchange(&header->state, kEmpty);
        }

        std::string Failure() const {
            DWORD exit_code = 0;
            if (WaitForSingleObject(process.get(), 0) == WAIT_OBJECT_0
                && GetExitCodeProcess(process.get(), &exit_code)) {
                return "The Minecraft Java Game Capture component exited (code "
                    + std::to_string(exit_code) + ").";
            }
            if (InterlockedCompareExchange(&header->error, 0, 0) != 0) {
                return "The Minecraft Java Game Capture component reported a source failure.";
            }
            return {};
        }
    };

    namespace {
        using WgcTimeSpan = winrt::Windows::Foundation::TimeSpan;

        template<class Operation>
        int32_t InvokeOptionalWgcIntervalApi(Operation operation) noexcept {
            try {
                operation();
                return S_OK;
            } catch (winrt::hresult_error const& error) {
                return error.code().value;
            } catch (...) {
                return E_FAIL;
            }
        }

        // This overload is present only when the build SDK projects the API.
        // The availability check still gates access on older Windows versions.
        template<class Session>
        auto TryConfigureWgcUpdateInterval(const Session& session, uint32_t fps, int)
            -> decltype(session.MinUpdateInterval(WgcTimeSpan{}),
                        session.MinUpdateInterval(), wgc::UpdateIntervalResult{}) {
            return wgc::ConfigureUpdateInterval(fps,
                [](bool& present) {
                    return InvokeOptionalWgcIntervalApi([&] {
                        using winrt::Windows::Foundation::Metadata::ApiInformation;
                        present = ApiInformation::IsPropertyPresent(
                            L"Windows.Graphics.Capture.GraphicsCaptureSession", L"MinUpdateInterval");
                    });
                },
                [&](int64_t& interval) {
                    return InvokeOptionalWgcIntervalApi([&] {
                        interval = session.MinUpdateInterval().count();
                    });
                },
                [&](int64_t interval) {
                    return InvokeOptionalWgcIntervalApi([&] {
                        session.MinUpdateInterval(WgcTimeSpan{interval});
                    });
                });
        }

        template<class Session>
        wgc::UpdateIntervalResult TryConfigureWgcUpdateInterval(const Session&, uint32_t fps, long) {
            wgc::UpdateIntervalResult result;
            if (wgc::ValidUpdateIntervalFps(fps)) {
                result.requested_100ns = wgc::kUnthrottledUpdateInterval100ns;
                result.status = wgc::UpdateIntervalStatus::SdkUnavailable;
            }
            return result;
        }

        void ConfigureWgcSessionCadence(
            const winrt::Windows::Graphics::Capture::GraphicsCaptureSession& session,
            uint32_t fps, const char* target) {
            const auto result = TryConfigureWgcUpdateInterval(session, fps, 0);
            std::ostringstream event;
            event << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"wgc_update_interval\",\"target\":\"" << target
                  << "\",\"status\":\"" << wgc::UpdateIntervalStatusName(result.status)
                  << "\",\"target_fps\":" << fps
                  << ",\"requested_100ns\":" << result.requested_100ns
                  << ",\"default_100ns\":" << result.default_100ns
                  << ",\"effective_100ns\":" << result.effective_100ns
                  << ",\"hresult\":" << result.hresult << '}';
            std::cout << event.str() << std::endl;
        }

        auto WgcPixelFormat(bool hdr) {
            using PixelFormat = winrt::Windows::Graphics::DirectX::DirectXPixelFormat;
            return hdr ? PixelFormat::R16G16B16A16Float
                       : PixelFormat::B8G8R8A8UIntNormalized;
        }

        std::optional<bool> QueryHdrDisplay(const monitor::MonitorTopologyEntry& monitor,
                                            IDXGIAdapter1* adapter = nullptr) {
            winrt::com_ptr<IDXGIFactory1> factory;
            winrt::com_ptr<IDXGIAdapter1> owned_adapter;
            if (!adapter) {
                std::string detail;
                if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(factory.put())))
                    || !monitor::OpenSelectedDxgiAdapter(
                        factory.get(), monitor, owned_adapter.put(), detail))
                    return std::nullopt;
                adapter = owned_adapter.get();
            }
            for (UINT i = 0; ; ++i) {
                winrt::com_ptr<IDXGIOutput> output;
                if (FAILED(adapter->EnumOutputs(i, output.put()))) return std::nullopt;
                DXGI_OUTPUT_DESC desc{};
                if (FAILED(output->GetDesc(&desc))
                    || reinterpret_cast<uintptr_t>(desc.Monitor) != monitor.hmonitor)
                    continue;
                winrt::com_ptr<IDXGIOutput6> output6;
                DXGI_OUTPUT_DESC1 desc1{};
                if (FAILED(output->QueryInterface(IID_PPV_ARGS(output6.put())))
                    || FAILED(output6->GetDesc1(&desc1))) return std::nullopt;
                return desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
            }
        }

        float QuerySdrReferenceWhite(const monitor::MonitorTopologyEntry& monitor) {
            if (!monitor.target_adapter_luid_available) return 1.0f;
            DISPLAYCONFIG_SDR_WHITE_LEVEL white{};
            white.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                sizeof(white), {monitor.target_adapter_luid.low_part,
                    monitor.target_adapter_luid.high_part},
                monitor.target_id};
            if (DisplayConfigGetDeviceInfo(&white.header) != ERROR_SUCCESS
                || !white.SDRWhiteLevel) return 1.0f;
            return static_cast<float>(white.SDRWhiteLevel) / 1000.0f;
        }

        std::string AdapterLuidJson(const monitor::AdapterLuid& luid) {
            std::ostringstream value;
            value << "{\"high_part\":" << luid.high_part
                  << ",\"low_part\":" << luid.low_part << '}';
            return value.str();
        }

        std::string AudioSignalMetricsJson(
            const AudioSignalMetricsSnapshot& signal) {
            std::ostringstream value;
            value << "{\"chunks\":" << signal.chunks
                  << ",\"frames\":" << signal.frames
                  << ",\"samples\":" << signal.samples
                  << ",\"finite_samples\":" << signal.finite_samples
                  << ",\"channels\":" << signal.channels
                  << ",\"peak\":" << signal.peak
                  << ",\"rms\":" << signal.rms
                  << ",\"dc_offset\":" << signal.dc_offset
                  << ",\"clipping_samples\":" << signal.clipping_samples
                  << ",\"clipping_ratio\":" << signal.clipping_ratio
                  << ",\"nan_samples\":" << signal.nan_samples
                  << ",\"inf_samples\":" << signal.inf_samples
                  << ",\"near_silence_samples\":"
                  << signal.near_silence_samples
                  << ",\"near_silence_ratio\":" << signal.near_silence_ratio
                  << ",\"channel_rms_left\":" << signal.channel_rms[0]
                  << ",\"channel_rms_right\":" << signal.channel_rms[1]
                  << '}';
            return value.str();
        }

        std::string AudioCorrectionDistributionJson(
            const AudioCorrectionDistributionSnapshot& correction) {
            std::ostringstream value;
            value << "{\"event_count\":" << correction.event_count
                  << ",\"total_magnitude_frames\":"
                  << correction.total_magnitude_frames
                  << ",\"signed_total_frames\":"
                  << correction.signed_total_frames
                  << ",\"max_single_frames\":"
                  << correction.max_single_frames
                  << ",\"buckets\":{\"1\":" << correction.buckets[0]
                  << ",\"2_4\":" << correction.buckets[1]
                  << ",\"5_16\":" << correction.buckets[2]
                  << ",\"17_64\":" << correction.buckets[3]
                  << ",\"65_256\":" << correction.buckets[4]
                  << ",\"257_plus\":" << correction.buckets[5]
                  << "}}";
            return value.str();
        }

        std::string AudioCorrectionLargestJson(
            const AudioCorrectionLargest& largest) {
            std::ostringstream value;
            value << "{\"present\":" << (largest.present ? "true" : "false")
                  << ",\"correction_frames\":" << largest.correction_frames
                  << ",\"correction_qpc_100ns\":"
                  << largest.correction_qpc_100ns
                  << ",\"clip_relative_time_ms\":"
                  << std::setprecision(12) << largest.clip_relative_time_ms << '}';
            return value.str();
        }

        std::string AudioCorrectionClipSummaryJson(
            uint32_t task_id, const AudioCorrectionClipSummary& summary) {
            std::ostringstream value;
            value << std::setprecision(12)
                  << "{\"subsystem\":\"audio\","
                  << "\"event\":\"continuity_corrections_in_clip\","
                  << "\"state\":\"OBSERVED\","
                  << "\"task_id\":" << task_id << ','
                  << "\"selected_window_start_qpc_100ns\":"
                  << summary.selected_start_qpc_100ns
                  << ",\"selected_window_end_qpc_100ns\":"
                  << summary.selected_end_qpc_100ns
                  << ",\"system_corrections_inside_clip\":"
                  << summary.system_corrections_inside_clip
                  << ",\"mic_corrections_inside_clip\":"
                  << summary.mic_corrections_inside_clip
                  << ",\"system_inserted_frames_inside_clip\":"
                  << summary.system_inserted_frames_inside_clip
                  << ",\"system_skipped_frames_inside_clip\":"
                  << summary.system_skipped_frames_inside_clip
                  << ",\"mic_inserted_frames_inside_clip\":"
                  << summary.mic_inserted_frames_inside_clip
                  << ",\"mic_skipped_frames_inside_clip\":"
                  << summary.mic_skipped_frames_inside_clip
                  << ",\"largest_system_correction_inside_clip\":"
                  << AudioCorrectionLargestJson(
                         summary.largest_system_correction_inside_clip)
                  << ",\"largest_mic_correction_inside_clip\":"
                  << AudioCorrectionLargestJson(
                         summary.largest_mic_correction_inside_clip)
                  << ",\"maximum_correction_density_250ms_event_count\":"
                  << summary.maximum_density_250ms.event_count
                  << ",\"maximum_correction_density_250ms_corrected_frames\":"
                  << summary.maximum_density_250ms.corrected_frames
                  << ",\"maximum_correction_density_1s_event_count\":"
                  << summary.maximum_density_1s.event_count
                  << ",\"maximum_correction_density_1s_corrected_frames\":"
                  << summary.maximum_density_1s.corrected_frames
                  << ",\"system_events_overwritten\":"
                  << summary.system_events_overwritten
                  << ",\"mic_events_overwritten\":"
                  << summary.mic_events_overwritten
                  << ",\"events\":[";
            for (size_t index = 0; index < summary.events.size(); ++index) {
                if (index != 0) value << ',';
                const auto& event = summary.events[index];
                value << "{\"source\":\""
                      << AudioCorrectionSourceName(event.source)
                      << "\",\"direction\":\""
                      << AudioCorrectionDirectionName(event.direction)
                      << "\",\"correction_qpc_100ns\":"
                      << event.correction_qpc_100ns
                      << ",\"packet_qpc_100ns\":" << event.packet_qpc_100ns
                      << ",\"expected_qpc_100ns\":"
                      << event.expected_qpc_100ns
                      << ",\"correction_frames\":"
                      << event.correction_frames
                      << ",\"correction_ms\":" << event.correction_ms
                      << ",\"cumulative_signed_correction_frames\":"
                      << event.cumulative_signed_correction_frames
                      << ",\"time_since_previous_correction_ms\":"
                      << event.time_since_previous_correction_ms
                      << ",\"swr_compensation_active_or_recent\":"
                      << (event.swr_compensation_active_or_recent
                          ? "true" : "false")
                      << ",\"clip_relative_time_ms\":"
                      << event.clip_relative_time_ms << '}';
            }
            value << "]}";
            return value.str();
        }

        bool QueryD3D11DeviceAdapterLuid(
            ID3D11Device* device,
            monitor::AdapterLuid& luid,
            std::string& diagnostic) {
            if (!device) {
                diagnostic = "D3D11 device is null";
                return false;
            }
            IDXGIDevice* dxgi_device = nullptr;
            HRESULT hr = device->QueryInterface(
                __uuidof(IDXGIDevice),
                reinterpret_cast<void**>(&dxgi_device));
            if (FAILED(hr) || !dxgi_device) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "ID3D11Device::QueryInterface(IDXGIDevice)", hr);
                return false;
            }
            IDXGIAdapter* adapter = nullptr;
            hr = dxgi_device->GetAdapter(&adapter);
            dxgi_device->Release();
            if (FAILED(hr) || !adapter) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIDevice::GetAdapter", hr);
                return false;
            }
            DXGI_ADAPTER_DESC description{};
            hr = adapter->GetDesc(&description);
            adapter->Release();
            if (FAILED(hr)) {
                diagnostic = diagnostics::FormatHResultFailure(
                    "IDXGIAdapter::GetDesc", hr);
                return false;
            }
            luid = {description.AdapterLuid.LowPart,
                    description.AdapterLuid.HighPart};
            return true;
        }

    }  // namespace


    static std::wstring QueryWindowExecutableName(HWND hwnd) {
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        if (!pid) return {};

        winrt::handle process;
        process.attach(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
        if (!process) return {};
        std::wstring path(32768, L'\0');
        DWORD length = static_cast<DWORD>(path.size());
        if (!QueryFullProcessImageNameW(process.get(), 0, path.data(), &length)) return {};
        path.resize(length);

        auto exe = path.substr(path.find_last_of(L'\\') + 1);
        std::transform(exe.begin(), exe.end(), exe.begin(),
                       [](wchar_t c) { return static_cast<wchar_t>(towlower(c)); });
        return exe;
    }

    static bool IsMinecraftJavaWindow(HWND hwnd, const std::wstring& executable) {
        if (!hwnd || GetAncestor(hwnd, GA_ROOT) != hwnd
            || (executable != L"javaw.exe" && executable != L"java.exe")) return false;
        wchar_t class_name[64]{};
        if (!GetClassNameW(hwnd, class_name, ARRAYSIZE(class_name))
            || (_wcsicmp(class_name, L"GLFW30") != 0
                && _wcsicmp(class_name, L"LWJGL") != 0
                && _wcsicmp(class_name, L"SDL_app") != 0)) return false;
        wchar_t title[128]{};
        if (!GetWindowTextW(hwnd, title, ARRAYSIZE(title))) return false;
        constexpr wchar_t name[] = L"Minecraft";
        constexpr auto length = ARRAYSIZE(name) - 1;
        return _wcsnicmp(title, name, length) == 0
            && (title[length] == L'\0' || title[length] == L'*'
                || iswspace(title[length]));
    }

    static std::pair<uint32_t, uint32_t> MinecraftJavaClientSize(HWND hwnd) {
        RECT client{};
        if (!GetClientRect(hwnd, &client)) return {};
        const auto width = client.right - client.left;
        const auto height = client.bottom - client.top;
        if (width < 4 || height < 2 || width > D3D11_REQ_TEXTURE2D_U_OR_V_DIMENSION
            || height > D3D11_REQ_TEXTURE2D_U_OR_V_DIMENSION) return {};
        // libobs aligns output width to four pixels and height to two pixels.
        return {static_cast<uint32_t>(width) & ~3u, static_cast<uint32_t>(height) & ~1u};
    }

    // Use executable basenames to select monitor capture for known protected games.
    static bool IsAntiCheatProtected(const std::wstring& exe) {
        // Known kernel-AC titles. All are matched lowercase.
        static const wchar_t* kProtected[] = {
            L"valorant.exe",                       // Vanguard
            L"valorant-win64-shipping.exe",        // Vanguard (UE shipping name)
            L"r5apex.exe",                         // Apex Legends — EAC
            L"fortniteclient-win64-shipping.exe",  // Fortnite — EAC
            L"rainbowsix.exe",                     // R6 Siege — BattlEye
            L"rainbowsix_be.exe",                  // R6 Siege — BattlEye launcher
            L"escapefromtarkov.exe",               // EFT — BattlEye
            L"destiny2.exe",                       // Destiny 2 — BattlEye
            L"tslgame.exe",                        // PUBG — BattlEye
            L"thefinals.exe",                      // The Finals — EAC
            L"deltaforceclient-win64-shipping.exe",// Delta Force — AC
        };
        for (const wchar_t* name : kProtected) {
            if (exe == name) return true;
        }
        return false;
    }



    void FramePool::Allocate(size_t frame_count, size_t bytes_per_frame) {
        frame_count_ = frame_count;
        bytes_per_frame_ = bytes_per_frame;
        storage_.resize(frame_count * bytes_per_frame);
        std::cout << "[FramePool] Allocated " << frame_count << " slots x "
            << bytes_per_frame << " bytes = "
            << (storage_.size() / 1024 / 1024) << " MB" << std::endl;
    }

    uint8_t* FramePool::GetSlot(size_t index) noexcept {
        return storage_.data() + (index * bytes_per_frame_);
    }


    // Constructor / Destructor

    CaptureEngine::CaptureEngine()
        : capture_thread_(nullptr)
        , stall_watchdog_thread_(nullptr)
        , save_clip_thread_(nullptr)
        , running_(false)
        , is_recording_(false)
        , device_(nullptr)
        , context_(nullptr)
        , duplication_(nullptr)
        , staging_texture_(nullptr)
        , crop_texture_(nullptr)
        , nvenc_device_(nullptr)
        , nvenc_context_(nullptr)
        , wgc_active_(false)
        , width_(0)
        , height_(0)
        , crop_enabled_(false)
        , crop_x_(0)
        , crop_y_(0)
        , crop_width_(0)
        , crop_height_(0)
        , target_hwnd_(0)
        , fps_(60)
        , buffer_seconds_(30)
        , target_width_(0)
        , target_height_(0)
        , bitrate_kbps_(16000)
        , scaling_mode_(0)
        , separate_audio_enabled_(false)
        , monitor_resolver_(monitor_topology_source_)
        , nvenc_active_(false)
        , nvidia_device_(false)
        , replay_encoder_cpu_input_(false)
        , capture_adapter_vendor_(EncoderVendor::Software)
        , max_frames_(0)
        , frames_captured_(0)
        , frames_dropped_(0)
        , audio_active_(false)
    {
    }

    CaptureEngine::~CaptureEngine() {
        Shutdown();
    }

    void CaptureEngine::SetCaptureFailure(std::string detail) {
        last_capture_failure_detail_ = std::move(detail);
    }

    void CaptureEngine::InvalidateDxgiReplayCache() noexcept {
        dxgi_replay_cadence_.Invalidate();
        dxgi_replay_cache_armed_.store(false, std::memory_order_release);
        // A cache/presentation boundary does not necessarily replace the
        // duplication generation. Preserve a valid pointer shape and position
        // across seedless cadence observations; real generation boundaries
        // invalidate the cursor explicitly at their capture-thread call site.
        dxgi_cursor_composer_.InvalidateBackground();
        if (dxgi_replay_texture_) {
            dxgi_replay_texture_->Release();
            dxgi_replay_texture_ = nullptr;
        }
    }

    bool CaptureEngine::EnsureDxgiReplayTexture(
        const D3D11_TEXTURE2D_DESC& source) {
        if (!device_ || !context_ || source.Width == 0 || source.Height == 0
            || source.SampleDesc.Count != 1) {
            SetCaptureFailure(
                "DXGI owned replay texture requires a valid single-sample source");
            return false;
        }
        if (dxgi_replay_texture_) {
            D3D11_TEXTURE2D_DESC cached{};
            dxgi_replay_texture_->GetDesc(&cached);
            if (cached.Width != source.Width || cached.Height != source.Height
                || cached.Format != source.Format) {
                dxgi_replay_texture_->Release();
                dxgi_replay_texture_ = nullptr;
            }
        }
        if (dxgi_replay_texture_) return true;

        D3D11_TEXTURE2D_DESC owned{};
        owned.Width = source.Width;
        owned.Height = source.Height;
        owned.MipLevels = 1;
        owned.ArraySize = 1;
        owned.Format = source.Format;
        owned.SampleDesc.Count = 1;
        owned.Usage = D3D11_USAGE_DEFAULT;
        // A hidden or out-of-output pointer bypasses the compositor. The clean
        // background must therefore also support video-processor input views.
        owned.BindFlags = dxgi_cursor::kReplayTextureBindFlags;
        const HRESULT result = device_->CreateTexture2D(
            &owned, nullptr, &dxgi_replay_texture_);
        if (FAILED(result) || !dxgi_replay_texture_) {
            dxgi_replay_texture_ = nullptr;
            SetCaptureFailure(diagnostics::FormatHResultFailure(
                "ID3D11Device::CreateTexture2D(DXGI owned replay image)",
                result));
            return false;
        }
        return true;
    }

    void CaptureEngine::EmitRecentDxgiEvidence(const char* reason) const {
        // Snapshot is intentionally taken only at a watchdog/recovery
        // boundary.  It copies at most the fixed evidence capacity and keeps
        // all AcquireNextFrame calls free of diagnostic I/O.
        const auto events = dxgi_recent_evidence_.Snapshot();
        monitor::MonitorTopologyEntry resolved_monitor;
        monitor::DxgiOutputIdentity resolved_output;
        monitor::AdapterLuid capture_luid;
        monitor::AdapterLuid encoder_luid;
        bool capture_luid_available = false;
        bool encoder_luid_available = false;
        RECT desktop_coordinates{};
        bool desktop_coordinates_available = false;
        {
            std::lock_guard<std::mutex> lock(dxgi_context_mutex_);
            resolved_monitor = resolved_monitor_;
            resolved_output = resolved_dxgi_output_;
            capture_luid = capture_device_adapter_luid_;
            encoder_luid = encoder_adapter_luid_;
            capture_luid_available = capture_device_adapter_luid_available_;
            encoder_luid_available = encoder_adapter_luid_available_;
            desktop_coordinates = resolved_desktop_coordinates_;
            desktop_coordinates_available =
                resolved_desktop_coordinates_available_;
        }
        std::ostringstream json;
        json << "{\"subsystem\":\"capture\","
             << "\"event\":\"dxgi_recent_evidence\","
             << "\"reason\":\""
             << diagnostics::JsonEscape(reason ? reason : "unknown")
             << "\",\"event_count\":" << events.size()
             << ",\"monitor_id\":\""
             << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                    monitor_device_path_))
             << "\",\"windows_display\":\""
             << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                    resolved_output.source_gdi_name))
             << "\",\"dxgi_output\":{\"index\":"
             << resolved_output.output_index
             << "},\"monitor_adapter_luid\":";
        if (!resolved_monitor.monitor_device_path.empty()) {
            json << AdapterLuidJson(resolved_monitor.adapter_luid);
        } else {
            json << "\"unavailable:not_resolved\"";
        }
        json << ",\"capture_device_luid\":"
             << (capture_luid_available
                 ? AdapterLuidJson(capture_luid)
                 : "\"unavailable:not_resolved\"")
             << ",\"encoder_adapter_luid\":"
             << (encoder_luid_available
                 ? AdapterLuidJson(encoder_luid)
                 : "\"unavailable:not_resolved\"")
             << ",\"capture_generation\":"
             << capture_generation_.load(std::memory_order_relaxed)
             << ",\"desktop_coordinates\":";
        if (desktop_coordinates_available) {
            const RECT& bounds = desktop_coordinates;
            json << "{\"left\":" << bounds.left
                 << ",\"top\":" << bounds.top
                 << ",\"right\":" << bounds.right
                 << ",\"bottom\":" << bounds.bottom << '}';
        } else {
            json << "\"unavailable:not_resolved\"";
        }
        json << ",\"events\":[";
        for (size_t index = 0; index < events.size(); ++index) {
            if (index != 0) json << ',';
            const auto& event = events[index];
            json << "{\"acquire_api\":\""
                 << "IDXGIOutputDuplication::AcquireNextFrame\","
                 << "\"acquire_start_qpc\":" << event.acquire_start_qpc
                 << ",\"acquire_end_qpc\":" << event.acquire_end_qpc
                 << ",\"last_present_qpc\":" << event.last_present_qpc
                 << ",\"presentation_gap_qpc\":"
                 << event.presentation_gap_qpc
                 << ",\"accumulated_frames\":"
                 << event.accumulated_frames
                 << ",\"acquire_hresult\":"
                 << event.acquire_hresult
                 << ",\"acquire_hresult_hex\":\"0x"
                 << std::hex << static_cast<uint32_t>(event.acquire_hresult)
                 << std::dec << "\",\"acquired\":"
                 << (event.acquired ? "true" : "false")
                 << ",\"timed_out\":"
                 << (event.timed_out ? "true" : "false")
                 << ",\"pacing_accepted\":"
                 << (event.pacing_accepted ? "true" : "false")
                 << ",\"pacing_rejected\":"
                 << (event.pacing_rejected ? "true" : "false")
                 << ",\"release_attempted\":"
                 << (event.release_attempted ? "true" : "false")
                 << ",\"release_api\":\""
                 << "IDXGIOutputDuplication::ReleaseFrame\","
                 << "\"release_hresult\":" << event.release_hresult
                 << ",\"release_hresult_hex\":\"0x"
                 << std::hex << static_cast<uint32_t>(event.release_hresult)
                 << std::dec << "\",\"resource_token\":"
                 << event.resource_token
                 << ",\"generation\":" << event.generation
                 << ",\"width\":" << event.width
                 << ",\"height\":" << event.height
                 << ",\"format\":" << event.format
                 << ",\"output_index\":" << event.output_index
                 << ",\"pointer_only\":"
                 << (event.pointer_only ? "true" : "false") << '}';
        }
        json << "]}";
        std::cout << "FTHR_DIAGNOSTIC_EVENT " << json.str() << std::endl;
    }

    std::string CaptureEngine::BuildStartupDiagnosticContext() const {
        std::string selected_monitor = diagnostics::WideToUtf8(
            monitor_device_path_);
        if (selected_monitor.empty()) {
            selected_monitor = "unavailable:not_configured";
        }
        std::ostringstream context;
        context << "startup_context={\"selected_monitor_id\":\""
                << diagnostics::JsonEscape(selected_monitor)
                << "\",\"dxgi_output\":";
        if (resolved_dxgi_output_.output_index != UINT32_MAX) {
            context << "{\"index\":" << resolved_dxgi_output_.output_index
                    << ",\"source_gdi_name\":\""
                    << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                           resolved_dxgi_output_.source_gdi_name))
                    << "\"}";
        } else {
            context << "\"unavailable:not_resolved\"";
        }
        context << ",\"owning_adapter_luid\":";
        if (!resolved_monitor_.monitor_device_path.empty()) {
            context << AdapterLuidJson(resolved_monitor_.adapter_luid);
        } else {
            context << "\"unavailable:not_resolved\"";
        }
        context << ",\"capture_device_adapter_luid\":";
        if (capture_device_adapter_luid_available_) {
            context << AdapterLuidJson(capture_device_adapter_luid_);
        } else {
            context << "\"unavailable:not_reached_or_resolved\"";
        }
        context << ",\"encoder_adapter_luid\":";
        if (encoder_adapter_luid_available_) {
            context << AdapterLuidJson(encoder_adapter_luid_);
        } else {
            context << "\"unavailable:not_reached_or_resolved\"";
        }
        context << ",\"capture_backend\":\""
                << diagnostics::JsonEscape(startup_capture_backend_)
                << "\",\"encoder_backend\":\""
                << diagnostics::JsonEscape(startup_encoder_backend_)
                << "\",\"codec\":\""
                << diagnostics::JsonEscape(startup_codec_) << "\"}";
        return context.str();
    }

    std::wstring CaptureEngine::GetReplayFailureMessage() const {
        if (replay_storage_ && !replay_storage_->healthy()) {
            const std::string detail = replay_storage_->failure();
            return std::wstring(detail.begin(), detail.end()) + L" Restart capture after adjusting replay settings.";
        }
        return L"Replay stopped because its RAM budget was exhausted. "
               L"Reduce clip length or bitrate and restart capture.";
    }

    bool CaptureEngine::FailStartup(
        ReplayStartupError code, std::string detail) {
        if (!last_capture_failure_detail_.empty()
            && detail.find(last_capture_failure_detail_) == std::string::npos) {
            detail += " ";
            detail += last_capture_failure_detail_;
        }
        detail += " ";
        detail += BuildStartupDiagnosticContext();
        last_startup_error_code_ = code;
        last_startup_error_ = std::move(detail);
        const char* diagnostic_error = "ENGINE_START_FAILED";
        switch (code) {
        case ReplayStartupError::CaptureAdapterUnsupported:
            if (startup_capture_backend_ == "WGC_MONITOR"
                || startup_capture_backend_ == "WGC_WINDOW") {
                diagnostic_error = "CAPTURE_WGC_INIT_FAILED";
            } else if (startup_capture_backend_ == "DXGI_OUTPUT_DUPLICATION"
                       && (last_startup_error_.find("CreateDXGIFactory")
                               != std::string::npos
                           || last_startup_error_.find("D3D11CreateDevice")
                               != std::string::npos
                           || last_startup_error_.find("DuplicateOutput")
                               != std::string::npos
                           || last_startup_error_.find("QueryInterface")
                               != std::string::npos)) {
                diagnostic_error = "CAPTURE_DXGI_INIT_FAILED";
            } else {
                diagnostic_error = "CAPTURE_OUTPUT_OPEN_FAILED";
            }
            break;
        case ReplayStartupError::CrossAdapterPathUnavailable:
            diagnostic_error = "ENCODER_ADAPTER_MISMATCH";
            break;
        case ReplayStartupError::CaptureTargetUnavailable:
            diagnostic_error = "CAPTURE_TARGET_UNAVAILABLE";
            break;
        case ReplayStartupError::EncoderInitFailed:
            if (startup_encoder_backend_ == "native-nvenc") {
                diagnostic_error = "ENCODER_NVENC_INIT_FAILED";
            } else if (startup_encoder_backend_ == "ffmpeg-amf") {
                diagnostic_error = "ENCODER_AMF_INIT_FAILED";
            } else if (startup_encoder_backend_ == "ffmpeg-qsv") {
                diagnostic_error = "ENCODER_QSV_INIT_FAILED";
            } else {
                diagnostic_error = "ENCODER_INIT_FAILED";
            }
            break;
        case ReplayStartupError::RequestedCodecUnsupported:
        case ReplayStartupError::HardwareEncoderUnavailable:
            diagnostic_error = "ENCODER_INIT_FAILED";
            break;
        case ReplayStartupError::ReplayCapacityLimited:
            diagnostic_error = "ENGINE_START_FAILED";
            break;
        case ReplayStartupError::None:
            break;
        }
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"engine\","
                  << "\"event\":\"startup_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"" << diagnostic_error << "\","
                  << "\"native_startup_code\":\""
                  << ReplayStartupErrorName(code) << "\","
                  << "\"detail\":\""
                  << diagnostics::JsonEscape(last_startup_error_) << "\"}"
                  << std::endl;
        std::cerr << "FTHR_STARTUP_ERROR: "
                  << ReplayStartupErrorName(code) << ": "
                  << last_startup_error_ << std::endl;
        return false;
    }


    // Initialize capture and a hardware encoder on the selected adapter.
    // Refuse startup if the requested codec/backend cannot initialize.

    bool CaptureEngine::Initialize(const CaptureConfig& config) {
        replay_memory_failed_.store(false);
        last_startup_error_code_ = ReplayStartupError::None;
        last_startup_error_.clear();
        last_capture_failure_detail_.clear();
        active_replay_capability_ = {};
        resolved_monitor_ = {};
        resolved_dxgi_output_ = {};
        resolved_desktop_coordinates_ = {};
        resolved_desktop_coordinates_available_ = false;
        capture_device_adapter_luid_ = {};
        encoder_adapter_luid_ = {};
        capture_device_adapter_luid_available_ = false;
        encoder_adapter_luid_available_ = false;
        source_hdr_ = replay_hdr_ = false;
        sdr_reference_white_ = 1.0f;
        startup_capture_backend_ = "unavailable:not_reached";
        startup_encoder_backend_ = "unavailable:not_reached";
        startup_codec_ = VideoCodecName(config.video_codec);
        fps_ = config.framerate;
        allow_replay_gaps_ = config.allow_replay_gaps;
        allow_low_fps_clips_ = config.allow_low_fps_clips;
        buffer_seconds_ = config.buffer_seconds;
        replay_duration_adapted_ = false;
        capture_mode_ = config.capture_mode;
        capture_cursor_ = config.capture_cursor;
        configured_target_width_ = config.target_width;
        configured_target_height_ = config.target_height;
        target_width_ = config.target_width;
        target_height_ = config.target_height;
        bitrate_kbps_ = config.bitrate_kbps;
        scaling_mode_ = (config.scaling_mode == CaptureConfig::ScalingModeEnum::FIT) ? 1u : 0u;
        separate_audio_enabled_ = config.separate_audio_enabled;
        audio_requested_ = config.audio_enabled;
        application_audio_tracks_enabled_ =
            config.ResolvedApplicationAudioTracksEnabled();
        omit_default_mix_track_ = !config.RetainDefaultMixAudio();
        device_audio_tracks_.clear();
        monitor_device_path_ = monitor::NormalizeMonitorDevicePath(
            config.monitor_device_path);
        capture_loop_iterations_.store(0);
        dxgi_skipped_capture_slots_.store(0);
        dxgi_acquire_wait_ms_.store(0);
        dxgi_processing_budget_us_.store(0);
        capture_acquire_attempts_.store(0);
        capture_acquire_successes_.store(0);
        capture_timeouts_.store(0);
        capture_frames_released_.store(0);
        pointer_only_frames_.store(0);
        dxgi_coalesced_extra_frames_.store(0);
        source_textures_received_.store(0);
        dxgi_held_presentations_.store(0);
        dxgi_resampled_presentations_.store(0);
        dxgi_replay_cache_armed_.store(false, std::memory_order_relaxed);
        wgc_repeated_frame_observations_.store(0);
        protected_monitor_source_size_.store({}, std::memory_order_relaxed);
        wgc_missed_output_deadlines_.store(0);
        wgc_acquisition_timings_.Take();
        wgc_readback_timings_.Take();
        wgc_readback_copy_timings_.Take();
        wgc_readback_map_timings_.Take();
        wgc_scale_timings_.Take();
        wgc_submit_timings_.Take();
        conversion_submissions_.store(0);
        conversion_completions_.store(0);
        video_packets_produced_.store(0);
        video_ring_insertions_.store(0);
        capture_thread_stage_.store(0);
        last_capture_hresult_.store(0);
        capture_restart_count_.store(0);
        capture_recovery_attempts_.store(0);
        capture_recovery_failures_.store(0);
        capture_start_qpc_.store(0, std::memory_order_relaxed);
        generation_start_qpc_.store(0, std::memory_order_relaxed);
        last_source_frame_qpc_.store(0, std::memory_order_relaxed);
        last_dxgi_completed_qpc_.store(0, std::memory_order_relaxed);
        last_dxgi_pointer_only_completed_qpc_.store(
            0, std::memory_order_relaxed);
        last_dxgi_presentation_qpc_.store(0, std::memory_order_relaxed);
        last_encoder_submission_qpc_.store(0, std::memory_order_relaxed);
        last_encoded_output_qpc_.store(0, std::memory_order_relaxed);
        last_ring_insertion_qpc_.store(0, std::memory_order_relaxed);
        encoder_failure_count_.store(0, std::memory_order_relaxed);

        std::cout << "[CaptureEngine] Initializing..." << std::endl;
        std::cout << "  FPS        : " << fps_ << std::endl;
        std::cout << "  Buffer     : " << buffer_seconds_ << "s" << std::endl;
        std::cout << "  Target res : ";
        if (target_width_ == 0 || target_height_ == 0)
            std::cout << "Native" << std::endl;
        else
            std::cout << target_width_ << "x" << target_height_ << std::endl;
        std::cout << "  Bitrate    : " << bitrate_kbps_ << " kbps" << std::endl;
        std::cout << "  Codec      : "
                  << (config.video_codec_auto ? "Auto" : VideoCodecName(config.video_codec))
                  << std::endl;

        // Keep backend policy tied to the selected source. Only desktop Auto may
        // switch from DXGI to WGC; a window target must never degrade to a full
        // monitor capture, except protected targets that require WGC monitor capture.
        target_hwnd_ = config.target_hwnd;
        window_source_identity_.Reset();
        replay_audio_origin_100ns_.store(0);
        const auto report_backend_fallback = [this](
            const char* from_backend, const char* to_backend) {
            const char* error_code =
                std::strcmp(from_backend, "WGC_MONITOR") == 0
                    ? "CAPTURE_WGC_INIT_FAILED"
                    : "CAPTURE_DXGI_INIT_FAILED";
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"backend_fallback\","
                      << "\"state\":\"DEGRADED\","
                      << "\"error_code\":\"" << error_code << "\","
                      << "\"from_backend\":\"" << from_backend << "\","
                      << "\"to_backend\":\"" << to_backend << "\","
                      << "\"detail\":\""
                      << diagnostics::JsonEscape(last_capture_failure_detail_)
                      << "\"}" << std::endl;
        };
        const bool window_mode = config.capture_mode
            == CaptureConfig::CaptureModeEnum::WINDOW;
        const HWND selected_window = reinterpret_cast<HWND>(target_hwnd_);
        if (window_mode && target_hwnd_ != 0
            && !window_source_identity_.Bind(selected_window)) {
            return FailStartup(ReplayStartupError::CaptureTargetUnavailable,
                "The selected window closed or changed owner before capture started.");
        }
        const auto selected_executable = window_mode && selected_window
            ? QueryWindowExecutableName(selected_window) : std::wstring{};
        const bool protected_window = IsAntiCheatProtected(selected_executable);
        const bool minecraft_java_window = window_mode && IsMinecraftJavaWindow(
            selected_window, selected_executable);
        const auto capture_plan = PlanWindowsCaptureStartup(
            window_mode, target_hwnd_ != 0, protected_window,
            config.capture_backend_preference, config.capture_cursor, minecraft_java_window);
        focus_gated_ = capture_plan.focus_gated;
        replay_pause_clock_ = ShouldUseCompactReplayPauseClock(
                focus_gated_, config.audio_enabled,
                config.separate_audio_enabled)
            ? std::make_shared<ReplayPauseClock>(buffer_seconds_) : nullptr;
        if (replay_pause_clock_) {
            LARGE_INTEGER now{}, frequency{};
            QueryPerformanceCounter(&now);
            QueryPerformanceFrequency(&frequency);
            replay_pause_clock_->Observe(
                GetForegroundWindow() != reinterpret_cast<HWND>(target_hwnd_),
                ReplayPauseClock::Scale(now.QuadPart, 10'000'000, frequency.QuadPart));
        }
        if (!capture_plan.supported) {
            std::string detail;
            switch (capture_plan.failure) {
            case WindowsCapturePolicyFailure::MissingWindowTarget:
                detail = "capture_mode=WINDOW requires a valid HWND; no desktop "
                    "capture fallback was attempted.";
                break;
            case WindowsCapturePolicyFailure::WindowRequiresWgc:
                detail = "The forced DXGI backend captures a desktop monitor and "
                    "cannot preserve the selected Window target. Choose WGC, or "
                    "select Desktop capture explicitly if the full monitor is intended.";
                break;
            case WindowsCapturePolicyFailure::CursorExclusionRequiresWgc:
                detail = "Cursor exclusion requires WGC; DXGI may embed the system "
                    "cursor in desktop pixels. Choose Auto or WGC.";
                break;
            case WindowsCapturePolicyFailure::UnknownPreference:
                detail = "The requested Windows capture backend preference is invalid.";
                break;
            case WindowsCapturePolicyFailure::None:
                detail = "The selected Windows capture source is unavailable.";
                break;
            }
            return FailStartup(
                ReplayStartupError::CaptureTargetUnavailable, std::move(detail));
        }

        if (protected_window) {
            std::cout << "[CaptureEngine] Protected game detected — using "
                      << "WGC monitor capture with replay-preserving focus pauses" << std::endl;
        } else if (window_mode) {
            std::cout << "[CaptureEngine] Mode: Window Capture  HWND=0x"
                      << std::hex << target_hwnd_ << std::dec << std::endl;
        } else {
            std::cout << "[CaptureEngine] Mode: Desktop Capture" << std::endl;
        }

        bool redirected_hdr_monitor = false;
        const auto initialize_capture_backend = [this, &redirected_hdr_monitor](
            WindowsCaptureBackend backend) {
            switch (backend) {
            case WindowsCaptureBackend::WgcWindow:
                wgc_active_ = true;
                return InitializeWindowCapture();
            case WindowsCaptureBackend::WgcMonitor:
                wgc_active_ = true;
                return InitializeWGC();
            case WindowsCaptureBackend::DxgiDesktop:
                wgc_active_ = false;
                if (InitializeD3D11()) return true;
                if (!source_hdr_ || redirected_hdr_monitor) return false;
                redirected_hdr_monitor = true;
                ShutdownD3D11();
                wgc_active_ = true;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"hdr_backend_redirect\","
                          << "\"from_backend\":\"DXGI_OUTPUT_DUPLICATION\","
                          << "\"to_backend\":\"WGC_MONITOR\","
                          << "\"detail\":\"Use a 16-bit scRGB surface on the same HDR display\"}"
                          << std::endl;
                return InitializeWGC();
            case WindowsCaptureBackend::MinecraftJavaGame:
                wgc_active_ = false;
                return InitializeMinecraftJavaCapture();
            case WindowsCaptureBackend::None:
                return false;
            }
            return false;
        };

        if (!initialize_capture_backend(capture_plan.primary)) {
            const std::string primary_failure = last_capture_failure_detail_;
            if (capture_plan.fallback != WindowsCaptureBackend::None
                && !redirected_hdr_monitor && !source_hdr_) {
                // Both backends may have partially created D3D/WGC resources.
                // Tear them down before attempting the exact same monitor via
                // the fallback backend.
                ShutdownWGC();
                ShutdownD3D11();
                report_backend_fallback(
                    WindowsCaptureBackendName(capture_plan.primary),
                    WindowsCaptureBackendName(capture_plan.fallback));
                std::cerr << "[CaptureEngine] "
                          << WindowsCaptureBackendName(capture_plan.primary)
                          << " initialization failed; trying "
                          << WindowsCaptureBackendName(capture_plan.fallback)
                          << " on the same selected monitor" << std::endl;
                if (!initialize_capture_backend(capture_plan.fallback)) {
                    const std::string fallback_failure =
                        last_capture_failure_detail_;
                    return FailStartup(
                        ReplayStartupError::CaptureAdapterUnsupported,
                        "The selected desktop monitor could not be initialized "
                        "by either capture backend on its exact display adapter. "
                        + std::string(WindowsCaptureBackendName(
                              capture_plan.primary)) + " failure: "
                        + primary_failure + "; "
                        + WindowsCaptureBackendName(capture_plan.fallback)
                        + " failure: " + fallback_failure);
                }
            } else if (capture_plan.primary == WindowsCaptureBackend::MinecraftJavaGame) {
                return FailStartup(ReplayStartupError::CaptureTargetUnavailable,
                    "Minecraft Java Game Capture could not start. " + primary_failure);
            } else if (window_mode) {
                const char* target_kind = protected_window
                    ? "protected Window capture requires focus-gated WGC monitor capture"
                    : "Window capture requires WGC window capture";
                std::string detail = target_kind;
                detail += "; the requested target was not captured. Select Desktop "
                    "capture explicitly only if capturing the entire monitor is intended. "
                    "No monitor or DXGI fallback was attempted.";
                detail += " Native capture failure: ";
                detail += primary_failure.empty()
                    ? "no native failure detail was reported."
                    : primary_failure;
                return FailStartup(
                    ReplayStartupError::CaptureTargetUnavailable,
                    std::move(detail));
            } else {
                std::string detail = redirected_hdr_monitor
                    ? "WGC scRGB could not initialize the selected HDR monitor. Native capture failure: "
                    : std::string("The selected ") + (wgc_active_ ? "WGC" : "DXGI")
                        + " desktop capture backend could not initialize the "
                          "selected monitor on its exact display adapter. Native capture failure: ";
                detail += primary_failure.empty()
                    ? "no native failure detail was reported."
                    : primary_failure;
                return FailStartup(
                    ReplayStartupError::CaptureAdapterUnsupported,
                    std::move(detail));
            }
        }

        // Select the replay encoder on the capture adapter. Clear any earlier
        // capture-backend fallback error so it cannot be reported as the cause
        // of an encoder failure.
        last_capture_failure_detail_.clear();
        ConfigureCrop(config);
        CaptureScaleGeometry scale_geometry;
        if (!BuildCaptureScaleGeometry(
                crop_width_, crop_height_, target_width_, target_height_,
                scaling_mode_ == 1, scale_geometry)) {
            return FailStartup(
                ReplayStartupError::EncoderInitFailed,
                "The selected capture and target resolutions cannot form a valid "
                "even hardware encoder geometry.");
        }
        target_width_ = scale_geometry.target_width;
        target_height_ = scale_geometry.target_height;
        std::cout << "[CaptureEngine] Attempting hardware replay initialization "
                  << "for selected " << EncoderVendorName(capture_adapter_vendor_)
                  << " adapter..." << std::endl;

        EncoderConfig hw_cfg;
        hw_cfg.src_width = crop_width_;
        hw_cfg.src_height = crop_height_;
        hw_cfg.enc_width = target_width_;
        hw_cfg.enc_height = target_height_;
        hw_cfg.fps = fps_;
        hw_cfg.bitrate_kbps = bitrate_kbps_;
        hw_cfg.hardware_preset = config.encoder_preset;
        hw_cfg.scaling_mode = scaling_mode_;

        auto selection = SelectWindowsReplayPolicy(
            capture_adapter_vendor_, config.encoder_preference,
            config.video_codec);
        startup_encoder_backend_ = ReplayEncoderBackendName(selection.backend);
        if (!selection.allowed) {
            return FailStartup(selection.error,
                "The requested encoder is unavailable for the selected capture "
                "source and codec. Cross-adapter AMD/Intel and raw replay "
                "fallbacks are disabled.");
        }
        std::cout << "[ReplayCapability] requested="
                  << (config.video_codec_auto ? "Auto" : VideoCodecName(config.video_codec))
                  << " capture_adapter="
                  << EncoderVendorName(selection.capture_vendor)
                  << " encoder_adapter="
                  << EncoderVendorName(selection.encoder_vendor)
                  << " backend="
                  << ReplayEncoderBackendName(selection.backend)
                  << " adapter_policy="
                  << (selection.same_adapter ? "same-adapter"
                                             : "explicit-cross-adapter")
                  << std::endl;

        MEMORYSTATUSEX memory_status{};
        memory_status.dwLength = sizeof(memory_status);
        if (!GlobalMemoryStatusEx(&memory_status)) {
            return FailStartup(ReplayStartupError::ReplayCapacityLimited,
                "Could not query available memory for replay admission. Restart capture.");
        }
        const uint64_t available_bytes =
            std::min<uint64_t>(memory_status.ullAvailPhys, memory_status.ullAvailPageFile);
        const auto& replay_config = config.replay_storage;
        const uint32_t replay_memory_mb = replay_config.memory_mb
            ? replay_config.memory_mb : config.max_buffer_mb;
        const auto memory_plan = PlanReplayMemory(
            buffer_seconds_, fps_, bitrate_kbps_, replay_memory_mb, available_bytes);
        if (replay_config.mode != ReplayStorageMode::Legacy) {
            const uint32_t audio_tracks = !config.audio_enabled ? 0 : 2
                + static_cast<uint32_t>(std::min<size_t>(kMaxDeviceAudioTracks, config.audio_device_tracks.size()))
                + (application_audio_tracks_enabled_ && DetectWindowsProcessLoopbackCapability().api_build_supported
                    ? kMaxRetainedAudioSources : 0);
            std::string error;
            const uint64_t memory_ceiling_bytes = replay_config.automatic_budget
                ? ReplayAvailableMemoryBudget(available_bytes) : memory_plan.total_bytes;
            const uint32_t requested_seconds = buffer_seconds_;
            buffer_seconds_ = EffectiveAutomaticReplaySeconds(replay_config, memory_ceiling_bytes,
                requested_seconds, fps_, bitrate_kbps_, audio_tracks);
            if (buffer_seconds_ != requested_seconds) {
                replay_duration_adapted_ = true;
                std::cout << "FTHR_STARTUP_WARNING: REPLAY_DURATION_REDUCED: "
                          << "Replay history temporarily reduced from " << requested_seconds
                          << "s to " << buffer_seconds_
                          << "s to fit available memory. Recording quality and saved preferences are unchanged."
                          << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"replay_memory\","
                          << "\"event\":\"duration_adapted\",\"requested_seconds\":" << requested_seconds
                          << ",\"effective_seconds\":" << buffer_seconds_
                          << ",\"memory_ceiling_bytes\":" << memory_ceiling_bytes << '}' << std::endl;
            }
            replay_storage_ = CreateCaptureReplayStorage(replay_config, memory_ceiling_bytes,
                buffer_seconds_, fps_, bitrate_kbps_, audio_tracks, error);
            if (!replay_storage_)
                return FailStartup(ReplayStartupError::ReplayCapacityLimited, error);
            if (replay_config.automatic_budget)
                std::cout << DescribeAutomaticReplayBudget(*replay_storage_) << std::endl;
        } else if (!memory_plan.fits) {
            std::ostringstream reason;
            reason << "Replay history and one save need approximately "
                   << (memory_plan.required_bytes + 1048575) / 1048576
                   << " MiB; the available replay budget is "
                   << memory_plan.total_bytes / 1048576
                   << " MiB. Reduce clip length or bitrate, or close other programs.";
            return FailStartup(ReplayStartupError::ReplayCapacityLimited, reason.str());
        }

        replay_encoder_ = CreateProductionReplayEncoder(
            selection.encoder_vendor, config.video_codec);
        if (!replay_encoder_) {
            return FailStartup(
                ReplayStartupError::HardwareEncoderUnavailable,
                "The approved same-adapter hardware encoder backend is not "
                "available in this build.");
        }

        replay_config_publish_failed_.store(false);
        auto packet_callback = [this](const uint8_t* data, uint32_t size,
                                      int64_t pts, bool is_keyframe,
                                      int64_t wall_qpc) {
            if (!encoded_ring_ || !replay_encoder_) {
                replay_config_publish_failed_.store(true);
                return;
            }
            if (!encoded_ring_->HasVideoConfig()) {
                const auto config = replay_encoder_->GetVideoConfig();
                if (!replay_encoder_->IsVideoConfigReady()
                    || !encoded_ring_->SetVideoConfig(config)) {
                    if (encoded_ring_->HasMemoryFailure()) {
                        replay_memory_failed_.store(true);
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                    }
                    replay_config_publish_failed_.store(true);
                    return;
                }
            }
            PublishedVideoPoint published;
            const bool ring_inserted = encoded_ring_->Push(
                data, size, pts, is_keyframe, wall_qpc, &published);
            if (replay_storage_ && !replay_storage_->healthy()) {
                SetCaptureFailure(replay_storage_->failure());
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                return;
            }
            if (!ring_inserted && encoded_ring_->HasMemoryFailure()) {
                replay_memory_failed_.store(true);
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                if (running_.exchange(false)) {
                    const auto budget = encoded_ring_->GetMemoryBudget();
                    std::cerr << "[ReplayMemory] Capture stopped: compressed video "
                                 "memory limit or allocation failure. Reduce clip "
                                 "length or bitrate and restart capture." << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"replay_memory\","
                              << "\"event\":\"ring_memory_exhausted\",\"state\":\"FAILED\","
                              << "\"ring_bytes\":" << budget->Used(ReplayMemoryUse::Ring)
                              << ",\"save_bytes\":" << budget->Used(ReplayMemoryUse::Save)
                              << ",\"ring_limit_bytes\":" << budget->ring_limit()
                              << ",\"total_limit_bytes\":" << budget->total_limit() << '}'
                              << std::endl;
                }
                return;
            }
            LARGE_INTEGER arrival{};
            QueryPerformanceCounter(&arrival);
            const auto health_qpc = replay_pause_clock_ ? arrival.QuadPart : wall_qpc;
            video_packets_produced_.fetch_add(1, std::memory_order_relaxed);
            if (wall_qpc > 0) {
                last_encoded_output_qpc_.store(health_qpc, std::memory_order_release);
            }
            if (ring_inserted) {
                video_ring_insertions_.fetch_add(1, std::memory_order_relaxed);
                if (wall_qpc > 0) {
                    last_ring_insertion_qpc_.store(
                        health_qpc, std::memory_order_release);
                }
            }

            std::shared_ptr<ContinuousRecordingWriter> writer;
            {
                std::lock_guard<std::mutex> lock(record_writer_mutex_);
                writer = record_writer_;
            }
            // The ring is the publication authority.  A delayed encoder
            // callback rejected by Push() must never enter a recording from a
            // newer generation.
            if (writer && ring_inserted
                && !writer->PushVideo(
                    data, size, pts, is_keyframe, published.generation,
                    [wall_qpc]() {
                        static const int64_t frequency = []() {
                            LARGE_INTEGER value{}; QueryPerformanceFrequency(&value); return value.QuadPart;
                        }();
                        return ReplayPauseClock::Scale(wall_qpc, 10000000, frequency);
                    }())) {
                // A stale token is an optional packet rejection. Only a real
                // writer failure belonging to the same current owner changes
                // recording state; recovery may have moved this old writer.
                if (writer->HasFailed()) {
                    std::lock_guard<std::mutex> lock(record_writer_mutex_);
                    if (record_writer_ == writer)
                        is_recording_.store(false, std::memory_order_release);
                }
            }
        };

        ID3D11Device* encoder_device = device_;
        ID3D11DeviceContext* encoder_context = context_;
        replay_encoder_cpu_input_ = !selection.same_adapter;
        if (replay_encoder_cpu_input_
            && selection.backend != ReplayEncoderBackend::Software) {
            std::string encoder_device_diagnostic;
            if (!CreateD3D11DeviceForVendor(
                    EncoderVendor::Nvidia,
                    &nvenc_device_, &nvenc_context_,
                    encoder_device_diagnostic)) {
                return FailStartup(
                    ReplayStartupError::HardwareEncoderUnavailable,
                    "NVIDIA was selected, but a usable NVIDIA D3D11 device "
                    "could not be created. " + encoder_device_diagnostic);
            }
            encoder_device = nvenc_device_;
            encoder_context = nvenc_context_;
            std::cout << "[ReplayCapability] Explicit hybrid NVIDIA path: "
                      << "capture readback -> NVENC CPU input" << std::endl;
        }
        std::string encoder_luid_diagnostic;
        encoder_adapter_luid_available_ = QueryD3D11DeviceAdapterLuid(
            encoder_device, encoder_adapter_luid_, encoder_luid_diagnostic);
        if (!encoder_adapter_luid_available_) {
            std::cerr << "[ReplayCapability] Could not resolve encoder adapter LUID: "
                      << encoder_luid_diagnostic << std::endl;
        }
        if (source_hdr_) {
            auto& state = *wgc_state_;
            const auto hdr_codec = SelectHdrReplayCodec(
                selection.backend, config.video_codec, config.video_codec_auto);
            std::string fallback_reason = "selected encoder or codec uses SDR input";
            if (hdr_codec) {
                auto converter = std::make_unique<HdrFrameConverter>();
                HRESULT hr = converter->Initialize(device_, target_width_, target_height_,
                                                    HdrFrameConverter::Output::Pq);
                if (SUCCEEDED(hr) && replay_encoder_cpu_input_) {
                    D3D11_TEXTURE2D_DESC staging{};
                    converter->Texture()->GetDesc(&staging);
                    staging.Usage = D3D11_USAGE_STAGING;
                    staging.BindFlags = 0;
                    staging.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                    hr = device_->CreateTexture2D(&staging, nullptr, state.hdr_staging.put());
                }
                if (SUCCEEDED(hr)) {
                    auto hdr_config = hw_cfg;
                    hdr_config.src_width = target_width_;
                    hdr_config.src_height = target_height_;
                    hdr_config.hdr10 = true;
                    auto hdr_encoder = CreateProductionReplayEncoder(
                        selection.encoder_vendor, *hdr_codec);
                    replay_hdr_ = hdr_encoder && hdr_encoder->Initialize(hdr_config,
                        encoder_device, encoder_context, packet_callback, replay_encoder_cpu_input_);
                    if (replay_hdr_) {
                        replay_encoder_ = std::move(hdr_encoder);
                        state.color_converter = std::move(converter);
                        selection.requested_codec = *hdr_codec;
                        startup_codec_ = VideoCodecName(*hdr_codec);
                    } else if (hdr_encoder) {
                        fallback_reason = hdr_encoder->GetLastError();
                    }
                } else {
                    fallback_reason = diagnostics::FormatHResultFailure("P010 conversion setup", hr);
                }
            }
            if (!replay_hdr_) {
                state.hdr_staging = nullptr;
                state.color_converter = std::make_unique<HdrFrameConverter>();
                const HRESULT hr = state.color_converter->Initialize(
                    device_, width_, height_, HdrFrameConverter::Output::Sdr);
                if (FAILED(hr)) {
                    return FailStartup(ReplayStartupError::EncoderInitFailed,
                        diagnostics::FormatHResultFailure("HDR-to-SDR conversion setup", hr));
                }
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"hdr_tone_mapped_to_sdr\",\"detail\":\""
                          << diagnostics::JsonEscape(fallback_reason) << "\"}" << std::endl;
            }
        }
        if (!replay_hdr_ && replay_encoder_cpu_input_ && !minecraft_java_state_) {
            if (!source_hdr_ && selection.backend == ReplayEncoderBackend::NativeNvenc
                && (scale_geometry.destination.width < scale_geometry.source_width
                    || scale_geometry.destination.height < scale_geometry.source_height)) {
                auto scaler = std::make_unique<BgraFrameScaler>();
                std::string detail;
                if (scaler->Initialize(device_, context_, scale_geometry, detail)) {
                    if (CreateStagingTexture(target_width_, target_height_)) {
                        hybrid_bgra_scaler_ = std::move(scaler);
                        hw_cfg.src_width = target_width_;
                        hw_cfg.src_height = target_height_;
                        hw_cfg.scaling_mode = 0; // The GPU already prepared the encoded canvas.
                    } else {
                        detail = "Could not allocate a target-sized staging texture";
                    }
                }
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"hybrid_bgra_downscale\",\"status\":\""
                          << (hybrid_bgra_scaler_ ? "active" : "cpu_fallback")
                          << "\",\"source_width\":" << scale_geometry.source_width
                          << ",\"source_height\":" << scale_geometry.source_height
                          << ",\"readback_width\":" << (hybrid_bgra_scaler_ ? target_width_ : width_)
                          << ",\"readback_height\":" << (hybrid_bgra_scaler_ ? target_height_ : height_)
                          << ",\"detail\":\"" << diagnostics::JsonEscape(detail) << "\"}"
                          << std::endl;
            }
            if (!hybrid_bgra_scaler_ && !CreateStagingTexture(width_, height_)) {
                const bool software = selection.backend == ReplayEncoderBackend::Software;
                return FailStartup(software ? ReplayStartupError::EncoderInitFailed
                                             : ReplayStartupError::CrossAdapterPathUnavailable,
                    "The capture readback texture could not be created.");
            }
        }
        nvenc_active_ = replay_hdr_ || replay_encoder_->Initialize(
            hw_cfg, encoder_device, encoder_context, packet_callback,
            replay_encoder_cpu_input_);
        if (!nvenc_active_) {
            std::cerr << "[CaptureEngine] "
                      << EncoderVendorName(selection.encoder_vendor) << ' '
                      << VideoCodecName(config.video_codec)
                      << " hardware initialization failed";
            const std::string detail = replay_encoder_->GetLastError();
            if (!detail.empty()) std::cerr << ": " << detail;
            std::cerr << std::endl;
            const auto raw_capacity = CalculateRawReplayCapacity(
                crop_width_, crop_height_, 4, fps_, buffer_seconds_,
                config.max_buffer_mb);
            std::ostringstream reason;
            if (!detail.empty()) reason << detail << ". ";
            reason << "The requested " << VideoCodecName(config.video_codec)
                   << " encoder on the requested "
                   << EncoderVendorName(selection.encoder_vendor)
                   << " adapter did not initialize. Automatic codec, "
                      "cross-adapter, and raw replay fallbacks are disabled";
            if (!raw_capacity.meets_requested_duration) {
                reason << "; the legacy raw budget would hold only "
                       << raw_capacity.capacity_milliseconds << " ms of the "
                       << (static_cast<uint64_t>(buffer_seconds_) * 1000ULL)
                       << " ms requested";
            }
            reason << '.';
            return FailStartup(
                ClassifyReplayInitializationFailure(detail), reason.str());
        }

        active_replay_capability_ = EvaluateActiveReplayCapability(
            selection, replay_encoder_->GetActiveEncoderInfo(), true,
            static_cast<uint64_t>(capture_generation_.load()) + 1ULL);
        if (!active_replay_capability_.initialized) {
            return FailStartup(active_replay_capability_.error,
                "The initialized encoder did not match the requested codec, "
                "backend, or selected capture adapter.");
        }

        {
            // Retain requested history plus the encoder's maximum four-second
            // GOP pre-roll and one second for asynchronous publication jitter.
            // Unlike the former 2x policy, memory no longer scales with a
            // second complete copy of long (up to 300-second) replay history.
            const size_t capacity = CalculateEncodedReplaySlotCapacity(
                buffer_seconds_, fps_);

            LARGE_INTEGER qpc_freq;
            QueryPerformanceFrequency(&qpc_freq);
            try {
                auto budget = replay_storage_ ? replay_storage_->memory_budget()
                    : std::make_shared<ReplayMemoryBudget>(memory_plan.total_bytes, memory_plan.ring_bytes);
                encoded_ring_ = std::make_unique<EncodedRingBuffer>(
                    capacity, fps_, qpc_freq.QuadPart, budget, replay_storage_);
            } catch (const std::bad_alloc&) {
                return FailStartup(ReplayStartupError::ReplayCapacityLimited,
                    "Could not allocate replay storage. Reduce clip length or bitrate, "
                    "or close other programs.");
            }
            const auto budget = encoded_ring_->GetMemoryBudget();
            std::cout << "[ReplayMemory] Budget: total=" << budget->total_limit()
                      << " ring_reserved=" << budget->ring_limit()
                      << " save_limit=" << budget->total_limit() - budget->ring_limit()
                      << " bytes" << std::endl;

            // Publish codec, geometry, timing, packet format and decoder
            // configuration as one immutable stream description.
            const auto video_config = replay_encoder_->GetVideoConfig();
            if (replay_encoder_->IsVideoConfigReady()) {
                if (!encoded_ring_->SetVideoConfig(video_config)) {
                    std::cerr << "[CaptureEngine] Could not publish immutable encoded "
                                 "stream configuration" << std::endl;
                    return FailStartup(encoded_ring_->HasMemoryFailure()
                            ? ReplayStartupError::ReplayCapacityLimited
                            : ReplayStartupError::EncoderInitFailed,
                        "Could not store replay decoder configuration. Reduce clip "
                        "length or bitrate, or close other programs.");
                }
            } else {
                std::cout << "[CaptureEngine] Decoder configuration will be "
                             "published with the first encoded packet" << std::endl;
            }

            // max_frames_ used for stats - set to time-based count.
            // ring_head_ / ring_count_ not used on NVENC path.
            max_frames_ = static_cast<size_t>(buffer_seconds_) * fps_;

            const auto active = replay_encoder_->GetActiveEncoderInfo();
            std::cout << "[CaptureEngine] " << active.name
                << " active. Encoded ring: " << capacity
                << " slots. Raw FramePool: skipped." << std::endl;
        }

        // Initialize WASAPI -> float packetizers -> bounded disk rings before video.
        // Saves snapshot shared payload references without copying the PCM history.
        // If optional audio initialization fails, continue with video only.
        if (!config.audio_enabled) {
            std::cout << "[CaptureEngine] Audio capture disabled by user settings." << std::endl;
        }
        else {
        do {
            // A device-wide loopback includes excluded applications. Do not
            // retain it as a backup when program exclusions are requested.
            if (config.ResolvedApplicationAudioTracksEnabled() && !config.RetainDefaultMixAudio()) break;
            AudioCaptureConfig audio_cfg;
            audio_cfg.bitrate_kbps = 128;
            audio_cfg.device_id = config.playback_endpoint_id;

            if (!audio_capture_.Initialize(nullptr, audio_cfg)) {
                std::cerr << "[CaptureEngine] AudioCapture init failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                break;
            }

            std::string default_mix_uuid;
            std::string default_mix_uuid_error;
            if (!CreateAudioManifestTransactionId(
                    &default_mix_uuid, &default_mix_uuid_error)) {
                std::cerr << "[CaptureEngine] Could not create Default Mix source UUID: "
                    << default_mix_uuid_error << std::endl;
                audio_capture_.Shutdown();
                break;
            }
            AudioSourceId default_mix_id{default_mix_uuid};
            default_mix_audio_source_ = {};
            default_mix_audio_source_.identity.id = default_mix_id;
            default_mix_audio_source_.identity.type = AudioSourceType::System;
            default_mix_audio_source_.identity.persistent_identity = "default-mix";
            default_mix_audio_source_.identity.display_name = "Default Mix";
            default_mix_audio_source_.identity.icon_reference = "system-audio";
            default_mix_audio_source_.format = {
                audio_capture_.GetSampleRate(), audio_capture_.GetChannels(), "flt", "pcm_f32le"};
            default_mix_audio_source_.state.health = AudioSourceHealth::Active;
            default_mix_audio_source_.state.admitted = true;
            default_mix_audio_source_.state.active_in_generation = true;
            default_mix_audio_ring_ = std::make_unique<EncodedAudioPacketRing>(
                default_mix_id,
                capture_generation_.load(std::memory_order_relaxed) + 1,
                AudioSourceFormat{audio_capture_.GetSampleRate(),
                                  audio_capture_.GetChannels(), "flt", "pcm_f32le"},
                buffer_seconds_, replay_storage_);
            if (!default_mix_audio_encoder_.Initialize(
                    audio_capture_.GetSampleRate(), audio_capture_.GetChannels(),
                    audio_cfg.bitrate_kbps,
                    [this](const uint8_t* data, uint32_t size, int64_t pts) {
                        const int64_t duration = size / (audio_capture_.GetChannels() * sizeof(float));
                        if (replay_pause_clock_) {
                            const auto origin = audio_capture_.GetTimelineOriginQpc100ns();
                            if (!origin) return;
                            auto replay_origin = replay_audio_origin_100ns_.load();
                            if (!replay_origin) {
                                replay_origin = replay_pause_clock_->Map(origin);
                                replay_audio_origin_100ns_.store(replay_origin);
                            }
                            const auto mapped = replay_pause_clock_->MapAudio(
                                origin, replay_origin, pts, duration, audio_capture_.GetSampleRate());
                            if (!mapped) return;
                            pts = *mapped;
                        }
                        if (default_mix_audio_ring_) {
                            default_mix_audio_ring_->Push(
                                {std::vector<uint8_t>(data, data + size), pts, duration});
                        }

                        std::shared_ptr<ContinuousRecordingWriter> writer;
                        {
                            std::lock_guard<std::mutex> lock(record_writer_mutex_);
                            writer = record_writer_;
                        }
                        const auto origin = replay_audio_origin_100ns_.load()
                            ? replay_audio_origin_100ns_.load() : audio_capture_.GetTimelineOriginQpc100ns();
                        if (writer && !omit_default_mix_track_ && !writer->PushSourceAudio(default_mix_audio_source_, data, size,
                                AudioSamplePositionToTimeline100ns(origin, pts, audio_capture_.GetSampleRate()), duration)
                            && writer->HasFailed()) {
                            // The callback can outlive recovery's owner move.
                            // A stale writer must never change the new session's
                            // recording state.
                            std::lock_guard<std::mutex> lock(record_writer_mutex_);
                            if (record_writer_ == writer)
                                is_recording_.store(false, std::memory_order_release);
                        }
                    }, 0, true)) {
                std::cerr << "[CaptureEngine] Float audio packetizer init failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                default_mix_audio_ring_.reset();
                break;
            }
            default_mix_audio_ring_->SetCodecExtradata(
                default_mix_audio_encoder_.GetExtradata());
            audio_capture_.SetEncoder(&default_mix_audio_encoder_);

            if (!audio_capture_.Start()) {
                std::cerr << "[CaptureEngine] AudioCapture start failed - "
                    << "audio disabled" << std::endl;
                audio_capture_.Shutdown();
                default_mix_audio_encoder_.Finalize();
                default_mix_audio_ring_.reset();
                break;
            }

            audio_active_ = true;
            std::cout << "[CaptureEngine] Audio capture active ("
                << audio_capture_.GetSampleRate() << "Hz, "
                << audio_capture_.GetChannels() << "ch, "
                << "lossless float packet replay)"
                << std::endl;

        } while (false);

            std::string microphone_uuid;
            std::string microphone_uuid_error;
            if (!CreateAudioManifestTransactionId(&microphone_uuid, &microphone_uuid_error)) {
                std::cerr << "[CaptureEngine] Could not create Microphone source UUID: "
                    << microphone_uuid_error << std::endl;
            } else {
                microphone_audio_metadata_ = {};
                microphone_audio_metadata_.identity.id = AudioSourceId{microphone_uuid};
                microphone_audio_metadata_.identity.type = AudioSourceType::Microphone;
                microphone_audio_metadata_.identity.persistent_identity = "microphone";
                microphone_audio_metadata_.identity.display_name = "Microphone";
                microphone_audio_metadata_.identity.icon_reference = "microphone";
                microphone_audio_metadata_.format = {
                    kCanonicalAudioSampleRate, kCanonicalAudioChannels, "fltp"};
                microphone_audio_metadata_.state.health = AudioSourceHealth::Discovered;

                WindowsMicrophoneAudioProviderConfig microphone_config;
                microphone_config.generation = capture_generation_.load(
                    std::memory_order_relaxed) + 1;
                microphone_config.endpoint_id = config.microphone_endpoint_id;
                microphone_config.use_default_endpoint = config.microphone_endpoint_id.empty();
                microphone_config.retention_seconds = buffer_seconds_;
                microphone_config.replay_storage = replay_storage_;
                microphone_config.bitrate_kbps = 320;
                microphone_config.downmix_mono = config.microphone_mono;
                microphone_config.input_gain = std::clamp(
                    static_cast<float>(config.microphone_gain_percent) / 100.0f,
                    0.0f, 2.0f);
                microphone_config.source = microphone_audio_metadata_;
                microphone_config.packet_sink = [this](const auto& source, const uint8_t* data,
                    uint32_t size, int64_t timestamp, int64_t duration) {
                    ForwardAudioToRecording(source, data, size, timestamp, duration);
                };
                microphone_config.replay_pause_clock = replay_pause_clock_;
                microphone_audio_source_ = std::make_unique<WindowsMicrophoneAudioProvider>(
                    std::move(microphone_config));
                if (!microphone_audio_source_->Start()) {
                    const std::string microphone_error =
                        microphone_audio_source_->last_error();
                    std::cerr << "[CaptureEngine] Native microphone provider did not start: "
                        << microphone_error << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"microphone_initialized\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"MIC_INIT_FAILED\","
                              << "\"endpoint\":"
                              << "\"unavailable:provider_start_failed\","
                              << "\"detail\":\""
                              << diagnostics::JsonEscape(microphone_error)
                              << "\"}" << std::endl;
                    microphone_audio_source_.reset();
                } else {
                    std::cout << "[CaptureEngine] Native microphone provider starting ("
                        << (config.microphone_endpoint_id.empty() ? "Default microphone"
                                                                   : "explicit endpoint")
                        << ")." << std::endl;
                }
            }

            for (size_t index = 0; index < config.audio_device_tracks.size()
                    && index < kMaxAudioDeviceTracks; ++index) {
                const auto& configured = config.audio_device_tracks[index];
                std::string source_uuid;
                std::string source_uuid_error;
                if (!CreateAudioManifestTransactionId(
                        &source_uuid, &source_uuid_error)) {
                    std::cerr << "[CaptureEngine] Could not create named audio "
                              << "track UUID: " << source_uuid_error << std::endl;
                    continue;
                }
                DeviceAudioTrack device_track;
                device_track.metadata.identity.id = AudioSourceId{source_uuid};
                device_track.metadata.identity.type = AudioSourceType::System;
                device_track.metadata.identity.persistent_identity =
                    "device-track-" + std::to_string(index + 1);
                device_track.metadata.identity.display_name = configured.name;
                device_track.metadata.identity.icon_reference = "system-audio";
                device_track.metadata.format = {
                    kCanonicalAudioSampleRate, kCanonicalAudioChannels, "fltp"};
                device_track.metadata.state.health = AudioSourceHealth::Discovered;

                WindowsMicrophoneAudioProviderConfig device_config;
                device_config.generation = capture_generation_.load(
                    std::memory_order_relaxed) + 1;
                device_config.endpoint_id = configured.endpoint_id;
                device_config.use_default_endpoint = false;
                device_config.loopback = true;
                device_config.retention_seconds = buffer_seconds_;
                device_config.replay_storage = replay_storage_;
                device_config.bitrate_kbps = 128;
                device_config.source = device_track.metadata;
                device_config.packet_sink = [this](const auto& source, const uint8_t* data,
                    uint32_t size, int64_t timestamp, int64_t duration) {
                    ForwardAudioToRecording(source, data, size, timestamp, duration);
                };
                device_config.replay_pause_clock = replay_pause_clock_;
                device_track.provider =
                    std::make_unique<WindowsMicrophoneAudioProvider>(
                        std::move(device_config));
                if (!device_track.provider->Start()) {
                    std::cerr << "[CaptureEngine] Named audio track unavailable: "
                              << configured.name << " (endpoint was not replaced)"
                              << std::endl;
                    continue;
                }
                device_track.metadata.state.health = AudioSourceHealth::Active;
                device_track.metadata.state.active_in_generation = true;
                std::cout << "[CaptureEngine] Named audio track starting: "
                          << configured.name << std::endl;
                device_audio_tracks_.push_back(std::move(device_track));
            }
            if (application_audio_tracks_enabled_) {
                application_audio_source_manager_ =
                    std::make_unique<WindowsApplicationAudioSourceManager>(
                        capture_generation_.load(std::memory_order_relaxed) + 1,
                        buffer_seconds_, replay_pause_clock_,
                        [this](const auto& source, const uint8_t* data, uint32_t size,
                               int64_t timestamp, int64_t duration) {
                            ForwardAudioToRecording(source, data, size, timestamp, duration);
                        }, replay_storage_, config.application_audio_exclusions);
                if (!application_audio_source_manager_->Start()) {
                    const auto capability = application_audio_source_manager_->capability();
                    const std::string detail = application_audio_source_manager_->last_error();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_initialized\","
                              << "\"state\":\"UNAVAILABLE\","
                              << "\"error_code\":"
                              << (capability.api_build_supported
                                  ? "\"PROCESS_AUDIO_INIT_FAILED\""
                                  : "\"AUDIO_PROCESS_LOOPBACK_UNSUPPORTED\"")
                              << ",\"os_build\":" << capability.os_build
                              << ",\"detail\":\""
                              << diagnostics::JsonEscape(detail.empty()
                                  ? "Windows 11 process loopback is unavailable"
                                  : detail)
                              << "\"}" << std::endl;
                    application_audio_source_manager_.reset();
                } else {
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_initialized\","
                              << "\"state\":\"STARTING\","
                              << "\"error_code\":null}"
                              << std::endl;
                }
            }
            std::cout << "[CaptureEngine] Audio routing: system mix + microphone"
                      << (application_audio_source_manager_
                          ? " + application stems."
                          : (application_audio_tracks_enabled_
                              ? " (application stems unavailable)."
                              : "."))
                      << std::endl;

        }

        if (!audio_active_ && !microphone_audio_source_
                && device_audio_tracks_.empty()
                && !application_audio_source_manager_) {
            std::cout << "[CaptureEngine] Running in video-only mode." << std::endl;
        }

        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"replay_memory\","
                  << "\"event\":\"duration_confirmed\",\"requested_seconds\":" << config.buffer_seconds
                  << ",\"effective_seconds\":" << buffer_seconds_ << '}' << std::endl;

        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"adapter_topology_resolved\","
                  << "\"monitor_id\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         monitor_device_path_)) << "\","
                  << "\"windows_display\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         resolved_dxgi_output_.source_gdi_name)) << "\","
                  << "\"dxgi_output\":";
        if (resolved_dxgi_output_.output_index != UINT32_MAX) {
            std::cout << "{\"index\":" << resolved_dxgi_output_.output_index << "},";
        } else {
            std::cout << "\"unavailable:not_resolved\",";
        }
        std::cout
                  << "\"monitor_adapter\":\""
                  << diagnostics::JsonEscape(EncoderVendorName(
                         selection.capture_vendor)) << "\","
                  << "\"monitor_adapter_luid\":"
                  << AdapterLuidJson(resolved_monitor_.adapter_luid) << ','
                  << "\"capture_d3d11_device\":\"selected-monitor-adapter\","
                  << "\"capture_device_luid\":"
                  << (capture_device_adapter_luid_available_
                      ? AdapterLuidJson(capture_device_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << ','
                  << "\"encoder_adapter\":\""
                  << diagnostics::JsonEscape(EncoderVendorName(
                         selection.encoder_vendor)) << "\","
                  << "\"encoder_adapter_luid\":"
                  << (encoder_adapter_luid_available_
                      ? AdapterLuidJson(encoder_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << ','
                  << "\"capture_backend\":\""
                  << diagnostics::JsonEscape(startup_capture_backend_) << "\","
                  << "\"encoder_backend\":\""
                  << diagnostics::JsonEscape(startup_encoder_backend_) << "\","
                  << "\"codec\":\""
                  << diagnostics::JsonEscape(startup_codec_) << "\","
                  << "\"source_dynamic_range\":\"" << (source_hdr_ ? "hdr" : "sdr") << "\","
                  << "\"encoded_dynamic_range\":\"" << (replay_hdr_ ? "hdr10" : "sdr") << "\","
                  << "\"capture_width\":" << width_ << ','
                  << "\"capture_height\":" << height_ << ','
                  << "\"encoder_width\":"
                  << (target_width_ == 0 ? crop_width_ : target_width_) << ','
                  << "\"encoder_height\":"
                  << (target_height_ == 0 ? crop_height_ : target_height_) << "}"
                  << std::endl;

        if (config.audio_enabled) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                      << "\"event\":\"system_audio_initialized\","
                      << "\"state\":\"" << (audio_active_ ? "READY" : "FAILED")
                      << "\",\"error_code\":"
                       << (audio_active_ ? "null" : "\"SYSTEM_AUDIO_INIT_FAILED\"")
                      << ",\"endpoint\":\""
                      << diagnostics::JsonEscape(audio_capture_.GetFriendlyName())
                      << "\",\"requested_sample_rate\":"
                      << audio_capture_.GetRequestedSampleRate()
                       << ",\"actual_sample_rate\":" << audio_capture_.GetInputSampleRate()
                      << ",\"requested_channels\":"
                      << audio_capture_.GetRequestedChannels()
                       << ",\"actual_channels\":" << audio_capture_.GetInputChannels()
                       << ",\"sample_format\":\""
                       << diagnostics::JsonEscape(audio_active_
                           ? audio_capture_.GetInputSampleFormat()
                           : "unavailable:initialization_failed") << "\""
                      << "}"
                      << std::endl;
            if (microphone_audio_source_) {
                const auto microphone_info = microphone_audio_source_->runtime_info();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                          << "\"event\":\"microphone_initialized\","
                          << "\"state\":\"STARTING\",\"endpoint\":\""
                          << diagnostics::JsonEscape(
                                 microphone_info.active_display_name.empty()
                                     ? "unavailable:provider_starting"
                                     : microphone_info.active_display_name)
                          << "\",\"requested_sample_rate\":48000,"
                          << "\"requested_channels\":2}"
                          << std::endl;
            }
        }

        // Start video capture and save-clip threads.
        // Audio is already running so both clocks start together.
        wgc_repeated_frame_observations_.store(0, std::memory_order_relaxed);
        running_.store(true);
        capture_generation_.fetch_add(1);
        LARGE_INTEGER capture_start_qpc{};
        if (QueryPerformanceCounter(&capture_start_qpc)) {
            capture_start_qpc_.store(capture_start_qpc.QuadPart,
                std::memory_order_release);
            generation_start_qpc_.store(capture_start_qpc.QuadPart,
                std::memory_order_release);
        }
        capture_health_flags_.store(CAPTURE_HEALTH_ACTIVE);

        if (minecraft_java_state_) {
            capture_thread_ = new std::thread(&CaptureEngine::CaptureThreadMinecraftJava, this);
        } else if (wgc_active_) {
            capture_thread_ = new std::thread(&CaptureEngine::CaptureThreadWGC, this);
        } else {
            capture_thread_ = new std::thread(&CaptureEngine::CaptureThread, this);
        }
        // NORMAL priority keeps us off-contention with game render threads.
        SetThreadPriority(capture_thread_->native_handle(), THREAD_PRIORITY_NORMAL);

        save_clip_thread_ = new std::thread(&CaptureEngine::SaveClipThread, this);
        stall_watchdog_thread_ = new std::thread(
            &CaptureEngine::ReplayStallWatchdogThread, this);

        std::cout << "[CaptureEngine] Running." << std::endl;
        return true;
    }



    void CaptureEngine::Shutdown() {
        const bool has_resources = capture_thread_ || stall_watchdog_thread_
            || save_clip_thread_
            || device_ || context_ || wgc_state_ || minecraft_java_state_
            || replay_encoder_ || audio_active_ || nvenc_device_
            || nvenc_context_ || record_writer_ || application_audio_source_manager_
            || !device_audio_tracks_.empty();
        if (!has_resources) return;

        std::cout << "[CaptureEngine] Shutting down..." << std::endl;

        bool has_recording_writer = false;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            has_recording_writer = static_cast<bool>(record_writer_);
        }
        if (is_recording_.load() || has_recording_writer) StopRecording();

        running_.store(false);

        // If CaptureThread is blocked waiting for a WGC frame, wake it so it exits.
        wgc_frame_cv_.notify_all();

        save_clip_queue_.Shutdown();

        if (stall_watchdog_thread_) {
            stall_watchdog_thread_->join();
            delete stall_watchdog_thread_;
            stall_watchdog_thread_ = nullptr;
        }

        if (capture_thread_) {
            capture_thread_->join();
            delete capture_thread_;
            capture_thread_ = nullptr;
        }

        if (save_clip_thread_) {
            save_clip_thread_->join();
            delete save_clip_thread_;
            save_clip_thread_ = nullptr;
        }

        // Finalize NVENC encoder after CaptureThread has exited
        // (guarantees no EncodeFrame call is in flight)
        if (nvenc_active_) {
            replay_encoder_->Shutdown();
        }
        nvenc_active_ = false;
        replay_encoder_cpu_input_ = false;

        // Stop audio pipeline. Order matters:
        //   1. Stop WASAPI thread (no more EncodeSamples calls after this)
        //   2. Finalize encoder (flushes partial AAC frame)
        //   3. compressed packet ring is released with the generation
        if (application_audio_source_manager_) {
            // Application providers can still be submitting AAC packets. Stop
            // and join their monitor/providers before touching any audio state.
            application_audio_source_manager_->Stop();
            application_audio_source_manager_.reset();
        }
        device_audio_tracks_.clear();
        if (audio_active_) {
            microphone_audio_source_.reset();
            audio_capture_.Stop();
            if (!default_mix_audio_encoder_.Finalize()) {
                std::cerr << "[CaptureEngine] Default Mix AAC finalization failed"
                          << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                             "\"event\":\"encoder_finalize_failed\","
                             "\"state\":\"FAILED\","
                             "\"error_code\":\"AUDIO_ENCODER_FAILED\","
                             "\"source\":\"system_audio\"}" << std::endl;
            }
            audio_capture_.Shutdown();
            default_mix_audio_ring_.reset();
            default_mix_audio_source_ = {};
            microphone_audio_metadata_ = {};
            audio_active_ = false;
            std::cout << "[CaptureEngine] Audio pipeline stopped." << std::endl;
        }
        else {
            microphone_audio_source_.reset();
        }

        ring_head_.store(0, std::memory_order_relaxed);
        ring_count_.store(0, std::memory_order_relaxed);

        // WGC session must be closed before releasing the D3D11 device it references.
        if (wgc_active_) {
            ShutdownWGC();
        }
        minecraft_java_state_.reset();

        ShutdownD3D11();

        // Release the Optimus NVENC device after D3D11 and NVENC are both shut down.
        if (nvenc_context_) { nvenc_context_->Release(); nvenc_context_ = nullptr; }
        if (nvenc_device_)  { nvenc_device_->Release();  nvenc_device_ = nullptr; }
        replay_encoder_.reset();
        encoded_ring_.reset();
        replay_storage_.reset();

        std::cout << "[CaptureEngine] Shutdown complete. Frames captured: "
            << frames_captured_.load() << std::endl;
    }

    static void WriteCaptureStageTiming(
        std::ostream& out, const char* stage, const CaptureStagePercentiles& timing) {
        out << '"' << stage << "\":{\"samples\":" << timing.samples
            << ",\"p95_us\":" << timing.p95_us << ",\"p99_us\":" << timing.p99_us << "},";
    }

    void CaptureEngine::ReplayStallWatchdogThread() {
        using clock = std::chrono::steady_clock;
        const bool trace_enabled = frame_pacing_trace::Enabled();
        uint64_t previous_acquire_attempts = 0;
        uint64_t previous_acquired = 0;
        uint64_t previous_submissions = 0;
        uint64_t previous_ring_insertions = 0;
        uint64_t previous_frames_dropped = 0;
        uint64_t previous_pointer_only_frames = 0;
        uint64_t previous_pointer_only_watchdog = pointer_only_frames_.load(
            std::memory_order_relaxed);
        uint32_t previous_recovery_attempts = 0;
        uint32_t previous_capture_generation = capture_generation_.load(
            std::memory_order_relaxed);
        uint64_t snapshot_previous_packets = 0;
        uint64_t snapshot_previous_acquired = 0;
        uint64_t snapshot_previous_submissions = 0;
        uint64_t snapshot_previous_ring_insertions = 0;
        uint64_t snapshot_previous_dxgi_coalesced_extra_frames = 0;
        uint64_t snapshot_previous_dxgi_acquire_successes = 0;
        uint64_t snapshot_previous_dxgi_acquire_timeouts = 0;
        uint64_t snapshot_previous_dxgi_skipped_slots = 0;
        uint64_t snapshot_previous_dxgi_held_presentations = 0;
        uint64_t snapshot_previous_wgc_missed_deadlines = 0;
        bool cadence_interval_interrupted = false;
        auto last_stall_progress = clock::now();
        capture_pipeline_health::EncoderOutputProgress output_progress(last_stall_progress);
        auto last_acquire_progress = clock::now();
        auto last_acquired_progress = clock::now();
        auto last_submission_progress = clock::now();
        auto last_ring_progress = clock::now();
        auto last_pointer_only_progress = clock::now();
        auto last_health_snapshot = clock::now();
        bool snapshot_emitted = false;

        while (running_.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
            if (ReplayWatchdogSuspended(
                    capture_health_flags_.load(std::memory_order_relaxed))) {
                // The capture thread owns DXGI recovery. The watchdog must not
                // cancel its bounded backoff or diagnose the intentional gap
                // as a second, unrelated pipeline stall. A focus-gated game
                // also intentionally stops submitting while tabbed out.
                const auto recovering_now = clock::now();
                output_progress.Rebaseline(recovering_now);
                last_stall_progress = recovering_now;
                last_acquire_progress = recovering_now;
                last_acquired_progress = recovering_now;
                last_submission_progress = recovering_now;
                last_ring_progress = recovering_now;
                cadence_interval_interrupted = true;
                continue;
            }
            const uint64_t packets = video_packets_produced_.load(
                std::memory_order_relaxed);
            const uint64_t acquire_attempts = capture_acquire_attempts_.load(
                std::memory_order_relaxed);
            const uint64_t acquired = source_textures_received_.load(
                std::memory_order_relaxed);
            const uint64_t submissions = frames_captured_.load(
                std::memory_order_relaxed);
            const uint64_t ring_insertions = video_ring_insertions_.load(
                std::memory_order_relaxed);
            const auto now = clock::now();
            const bool output_advanced = output_progress.Observe(packets, now);
            if (acquire_attempts != previous_acquire_attempts) {
                previous_acquire_attempts = acquire_attempts;
                last_acquire_progress = now;
            }
            if (acquired != previous_acquired) {
                previous_acquired = acquired;
                last_acquired_progress = now;
            }
            if (submissions != previous_submissions) {
                previous_submissions = submissions;
                last_submission_progress = now;
            }
            if (ring_insertions != previous_ring_insertions) {
                previous_ring_insertions = ring_insertions;
                last_ring_progress = now;
            }
            const uint64_t pointer_only_watchdog = pointer_only_frames_.load(
                std::memory_order_relaxed);
            if (pointer_only_watchdog != previous_pointer_only_watchdog) {
                previous_pointer_only_watchdog = pointer_only_watchdog;
                last_pointer_only_progress = now;
            }
            if (now - last_health_snapshot >= std::chrono::seconds(10)) {
                const auto observation_interval_ms = std::chrono::duration_cast<
                    std::chrono::milliseconds>(now - last_health_snapshot).count();
                last_health_snapshot = now;
                const auto acquired_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(now - last_acquired_progress).count();
                const auto submission_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(now - last_submission_progress).count();
                const auto output_age = output_progress.AgeMs(now);
                const auto ring_age = std::chrono::duration_cast<
                    std::chrono::milliseconds>(
                        now - last_ring_progress).count();
                const uint32_t health = capture_health_flags_.load(
                    std::memory_order_relaxed);
                const uint64_t dropped = frames_dropped_.load(
                    std::memory_order_relaxed);
                const uint64_t pointer_only = pointer_only_frames_.load(
                    std::memory_order_relaxed);
                const uint32_t recovery_attempts = capture_recovery_attempts_.load(
                    std::memory_order_relaxed);
                const uint32_t capture_generation = capture_generation_.load(
                    std::memory_order_relaxed);
                const uint64_t dxgi_coalesced_extra_frames =
                    dxgi_coalesced_extra_frames_.load(
                        std::memory_order_relaxed);
                const uint64_t interval_dxgi_coalesced_extra_frames =
                    capture_pipeline_health::SaturatingDifference(
                        dxgi_coalesced_extra_frames,
                        snapshot_previous_dxgi_coalesced_extra_frames);
                const uint64_t dxgi_acquire_successes =
                    capture_acquire_successes_.load(
                        std::memory_order_relaxed);
                const uint64_t dxgi_acquire_timeouts = capture_timeouts_.load(
                    std::memory_order_relaxed);
                const uint64_t interval_dxgi_acquire_successes =
                    capture_pipeline_health::SaturatingDifference(
                        dxgi_acquire_successes,
                        snapshot_previous_dxgi_acquire_successes);
                const uint64_t interval_dxgi_acquire_timeouts =
                    capture_pipeline_health::SaturatingDifference(
                        dxgi_acquire_timeouts,
                        snapshot_previous_dxgi_acquire_timeouts);
                const uint64_t dxgi_skipped_slots = dxgi_skipped_capture_slots_.load(
                    std::memory_order_relaxed);
                const uint64_t interval_dxgi_skipped_slots =
                    capture_pipeline_health::SaturatingDifference(
                        dxgi_skipped_slots, snapshot_previous_dxgi_skipped_slots);
                const uint64_t dxgi_held_presentations =
                    dxgi_held_presentations_.load(std::memory_order_relaxed);
                const uint64_t interval_dxgi_held_presentations =
                    capture_pipeline_health::SaturatingDifference(
                        dxgi_held_presentations,
                        snapshot_previous_dxgi_held_presentations);
                const uint64_t dxgi_resampled_presentations =
                    dxgi_resampled_presentations_.load(
                        std::memory_order_relaxed);
                const uint64_t interval_pointer_only_frames =
                    capture_pipeline_health::SaturatingDifference(
                        pointer_only, previous_pointer_only_frames);
                const uint64_t interval_image_acquire_successes =
                    capture_pipeline_health::SaturatingDifference(
                        interval_dxgi_acquire_successes,
                        interval_pointer_only_frames);
                const uint64_t interval_pacing_drops =
                    capture_pipeline_health::SaturatingDifference(
                        capture_pipeline_health::SaturatingDifference(
                            dropped, previous_frames_dropped),
                        interval_pointer_only_frames);
                const uint64_t interval_recovery_attempts =
                    recovery_attempts >= previous_recovery_attempts
                    ? recovery_attempts - previous_recovery_attempts : 0;
                const uint64_t interval_generation_resets =
                    capture_generation >= previous_capture_generation
                    ? static_cast<uint64_t>(capture_generation)
                        - previous_capture_generation : 0;
                previous_frames_dropped = dropped;
                previous_pointer_only_frames = pointer_only;
                previous_recovery_attempts = recovery_attempts;
                previous_capture_generation = capture_generation;
                const auto encoder_diagnostics = replay_encoder_
                    ? replay_encoder_->GetDiagnostics()
                    : ReplayEncoderDiagnostics{};
                const bool encoder_diagnostics_observed = replay_encoder_ != nullptr
                    && (encoder_diagnostics.encode_attempts > 0
                        || encoder_diagnostics.packets_produced > 0);
                const bool encoder_idle = encoder_diagnostics_observed
                    && encoder_diagnostics.pending_resources == 0
                    && encoder_diagnostics.queued_outputs == 0
                    && encoder_diagnostics.locked_bitstreams == 0
                    && encoder_diagnostics.submit_stage == 0
                    && encoder_diagnostics.drain_stage == 0;
                const bool has_pending_work = replay_encoder_ != nullptr
                    && (encoder_diagnostics.pending_resources > 0
                        || encoder_diagnostics.queued_outputs > 0
                        || encoder_diagnostics.locked_bitstreams > 0
                        || encoder_diagnostics.submit_stage > 0
                        || encoder_diagnostics.drain_stage > 0);
                const HRESULT last_capture_hresult = static_cast<HRESULT>(
                    last_capture_hresult_.load(std::memory_order_relaxed));
                LARGE_INTEGER health_qpc{};
                LARGE_INTEGER health_qpc_frequency{};
                const bool health_clock_valid =
                    QueryPerformanceCounter(&health_qpc)
                    && QueryPerformanceFrequency(&health_qpc_frequency)
                    && health_qpc.QuadPart > 0
                    && health_qpc_frequency.QuadPart > 0;
                int64_t maximum_api_gap_qpc = 0;
                if (health_clock_valid && fps_ > 0) {
                    const int64_t frequency = health_qpc_frequency.QuadPart;
                    const int64_t fps = static_cast<int64_t>(fps_);
                    const int64_t nominal_period = frequency / fps
                        + (frequency % fps != 0 ? 1 : 0);
                    const int64_t four_periods = nominal_period
                        <= (std::numeric_limits<int64_t>::max)() / 4
                        ? nominal_period * 4
                        : (std::numeric_limits<int64_t>::max)();
                    const int64_t quarter_second = frequency / 4
                        + (frequency % 4 != 0 ? 1 : 0);
                    maximum_api_gap_qpc = std::max(
                        quarter_second, four_periods);
                }
                const int64_t last_dxgi_completed_qpc =
                    last_dxgi_completed_qpc_.load(std::memory_order_acquire);
                const int64_t last_dxgi_pointer_only_qpc =
                    last_dxgi_pointer_only_completed_qpc_.load(
                        std::memory_order_acquire);
                const auto qpc_is_recent = [&](int64_t qpc) {
                    return health_clock_valid && maximum_api_gap_qpc > 0
                        && qpc > 0 && qpc <= health_qpc.QuadPart
                        && health_qpc.QuadPart - qpc <= maximum_api_gap_qpc;
                };
                const bool api_completion_recent = qpc_is_recent(
                    last_dxgi_completed_qpc);
                const bool dxgi_backend = !wgc_active_ && !minecraft_java_state_;
                const bool acquire_loop_recent = dxgi_backend
                    && api_completion_recent;
                const bool wait_timeout_current = dxgi_backend
                    && api_completion_recent
                    && last_capture_hresult == DXGI_ERROR_WAIT_TIMEOUT;
                const bool pointer_only_recent = dxgi_backend
                    && qpc_is_recent(last_dxgi_pointer_only_qpc);
                const bool idle_evidence = dxgi_backend
                    && dxgi::IsDxgiAliveButDesktopStatic(
                        packets > 0, acquire_loop_recent, encoder_idle,
                        wait_timeout_current, pointer_only_recent);
                capture_pipeline_health::Snapshot pipeline;
                pipeline.backend_failed =
                    (health & CAPTURE_HEALTH_BACKEND_FAILED) != 0;
                pipeline.intentional_pause_or_recovery =
                    ReplayWatchdogSuspended(health);
                pipeline.wgc_backend = wgc_active_;
                pipeline.expected_fps = static_cast<double>(fps_);
                pipeline.observation_interval_ms = observation_interval_ms;
                pipeline.source_frames = acquired;
                pipeline.raw_source_frame_delta = !dxgi_backend
                    ? capture_pipeline_health::SaturatingDifference(
                        acquired, snapshot_previous_acquired)
                    : interval_image_acquire_successes;
                pipeline.accepted_source_frame_delta =
                    capture_pipeline_health::SaturatingDifference(
                        acquired, snapshot_previous_acquired);
                pipeline.pacing_drop_frames =
                    capture_pipeline_health::SaturatingDifference(
                        dropped, pointer_only);
                pipeline.interval_pacing_drops = interval_pacing_drops;
                pipeline.interval_recovery_attempts = interval_recovery_attempts;
                pipeline.interval_generation_resets = interval_generation_resets;
                pipeline.repeated_frame_observations =
                    wgc_repeated_frame_observations_.load(
                        std::memory_order_relaxed);
                pipeline.held_presentation_observations =
                    interval_dxgi_held_presentations;
                pipeline.api_completion_recent = api_completion_recent;
                pipeline.dxgi_replay_cache_armed =
                    dxgi_replay_cache_armed_.load(std::memory_order_acquire);
                pipeline.encoder_submissions = submissions;
                pipeline.encoded_packets = packets;
                pipeline.replay_ring_insertions = ring_insertions;
                pipeline.source_age_ms = acquired_age;
                pipeline.encoder_submission_age_ms = submission_age;
                pipeline.encoded_output_age_ms = output_age;
                pipeline.replay_ring_insertion_age_ms = ring_age;
                pipeline.acquire_loop_recent = acquire_loop_recent;
                pipeline.wait_timeout_current = wait_timeout_current;
                pipeline.pointer_only_recent = pointer_only_recent;
                pipeline.idle_evidence = idle_evidence;
                pipeline.encoder_idle = encoder_idle;
                pipeline.has_pending_work = has_pending_work;
                const auto classification = capture_pipeline_health::Classify(
                    pipeline);
                const uint64_t submission_delta = capture_pipeline_health::SaturatingDifference(
                    submissions, snapshot_previous_submissions);
                const uint64_t output_delta = capture_pipeline_health::SaturatingDifference(
                    packets, snapshot_previous_packets);
                const uint64_t wgc_missed_deadlines = wgc_missed_output_deadlines_.load(
                    std::memory_order_relaxed);
                const auto output_cadence = capture_pipeline_health::ClassifyOutputCadence(
                    pipeline, output_delta, snapshot_previous_packets > 0,
                    cadence_interval_interrupted);
                cadence_interval_interrupted = false;
                const bool diagnostic_candidate =
                    capture_pipeline_health::IsDiagnosticCandidate(
                        classification);
                const auto content_sampling = content_sampler_.Stats();
                std::ostringstream stage_timings;
                WriteCaptureStageTiming(stage_timings, "wgc_acquisition", wgc_acquisition_timings_.Take());
                const auto wgc_readback = wgc_readback_timings_.Take();
                WriteCaptureStageTiming(stage_timings, "wgc_readback", wgc_readback);
                WriteCaptureStageTiming(stage_timings, "wgc_scale", wgc_scale_timings_.Take());
                WriteCaptureStageTiming(stage_timings, "wgc_submit", wgc_submit_timings_.Take());
                WriteCaptureStageTiming(stage_timings, "nvenc_drain",
                    replay_encoder_ ? replay_encoder_->TakeDrainTimings() : CaptureStagePercentiles{});
                if (trace_enabled) {
                    const auto staging_copy = wgc_readback_copy_timings_.Take();
                    const auto staging_map = wgc_readback_map_timings_.Take();
                    frame_pacing_trace::Emit("native_capture_interval", 0,
                        frame_pacing_trace::Qpc(), [&](std::ostream& line) {
                            line << ",\"stages\":{";
                            WriteCaptureStageTiming(line, "wgc_readback", wgc_readback);
                            WriteCaptureStageTiming(line, "wgc_copy_submit", staging_copy);
                            WriteCaptureStageTiming(line, "wgc_map", staging_map);
                            line << "\"wgc_missed_output_deadlines\":"
                                 << capture_pipeline_health::SaturatingDifference(
                                     wgc_missed_deadlines, snapshot_previous_wgc_missed_deadlines)
                                 << "}";
                        });
                }
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"capture_health_snapshot\","
                          << "\"frames_acquired\":" << acquired << ','
                          << "\"frames_submitted\":" << submissions << ','
                          << "\"frames_dropped\":" << frames_dropped_.load() << ','
                          << "\"capture_generation\":" << capture_generation << ','
                          << "\"observation_interval_ms\":"
                          << observation_interval_ms << ','
                          << "\"target_fps\":" << fps_ << ','
                          << "\"accepted_source_frame_delta\":"
                          << pipeline.accepted_source_frame_delta << ','
                          << "\"encoder_submission_delta\":" << submission_delta << ','
                          << "\"encoded_output_delta\":" << output_delta << ','
                          << "\"interval_wgc_missed_output_deadlines\":"
                          << capture_pipeline_health::SaturatingDifference(
                                 wgc_missed_deadlines, snapshot_previous_wgc_missed_deadlines) << ','
                          << "\"output_cadence\":\""
                          << capture_pipeline_health::OutputCadenceName(output_cadence) << "\","
                          << "\"encoder_queue_depth\":" << encoder_diagnostics.queued_outputs << ','
                          << "\"encoder_pending_resources\":" << encoder_diagnostics.pending_resources << ','
                          << "\"encoder_queue_capacity\":" << encoder_diagnostics.pool_capacity << ','
                          << stage_timings.str()
                          << "\"accepted_source_fps\":"
                          << (observation_interval_ms > 0
                              ? static_cast<double>(pipeline.accepted_source_frame_delta)
                                  * 1000.0 / observation_interval_ms : 0.0) << ','
                          << "\"raw_source_frame_delta\":"
                          << pipeline.raw_source_frame_delta << ','
                          << "\"encoder_input_fps\":"
                          << (observation_interval_ms > 0
                              ? static_cast<double>(submission_delta)
                                  * 1000.0 / observation_interval_ms : 0.0) << ','
                          << "\"encoded_output_fps\":"
                          << (observation_interval_ms > 0
                              ? static_cast<double>(output_delta)
                                  * 1000.0 / observation_interval_ms : 0.0) << ','
                          << "\"replay_ring_fps\":"
                          << (observation_interval_ms > 0
                              ? static_cast<double>(
                                  capture_pipeline_health::SaturatingDifference(
                                      ring_insertions, snapshot_previous_ring_insertions))
                                  * 1000.0 / observation_interval_ms : 0.0) << ','
                          << "\"pacing_drop_frames\":"
                          << pipeline.pacing_drop_frames << ','
                          << "\"interval_pacing_drops\":"
                          << interval_pacing_drops << ','
                          << "\"interval_pointer_only_frames\":"
                          << interval_pointer_only_frames << ','
                          << "\"interval_recovery_attempts\":"
                          << interval_recovery_attempts << ','
                          << "\"interval_generation_resets\":"
                          << interval_generation_resets << ','
                          << "\"dxgi_coalesced_extra_frames\":"
                          << dxgi_coalesced_extra_frames << ','
                          << "\"interval_dxgi_coalesced_extra_frames\":"
                          << interval_dxgi_coalesced_extra_frames << ','
                          << "\"dxgi_skipped_capture_slots\":"
                          << dxgi_skipped_slots << ','
                          << "\"interval_dxgi_skipped_capture_slots\":"
                          << interval_dxgi_skipped_slots << ','
                          << "\"dxgi_acquire_wait_ms\":"
                          << dxgi_acquire_wait_ms_.load(std::memory_order_relaxed) << ','
                          << "\"dxgi_processing_budget_us\":"
                          << dxgi_processing_budget_us_.load(std::memory_order_relaxed) << ','
                          << "\"dxgi_acquire_successes\":"
                          << dxgi_acquire_successes << ','
                          << "\"dxgi_acquire_timeouts\":"
                          << dxgi_acquire_timeouts << ','
                          << "\"interval_dxgi_acquire_successes\":"
                          << interval_dxgi_acquire_successes << ','
                          << "\"interval_dxgi_acquire_timeouts\":"
                          << interval_dxgi_acquire_timeouts << ','
                          << "\"dxgi_held_presentations\":"
                          << dxgi_held_presentations << ','
                          << "\"interval_dxgi_held_presentations\":"
                          << interval_dxgi_held_presentations << ','
                          << "\"dxgi_resampled_presentations\":"
                          << dxgi_resampled_presentations << ','
                          << "\"dxgi_acquire_success_fps\":"
                          << (observation_interval_ms > 0
                              ? static_cast<double>(interval_dxgi_acquire_successes)
                                  * 1000.0 / observation_interval_ms : 0.0) << ','
                          << "\"stale_frame_observations\":"
                          << content_suspicious_streak_.load() << ','
                          << "\"content_samples_queued\":" << content_sampling.queued << ','
                          << "\"content_samples_completed\":" << content_sampling.completed << ','
                          << "\"content_samples_busy\":" << content_sampling.busy << ','
                          << "\"content_samples_skipped\":" << content_sampling.skipped << ','
                          << "\"content_sample_errors\":" << content_sampling.errors << ','
                          << "\"content_sample_max_call_ns\":" << content_sampling.max_call_ns << ','
                          << "\"duplicate_frame_observations\":"
                          << wgc_repeated_frame_observations_.load() << ','
                          << "\"replay_ring_insertions\":"
                          << ring_insertions << ','
                          << "\"replay_ring_rejections\":"
                          << capture_pipeline_health::SaturatingDifference(
                                 packets, ring_insertions) << ','
                          << "\"pipeline_classification\":\""
                          << capture_pipeline_health::ClassificationName(
                                 classification) << "\","
                          << "\"diagnostic_candidate\":"
                          << (diagnostic_candidate ? "true" : "false") << ','
                          << "\"acquire_loop_recent\":"
                          << (pipeline.acquire_loop_recent ? "true" : "false") << ','
                          << "\"api_completion_recent\":"
                          << (pipeline.api_completion_recent ? "true" : "false") << ','
                          << "\"dxgi_replay_cache_armed\":"
                          << (pipeline.dxgi_replay_cache_armed ? "true" : "false") << ','
                          << "\"last_source_qpc\":"
                          << last_source_frame_qpc_.load(
                                 std::memory_order_acquire) << ','
                          << "\"last_presentation_qpc\":"
                          << last_dxgi_presentation_qpc_.load(
                                 std::memory_order_acquire) << ','
                          << "\"last_dxgi_completed_qpc\":"
                          << last_dxgi_completed_qpc << ','
                          << "\"wait_timeout_current\":"
                          << (pipeline.wait_timeout_current ? "true" : "false") << ','
                          << "\"pointer_only_recent\":"
                          << (pipeline.pointer_only_recent ? "true" : "false") << ','
                          << "\"idle_evidence\":"
                          << (pipeline.idle_evidence ? "true" : "false") << ','
                          << "\"encoder_diagnostics_observed\":"
                          << (encoder_diagnostics_observed ? "true" : "false") << ','
                          << "\"encoder_idle\":"
                          << (pipeline.encoder_idle ? "true" : "false") << ','
                          << "\"encoder_has_pending_work\":"
                          << (pipeline.has_pending_work ? "true" : "false") << ','
                          << "\"last_capture_hresult\":"
                          << static_cast<int32_t>(last_capture_hresult) << ','
                          << "\"pointer_only_frames\":"
                          << pointer_only_frames_.load() << ','
                          << "\"encoder_submissions\":" << submissions << ','
                          << "\"encoded_packets\":" << packets << ','
                          << "\"last_acquired_age_ms\":" << acquired_age << ','
                          << "\"last_encoder_submission_age_ms\":"
                          << submission_age << ','
                          << "\"last_encoded_output_age_ms\":" << output_age << ','
                          << "\"last_replay_ring_insertion_age_ms\":"
                          << ring_age << ','
                          << "\"capture_restart_count\":"
                          << capture_restart_count_.load() << ','
                          << "\"capture_recovery_attempts\":"
                          << capture_recovery_attempts_.load() << ','
                          << "\"capture_recovery_failures\":"
                          << capture_recovery_failures_.load() << ','
                          << "\"encoder_restart_count\":0}"
                          << std::endl;
                snapshot_previous_packets = packets;
                snapshot_previous_acquired = acquired;
                snapshot_previous_submissions = submissions;
                snapshot_previous_ring_insertions = ring_insertions;
                snapshot_previous_dxgi_coalesced_extra_frames =
                    dxgi_coalesced_extra_frames;
                snapshot_previous_dxgi_acquire_successes =
                    dxgi_acquire_successes;
                snapshot_previous_dxgi_skipped_slots = dxgi_skipped_slots;
                snapshot_previous_dxgi_acquire_timeouts =
                    dxgi_acquire_timeouts;
                snapshot_previous_dxgi_held_presentations =
                    dxgi_held_presentations;
                snapshot_previous_wgc_missed_deadlines = wgc_missed_deadlines;

                if (audio_active_) {
                    const uint64_t system_packets = audio_capture_.GetPacketCount();
                    const bool system_failed = audio_capture_.IsDeviceLost();
                    const bool system_recovering = audio_capture_.IsRecoveryPending();
                    const char* system_error = system_failed
                        ? audio_capture_.GetFailureReasonName()
                        : system_recovering ? "SYSTEM_AUDIO_RECOVERING"
                        : system_packets == 0 ? "SYSTEM_AUDIO_NO_PACKETS"
                        : audio_capture_.GetDiscontinuityCount() > 0
                            ? "AUDIO_PACKET_DISCONTINUITY" : nullptr;
                    const uint64_t system_discontinuities =
                        audio_capture_.GetDiscontinuityCount();
                    const auto system_silence_corrections =
                        audio_capture_.GetSilenceCorrectionDistribution();
                    const auto system_skip_corrections =
                        audio_capture_.GetSkipCorrectionDistribution();
                    const auto system_resampler_corrections =
                        audio_capture_.GetResamplerCorrectionDistribution();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"audio_health_snapshot\","
                              << "\"source\":\"system_audio\","
                              << "\"state\":\""
                              << (system_failed ? "FAILED" : system_recovering ? "RECOVERING"
                                  : system_packets == 0 ? "NO_PACKETS" : "ACTIVE") << "\","
                              << "\"error_code\":"
                              << (system_error ? "\"" : "null")
                              << (system_error ? system_error : "")
                              << (system_error ? "\"" : "") << ','
                              << "\"endpoint\":\""
                              << diagnostics::JsonEscape(audio_capture_.GetFriendlyName())
                              << "\",\"actual_sample_rate\":"
                              << audio_capture_.GetInputSampleRate()
                              << ",\"actual_channels\":"
                              << audio_capture_.GetInputChannels()
                              << ",\"actual_sample_format\":\""
                              << diagnostics::JsonEscape(audio_capture_.GetInputSampleFormat())
                              << "\",\"internal_sample_rate\":"
                              << audio_capture_.GetSampleRate()
                              << ",\"internal_channels\":"
                              << audio_capture_.GetChannels()
                              << ",\"internal_sample_format\":\"float32\","
                              << "\"packet_count\":" << system_packets << ','
                              << "\"timestamp_error_packets\":"
                              << audio_capture_.GetTimestampErrorPacketCount() << ','
                              << "\"processing_time_fallback_packets\":"
                              << audio_capture_.GetProcessingTimeFallbackCount() << ','
                              << "\"unavailable_timestamp_packets\":"
                              << audio_capture_.GetUnavailableTimestampCount() << ','
                              << "\"timestamp_provenance\":\""
                              << (audio_capture_.GetUnavailableTimestampCount() > 0
                                  ? "unavailable:no_valid_packet_or_processing_clock"
                                  : "packet_qpc_100ns_or_processing_time_approximation") << "\","
                              << "\"discontinuity_count\":"
                              << system_discontinuities << ','
                              << "\"silent_packet_count\":"
                              << audio_capture_.GetSilentPacketCount() << ','
                              << "\"no_packet_intervals\":"
                              << audio_capture_.GetNoPacketIntervalCount() << ','
                              << "\"converted_frames\":"
                              << audio_capture_.GetConvertedFrameCount() << ','
                              << "\"submitted_frames\":"
                              << audio_capture_.GetSubmittedFrameCount() << ','
                              << "\"encoded_packets\":"
                              << audio_capture_.GetEncodedPacketCount() << ','
                              << "\"encoder_errors\":"
                              << audio_capture_.GetEncoderErrorCount() << ','
                              << "\"largest_no_packet_gap_100ns\":"
                              << audio_capture_.GetLargestNoPacketGap100ns() << ','
                              << "\"max_drift_samples\":"
                              << audio_capture_.GetMaxObservedDriftSamples() << ','
                              << "\"restart_count\":"
                              << audio_capture_.GetRestartCount() << ','
                              << "\"underrun_count\":"
                              << "\"unavailable:not_exposed_by_wasapi_capture\","
                              << "\"first_packet_timestamp_100ns\":"
                              << audio_capture_.GetFirstPacketQpc100ns() << ','
                              << "\"last_packet_timestamp_100ns\":"
                              << audio_capture_.GetLastPacketQpc100ns() << ','
                              << "\"synthetic_silence_frames\":"
                              << audio_capture_.GetSyntheticSilenceFrameCount() << ','
                              << "\"skipped_input_frames\":"
                              << audio_capture_.GetSkippedInputFrameCount() << ','
                              << "\"resampler_correction_count\":"
                              << audio_capture_.GetResamplerCorrectionCount() << ','
                              << "\"normal_jitter_absorbed_count\":"
                              << audio_capture_.GetNormalJitterAbsorbedCount() << ','
                              << "\"hard_discontinuity_recovery_count\":"
                              << audio_capture_.GetHardDiscontinuityRecoveryCount() << ','
                              << "\"silence_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     system_silence_corrections) << ','
                              << "\"skip_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     system_skip_corrections) << ','
                              << "\"resampler_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     system_resampler_corrections) << ','
                              << "\"native_input_signal\":"
                              << AudioSignalMetricsJson(
                                     audio_capture_.GetNativeInputSignalMetrics()) << ','
                              << "\"converter_output_signal\":"
                              << AudioSignalMetricsJson(
                                     audio_capture_.GetCanonicalOutputSignalMetrics()) << ','
                              << "\"encoder_input_signal\":"
                              << AudioSignalMetricsJson(
                                     audio_capture_.GetEncoderInputSignalMetrics()) << "}"
                              << std::endl;
                }
                if (microphone_audio_source_) {
                    const auto microphone = microphone_audio_source_->runtime_info();
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"audio_health_snapshot\","
                              << "\"source\":\"microphone\","
                              << "\"state\":\""
                              << (microphone.failed ? "FAILED"
                                  : (microphone.packet_count == 0
                                      ? "NO_PACKETS" : "ACTIVE")) << "\","
                              << "\"error_code\":"
                              << (microphone.failed
                                  ? "\"MIC_INIT_FAILED\""
                                  : (microphone.packet_count == 0
                                      ? "\"MIC_NO_PACKETS\""
                                      : (microphone.discontinuity_count > 0
                                          ? "\"AUDIO_PACKET_DISCONTINUITY\""
                                          : "null"))) << ','
                              << "\"endpoint\":\""
                              << diagnostics::JsonEscape(
                                     microphone.active_display_name.empty()
                                         ? "unavailable:provider_starting"
                                         : microphone.active_display_name) << "\","
                              << "\"requested_sample_rate\":48000,"
                              << "\"actual_sample_rate\":"
                              << microphone.input_format.sample_rate << ','
                              << "\"requested_channels\":2,"
                              << "\"actual_channels\":"
                              << microphone.input_format.channels << ','
                              << "\"sample_format\":\""
                              << diagnostics::JsonEscape(
                                     microphone.input_format.sample_format)
                              << "\","
                              << "\"packet_count\":" << microphone.packet_count << ','
                              << "\"capture_buffer_frames\":"
                              << microphone.processing_diagnostics.capture_buffer_frames << ','
                              << "\"capture_buffer_duration_100ns\":"
                              << microphone.processing_diagnostics.capture_buffer_duration_100ns
                              << ','
                              << "\"timestamp_error_count\":"
                              << microphone.processing_diagnostics.timestamp_error_count << ','
                              << "\"max_packet_processing_duration_100ns\":"
                              << microphone.processing_diagnostics
                                     .max_packet_processing_duration_100ns << ','
                              << "\"packet_processing_at_or_above_buffer_duration_count\":"
                              << microphone.processing_diagnostics
                                     .packet_processing_at_or_above_buffer_duration_count << ','
                              << "\"converted_frames\":"
                              << microphone.converted_frame_count << ','
                              << "\"encoded_packets\":"
                              << microphone.encoded_packet_count << ','
                              << "\"encoder_errors\":"
                              << microphone.encoder_error_count << ','
                              << "\"rejected_packets\":"
                              << microphone.rejected_packet_count << ','
                              << "\"discontinuity_count\":"
                              << microphone.discontinuity_count << ','
                              << "\"largest_no_packet_gap_100ns\":"
                              << microphone.largest_no_packet_gap_100ns << ','
                              << "\"underrun_count\":"
                              << "\"unavailable:not_exposed_by_wasapi_capture\","
                              << "\"first_packet_timestamp_100ns\":"
                              << microphone.first_packet_qpc_100ns << ','
                              << "\"last_packet_timestamp_100ns\":"
                              << microphone.last_packet_qpc_100ns << ','
                              << "\"max_drift_samples\":"
                              << microphone.max_observed_drift_samples << ','
                              << "\"configured_input_gain\":"
                              << microphone.configured_input_gain << ','
                              << "\"synthetic_silence_frames\":"
                              << microphone.synthetic_silence_frame_count << ','
                              << "\"skipped_input_frames\":"
                              << microphone.skipped_input_frame_count << ','
                              << "\"resampler_correction_count\":"
                              << microphone.resampler_correction_count << ','
                              << "\"normal_jitter_absorbed_count\":"
                              << microphone.normal_jitter_absorbed_count << ','
                              << "\"hard_discontinuity_recovery_count\":"
                              << microphone.hard_discontinuity_recovery_count << ','
                              << "\"silence_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     microphone.silence_corrections) << ','
                              << "\"skip_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     microphone.skip_corrections) << ','
                              << "\"resampler_correction_distribution\":"
                              << AudioCorrectionDistributionJson(
                                     microphone.resampler_corrections) << ','
                              << "\"native_input_signal\":"
                              << AudioSignalMetricsJson(microphone.native_input_signal) << ','
                              << "\"converter_output_signal\":"
                              << AudioSignalMetricsJson(microphone.converter_output_signal) << ','
                              << "\"encoder_input_signal\":"
                              << AudioSignalMetricsJson(microphone.encoder_input_signal) << ','
                              << "\"failed\":"
                              << (microphone.failed ? "true" : "false") << "}"
                              << std::endl;
                }
            }
            if (output_advanced) {
                last_stall_progress = now;
                snapshot_emitted = false;
                continue;
            }
            const bool awaiting_first_packet = packets == 0;
            const auto stall_limit = awaiting_first_packet
                ? std::chrono::seconds(8)
                : std::chrono::seconds(2);
            if (snapshot_emitted || now - last_stall_progress <= stall_limit) {
                continue;
            }

            const auto encoder = replay_encoder_
                ? replay_encoder_->GetDiagnostics()
                : ReplayEncoderDiagnostics{};
            const bool encoder_idle = encoder.pending_resources == 0
                && encoder.queued_outputs == 0
                && encoder.locked_bitstreams == 0
                && encoder.submit_stage == 0
                && encoder.drain_stage == 0;
            const bool encoder_has_pending_work = !encoder_idle;
            const bool dxgi_is_alive_but_desktop_is_static =
                dxgi::IsDxgiAliveButDesktopStatic(
                    !awaiting_first_packet,
                    now - last_acquire_progress < std::chrono::seconds(1),
                    encoder_idle,
                    static_cast<HRESULT>(last_capture_hresult_.load(
                        std::memory_order_relaxed)) == DXGI_ERROR_WAIT_TIMEOUT,
                    now - last_pointer_only_progress < std::chrono::seconds(1));
            if (dxgi_is_alive_but_desktop_is_static) {
                // Desktop Duplication reports only changed frames. Repeated
                // WAIT_TIMEOUT or S_OK pointer-only activity with a live
                // acquire loop and an empty encoder is expected and must not
                // be diagnosed as a replay stall.
                last_stall_progress = now;
                continue;
            }
            const HRESULT removed_reason = device_
                ? device_->GetDeviceRemovedReason()
                : E_POINTER;
            std::cerr
                << "[ReplayStall] no encoded packet for >2s"
                << " capture_stage=" << capture_thread_stage_.load()
                << " loops=" << capture_loop_iterations_.load()
                << " acquire_attempts=" << capture_acquire_attempts_.load()
                << " acquired=" << capture_acquire_successes_.load()
                << " timeouts=" << capture_timeouts_.load()
                << " released=" << capture_frames_released_.load()
                << " owned=" << (capture_acquire_successes_.load()
                    - capture_frames_released_.load())
                << " textures=" << source_textures_received_.load()
                << " conversion_submit=" << conversion_submissions_.load()
                << " conversion_complete=" << conversion_completions_.load()
                << " frames=" << frames_captured_.load()
                << " packets=" << video_packets_produced_.load()
                << " ring_push=" << video_ring_insertions_.load()
                << " last_hr=0x" << std::hex
                << static_cast<uint32_t>(last_capture_hresult_.load())
                << " removed_reason=0x"
                << static_cast<uint32_t>(removed_reason) << std::dec
                << " encoder_submit_stage=" << encoder.submit_stage
                << " encoder_drain_stage=" << encoder.drain_stage
                << " backpressure=" << encoder.backpressure_events
                << " backend_errors=" << encoder.backend_errors
                << " device_removed_errors=" << encoder.device_removed_errors
                << " slots_acquired=" << encoder.input_slots_acquired
                << " map=" << encoder.maps_succeeded << '/'
                << encoder.map_attempts
                << " mapped_now=" << encoder.mapped_resources
                << " encode_return=" << encoder.encode_returns << '/'
                << encoder.encode_attempts
                << " encode_ok=" << encoder.encode_successes
                << " drain=" << encoder.drain_dequeues
                << " completion=" << encoder.completion_events
                << " lock=" << encoder.bitstream_locks << '/'
                << encoder.bitstream_lock_attempts
                << " locked_now=" << encoder.locked_bitstreams
                << " unlock=" << encoder.bitstream_unlocks
                << " unmap=" << encoder.resources_unmapped
                << " recycled=" << encoder.slots_recycled
                << " pending=" << encoder.pending_resources
                << " queue=" << encoder.queued_outputs
                << " pool=" << encoder.pool_capacity
                << " registered=" << encoder.registered_resources
                << " nvenc_status=" << encoder.last_nvenc_status
                << std::endl;
            const bool capture_stalled =
                now - last_acquired_progress > std::chrono::seconds(2);
            const bool encoder_received_recent_submission =
                submissions > 0
                && now - last_submission_progress <= std::chrono::seconds(2);
            const auto stall_boundary = dxgi::ClassifyPipelineStall(
                capture_stalled, acquired > 0,
                encoder_received_recent_submission,
                encoder_has_pending_work);
            const char* diagnostic_code = dxgi::PipelineStallCode(
                stall_boundary);
            if (!wgc_active_ && !minecraft_java_state_) {
                EmitRecentDxgiEvidence("replay_stall_detected");
            }
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"pipeline_stall_detected\","
                      << "\"state\":\"FAILED\",\"error_code\":\""
                      << diagnostic_code << "\","
                      << "\"frames_acquired\":" << acquired << ','
                      << "\"encoder_submissions\":" << submissions << ','
                      << "\"encoded_packets\":" << packets << ','
                      << "\"capture_stage\":" << capture_thread_stage_.load() << ','
                      << "\"encoder_submit_stage\":" << encoder.submit_stage << ','
                      << "\"encoder_drain_stage\":" << encoder.drain_stage << ','
                      << "\"encoder_backpressure_events\":"
                      << encoder.backpressure_events << ','
                      << "\"encoder_backend_errors\":"
                      << encoder.backend_errors << ','
                      << "\"encoder_device_removed_errors\":"
                      << encoder.device_removed_errors << "}"
                      << std::endl;
            snapshot_emitted = true;
            SetCaptureFailure(std::string(diagnostic_code)
                + ": the replay pipeline stopped making bounded progress");
            ClearReplayForRecovery();
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
            running_.store(false);
            break;
        }
    }


    // StartRecording / StopRecording

    bool CaptureEngine::StartRecording(const wchar_t* path) {
        uint64_t start_generation = 0;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            const uint32_t health = capture_health_flags_.load(
                std::memory_order_acquire);
            const bool health_allows_start = (health & (
                CAPTURE_HEALTH_RECOVERING
                | CAPTURE_HEALTH_BACKEND_FAILED
                | CAPTURE_HEALTH_PAUSED)) == 0;
            if (!running_.load(std::memory_order_acquire) || !path || !*path
                || !replay_encoder_ || !encoded_ring_
                || !health_allows_start
                || recording_recovery_in_progress_
                || recording_stop_in_progress_
                || recovery_stop_result_valid_
                || is_recording_.load(std::memory_order_acquire)
                || record_writer_) {
                if (record_writer_ && record_writer_->HasFailed())
                    last_recording_error_ = record_writer_->LastError();
                else if (recording_recovery_in_progress_)
                    last_recording_error_ =
                        "A recording recovery is still in progress.";
                else if (recording_stop_in_progress_)
                    last_recording_error_ =
                        "The previous recording is still closing.";
                else if (recovery_stop_result_valid_)
                    last_recording_error_ = recovery_stop_error_.empty()
                        ? "The previous recording closed with a recoverable error."
                        : recovery_stop_error_;
                else if (record_writer_ || is_recording_.load())
                    last_recording_error_ =
                        "A manual recording is already active.";
                else if (!health_allows_start)
                    last_recording_error_ =
                        "Replay capture is recovering or paused.";
                else
                    last_recording_error_ =
                        "Replay capture is not ready for a manual recording.";
                return false;
            }
            // Capture the generation under the same authority used to publish
            // the writer. Recovery marks itself before clearing the ring, so a
            // later recheck cannot publish across that boundary.
            start_generation = encoded_ring_->GetGeneration();
        }

        // Some hardware codecs publish decoder configuration with their first
        // packet. Read the synchronized ring copy rather than racing that
        // publication or rejecting a start immediately after a quality switch.
        EncodedVideoConfig video_config;
        if (!encoded_ring_->WaitForVideoConfig(
                video_config, std::chrono::seconds(3))
            || !running_.load(std::memory_order_acquire)) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            if (!recording_recovery_in_progress_ && !recovery_stop_result_valid_)
                last_recording_error_ =
                    "The hardware video stream is still starting. Try again in a moment.";
            return false;
        }

        ContinuousRecordingAudioConfig audio_config;
        audio_config.codec = "pcm_f32le";
        if (audio_active_ && !audio_capture_.IsDeviceLost()
                && default_mix_audio_encoder_.IsInitialized()) {
            audio_config.sample_rate = default_mix_audio_encoder_.GetSampleRate();
            audio_config.channels = default_mix_audio_encoder_.GetChannels();
            audio_config.codec_extradata =
                default_mix_audio_encoder_.GetExtradata();
        }

        auto writer = std::make_shared<ContinuousRecordingWriter>();
        if (!writer->Start(
                std::filesystem::path(path), video_config, audio_config,
                start_generation, audio_active_ || microphone_audio_source_
                    || application_audio_source_manager_ || !device_audio_tracks_.empty())) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            if (!recovery_stop_result_valid_)
                last_recording_error_ = writer->LastError();
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            const bool generation_unchanged = encoded_ring_->GetGeneration()
                == start_generation;
            const uint32_t health = capture_health_flags_.load(
                std::memory_order_acquire);
            const bool health_allows_start = (health & (
                CAPTURE_HEALTH_RECOVERING
                | CAPTURE_HEALTH_BACKEND_FAILED
                | CAPTURE_HEALTH_PAUSED)) == 0;
            const bool can_publish = running_.load(std::memory_order_acquire)
                && health_allows_start
                && !recording_recovery_in_progress_
                && !recording_stop_in_progress_
                && !recovery_stop_result_valid_
                && !record_writer_
                && !is_recording_.load(std::memory_order_acquire)
                && generation_unchanged;
            if (can_publish) {
                record_writer_ = std::move(writer);
                is_recording_.store(true, std::memory_order_release);
                last_recording_error_.clear();
            } else {
                if (!recovery_stop_result_valid_)
                    last_recording_error_ = recording_recovery_in_progress_
                        ? "A recording recovery started before the writer could be published."
                        : !health_allows_start
                            ? "Replay capture entered recovery or paused before the writer could be published."
                            : "The replay generation changed before the recording could start.";
                // Stop outside record_writer_mutex_; the writer was never
                // published and must not alter the current session state.
            }
        }
        if (writer) {
            writer->Stop();
            return false;
        }
        replay_encoder_->RequestKeyframe();
        std::cout << "[CaptureEngine] Packet-stream recording started." << std::endl;
        return true;
    }

    bool CaptureEngine::MarkHighlight(
        const std::string& marker_id, int64_t out_point_qpc) {
        if (!encoded_ring_ || !running_.load(std::memory_order_acquire))
            return false;
        CapturedHighlightMark captured;
        if (!encoded_ring_->MarkHighlight(marker_id, out_point_qpc, &captured))
            return false;
        std::shared_ptr<ContinuousRecordingWriter> writer;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            writer = record_writer_;
        }
        if (writer && !writer->PushHighlight(
                captured.marker_id, captured.pts, captured.generation)) {
            if (writer->HasFailed()) {
                std::lock_guard<std::mutex> lock(record_writer_mutex_);
                if (record_writer_ == writer)
                    last_recording_error_ = writer->LastError();
            }
            return false;
        }
        return true;
    }

    void CaptureEngine::ForwardAudioToRecording(const AudioSourceMetadata& source,
            const uint8_t* data, uint32_t size, int64_t timeline_100ns, int64_t duration) {
        std::shared_ptr<ContinuousRecordingWriter> writer;
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            writer = record_writer_;
        }
        if (writer && !writer->PushSourceAudio(source, data, size, timeline_100ns, duration)
                && writer->HasFailed()) {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            if (record_writer_ == writer) {
                is_recording_.store(false, std::memory_order_release);
                last_recording_error_ = writer->LastError();
            }
        }
    }

    bool CaptureEngine::StopRecording() {
        std::shared_ptr<ContinuousRecordingWriter> writer;
        {
            std::unique_lock<std::mutex> lock(record_writer_mutex_);
            constexpr auto kRecoveryWait = std::chrono::seconds(5);
            if (!record_writer_cv_.wait_for(lock, kRecoveryWait, [this] {
                    return !recording_recovery_in_progress_
                        && !recording_stop_in_progress_;
                })) {
                last_recording_error_ =
                    "Timed out waiting for recording recovery to close.";
                return false;
            }
            if (!record_writer_) {
                if (recovery_stop_result_valid_) {
                    const bool saved = recovery_stop_result_;
                    last_recording_error_ = recovery_stop_error_;
                    recovery_stop_result_valid_ = false;
                    recovery_stop_error_.clear();
                    record_writer_cv_.notify_all();
                    return saved;
                }
                is_recording_.store(false, std::memory_order_release);
                return true;
            }
            recording_stop_in_progress_ = true;
            is_recording_.store(false, std::memory_order_release);
            writer = std::move(record_writer_);
        }

        const bool saved = writer->Stop();
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            last_recording_error_ = saved ? std::string{} : writer->LastError();
            if (!saved && last_recording_error_.empty()) {
                last_recording_error_ =
                    "The recording contained no complete video fragment.";
            }
            recording_stop_in_progress_ = false;
            record_writer_cv_.notify_all();
        }
        std::cout << "[CaptureEngine] Packet-stream recording "
                  << (saved ? "stopped." : "closed with a recoverable error.")
                  << std::endl;
        return saved;
    }

    bool CaptureEngine::IsRecording() const {
        if (!is_recording_.load(std::memory_order_acquire)) return false;
        std::lock_guard<std::mutex> lock(record_writer_mutex_);
        return record_writer_ && record_writer_->IsRunning();
    }

    std::string CaptureEngine::GetLastRecordingError() const {
        std::lock_guard<std::mutex> lock(record_writer_mutex_);
        if (record_writer_ && record_writer_->HasFailed()) {
            const std::string writer_error = record_writer_->LastError();
            if (!writer_error.empty()) return writer_error;
        }
        return last_recording_error_;
    }

    uint64_t CaptureEngine::GetFrameCount() const {
        return frames_captured_.load(std::memory_order_relaxed);
    }


    // SaveClip
    //
    // NVENC path:  TakeSnapshot from encoded ring -> queue encoded task
    // x264 path:   Snapshot raw ring indices -> queue raw task (unchanged)

    bool CaptureEngine::SaveClip(const wchar_t* path, uint32_t duration_seconds,
        SharedMemoryLayout* shared_memory, uint64_t end_monotonic_ns) try {

        // Ring capacity includes decoder pre-roll; it must not become visible
        // history beyond the matching audio retention after RAM adaptation.
        const bool anchored = end_monotonic_ns != 0;
        if (anchored && (!nvenc_active_ || duration_seconds == 0
                         || duration_seconds > buffer_seconds_)) {
            SetEngineError(shared_memory,
                L"The replay buffer cannot retain the requested clip length plus "
                L"additional seconds. Choose a shorter clip or increase replay capacity.");
            return false;
        }
        if (replay_duration_adapted_ && duration_seconds > buffer_seconds_) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"duration_limited_to_replay\",\"requested_seconds\":" << duration_seconds
                      << ",\"effective_seconds\":" << buffer_seconds_ << '}' << std::endl;
            duration_seconds = buffer_seconds_;
        }

        const auto save_request_started = std::chrono::steady_clock::now();
        // Allocate the request identity before any readiness gate so a rejected
        // save can be correlated with the same ID a queued task would use.
        const uint32_t save_request_id = next_task_id_.fetch_add(1);
        LARGE_INTEGER save_qpc{};
        LARGE_INTEGER save_qpc_frequency{};
        QueryPerformanceCounter(&save_qpc);
        QueryPerformanceFrequency(&save_qpc_frequency);
        if (anchored) {
            const auto frequency = save_qpc_frequency.QuadPart;
            const auto now_ns = ReplayPauseClock::Scale(
                save_qpc.QuadPart, 1'000'000'000, frequency);
            if (now_ns <= 0 || end_monotonic_ns > static_cast<uint64_t>(now_ns)) {
                SetEngineError(shared_memory, L"The requested clip time is invalid.");
                return false;
            }
            save_qpc.QuadPart = ReplayPauseClock::Scale(
                static_cast<int64_t>(end_monotonic_ns), frequency, 1'000'000'000);
        }
        SaveSelectionTrace selection_trace{
            frame_pacing_trace::Enabled(), save_request_id, save_qpc.QuadPart};

        if (save_clip_queue_.IsShutdown()
            || save_clip_queue_.GetQueueDepth() >= save_clip_queue_.GetCapacity()) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"save_queue_rejected\",\"state\":\"FAILED\","
                      << "\"error_code\":\"CLIP_SAVE_FAILED\",\"save_request_id\":"
                      << save_request_id << ",\"stage\":\"precondition\","
                      << "\"writer_invoked\":false,\"queue_depth\":"
                      << save_clip_queue_.GetQueueDepth() << ",\"queue_capacity\":"
                      << save_clip_queue_.GetCapacity() << '}' << std::endl;
            SetEngineError(shared_memory,
                L"The clip save queue is full or shutting down. Wait for the "
                L"current save to finish, then try again.");
            return false;
        }

        if (HasReplayMemoryFailure()) {
            SetEngineError(shared_memory, GetReplayFailureMessage().c_str());
            return false;
        }

        const uint32_t health = capture_health_flags_.load();
        if ((health & CAPTURE_HEALTH_PAUSED) && (anchored || !replay_pause_clock_)) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"save_rejected\",\"state\":\"FAILED\","
                      << "\"save_request_id\":" << save_request_id
                      << ",\"request_timestamp_qpc\":" << save_qpc.QuadPart
                      << ",\"requested_duration_seconds\":" << duration_seconds
                      << ",\"stage\":\"precondition\",\"writer_invoked\":false"
                      << ",\"reason\":\"capture_paused\""
                      << ",\"classification\":\"OTHER_PROVEN_REASON\""
                      << ",\"diagnostic_schema\":\"precondition_minimal\"}"
                      << std::endl;
            SetEngineError(shared_memory,
                L"Game capture is paused while the game is in the background. "
                L"Return to the game before saving.");
            return false;
        }
        if (!running_.load() ||
            (health & (CAPTURE_HEALTH_BACKEND_FAILED |
                       CAPTURE_HEALTH_RECOVERING))) {
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"save_rejected\",\"state\":\"FAILED\","
                      << "\"save_request_id\":" << save_request_id
                      << ",\"request_timestamp_qpc\":" << save_qpc.QuadPart
                      << ",\"requested_duration_seconds\":" << duration_seconds
                      << ",\"stage\":\"precondition\",\"writer_invoked\":false"
                      << ",\"reason\":\"capture_not_running\""
                      << ",\"classification\":\"OTHER_PROVEN_REASON\""
                      << ",\"diagnostic_schema\":\"precondition_minimal\"}"
                      << std::endl;
            SetEngineError(shared_memory,
                L"Capture is not receiving new frames. Restart capture before saving.");
            return false;
        }
        std::cout << "[CaptureEngine] SaveClip: " << duration_seconds << "s" << std::endl;

        const double save_qpc_s = save_qpc_frequency.QuadPart > 0
            ? static_cast<double>(save_qpc.QuadPart)
                / static_cast<double>(save_qpc_frequency.QuadPart)
            : 0.0;

        const auto emit_replay_save_diagnostic =
            [&](const char* event_name,
                const EncodedRingSnapshot& snapshot,
                const replay_health::Report* health_report,
                const char* stage,
                bool writer_invoked) -> replay_save::Classification {
            replay_save::RuntimeEvidence runtime;
            runtime.capture_running = running_.load(std::memory_order_relaxed);
            runtime.now_qpc = save_qpc.QuadPart;
            runtime.qpc_frequency = save_qpc_frequency.QuadPart;
            runtime.capture_start_qpc = capture_start_qpc_.load(
                std::memory_order_acquire);
            runtime.generation_start_qpc = generation_start_qpc_.load(
                std::memory_order_acquire);
            runtime.last_source_frame_qpc = last_source_frame_qpc_.load(
                std::memory_order_acquire);
            runtime.last_encoder_submission_qpc = last_encoder_submission_qpc_.load(
                std::memory_order_acquire);
            runtime.last_encoded_output_qpc = last_encoded_output_qpc_.load(
                std::memory_order_acquire);
            runtime.last_ring_insertion_qpc = last_ring_insertion_qpc_.load(
                std::memory_order_acquire);
            runtime.source_frames = source_textures_received_.load(
                std::memory_order_relaxed);
            runtime.encoder_submissions = frames_captured_.load(
                std::memory_order_relaxed);
            runtime.encoded_packets = video_packets_produced_.load(
                std::memory_order_relaxed);
            runtime.ring_insertions = video_ring_insertions_.load(
                std::memory_order_relaxed);
            runtime.encoder_failures = encoder_failure_count_.load(
                std::memory_order_relaxed);
            runtime.capture_generation = capture_generation_.load(
                std::memory_order_relaxed);
            runtime.capture_restart_count = capture_restart_count_.load(
                std::memory_order_relaxed);
            const auto classification = replay_save::Classify(
                health_report ? health_report->failure : replay_health::Failure::Empty,
                health_report ? health_report->timeline_subtype
                              : replay_health::TimelineSubtype::None,
                {snapshot.selection_outcome}, runtime,
                static_cast<double>(duration_seconds));
            const auto age_ms = [&](int64_t qpc) -> double {
                const double seconds = replay_save::AgeSeconds(
                    save_qpc.QuadPart, qpc, save_qpc_frequency.QuadPart);
                return seconds >= 0.0 ? seconds * 1000.0 : -1.0;
            };
            const auto monitor_source = protected_monitor_source_size_.load(std::memory_order_relaxed);
            std::ostringstream json;
            json << std::setprecision(12)
                 << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                 << "\"event\":\""
                 << diagnostics::JsonEscape(event_name ? event_name : "replay_save")
                 << "\",\"state\":\"FAILED\",\"save_request_id\":"
                 << save_request_id
                 << ",\"request_timestamp_qpc\":" << save_qpc.QuadPart
                 << ",\"requested_duration_seconds\":" << duration_seconds
                 << ",\"stage\":\"" << stage << "\""
                 << ",\"writer_invoked\":"
                 << (writer_invoked ? "true" : "false")
                 << ",\"error_code\":\""
                 << (health_report ? "CLIP_REPLAY_VIDEO_INSUFFICIENT"
                                    : "CLIP_SAVE_FAILED")
                 << "\",\"packet_count\":"
                 << (health_report ? health_report->packet_count : 0)
                 << ",\"reason\":\""
                 << (health_report
                     ? replay_health::FailureName(health_report->failure)
                     : replay_save::SnapshotOutcomeName(
                           snapshot.selection_outcome))
                 << "\""
                 << ",\"actual_coverage_seconds\":"
                 << (health_report ? health_report->actual_coverage_s : 0.0)
                 << ",\"largest_gap_seconds\":"
                 << (health_report ? health_report->largest_gap_s : 0.0)
                 << ",\"largest_wall_gap_seconds\":"
                 << (health_report ? health_report->largest_wall_gap_s : 0.0)
                 << ",\"largest_pts_gap_seconds\":"
                 << (health_report ? health_report->largest_pts_gap_s : 0.0)
                 << ",\"effective_packet_rate\":"
                 << (health_report ? health_report->effective_packet_rate : 0.0)
                 << ",\"effective_packet_rate_measured\":"
                 << (health_report
                     && health_report->effective_packet_rate_measured
                     ? "true" : "false")
                 << ",\"capture_mode\":\""
                 << (capture_mode_ == CaptureConfig::CaptureModeEnum::WINDOW
                     ? "WINDOW" : "DESKTOP") << "\""
                 << ",\"capture_backend\":\""
                 << diagnostics::JsonEscape(startup_capture_backend_) << "\""
                 << ",\"encoder_backend\":\""
                 << diagnostics::JsonEscape(startup_encoder_backend_) << "\""
                 << ",\"encoder\":\""
                 << diagnostics::JsonEscape(nvenc_active_
                     ? GetActiveEncoderName() : "unavailable:not_active") << "\""
                 << ",\"codec\":\""
                 << diagnostics::JsonEscape(startup_codec_) << "\""
                 << ",\"configured_fps\":" << fps_
                 << ",\"configured_target_width\":"
                 << configured_target_width_
                 << ",\"configured_target_height\":"
                 << configured_target_height_
                 << ",\"actual_source_width\":" << (monitor_source.Positive() ? monitor_source.width : width_)
                 << ",\"actual_source_height\":" << (monitor_source.Positive() ? monitor_source.height : height_)
                 << ",\"actual_encoded_width\":" << target_width_
                 << ",\"actual_encoded_height\":" << target_height_
                 << ",\"capture_generation\":" << runtime.capture_generation
                 << ",\"replay_ring_generation\":"
                 << snapshot.capture_generation
                 << ",\"capture_restart_count\":"
                 << runtime.capture_restart_count
                 << ",\"capture_running\":"
                 << (runtime.capture_running ? "true" : "false")
                 << ",\"capture_generation_age_ms\":"
                 << age_ms(runtime.generation_start_qpc)
                 << ",\"capture_start_age_ms\":"
                 << age_ms(runtime.capture_start_qpc)
                 << ",\"source_frame_age_ms\":"
                 << age_ms(runtime.last_source_frame_qpc)
                 << ",\"encoder_submission_age_ms\":"
                 << age_ms(runtime.last_encoder_submission_qpc)
                 << ",\"encoded_output_age_ms\":"
                 << age_ms(runtime.last_encoded_output_qpc)
                 << ",\"ring_insertion_age_ms\":"
                 << age_ms(runtime.last_ring_insertion_qpc)
                  << ",\"last_source_qpc\":"
                  << runtime.last_source_frame_qpc
                  << ",\"last_presentation_qpc\":"
                  << last_dxgi_presentation_qpc_.load(
                         std::memory_order_acquire)
                  << ",\"last_dxgi_completed_qpc\":"
                  << last_dxgi_completed_qpc_.load(
                         std::memory_order_acquire)
                  << ",\"dxgi_replay_cache_armed\":"
                  << (dxgi_replay_cache_armed_.load(
                          std::memory_order_acquire) ? "true" : "false")
                  << ",\"dxgi_held_presentations\":"
                  << dxgi_held_presentations_.load(
                         std::memory_order_relaxed)
                  << ",\"dxgi_resampled_presentations\":"
                  << dxgi_resampled_presentations_.load(
                         std::memory_order_relaxed)
                 << ",\"source_frames\":" << runtime.source_frames
                 << ",\"capture_acquire_attempts\":"
                 << capture_acquire_attempts_.load(std::memory_order_relaxed)
                 << ",\"capture_acquire_successes\":"
                 << capture_acquire_successes_.load(std::memory_order_relaxed)
                 << ",\"capture_timeouts\":"
                 << capture_timeouts_.load(std::memory_order_relaxed)
                 << ",\"capture_recovery_attempts\":"
                 << capture_recovery_attempts_.load(std::memory_order_relaxed)
                 << ",\"capture_recovery_failures\":"
                 << capture_recovery_failures_.load(std::memory_order_relaxed)
                 << ",\"encoder_submissions\":"
                 << runtime.encoder_submissions
                 << ",\"encoded_packets\":" << runtime.encoded_packets
                 << ",\"ring_insertions\":" << runtime.ring_insertions
                 << ",\"encoder_failures\":" << runtime.encoder_failures
                 << ",\"pre_encode_drops\":"
                 << frames_dropped_.load(std::memory_order_relaxed)
                 << ",\"queue_occupancy\":" << save_clip_queue_.GetQueueDepth()
                 << ",\"queue_capacity\":" << save_clip_queue_.GetCapacity()
                 << ",\"queue_overflow_status\":\"unavailable:not_exposed\""
                 << ",\"snapshot_outcome\":\""
                 << replay_save::SnapshotOutcomeName(snapshot.selection_outcome)
                 << "\",\"selection_attempts\":" << snapshot.selection_attempts
                 << ",\"internal_consistency\":"
                 << (snapshot.internal_consistency ? "true" : "false")
                 << ",\"scanned_packet_count\":"
                 << snapshot.scanned_packet_count
                 << ",\"eligible_packet_count\":"
                 << snapshot.eligible_packet_count
                 << ",\"keyframe_count\":" << snapshot.keyframe_count
                 << ",\"ring_count\":" << snapshot.ring_count
                 << ",\"ring_capacity\":" << snapshot.ring_capacity
                 << ",\"ring_head\":" << snapshot.ring_head
                 << ",\"oldest_wall_qpc\":" << snapshot.oldest_wall_qpc
                 << ",\"newest_wall_qpc\":" << snapshot.newest_wall_qpc
                 << ",\"requested_start_qpc\":" << snapshot.requested_start_qpc
                 << ",\"target_end_qpc\":" << snapshot.target_end_qpc
                 << ",\"interval_spanned_qpc\":"
                 << snapshot.interval_spanned_qpc
                 << ",\"valid_starting_keyframe\":"
                 << (snapshot.valid_starting_keyframe ? "true" : "false")
                 << ",\"full_history\":"
                 << (snapshot.full_history ? "true" : "false")
                 << ",\"classification\":\""
                 << replay_save::ClassificationName(classification) << "\""
                 << ",\"failed_predicate\":\""
                 << (health_report
                     ? replay_health::FailureName(health_report->failure)
                     : "SNAPSHOT_SELECTION") << "\""
                 << ",\"failed_actual\":";
            if (!health_report) {
                json << "\"" << replay_save::SnapshotOutcomeName(
                    snapshot.selection_outcome) << "\"";
            } else if (health_report->failure
                       == replay_health::Failure::InsufficientCoverage) {
                json << health_report->actual_coverage_s;
            } else if (health_report->failure
                       == replay_health::Failure::LargeTimelineGap) {
                json << health_report->largest_gap_s;
            } else if (health_report->failure
                       == replay_health::Failure::LowEffectiveRate) {
                json << health_report->effective_packet_rate;
            } else if (health_report->failure
                       == replay_health::Failure::StaleEnd) {
                json << health_report->end_staleness_s;
            } else {
                json << "\"" << replay_health::TimelineSubtypeName(
                    health_report->timeline_subtype) << "\"";
            }
            json << ",\"failed_required\":";
            if (!health_report) {
                json << "\"VALID\"";
            } else if (health_report->failure
                       == replay_health::Failure::InsufficientCoverage) {
                json << health_report->required_min_coverage_s;
            } else if (health_report->failure
                       == replay_health::Failure::LargeTimelineGap) {
                json << health_report->max_allowed_gap_s;
            } else if (health_report->failure
                       == replay_health::Failure::LowEffectiveRate) {
                json << health_report->minimum_effective_rate;
            } else if (health_report->failure
                       == replay_health::Failure::StaleEnd) {
                json << health_report->allowed_end_staleness_s;
            } else {
                json << "\"monotonic timeline\"";
            }
            json
                 << ",\"timeline_subtype\":\""
                 << (health_report
                     ? replay_health::TimelineSubtypeName(
                           health_report->timeline_subtype)
                     : "NONE") << "\""
                 << ",\"before_snapshot\":{\"ring_head\":"
                 << snapshot.snapshot_start_ring_head << ",\"ring_count\":"
                 << snapshot.snapshot_start_ring_count << ",\"ring_generation\":"
                 << snapshot.snapshot_start_ring_generation << "}"
                 << ",\"after_snapshot\":{\"ring_head\":"
                 << snapshot.snapshot_end_ring_head << ",\"ring_count\":"
                 << snapshot.snapshot_end_ring_count << ",\"ring_generation\":"
                 << snapshot.snapshot_end_ring_generation
                 << ",\"outcome\":\""
                 << replay_save::SnapshotOutcomeName(snapshot.selection_outcome)
                 << "\",\"packet_count\":" << snapshot.packets.size()
                 << "}"
                 << ",\"replay_health\":";
            if (!health_report) {
                json << "null";
            } else {
                json << "{\"accepted\":"
                     << (health_report->accepted ? "true" : "false")
                     << ",\"packet_count\":" << health_report->packet_count
                     << ",\"wall_coverage_s\":"
                     << health_report->wall_coverage_s
                     << ",\"media_coverage_s\":"
                     << health_report->actual_coverage_s
                     << ",\"first_wall_qpc\":"
                     << health_report->first_wall_qpc
                     << ",\"last_wall_qpc\":"
                     << health_report->last_wall_qpc
                     << ",\"first_pts\":" << health_report->first_pts
                     << ",\"last_pts\":" << health_report->last_pts
                     << ",\"required_min_coverage_s\":"
                     << health_report->required_min_coverage_s
                     << ",\"largest_gap_s\":"
                     << health_report->largest_gap_s
                     << ",\"largest_wall_gap_s\":"
                     << health_report->largest_wall_gap_s
                     << ",\"largest_wall_gap_start_qpc\":"
                     << health_report->largest_wall_gap_start_qpc
                     << ",\"largest_wall_gap_end_qpc\":"
                     << health_report->largest_wall_gap_end_qpc
                     << ",\"largest_pts_gap_s\":"
                     << health_report->largest_pts_gap_s
                     << ",\"largest_pts_gap_start\":"
                     << health_report->largest_pts_gap_start
                     << ",\"largest_pts_gap_end\":"
                     << health_report->largest_pts_gap_end
                     << ",\"max_allowed_gap_s\":"
                     << health_report->max_allowed_gap_s
                     << ",\"intervals_over_gap_threshold\":"
                     << health_report->intervals_over_gap_threshold
                     << ",\"effective_packet_rate\":"
                     << health_report->effective_packet_rate
                     << ",\"effective_packet_rate_measured\":"
                     << (health_report->effective_packet_rate_measured
                         ? "true" : "false")
                     << ",\"minimum_effective_rate\":"
                     << health_report->minimum_effective_rate
                     << ",\"end_staleness_s\":"
                     << health_report->end_staleness_s
                     << ",\"allowed_end_staleness_s\":"
                     << health_report->allowed_end_staleness_s
                     << ",\"wall_monotonic\":"
                     << (health_report->wall_monotonic ? "true" : "false")
                     << ",\"pts_monotonic\":"
                     << (health_report->pts_monotonic ? "true" : "false")
                     << ",\"generation\":" << health_report->generation
                     << ",\"generation_continuous\":"
                     << (health_report->generation_continuous ? "true" : "false")
                     << "}";
            }
            json << "}";
            std::cout << json.str() << std::endl;
            return classification;
        };

        // NVENC path - mux only, no encoding
        if (nvenc_active_) {
            const auto replay_save_qpc = replay_pause_clock_
                ? replay_pause_clock_->MapQpc(save_qpc.QuadPart, save_qpc_frequency.QuadPart)
                : save_qpc.QuadPart;
            if (anchored && replay_pause_clock_) {
                const int64_t duration_qpc = duration_seconds * save_qpc_frequency.QuadPart;
                const auto replay_start_qpc = replay_pause_clock_->MapQpc(
                    save_qpc.QuadPart - duration_qpc, save_qpc_frequency.QuadPart);
                if (std::abs(replay_save_qpc - replay_start_qpc - duration_qpc) > 2) {
                    SetEngineError(shared_memory,
                        L"Capture paused during the requested clip. "
                        L"Wait for the replay buffer to refill and try again.");
                    return false;
                }
            }
            const auto publish_timeout = std::chrono::milliseconds(
                std::max<uint32_t>(100, 3000 / std::max<uint32_t>(fps_, 1)));
            selection_trace.replay_target_qpc = replay_save_qpc;
            selection_trace.publication_begin_qpc = frame_pacing_trace::Now(selection_trace.enabled);
            selection_trace.publication_reached = encoded_ring_->WaitUntilPublished(
                replay_save_qpc, publish_timeout);
            selection_trace.publication_end_qpc = frame_pacing_trace::Now(selection_trace.enabled);
            if (!selection_trace.publication_reached) {
                if (anchored) {
                    SetEngineError(shared_memory,
                        L"Capture did not reach the requested end of the additional seconds.");
                    return false;
                }
                std::cerr << "[SaveClip] Encoder publication did not reach the "
                             "save boundary within "
                          << publish_timeout.count() << "ms; using the latest "
                             "published packet" << std::endl;
            }
            auto save_lease = replay_storage_ ? replay_storage_->BeginSave() : nullptr;
            selection_trace.storage_lease_end_qpc = frame_pacing_trace::Now(selection_trace.enabled);
            if (replay_storage_ && !save_lease) {
                SetEngineError(shared_memory,
                    L"Another replay clip is being saved. Wait for it to finish before saving again.");
                return false;
            }
            selection_trace.snapshot_begin_qpc = frame_pacing_trace::Now(selection_trace.enabled);
            EncodedRingSnapshot snapshot = encoded_ring_->TakeSnapshotByTime(
                duration_seconds, replay_save_qpc,
                selection_trace.enabled ? &selection_trace.lock_timing : nullptr, anchored);
            selection_trace.snapshot_end_qpc = frame_pacing_trace::Now(selection_trace.enabled);
            selection_trace.snapshot_packets = snapshot.packets.size();
            selection_trace.snapshot_attempts = snapshot.selection_attempts;

            if (snapshot.selection_outcome == replay_save::SnapshotOutcome::MemoryBudgetExceeded) {
                const auto budget = encoded_ring_->GetMemoryBudget();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"snapshot_memory_rejected\",\"state\":\"FAILED\","
                          << "\"classification\":\"MEMORY_BUDGET_EXCEEDED\","
                          << "\"writer_invoked\":false,"
                          << "\"error_code\":\"CLIP_SAVE_FAILED\",\"save_request_id\":"
                          << save_request_id << ",\"ring_bytes\":"
                          << budget->Used(ReplayMemoryUse::Ring) << ",\"save_bytes\":"
                          << budget->Used(ReplayMemoryUse::Save) << ",\"total_limit_bytes\":"
                          << budget->total_limit() << '}' << std::endl;
                SetEngineError(shared_memory,
                    L"Not enough replay memory for another clip save. Wait for the "
                    L"current saves to finish, or reduce clip length or bitrate.");
                return false;
            }

            if (snapshot.packets.empty()) {
                std::cerr << "[SaveClip] Encoded ring buffer empty - nothing to save" << std::endl;
                const auto classification = emit_replay_save_diagnostic(
                    "ring_selection_failed", snapshot, nullptr,
                    "selection", false);
                std::wstring message =
                    L"Replay save rejected (";
                for (const char* c = replay_save::ClassificationName(classification);
                     *c; ++c) {
                    message.push_back(static_cast<wchar_t>(*c));
                }
                message += L"). Export Diagnostic Report for details.";
                SetEngineError(shared_memory, message.c_str());
                return false;
            }

            // The encoded ring is selected by time, so a non-empty snapshot is
            // not sufficient proof that it contains a continuous replay. Check
            // the actual packet timeline before handing it to the mux worker;
            // otherwise a handful of stale packets can be expanded into a
            // misleading multi-second clip by downstream CFR processing.
            const auto replay_health_input =
                encoded_replay_health::MakeInput(
                    snapshot, duration_seconds, static_cast<double>(fps_),
                    save_qpc_frequency.QuadPart, replay_save_qpc);
            if (anchored) {
                const double target_end_s = static_cast<double>(replay_save_qpc)
                    / save_qpc_frequency.QuadPart;
                const double boundary_tolerance_s = std::max(0.25, 2.0 / fps_);
                if (!snapshot.full_history
                    || snapshot.presentation_start_qpc_s
                        > target_end_s - duration_seconds + boundary_tolerance_s
                    || snapshot.presentation_end_qpc_s < target_end_s - boundary_tolerance_s) {
                    SetEngineError(shared_memory,
                        L"The full requested time range is no longer available. "
                        L"Wait for the replay buffer to fill or choose a shorter clip.");
                    return false;
                }
            }
            const auto replay_health_report = replay_health::Evaluate(
                replay_health_input, allow_replay_gaps_, allow_low_fps_clips_);
            if (!replay_health_report.accepted) {
                std::cerr << "[SaveClip] Replay video rejected: "
                          << replay_health::FailureName(
                                 replay_health_report.failure)
                          << ", packets=" << replay_health_report.packet_count
                          << ", coverage=" << replay_health_report.actual_coverage_s
                          << "s, largest_gap=" << replay_health_report.largest_gap_s
                          << "s, rate=" << replay_health_report.effective_packet_rate
                          << " fps" << std::endl;
                const auto classification = emit_replay_save_diagnostic(
                    "replay_health_rejected", snapshot,
                    &replay_health_report, "health", false);
                EmitRecentDxgiEvidence("replay_health_rejected");
                if (replay_health_report.failure
                        == replay_health::Failure::LowEffectiveRate) {
                    std::wostringstream message;
                    message << L"Replay captured " << std::fixed
                            << std::setprecision(1)
                            << replay_health_report.effective_packet_rate
                            << L" FPS. At least "
                            << replay_health_report.minimum_effective_rate
                            << L" FPS is required. Try a hardware encoder or lower "
                               L"the capture resolution.";
                    SetEngineError(shared_memory, message.str().c_str());
                    return false;
                }
                std::wstring message = L"Replay save rejected (";
                for (const char* c = replay_save::ClassificationName(classification);
                     *c; ++c) {
                    message.push_back(static_cast<wchar_t>(*c));
                }
                message += L"). Export Diagnostic Report for details.";
                SetEngineError(shared_memory, message.c_str());
                return false;
            }

            if (replay_health_report.tolerated_cadence_failure
                    != replay_health::Failure::None) {
                std::cerr << "[SaveClip] Replay cadence warning allowed: "
                          << replay_health::FailureName(
                                 replay_health_report.tolerated_cadence_failure)
                          << ", largest_gap=" << replay_health_report.largest_gap_s
                          << "s, rate=" << replay_health_report.effective_packet_rate
                          << " fps" << std::endl;
            }

            SaveClipTask task;
            task.replay_save_lease = std::move(save_lease);
            task.output_path = path;
            task.duration_seconds = duration_seconds;
            task.use_encoded_path = true;
            task.encoded_snapshot = std::move(snapshot);
            task.enc_width = (target_width_ > 0) ? target_width_ : crop_width_;
            task.enc_height = (target_height_ > 0) ? target_height_ : crop_height_;
            task.fps = fps_;
            task.separate_audio_enabled = separate_audio_enabled_;
            task.shared_memory = shared_memory;
            task.task_id = save_request_id;

            // Populate the QPC epoch so MuxEncodedClip can convert video PTS
            // to wall-clock seconds and align audio to it exactly.
            replay_encoder_->GetEncodeEpoch(
                task.video_qpc_epoch, task.video_qpc_freq);

            // Snapshot the persistent AAC packet ring against the exact video
            // presentation interval. Every provider maps that interval through
            // its own QPC origin, so a missing Default Mix never suppresses a
            // valid microphone or process-loopback stem.
            const double start_qpc_s = task.encoded_snapshot.presentation_start_qpc_s;
            const double end_qpc_s = task.encoded_snapshot.presentation_end_qpc_s;
            if (audio_active_ && default_mix_audio_ring_
                    && !audio_capture_.IsDeviceLost()) {
                const uint64_t origin_qpc = replay_pause_clock_
                    ? replay_audio_origin_100ns_.load()
                    : audio_capture_.GetTimelineOriginQpc100ns();
                const uint32_t sample_rate = audio_capture_.GetSampleRate();
                if (origin_qpc > 0 && start_qpc_s > 0.0 && end_qpc_s > start_qpc_s
                        && sample_rate > 0) {
                    const auto range = MapAudioSourcePresentationRange(
                        start_qpc_s, end_qpc_s, origin_qpc, sample_rate);
                    auto default_mix_track =
                        TakeEncodedAudioTrackForPresentationRange(
                            default_mix_audio_source_, *default_mix_audio_ring_, range);
                    task.encoded_audio_snapshot = default_mix_track.snapshot;
                    task.audio_presentation_start_pts_samples =
                        default_mix_track.presentation_start_pts_samples;
                    task.has_encoded_audio = task.encoded_audio_snapshot.valid();
                    if (task.has_encoded_audio) {
                        // The source interval reflects the packet timeline that
                        // actually survived in this clip, not a guessed requested
                        // duration.  Default Mix is a real aggregate loopback
                        // stream and therefore remains present even for silence.
                        default_mix_track.source.state.first_active_100ns =
                            AudioSamplePositionToTimeline100ns(
                                origin_qpc,
                                default_mix_track.snapshot.first_pts_samples,
                                sample_rate);
                        default_mix_track.source.state.last_active_100ns =
                            AudioSamplePositionToTimeline100ns(
                                origin_qpc,
                                default_mix_track.snapshot.last_pts_samples,
                                sample_rate);
                        if (!omit_default_mix_track_)
                            task.encoded_audio_tracks.push_back(std::move(default_mix_track));
                        std::cout << "[SaveClip] Lossless float snapshot: "
                            << task.encoded_audio_snapshot.packets.size()
                            << " packets, PTS "
                            << task.audio_presentation_start_pts_samples << " - "
                            << std::max(task.audio_presentation_start_pts_samples + 1,
                                        range.end_pts_samples)
                            << std::endl;
                    } else {
                        std::cerr << "[SaveClip] Default Mix float ring had no packets for "
                            << "presentation PTS "
                            << task.audio_presentation_start_pts_samples << " - "
                            << std::max(task.audio_presentation_start_pts_samples + 1,
                                        range.end_pts_samples)
                            << std::endl;
                    }
                } else {
                    std::cerr << "[SaveClip] Default Mix timeline is not ready; origin="
                        << origin_qpc << ", video interval=" << start_qpc_s << " - "
                        << end_qpc_s << ", sample_rate=" << sample_rate << std::endl;
                }
            }
            else if (audio_active_ && audio_capture_.IsDeviceLost()) {
                std::cerr << "[SaveClip] Audio device lost - saving clip without audio" << std::endl;
            }

            if (start_qpc_s > 0.0 && end_qpc_s > start_qpc_s) {
                if (microphone_audio_source_) {
                    if (const auto microphone_track =
                            microphone_audio_source_->TakeTrackForInterval(
                                start_qpc_s, end_qpc_s, microphone_audio_metadata_)) {
                        task.encoded_audio_tracks.push_back(*microphone_track);
                    } else {
                        const std::string detail = microphone_audio_source_->last_error();
                        task.audio_warnings.push_back("microphone_unavailable");
                        if (!detail.empty()) {
                            std::cerr << "[SaveClip] Native microphone unavailable: "
                                      << detail << std::endl;
                        }
                    }
                }
                for (auto& device_track : device_audio_tracks_) {
                    if (!device_track.provider) continue;
                    if (const auto named_track =
                            device_track.provider->TakeTrackForInterval(
                                start_qpc_s, end_qpc_s, device_track.metadata)) {
                        task.encoded_audio_tracks.push_back(*named_track);
                    } else {
                        const std::string detail = device_track.provider->last_error();
                        if (!detail.empty()) {
                            std::cerr << "[SaveClip] Named audio track unavailable: "
                                      << device_track.metadata.identity.display_name
                                      << ": " << detail << std::endl;
                        }
                    }
                }

                if (application_audio_tracks_enabled_ && application_audio_source_manager_) {
                    const auto application_tracks =
                        application_audio_source_manager_->TakeTracksForInterval(
                            start_qpc_s, end_qpc_s);
                    for (const auto& application_track : application_tracks)
                        task.encoded_audio_tracks.push_back(application_track);
                    // New clips mix independent application streams directly.
                    // Keep the original desktop recording as an alternative
                    // for untracked/failed applications, never as a subtraction
                    // base: separately encoded AAC cannot cancel cleanly.
                    if (!application_tracks.empty()) {
                        for (auto& track : task.encoded_audio_tracks) {
                            if (track.source.identity.type == AudioSourceType::System
                                    && track.source.identity.display_name == "Default Mix") {
                                track.source.identity.display_name = "System Audio (backup)";
                            }
                        }
                    }
                    const auto application_info =
                        application_audio_source_manager_->runtime_info();
                    if (application_info.source_limit_rejections)
                        task.audio_warnings.push_back("source_limit_reached");
                    if (application_info.failed_sources || application_info.rejected_packets)
                        task.audio_warnings.push_back("application_audio_failed");
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"audio\","
                              << "\"event\":\"application_audio_snapshot\","
                              << "\"state\":\"SNAPSHOTTED\","
                              << "\"track_count\":" << application_tracks.size()
                              << ",\"candidate_count\":"
                              << application_info.candidate_count
                              << ",\"packet_count\":" << application_info.packet_count
                              << ",\"admitted_source_count\":"
                              << application_info.admitted_source_count
                              << ",\"encoded_packets\":"
                              << application_info.encoded_packets
                              << ",\"encode_failures\":"
                              << application_info.encode_failures
                              << ",\"finalize_failures\":"
                              << application_info.finalize_failures
                              << ",\"dropped_blocks\":"
                              << application_info.dropped_blocks
                              << ",\"no_packet_intervals\":"
                              << application_info.no_packet_intervals
                              << ",\"largest_no_packet_gap_100ns\":"
                              << application_info.largest_no_packet_gap_100ns
                              << ",\"discontinuities\":"
                              << application_info.discontinuity_count
                              << ",\"source_limit_rejections\":"
                              << application_info.source_limit_rejections
                              << ",\"largest_gap_100ns\":"
                              << application_info.largest_gap_100ns << "}"
                              << std::endl;
                }
            }

            if (audio_requested_ && !omit_default_mix_track_ && std::none_of(
                    task.encoded_audio_tracks.begin(), task.encoded_audio_tracks.end(),
                    [](const auto& track) { return track.source.identity.persistent_identity == "default-mix"; }))
                task.audio_warnings.push_back("system_audio_failed");
            if (audio_requested_ && !microphone_audio_source_)
                task.audio_warnings.push_back("microphone_unavailable");
            if (audio_requested_ && application_audio_tracks_enabled_ && !application_audio_source_manager_)
                task.audio_warnings.push_back("application_audio_failed");

            // Correlate only the bounded hard continuity-edit history with the
            // exact encoded video presentation window. This is save-time
            // diagnostics; it does not inspect or alter audio samples.
            if (start_qpc_s > 0.0 && end_qpc_s > start_qpc_s) {
                const uint64_t selected_start_qpc_100ns = static_cast<uint64_t>(
                    std::llround(start_qpc_s * 10'000'000.0));
                const uint64_t selected_end_qpc_100ns = static_cast<uint64_t>(
                    std::llround(end_qpc_s * 10'000'000.0));
                AudioCorrectionEventSnapshot microphone_corrections;
                if (microphone_audio_source_) {
                    microphone_corrections =
                        microphone_audio_source_->correction_events();
                }
                const auto correction_summary = SummarizeAudioCorrections(
                    audio_capture_.GetCorrectionEvents(), microphone_corrections,
                    selected_start_qpc_100ns, selected_end_qpc_100ns);
                std::cout << "FTHR_DIAGNOSTIC_EVENT "
                          << AudioCorrectionClipSummaryJson(
                                 task.task_id, correction_summary)
                          << std::endl;
            }

            // Preserve one bounded, authoritative account of the selected
            // media window before any stream is independently rebased for MP4.
            // Video is measured in raw QPC plus encoder PTS; audio source
            // metadata has already mapped packet PTS back to that same QPC
            // domain. No samples or per-packet records are emitted.
            struct SaveAudioTimelineEvidence {
                bool available = false;
                int64_t first_pts = 0;
                int64_t last_packet_pts = 0;
                int64_t end_pts = 0;
                uint32_t sample_rate = 0;
                size_t packet_count = 0;
                MediaTimelineRange qpc_range;
            };
            const auto audio_evidence = [](const EncodedAudioTrack* track) {
                SaveAudioTimelineEvidence evidence;
                if (!track || !track->valid() || track->snapshot.packets.empty()
                        || track->snapshot.format.sample_rate == 0) {
                    return evidence;
                }
                evidence.available = true;
                evidence.first_pts = track->snapshot.first_pts_samples;
                evidence.last_packet_pts =
                    track->snapshot.packets.back().pts_samples;
                evidence.end_pts = track->snapshot.last_pts_samples;
                evidence.sample_rate = track->snapshot.format.sample_rate;
                evidence.packet_count = track->snapshot.packets.size();
                if (track->source.state.first_active_100ns > 0
                        && track->source.state.last_active_100ns
                            >= track->source.state.first_active_100ns) {
                    evidence.qpc_range = {
                        static_cast<double>(track->source.state.first_active_100ns)
                            / 10'000'000.0,
                        static_cast<double>(track->source.state.last_active_100ns)
                            / 10'000'000.0,
                        true};
                }
                return evidence;
            };

            const EncodedAudioTrack* system_track = nullptr;
            const EncodedAudioTrack* microphone_track = nullptr;
            for (const auto& track : task.encoded_audio_tracks) {
                if (!system_track
                        && track.source.identity.type == AudioSourceType::System
                        && track.source.identity.persistent_identity == "default-mix") {
                    system_track = &track;
                } else if (!microphone_track
                        && track.source.identity.type == AudioSourceType::Microphone) {
                    microphone_track = &track;
                }
            }
            const auto system_timeline = audio_evidence(system_track);
            const auto microphone_timeline = audio_evidence(microphone_track);

            MediaTimelineRange video_qpc_range;
            int64_t video_first_pts = 0;
            int64_t video_last_pts = 0;
            int64_t video_decode_first_pts = 0;
            double video_duration_s = 0.0;
            const auto& video_snapshot = task.encoded_snapshot;
            const auto& video_config = video_snapshot.video_config;
            const double video_tick_s = video_config.time_base.denominator > 0
                ? static_cast<double>(video_config.time_base.numerator)
                    / static_cast<double>(video_config.time_base.denominator)
                : 0.0;
            if (!video_snapshot.packets.empty() && save_qpc_frequency.QuadPart > 0
                    && video_tick_s > 0.0) {
                const auto presentation_packet = std::find_if(
                    video_snapshot.packets.begin(), video_snapshot.packets.end(),
                    [&](const EncodedRingPacket& packet) {
                        return packet.pts >= video_snapshot.presentation_start_pts;
                    });
                if (presentation_packet != video_snapshot.packets.end()) {
                    const auto& last = video_snapshot.packets.back();
                    video_decode_first_pts = video_snapshot.packets.front().pts;
                    video_first_pts = presentation_packet->pts;
                    video_last_pts = last.pts;
                    video_duration_s = static_cast<double>(
                        video_last_pts - video_first_pts + 1) * video_tick_s;
                    video_qpc_range = {
                        static_cast<double>(presentation_packet->wall_qpc)
                            / static_cast<double>(save_qpc_frequency.QuadPart),
                        static_cast<double>(last.wall_qpc)
                            / static_cast<double>(save_qpc_frequency.QuadPart)
                            + video_tick_s,
                        true};
                }
            }

            MediaTimelineRange combined_audio_qpc_range;
            for (const auto* source : {&system_timeline, &microphone_timeline}) {
                if (!source->qpc_range.valid) continue;
                if (!combined_audio_qpc_range.valid) {
                    combined_audio_qpc_range = source->qpc_range;
                } else {
                    combined_audio_qpc_range.start_s = std::min(
                        combined_audio_qpc_range.start_s, source->qpc_range.start_s);
                    combined_audio_qpc_range.end_s = std::max(
                        combined_audio_qpc_range.end_s, source->qpc_range.end_s);
                }
            }
            const auto combined_sync = EvaluateAvSyncTimeline(
                start_qpc_s, end_qpc_s, video_qpc_range,
                combined_audio_qpc_range, video_snapshot.generation_continuous);
            const auto system_sync = EvaluateAvSyncTimeline(
                start_qpc_s, end_qpc_s, video_qpc_range,
                system_timeline.qpc_range, video_snapshot.generation_continuous);
            const auto microphone_sync = EvaluateAvSyncTimeline(
                start_qpc_s, end_qpc_s, video_qpc_range,
                microphone_timeline.qpc_range, video_snapshot.generation_continuous);

            const auto json_number_or_null = [](bool available, double value) {
                if (!available || !std::isfinite(value)) return std::string("null");
                std::ostringstream stream;
                stream << std::setprecision(12) << value;
                return stream.str();
            };
            const auto json_integer_or_null = [](bool available, int64_t value) {
                return available ? std::to_string(value) : std::string("null");
            };
            const auto audio_duration = [](const SaveAudioTimelineEvidence& source) {
                return source.available && source.sample_rate > 0
                    ? static_cast<double>(source.end_pts - source.first_pts)
                        / static_cast<double>(source.sample_rate)
                    : 0.0;
            };
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"pre_mux_av_timeline\",\"state\":\"OBSERVED\","
                      << "\"task_id\":" << task.task_id
                      << ",\"save_request_id\":" << task.task_id
                      << ",\"selected_window_start_qpc\":"
                      << json_number_or_null(start_qpc_s > 0.0, start_qpc_s)
                      << ",\"selected_window_end_qpc\":"
                      << json_number_or_null(end_qpc_s > start_qpc_s, end_qpc_s)
                      << ",\"capture_generation\":"
                      << video_snapshot.capture_generation
                      << ",\"generation_continuous\":"
                      << (video_snapshot.generation_continuous ? "true" : "false")
                      << ",\"video_decode_first_pts\":"
                      << json_integer_or_null(video_qpc_range.valid, video_decode_first_pts)
                      << ",\"video_first_pts\":"
                      << json_integer_or_null(video_qpc_range.valid, video_first_pts)
                      << ",\"video_last_pts\":"
                      << json_integer_or_null(video_qpc_range.valid, video_last_pts)
                      << ",\"video_duration\":"
                      << json_number_or_null(video_qpc_range.valid, video_duration_s)
                      << ",\"system_audio_first_pts\":"
                      << json_integer_or_null(system_timeline.available,
                             system_timeline.first_pts)
                      << ",\"system_audio_last_pts\":"
                      << json_integer_or_null(system_timeline.available,
                             system_timeline.last_packet_pts)
                      << ",\"system_audio_end_pts\":"
                      << json_integer_or_null(system_timeline.available,
                             system_timeline.end_pts)
                      << ",\"system_audio_duration\":"
                      << json_number_or_null(system_timeline.available,
                             audio_duration(system_timeline))
                      << ",\"mic_first_pts\":"
                      << json_integer_or_null(microphone_timeline.available,
                             microphone_timeline.first_pts)
                      << ",\"mic_last_pts\":"
                      << json_integer_or_null(microphone_timeline.available,
                             microphone_timeline.last_packet_pts)
                      << ",\"mic_end_pts\":"
                      << json_integer_or_null(microphone_timeline.available,
                             microphone_timeline.end_pts)
                      << ",\"mic_duration\":"
                      << json_number_or_null(microphone_timeline.available,
                             audio_duration(microphone_timeline))
                      << ",\"video_start_relative_to_window\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.video_start_relative_to_window_ms / 1000.0)
                      << ",\"video_end_relative_to_window\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.video_end_relative_to_window_ms / 1000.0)
                      << ",\"system_start_relative_to_window\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.audio_start_relative_to_window_ms / 1000.0)
                      << ",\"system_end_relative_to_window\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.audio_end_relative_to_window_ms / 1000.0)
                      << ",\"mic_start_relative_to_window\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.audio_start_relative_to_window_ms / 1000.0)
                      << ",\"mic_end_relative_to_window\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.audio_end_relative_to_window_ms / 1000.0)
                      << ",\"video_start_relative_to_window_ms\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.video_start_relative_to_window_ms)
                      << ",\"video_end_relative_to_window_ms\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.video_end_relative_to_window_ms)
                      << ",\"system_start_relative_to_window_ms\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.audio_start_relative_to_window_ms)
                      << ",\"system_end_relative_to_window_ms\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.audio_end_relative_to_window_ms)
                      << ",\"mic_start_relative_to_window_ms\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.audio_start_relative_to_window_ms)
                      << ",\"mic_end_relative_to_window_ms\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.audio_end_relative_to_window_ms)
                      << ",\"pre_mux_av_start_delta_ms\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.av_start_delta_ms)
                      << ",\"pre_mux_av_end_delta_ms\":"
                      << json_number_or_null(combined_sync.valid,
                             combined_sync.av_end_delta_ms)
                      << ",\"pre_mux_system_av_start_delta_ms\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.av_start_delta_ms)
                      << ",\"pre_mux_system_av_end_delta_ms\":"
                      << json_number_or_null(system_sync.valid,
                             system_sync.av_end_delta_ms)
                      << ",\"pre_mux_mic_av_start_delta_ms\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.av_start_delta_ms)
                      << ",\"pre_mux_mic_av_end_delta_ms\":"
                      << json_number_or_null(microphone_sync.valid,
                             microphone_sync.av_end_delta_ms) << "}"
                      << std::endl;

            const uint32_t diagnostic_task_id = task.task_id;
            const size_t diagnostic_video_packets =
                task.encoded_snapshot.packets.size();
            const size_t diagnostic_audio_tracks =
                task.encoded_audio_tracks.size();
            if (!save_clip_queue_.Push(std::move(task))) {
                SetEngineError(shared_memory,
                    L"The clip save queue is full or shutting down. Wait for "
                    L"the current save to finish, then try again.");
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"save_queue_rejected\","
                          << "\"state\":\"FAILED\","
                          << "\"error_code\":\"CLIP_SAVE_FAILED\","
                          << "\"save_request_id\":" << diagnostic_task_id << ','
                          << "\"queue_depth\":"
                          << save_clip_queue_.GetQueueDepth() << ','
                          << "\"queue_capacity\":"
                          << save_clip_queue_.GetCapacity()
                          << ",\"queue_overflow_status\":\"unavailable:not_exposed\"}"
                          << std::endl;
                return false;
            }
            const auto selection_ms = std::chrono::duration_cast<
                std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - save_request_started).count();
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"ring_selection_completed\","
                      << "\"state\":\"QUEUED\","
                      << "\"task_id\":" << diagnostic_task_id << ','
                      << "\"save_request_id\":" << diagnostic_task_id << ','
                      << "\"requested_duration_seconds\":" << duration_seconds << ','
                      << "\"video_packet_count\":"
                      << diagnostic_video_packets << ','
                      << "\"audio_track_count\":"
                      << diagnostic_audio_tracks << ','
                      << "\"elapsed_ms\":" << selection_ms << "}"
                      << std::endl;
            selection_trace.queued = true;
            std::wcout << L"[SaveClip] Encoded task queued: " << path << std::endl;
            return true;
        }

        // Legacy raw replay: snapshot frame-pool indices.
        const size_t needed = static_cast<size_t>(duration_seconds) * fps_;

        size_t snap_head = 0;
        size_t snap_count = 0;
        {
            std::lock_guard<std::mutex> lock(ring_mutex_);
            snap_head = ring_head_.load(std::memory_order_relaxed);
            snap_count = ring_count_.load(std::memory_order_relaxed);
        }

        if (snap_count == 0) {
            std::cerr << "[SaveClip] Ring buffer is empty" << std::endl;
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"ring_selection_failed\","
                      << "\"state\":\"FAILED\","
                      << "\"save_request_id\":" << save_request_id
                      << ",\"stage\":\"selection\","
                      << "\"snapshot_outcome\":\"EMPTY_RING\","
                      << "\"classification\":\"OTHER_PROVEN_REASON\","
                      << "\"writer_invoked\":false,\"ring_count\":0,"
                      << "\"ring_capacity\":" << max_frames_ << "}"
                      << std::endl;
            return false;
        }

        const size_t safety_frames = fps_;
        const auto raw_selection = replay_interval::SelectFixedRateFrames(
            snap_head, snap_count, needed, safety_frames);
        const size_t frames_to_encode = raw_selection.frame_count;
        const size_t start_pos = static_cast<size_t>(raw_selection.decode_start);

        if (frames_to_encode < needed) {
            std::cerr << "[SaveClip] Only " << frames_to_encode << " frames available "
                << "(requested " << needed << ")" << std::endl;
        }

        SaveClipTask task;
        task.output_path = path;
        task.duration_seconds = duration_seconds;
        task.use_encoded_path = false;
        task.start_frame_idx = start_pos % max_frames_;
        task.frame_count = frames_to_encode;
        task.src_width = crop_width_;
        task.src_height = crop_height_;
        task.enc_width = target_width_;
        task.enc_height = target_height_;
        task.fps = fps_;
        task.bitrate_kbps = bitrate_kbps_;
        task.scaling_mode = scaling_mode_;
        task.separate_audio_enabled = separate_audio_enabled_;
        task.shared_memory = shared_memory;
        task.task_id = save_request_id;

        // The public alpha does not automatically select the raw-video path.
        // Preserve its video-only behavior instead of reintroducing a long
        // raw PCM ring solely for a non-production fallback.
        if (audio_active_ && audio_capture_.IsDeviceLost()) {
            std::cerr << "[SaveClip] Audio device lost - saving clip without audio" << std::endl;
        }

        if (!save_clip_queue_.Push(std::move(task))) {
            SetEngineError(shared_memory,
                L"The clip save queue is full or shutting down. Wait for the "
                L"current save to finish, then try again.");
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                      << "\"event\":\"save_queue_rejected\","
                      << "\"state\":\"FAILED\","
                      << "\"save_request_id\":" << save_request_id
                      << ",\"stage\":\"selection\","
                      << "\"classification\":\"OTHER_PROVEN_REASON\","
                      << "\"queue_depth\":" << save_clip_queue_.GetQueueDepth()
                      << ",\"queue_capacity\":" << save_clip_queue_.GetCapacity()
                      << ",\"queue_overflow_status\":\"unavailable:not_exposed\""
                      << "}" << std::endl;
            return false;
        }
        selection_trace.queued = true;
        std::wcout << L"[SaveClip] Raw task queued: " << path << std::endl;
        return true;
    } catch (const std::bad_alloc&) {
        SetEngineError(shared_memory,
            L"Not enough RAM to prepare this clip. Wait for current saves to finish, "
            L"reduce clip length or bitrate, or close other programs.");
        return false;
    }


    // SaveClipThread - ProcessSaveClipTask branches internally

    void CaptureEngine::SaveClipThread() {
        std::cout << "[SaveClipThread] Started." << std::endl;

        while (true) {
            SaveClipTask task;
            if (!save_clip_queue_.Pop(task)) break;

            std::wcout << L"[SaveClipThread] Processing task " << task.task_id
                << L": " << task.output_path << std::endl;

            SaveWorkerTiming timing;
            const bool trace_enabled = task.trace_dequeued_qpc != 0;
            timing.worker_begin_qpc = frame_pacing_trace::Now(trace_enabled);
            const bool ok = ProcessSaveClipTask(task, trace_enabled ? &timing : nullptr);
            int64_t response_qpc = 0;

            // Failures already publish SetEngineError(). Publish success only after
            // ProcessSaveClipTask confirms the file was written.
            if (ok && task.shared_memory) {
                // Clear the message channel before announcing success so a clip
                // that follows a failed one cannot carry the old error text.
                SetEngineString(task.shared_memory, L"");
                task.shared_memory->engine_response = ResponseType::CLIP_SAVED;
                response_qpc = frame_pacing_trace::Now(trace_enabled);
            }
            if (trace_enabled) timing.Emit(task, ok, response_qpc);
        }

        std::cout << "[SaveClipThread] Exiting." << std::endl;
    }


    // ProcessSaveClipTask - transactional wrapper around both media writers

    bool CaptureEngine::ProcessSaveClipTask(
        const SaveClipTask& task, SaveWorkerTiming* timing) try {
        if (task.use_encoded_path && (!task.encoded_audio_tracks.empty() || !task.audio_warnings.empty())) {
            std::string transaction_id;
            std::string transaction_error;
            if (!CreateAudioManifestTransactionId(&transaction_id, &transaction_error)) {
                SetEngineError(task.shared_memory,
                    L"Could not prepare the audio manifest transaction for this clip.");
                std::cerr << "[SaveClip] " << transaction_error << std::endl;
                return false;
            }
            const auto manifest_path = AudioManifestPathFor(task.output_path);
            const auto result = transactional_save::RunPair(
                task.output_path,
                manifest_path,
                [this, &task, &transaction_id, timing](
                    const std::filesystem::path& media_temporary_path,
                    const std::filesystem::path& manifest_temporary_path,
                    std::string& writer_error) {
                    if (!MuxEncodedClip(task, media_temporary_path.wstring(), timing)) {
                        writer_error = "The temporary clip could not be muxed.";
                        return false;
                    }
                    const bool ok = WriteClipAudioManifest(
                        task.output_path,
                        media_temporary_path,
                        manifest_temporary_path,
                        transaction_id,
                        task.encoded_audio_tracks,
                        &writer_error, task.audio_warnings);
                    if (timing) timing->writer_complete_qpc = frame_pacing_trace::Qpc();
                    return ok;
                });
            if (timing) timing->transaction_end_qpc = frame_pacing_trace::Qpc();
            if (result.cleanup_error) {
                std::wcerr << L"[SaveClip] Secondary paired cleanup failure for "
                    << result.media_temporary_path.wstring() << L": "
                    << result.cleanup_error.value() << std::endl;
            }
            if (result.success) return true;
            if (result.failure != transactional_save::PairFailure::Writer
                    || !result.writer_error.empty()) {
                const std::string detail = transactional_save::DescribeFailure(result);
                const int required = MultiByteToWideChar(
                    CP_UTF8, 0, detail.c_str(), -1, nullptr, 0);
                std::wstring wide_detail;
                if (required > 0) {
                    wide_detail.resize(static_cast<size_t>(required));
                    MultiByteToWideChar(CP_UTF8, 0, detail.c_str(), -1,
                        wide_detail.data(), required);
                }
                SetEngineError(task.shared_memory,
                    wide_detail.empty()
                        ? L"The clip and its audio manifest could not be finalized."
                        : wide_detail.c_str());
            }
            return false;
        }

        const auto result = transactional_save::Run(
            task.output_path,
            [this, &task, timing](const std::filesystem::path& temporary_path,
                          std::string&) {
                const std::wstring output_path = temporary_path.wstring();
                const bool ok = task.use_encoded_path
                    ? MuxEncodedClip(task, output_path, timing)
                    : EncodeRawClip(task, output_path);
                if (timing) timing->writer_complete_qpc = frame_pacing_trace::Qpc();
                return ok;
            });
        if (timing) timing->transaction_end_qpc = frame_pacing_trace::Qpc();

        if (result.cleanup_error) {
            std::wcerr << L"[SaveClip] Secondary cleanup failure for "
                << result.temporary_path.wstring() << L": "
                << result.cleanup_error.value() << std::endl;
        }

        if (result.success) return true;

        // Normal writer failures already published their precise FFmpeg/encoder
        // diagnostic. Preflight/rename failures happen outside the writer and
        // therefore need a message here. A thrown writer exception also carries
        // text in writer_error and is published here.
        if (result.failure != transactional_save::Failure::Writer
                || !result.writer_error.empty()) {
            const std::string detail = transactional_save::DescribeFailure(result);
            const int required = MultiByteToWideChar(
                CP_UTF8, 0, detail.c_str(), -1, nullptr, 0);
            std::wstring wide_detail;
            if (required > 0) {
                wide_detail.resize(static_cast<size_t>(required));
                MultiByteToWideChar(
                    CP_UTF8, 0, detail.c_str(), -1,
                    wide_detail.data(), required);
            }
            SetEngineError(
                task.shared_memory,
                wide_detail.empty()
                    ? L"The temporary clip could not be finalized."
                    : wide_detail.c_str());
        }
        return false;
    } catch (const std::bad_alloc&) {
        SetEngineError(task.shared_memory,
            L"Not enough RAM to finish this clip. Reduce clip length or bitrate, "
            L"or close other programs.");
        return false;
    }


    // Mux replay packets and their codec configuration directly into MP4.
    // Rescale packet timestamps to the output stream timebase without re-encoding.

    bool CaptureEngine::MuxEncodedClip(
        const SaveClipTask& task, const std::wstring& output_path, SaveWorkerTiming* timing) {
        struct MuxTraceSpan {
            SaveWorkerTiming* timing;
            ~MuxTraceSpan() {
                if (timing) timing->mux_end_qpc = frame_pacing_trace::Qpc();
            }
        } mux_trace{timing};
        if (timing) timing->mux_begin_qpc = frame_pacing_trace::Qpc();
        struct MuxDiagnosticSpan {
            uint32_t task_id;
            std::chrono::steady_clock::time_point started;
            bool success = false;
            ~MuxDiagnosticSpan() {
                const auto elapsed = std::chrono::duration_cast<
                    std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - started).count();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"clip_save\","
                          << "\"event\":\"native_mux_completed\","
                          << "\"state\":\"" << (success ? "COMPLETED" : "FAILED")
                          << "\",\"error_code\":"
                          << (success ? "null" : "\"CLIP_SAVE_FAILED\"") << ','
                          << "\"task_id\":" << task_id << ','
                          << "\"elapsed_ms\":" << elapsed << "}"
                          << std::endl;
            }
        } mux_diagnostic{task.task_id, std::chrono::steady_clock::now()};
        const auto& snap = task.encoded_snapshot;
        const auto& video_config = snap.video_config;

        if (!IsValidEncodedVideoConfig(video_config)
            || ToAvCodecId(video_config.codec) == AV_CODEC_ID_NONE) {
            std::cerr << "[MuxEncodedClip] Invalid encoded video config" << std::endl;
            SetEngineError(task.shared_memory,
                L"The replay stream configuration is invalid; the clip was not written.");
            return false;
        }
        const double video_tick_seconds =
            static_cast<double>(video_config.time_base.numerator)
            / static_cast<double>(video_config.time_base.denominator);
        const double video_fps =
            static_cast<double>(video_config.frame_rate.numerator)
            / static_cast<double>(video_config.frame_rate.denominator);
        const double video_frame_seconds = 1.0 / video_fps;

        if (snap.packets.empty()) {
            std::cerr << "[MuxEncodedClip] No packets in snapshot" << std::endl;
            SetEngineError(task.shared_memory,
                L"Nothing to save: the replay buffer held no video frames. "
                L"Let the engine capture for a few seconds before saving.");
            return false;
        }

        // Require a keyframe at the decode start, including for legacy snapshots.
        size_t keyframe_start = snap.packets.size();  // sentinel = not found
        for (size_t i = 0; i < snap.packets.size(); i++) {
            if (snap.packets[i].byte_size() != 0 && snap.packets[i].is_keyframe) {
                keyframe_start = i;
                break;
            }
        }

        if (keyframe_start == snap.packets.size()) {
            std::cerr << "[MuxEncodedClip] WARNING: no keyframe found - "
                << "writing all packets (clip may not decode correctly)" << std::endl;
            keyframe_start = 0;
        }

        // Timestamp-aware snapshots already retain the keyframe before the requested
        // start. Only legacy snapshots need duration trimming by PTS span; packet
        // count does not measure elapsed time when capture cadence varies.
        if (!snap.packets.empty() && snap.presentation_start_qpc_s <= 0.0) {
            const int64_t max_pts_span = DurationInVideoTicks(
                video_config, task.duration_seconds);
            const int64_t newest_pts = snap.packets.back().pts;
            const int64_t cutoff_pts = newest_pts - max_pts_span;

            if (snap.packets[keyframe_start].pts < cutoff_pts) {
                // Current start is outside the time budget. Advance to the
                // earliest keyframe at or after cutoff_pts.
                size_t trimmed = snap.packets.size();  // sentinel
                for (size_t i = keyframe_start + 1; i < snap.packets.size(); i++) {
                    if (snap.packets[i].pts >= cutoff_pts && snap.packets[i].is_keyframe) {
                        trimmed = i;
                        break;
                    }
                }
                if (trimmed < snap.packets.size()) {
                    const size_t frames_discarded = trimmed - keyframe_start;
                    keyframe_start = trimmed;
                    std::cout << "[MuxEncodedClip] PTS trim: discarded "
                        << frames_discarded << " leading frames, "
                        << "keeping last " << task.duration_seconds
                        << "s of footage" << std::endl;
                }
            }
        }

        // Only legacy snapshots trim video to raw-audio coverage using the shared
        // QPC clock. Timestamp-aware saves retain the full video interval and allow
        // shorter or late audio instead of discarding valid footage.
        if (snap.presentation_start_qpc_s <= 0.0
            && task.audio_snapshot.valid
            && task.audio_snapshot.qpc_start_s > 0.0
            && keyframe_start < snap.packets.size()
            && (snap.qpc_start_s > 0.0 || (task.video_qpc_epoch != 0 && task.video_qpc_freq != 0))) {

            const double T_epoch_s = (task.video_qpc_freq != 0)
                ? static_cast<double>(task.video_qpc_epoch)
                  / static_cast<double>(task.video_qpc_freq)
                : 0.0;

            // Wall-clock time of the current video start (keyframe_start).
            // Prefer per-packet QPC if available, fall back to epoch + PTS.
            double video_start_wall_s = 0.0;
            if (snap.packets[keyframe_start].wall_qpc > 0 && task.video_qpc_freq > 0) {
                video_start_wall_s = static_cast<double>(snap.packets[keyframe_start].wall_qpc)
                    / static_cast<double>(task.video_qpc_freq);
            } else {
                video_start_wall_s = T_epoch_s
                    + static_cast<double>(snap.packets[keyframe_start].pts)
                    * video_tick_seconds;
            }

            if (video_start_wall_s < task.audio_snapshot.qpc_start_s) {
                // Video reaches further back than audio. Advance keyframe_start
                // to the first keyframe whose wall-clock time >= audio start.
                const double audio_start_wall_s = task.audio_snapshot.qpc_start_s;
                const int64_t trim_pts = static_cast<int64_t>(
                    (audio_start_wall_s - T_epoch_s)
                    / video_tick_seconds + 0.5);

                size_t new_kf = snap.packets.size(); // sentinel = not found
                for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                    if (snap.packets[i].pts >= trim_pts && snap.packets[i].is_keyframe) {
                        new_kf = i;
                        break;
                    }
                }
                if (new_kf < snap.packets.size()) {
                    const size_t trimmed_frames = new_kf - keyframe_start;
                    keyframe_start = new_kf;
                    std::cout << "[MuxEncodedClip] Audio-range trim: discarded "
                        << trimmed_frames << " leading video frames ("
                        << (static_cast<double>(trimmed_frames) / video_fps) << "s) "
                        << "— audio ring only covers from "
                        << (audio_start_wall_s - T_epoch_s) << "s" << std::endl;
                }
                else {
                    std::cerr << "[MuxEncodedClip] Audio-range trim: no keyframe found "
                        "after audio start (" << (audio_start_wall_s - T_epoch_s)
                        << "s into recording) — clip may have audio gap at start"
                        << std::endl;
                }
            }
        }

        // Subtract the presentation boundary, preserving negative PTS for decoder
        // pre-roll. The MP4 edit list hides that pre-roll without re-encoding.

        // Legacy raw PCM remains only for the non-production compatibility path.
        // The normal AUDIT-050 contract carries one Default Mix plus actual
        // source tracks in their own persistent AAC packet snapshots.
        const bool write_legacy_audio = task.has_audio
            && task.audio_snapshot.valid
            && !task.audio_snapshot.samples.empty();
        const bool has_contract_audio_tracks = !task.encoded_audio_tracks.empty();
        const bool write_packet_audio = has_contract_audio_tracks
            || (!omit_default_mix_track_ && task.has_encoded_audio
                && task.encoded_audio_snapshot.valid());

        const int64_t pts_offset = snap.presentation_start_pts;

        const size_t usable_count = snap.packets.size() - keyframe_start;

        // Trimmed video clip duration in seconds.
        // Keep the source wall-clock span. A sparse capture must never be
        // shortened by converting its packet count directly into duration.
        const int64_t newest_video_pts     = snap.packets.back().pts;
        const int64_t video_clip_pts_span  = newest_video_pts - pts_offset;
        const double video_clip_duration_s =
            static_cast<double>(video_clip_pts_span + 1) * video_tick_seconds;

        // Intersect the PCM snapshot with the video presentation interval using QPC.
        // If QPC timing is unavailable, align video_duration seconds from the audio end.

        int64_t audio_aligned_start_sample = 0;
        int64_t audio_output_pts_offset = 0;

        if (write_legacy_audio) {
            const int64_t total_snap_frames = static_cast<int64_t>(
                task.audio_snapshot.samples.size())
                / static_cast<int64_t>(task.audio_snapshot.channels);

            bool qpc_aligned = false;

            // Both timestamps share the QPC clock: video uses raw ticks / qpc_freq;
            // WASAPI uses 100 ns units / 10,000,000. Match the first video packet
            // after trimming to the corresponding audio sample.

            // Presentation starts at the requested cutoff, not at the older
            // keyframe retained solely for decoder pre-roll.
            double video_start_wall_s = 0.0;
            bool have_video_wall_time = false;

            if (snap.presentation_start_qpc_s > 0.0) {
                video_start_wall_s = snap.presentation_start_qpc_s;
                have_video_wall_time = true;
            }
            // Legacy fallback: use per-packet QPC from the physical first packet.
            else if (snap.qpc_start_s > 0.0) {
                // snap.qpc_start_s is from the FULL snapshot. We need the QPC of
                // the first packet after keyframe_start trimming.
                for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                    if (snap.packets[i].byte_size() != 0 && snap.packets[i].wall_qpc > 0) {
                        // Convert raw QPC ticks to seconds using the same freq
                        // that was used in the snapshot (stored in qpc_start_s derivation)
                        if (task.video_qpc_freq > 0) {
                            video_start_wall_s = static_cast<double>(snap.packets[i].wall_qpc)
                                / static_cast<double>(task.video_qpc_freq);
                        } else {
                            // qpc_freq not available from encoder, derive from snapshot ratio
                            // snap.qpc_start_s was computed by EncodedRingBuffer using its qpc_freq_
                            // Use the first packet's wall_qpc with the snapshot's known conversion
                            video_start_wall_s = snap.qpc_start_s;
                        }
                        have_video_wall_time = true;
                        break;
                    }
                }
            }

            // Method 2: Fall back to epoch + PTS derivation
            if (!have_video_wall_time
                && task.video_qpc_epoch != 0 && task.video_qpc_freq != 0) {
                video_start_wall_s =
                    static_cast<double>(task.video_qpc_epoch)
                        / static_cast<double>(task.video_qpc_freq)
                    + static_cast<double>(pts_offset)
                        * video_tick_seconds;
                have_video_wall_time = true;
            }

            // Compute video end wall time (for overlap calculation)
            double video_end_wall_s = 0.0;
            if (have_video_wall_time) {
                video_end_wall_s = video_start_wall_s + video_clip_duration_s;
            }

            if (have_video_wall_time && task.audio_snapshot.qpc_start_s > 0.0) {
                const double overlap_start_s = std::max(
                    video_start_wall_s, task.audio_snapshot.qpc_start_s);
                const double overlap_end_s = std::min(
                    video_end_wall_s, task.audio_snapshot.qpc_end_s);

                if (overlap_start_s < overlap_end_s) {
                    audio_aligned_start_sample = static_cast<int64_t>(
                        (overlap_start_s - task.audio_snapshot.qpc_start_s)
                        * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);
                    audio_output_pts_offset = static_cast<int64_t>(
                        (overlap_start_s - video_start_wall_s)
                        * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);
                    audio_aligned_start_sample = std::clamp<int64_t>(
                        audio_aligned_start_sample, 0, total_snap_frames);
                    qpc_aligned = true;

                    std::cout << "[MuxEncodedClip] A/V sync (QPC overlap):" << std::endl;
                    std::cout << "  Video start wall time    : " << video_start_wall_s << "s" << std::endl;
                    std::cout << "  Video end wall time      : " << video_end_wall_s << "s" << std::endl;
                    std::cout << "  Audio snap QPC start     : " << task.audio_snapshot.qpc_start_s << "s" << std::endl;
                    std::cout << "  Audio snap QPC end       : " << task.audio_snapshot.qpc_end_s << "s" << std::endl;
                    std::cout << "  Audio overlap start      : " << overlap_start_s << "s" << std::endl;
                    std::cout << "  Audio snap frames        : " << total_snap_frames << std::endl;
                    std::cout << "  Audio start sample       : " << audio_aligned_start_sample
                        << " (skip " << (static_cast<double>(audio_aligned_start_sample)
                            / task.audio_snapshot.sample_rate) << "s)" << std::endl;
                    std::cout << "  Audio timeline offset    : " << audio_output_pts_offset
                        << " samples" << std::endl;
                }
                else {
                    audio_aligned_start_sample = total_snap_frames;
                    qpc_aligned = true;
                    std::cerr << "[MuxEncodedClip] Audio has no overlap with the "
                        "requested video interval; writing video-only" << std::endl;
                }
            }

            // Fallback: end-aligned duration-based
            // Used only when QPC data is completely unavailable.
            // Takes video_duration worth of audio from the end of the snapshot.
            if (!qpc_aligned) {
                const int64_t frames_needed = static_cast<int64_t>(
                    video_clip_duration_s
                    * static_cast<double>(task.audio_snapshot.sample_rate) + 0.5);

                audio_aligned_start_sample = total_snap_frames - frames_needed;
                if (audio_aligned_start_sample < 0)
                    audio_aligned_start_sample = 0;
                if (audio_aligned_start_sample > total_snap_frames)
                    audio_aligned_start_sample = total_snap_frames;

                std::cout << "[MuxEncodedClip] A/V sync (end-aligned fallback):" << std::endl;
                std::cout << "  Video clip duration      : " << video_clip_duration_s << "s" << std::endl;
                std::cout << "  Audio snap frames        : " << total_snap_frames << std::endl;
                std::cout << "  Audio start frame        : " << audio_aligned_start_sample
                    << " (skip " << (static_cast<double>(audio_aligned_start_sample)
                        / task.audio_snapshot.sample_rate) << "s)" << std::endl;
            }
        }

        // Convert output path to UTF-8.
        // UTF-16 -> UTF-8 worst-case expansion is 3x; MAX_PATH is 260 chars but
        // long-path-aware builds can exceed that. 1024 bytes covers typical paths.
        char output_utf8[1024] = {};
        WideCharToMultiByte(CP_UTF8, 0, output_path.c_str(), -1,
            output_utf8, sizeof(output_utf8) - 1, nullptr, nullptr);

        AVFormatContext* fmt_ctx = nullptr;
        avformat_alloc_output_context2(&fmt_ctx, nullptr, "mp4", output_utf8);
        if (!fmt_ctx) {
            std::cerr << "[MuxEncodedClip] avformat_alloc_output_context2 failed" << std::endl;
            SetEngineError(task.shared_memory,
                L"Failed to create the output file container. The clip path may "
                L"be invalid or on an unsupported filesystem.");
            return false;
        }
        fmt_ctx->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;
        fmt_ctx->max_interleave_delta = 1'000'000;

        AVStream* video_stream = avformat_new_stream(fmt_ctx, nullptr);
        if (!video_stream) {
            std::cerr << "[MuxEncodedClip] avformat_new_stream (video) failed" << std::endl;
            avformat_free_context(fmt_ctx);
            SetEngineError(task.shared_memory,
                L"Failed to create the video stream in the output file.");
            return false;
        }

        video_stream->codecpar->codec_type = AVMEDIA_TYPE_VIDEO;
        video_stream->codecpar->codec_id = ToAvCodecId(video_config.codec);
        video_stream->codecpar->width = static_cast<int>(video_config.width);
        video_stream->codecpar->height = static_cast<int>(video_config.height);
        video_stream->codecpar->format = AV_PIX_FMT_YUV420P;

        // High-resolution MP4 stream timebase; packet timestamps are rescaled
        // from the encoder-provided time base below.
        video_stream->time_base = AVRational{ 1, 90000 };
        ApplyConfiguredVideoMetadata(fmt_ctx, video_stream, video_config);

        // Copy the codec's decoder configuration record (avcC/hvcC/av1C).
        if (!video_config.codec_extradata.empty()) {
            video_stream->codecpar->extradata = static_cast<uint8_t*>(
                av_malloc(video_config.codec_extradata.size()
                    + AV_INPUT_BUFFER_PADDING_SIZE));
            memcpy(video_stream->codecpar->extradata,
                video_config.codec_extradata.data(),
                video_config.codec_extradata.size());
            memset(video_stream->codecpar->extradata
                    + video_config.codec_extradata.size(),
                0, AV_INPUT_BUFFER_PADDING_SIZE);
            video_stream->codecpar->extradata_size =
                static_cast<int>(video_config.codec_extradata.size());
        }
        else {
            std::cerr << "[MuxEncodedClip] WARNING: no video extradata - "
                << "file may not play everywhere" << std::endl;
        }

        // Legacy raw-PCM saves encode aligned audio to AAC on the save thread.
        // Add the audio stream and encoder ASC before avformat_write_header().
        struct MuxAudioTrack {
            AudioSourceMetadata metadata;
            AVStream* stream = nullptr;
            std::vector<std::vector<uint8_t>> packets;
            std::vector<const EncodedAudioPacket*> persistent_packets;
            std::string codec = "aac";
            std::vector<int64_t> packet_pts;
            std::vector<int64_t> packet_durations;
            std::vector<uint8_t> extradata;
            uint32_t sample_rate = 0;
            uint32_t channels = 0;
            int64_t output_pts_offset = 0;
            bool is_default_mix = false;
        };
        std::vector<MuxAudioTrack> mux_audio_tracks;

        EncodedAudioTrack legacy;
        const auto append_persistent_track = [&](const EncodedAudioTrack& contract,
                                                  bool is_default_mix) {
            MuxAudioTrack output;
            output.metadata = contract.source;
            output.sample_rate = contract.snapshot.format.sample_rate;
            output.channels = contract.snapshot.format.channels;
            output.extradata = contract.snapshot.codec_extradata;
            output.codec = contract.snapshot.format.codec;
            output.output_pts_offset = -contract.presentation_start_pts_samples;
            output.is_default_mix = is_default_mix;
            for (const auto& packet : contract.snapshot.packets) {
                if (packet.byte_size() == 0 || packet.duration_samples <= 0) continue;
                output.packets.emplace_back();
                output.persistent_packets.push_back(&packet);
                output.packet_pts.push_back(packet.pts_samples);
                output.packet_durations.push_back(packet.duration_samples);
            }
            if (!output.packets.empty()) mux_audio_tracks.push_back(std::move(output));
        };

        if (has_contract_audio_tracks) {
            if (task.encoded_audio_tracks.size() > kMaxClipAudioTracks) {
                SetEngineError(task.shared_memory,
                    L"The clip contains more audio tracks than the supported alpha limit.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            for (size_t index = 0; index < task.encoded_audio_tracks.size(); ++index) {
                const auto& contract = task.encoded_audio_tracks[index];
                if (!contract.valid()) {
                    SetEngineError(task.shared_memory,
                        L"An audio track no longer matches this capture generation.");
                    avformat_free_context(fmt_ctx);
                    return false;
                }
                const bool is_default_mix = index == 0
                    && contract.source.identity.type == AudioSourceType::System
                    && contract.source.identity.persistent_identity == "default-mix"
                    && contract.source.identity.display_name == "Default Mix";
                append_persistent_track(contract, is_default_mix);
            }
            if (mux_audio_tracks.empty()) {
                SetEngineError(task.shared_memory,
                    L"No valid packets were available for the captured audio tracks.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            std::cout << "[MuxEncodedClip] Using persistent audio replay: "
                << mux_audio_tracks.size() << " track(s)" << std::endl;
        }
        else if (write_packet_audio) {
            // Compatibility for a queued task created before the structured
            // contract was introduced. New saves always use the branch above.
            legacy.snapshot = task.encoded_audio_snapshot;
            legacy.presentation_start_pts_samples = task.audio_presentation_start_pts_samples;
            legacy.source.identity.id = legacy.snapshot.source_id;
            legacy.source.identity.type = AudioSourceType::System;
            legacy.source.identity.persistent_identity = "default-mix";
            legacy.source.identity.display_name = "Default Mix";
            legacy.source.identity.icon_reference = "system-audio";
            legacy.source.format = legacy.snapshot.format;
            legacy.source.state.admitted = true;
            legacy.source.state.first_active_100ns = 0;
            legacy.source.state.last_active_100ns = 0;
            append_persistent_track(legacy, true);
        }
        else if (write_legacy_audio) {
            MuxAudioTrack output;
            const uint32_t sr = task.audio_snapshot.sample_rate;
            const uint32_t ch = task.audio_snapshot.channels;
            output.sample_rate = sr;
            output.channels = ch;
            output.is_default_mix = true;
            AudioEncoder aac_enc;
            bool enc_ok = aac_enc.Initialize(
                sr, ch, task.audio_bitrate_kbps,
                [&](const uint8_t* data, uint32_t size, int64_t pts) {
                    output.packets.push_back(std::vector<uint8_t>(data, data + size));
                    output.packet_pts.push_back(pts);
                    output.packet_durations.push_back(1024);
                });
            if (enc_ok) {
                const int64_t total_frames = static_cast<int64_t>(
                    task.audio_snapshot.samples.size()) / static_cast<int64_t>(ch);
                const int64_t frames_to_encode = total_frames - audio_aligned_start_sample;
                if (frames_to_encode > 0) {
                    const float* pcm_start = task.audio_snapshot.samples.data()
                        + static_cast<size_t>(audio_aligned_start_sample) * ch;
                    aac_enc.EncodeSamples(pcm_start,
                        static_cast<uint32_t>(frames_to_encode) * ch);
                }
                aac_enc.Finalize();
                output.extradata = aac_enc.GetExtradata();
                output.output_pts_offset = audio_output_pts_offset;
                if (!output.packets.empty()) mux_audio_tracks.push_back(std::move(output));
            }
        }

        for (auto& audio : mux_audio_tracks) {
            audio.stream = avformat_new_stream(fmt_ctx, nullptr);
            if (!audio.stream) {
                SetEngineError(task.shared_memory,
                    L"Could not create the clip audio stream.");
                avformat_free_context(fmt_ctx);
                return false;
            }
            audio.stream->codecpar->codec_type = AVMEDIA_TYPE_AUDIO;
            const bool lossless = audio.codec == "pcm_f32le";
            audio.stream->codecpar->codec_id = lossless ? AV_CODEC_ID_PCM_F32LE : AV_CODEC_ID_AAC;
            audio.stream->codecpar->sample_rate = static_cast<int>(audio.sample_rate);
            av_channel_layout_default(&audio.stream->codecpar->ch_layout, static_cast<int>(audio.channels));
            audio.stream->codecpar->frame_size = lossless ? 0 : 1024;
            audio.stream->codecpar->format = lossless ? AV_SAMPLE_FMT_FLT : AV_SAMPLE_FMT_FLTP;
            if (lossless) {
                audio.stream->codecpar->bits_per_coded_sample = 32;
                audio.stream->codecpar->block_align = audio.channels * sizeof(float);
            }
            audio.stream->time_base = AVRational{1, static_cast<int>(audio.sample_rate)};
            if (audio.is_default_mix) audio.stream->disposition |= AV_DISPOSITION_DEFAULT;
            const std::string name = audio.is_default_mix
                ? "Default Mix" : audio.metadata.identity.display_name;
            av_dict_set(&audio.stream->metadata, "handler_name", name.c_str(), 0);
            if (audio.metadata.identity.id.IsValid())
                av_dict_set(&audio.stream->metadata, "handler_name",
                            AudioSourceHandler(audio.metadata, audio.packet_pts.empty() ? 0 :
                                std::max<int64_t>(0, audio.packet_pts.front() + audio.output_pts_offset),
                                audio.packet_pts.empty() ? 0 : std::max<int64_t>(0,
                                    audio.packet_pts.back() + audio.packet_durations.back() + audio.output_pts_offset)).c_str(), 0);
            av_dict_set(&audio.stream->metadata, "title", name.c_str(), 0);
            if (!audio.extradata.empty()) {
                audio.stream->codecpar->extradata = static_cast<uint8_t*>(
                    av_malloc(audio.extradata.size() + AV_INPUT_BUFFER_PADDING_SIZE));
                if (!audio.stream->codecpar->extradata) {
                    SetEngineError(task.shared_memory,
                        L"Could not allocate clip audio stream metadata.");
                    avformat_free_context(fmt_ctx);
                    return false;
                }
                memcpy(audio.stream->codecpar->extradata,
                    audio.extradata.data(), audio.extradata.size());
                memset(audio.stream->codecpar->extradata + audio.extradata.size(),
                    0, AV_INPUT_BUFFER_PADDING_SIZE);
                audio.stream->codecpar->extradata_size = static_cast<int>(audio.extradata.size());
            }
        }

        // The native publication is source-preserving. Combined mode is
        // collapsed by the UI after the file is committed; this marker keeps
        // an interrupted/failing finalization truthful about the current MP4
        // topology so the editor never invents a single-track interpretation.
        const bool native_audio_is_separated = task.separate_audio_enabled
            || std::any_of(mux_audio_tracks.begin(), mux_audio_tracks.end(),
                           [](const auto& track) { return track.codec == "pcm_f32le"; })
            || mux_audio_tracks.size() > 1;
        av_dict_set(&fmt_ctx->metadata, "comment",
            native_audio_is_separated
                ? "fthr-audio-mode=separated"
                : "fthr-audio-mode=combined", 0);

        int ret = avio_open(&fmt_ctx->pb, output_utf8, AVIO_FLAG_WRITE);
        if (ret < 0) {
            std::cerr << "[MuxEncodedClip] avio_open failed: " << ret << std::endl;
            avformat_free_context(fmt_ctx);
            {
                // avio_open is the disk-full / read-only / path-missing case —
                // the single most common real failure. Carry the errno-style
                // code so a bug report can name it.
                wchar_t msg[512];
                _snwprintf_s(msg, _TRUNCATE,
                    L"Could not open the clip file for writing (error %d). "
                    L"Check free disk space and folder permissions.", ret);
                SetEngineError(task.shared_memory, msg);
            }
            return false;
        }

        AVDictionary* output_options = nullptr;
        // Snapshot-owned identity must never be read from the current capture:
        // this queued save can finish after recovery has started a new generation.
        const auto capture_timeline = SerializeCaptureTimeline(snap.capture_timeline);
        if (!capture_timeline.empty())
            av_dict_set(&fmt_ctx->metadata, kCaptureTimelineMetadataTag,
                        capture_timeline.c_str(), 0);
        av_dict_set(&output_options, "movflags", "use_metadata_tags", 0);
        ret = avformat_write_header(fmt_ctx, &output_options);
        av_dict_free(&output_options);
        if (ret < 0) {
            std::cerr << "[MuxEncodedClip] avformat_write_header failed: " << ret << std::endl;
            avio_closep(&fmt_ctx->pb);
            avformat_free_context(fmt_ctx);
            {
                wchar_t msg[512];
                _snwprintf_s(msg, _TRUNCATE,
                    L"Failed to write the clip file header (error %d).", ret);
                SetEngineError(task.shared_memory, msg);
            }
            return false;
        }

        const AVRational encode_tb = {
            video_config.time_base.numerator,
            video_config.time_base.denominator};

        std::cout << "\n========================================" << std::endl;
        std::cout << "[MuxEncodedClip] DIAGNOSTIC INFO" << std::endl;
        std::cout << "========================================" << std::endl;
        std::cout << "Total packets in snapshot : " << snap.packets.size() << std::endl;
        std::cout << "Keyframe start index      : " << keyframe_start << std::endl;
        std::cout << "Usable packets            : " << usable_count << std::endl;
        std::cout << "Presentation PTS boundary : " << pts_offset
            << " (" << (static_cast<double>(pts_offset) * video_tick_seconds)
            << "s absolute)" << std::endl;
        std::cout << "History classification   : "
            << (snap.full_history ? "full" : "partial") << std::endl;
        std::cout << "Video codec               : "
            << VideoCodecName(video_config.codec) << std::endl;
        std::cout << "Task FPS                  : " << video_fps << std::endl;
        std::cout << "Task duration             : " << task.duration_seconds << " seconds" << std::endl;
        std::cout << "Expected frame count      : "
            << (static_cast<double>(task.duration_seconds) * video_fps)
            << std::endl;
        std::cout << "Audio stream              : "
            << (mux_audio_tracks.empty() ? "NO" : "YES")
            << " (" << mux_audio_tracks.size() << " track(s))" << std::endl;

        // Compute average PTS delta to verify actual capture rate.
        {
            int64_t prev_raw_pts = -1;
            int64_t total_delta = 0;
            int64_t delta_count = 0;
            for (size_t i = keyframe_start; i < snap.packets.size(); i++) {
                if (snap.packets[i].byte_size() == 0) continue;
                if (prev_raw_pts >= 0) {
                    total_delta += snap.packets[i].pts - prev_raw_pts;
                    ++delta_count;
                }
                prev_raw_pts = snap.packets[i].pts;
            }
            if (delta_count > 0) {
                double avg_delta = static_cast<double>(total_delta) / delta_count;
                double actual_fps = (avg_delta > 0.0)
                    ? (1.0 / (avg_delta * video_tick_seconds))
                    : 0.0;
                std::cout << "Avg PTS delta             : " << avg_delta
                    << " (actual capture rate ~" << actual_fps << " fps)" << std::endl;
            }
        }
        std::cout << "\nTimebase configuration:" << std::endl;
        std::cout << "  Encode timebase : " << encode_tb.num << "/" << encode_tb.den
            << " (each tick = " << (1.0 / encode_tb.den) << " sec)" << std::endl;
        std::cout << "  Stream timebase : " << video_stream->time_base.num
            << "/" << video_stream->time_base.den
            << " (each tick = " << (1.0 / video_stream->time_base.den) << " sec)" << std::endl;
        std::cout << "  avg_frame_rate  : " << video_stream->avg_frame_rate.num << "/"
            << video_stream->avg_frame_rate.den << " = "
            << (static_cast<float>(video_stream->avg_frame_rate.num)
                / video_stream->avg_frame_rate.den)
            << " fps" << std::endl;
        std::cout << "========================================\n" << std::endl;

        int     video_packet_count = 0;
        int64_t last_video_pts = -1;

        // Reuse the packet descriptor while reading one payload at a time.
        AVPacket* av_pkt = av_packet_alloc();
        if (!av_pkt) {
            std::cerr << "[MuxEncodedClip] av_packet_alloc failed" << std::endl;
            avio_closep(&fmt_ctx->pb);
            avformat_free_context(fmt_ctx);
            SetEngineError(task.shared_memory,
                L"Out of memory while preparing the clip for writing.");
            return false;
        }

        auto fail_media_write = [&](const wchar_t* operation, int error_code) {
            av_packet_free(&av_pkt);
            const int close_error = avio_closep(&fmt_ctx->pb);
            if (close_error < 0) {
                std::cerr << "[MuxEncodedClip] Secondary close failure: "
                    << close_error << std::endl;
            }
            avformat_free_context(fmt_ctx);
            wchar_t message[512];
            _snwprintf_s(
                message,
                _TRUNCATE,
                L"Failed while %ls the temporary clip (error %d). "
                L"The incomplete file was not published.",
                operation,
                error_code);
            SetEngineError(task.shared_memory, message);
            return false;
        };

        const auto save_budget = task.replay_save_lease && replay_storage_
            ? replay_storage_->memory_budget() : nullptr;
        ReplayMemoryReservation packet_memory(save_budget, ReplayMemoryUse::Save);
        std::unique_ptr<uint8_t, decltype(&av_free)> packet_bytes(nullptr, &av_free);
        const auto release_packet_bytes = [&] {
            packet_bytes.reset();
            packet_memory.Shrink(packet_memory.bytes());
        };
        const auto prepare_packet = [&](size_t size, size_t scratch_size,
                                        ReplayMemoryReservation& scratch,
                                        SavePacketTiming* packet_timing) {
            const size_t padded_size = size + AV_INPUT_BUFFER_PADDING_SIZE;
            if (padded_size > packet_memory.bytes()) release_packet_bytes();

            // Keep charging video conversion scratch. If a large preceding
            // packet ties up the save budget, release its buffer and retry.
            if (scratch_size && !scratch.Grow(scratch_size)) {
                if (padded_size >= packet_memory.bytes())
                    return fail_media_write(L"reserving replay RAM for", AVERROR(ENOMEM));
                release_packet_bytes();
                if (!scratch.Grow(scratch_size))
                    return fail_media_write(L"reserving replay RAM for", AVERROR(ENOMEM));
            }

            if (!packet_bytes) {
                if (!packet_memory.Grow(padded_size))
                    return fail_media_write(L"reserving replay RAM for", AVERROR(ENOMEM));
                const auto started = frame_pacing_trace::Now(packet_timing != nullptr);
                packet_bytes.reset(static_cast<uint8_t*>(av_malloc(padded_size)));
                if (packet_timing)
                    packet_timing->allocation_ticks += frame_pacing_trace::Qpc() - started;
                if (!packet_bytes)
                    return fail_media_write(L"allocating a packet for", AVERROR(ENOMEM));
            }

            // av_write_frame borrows the packet. FFmpeg copies an unrefcounted
            // payload if it retains a reference. Keep the required padding
            // zero even when the next packet uses less of this buffer.
            av_pkt->data = packet_bytes.get();
            av_pkt->size = static_cast<int>(size);
            std::memset(av_pkt->data + size, 0, AV_INPUT_BUFFER_PADDING_SIZE);
            return true;
        };
        if (timing) timing->packet_loop_begin_qpc = frame_pacing_trace::Qpc();
        const auto write_video = [&](const EncodedRingPacket& pkt) {
            const size_t size = pkt.byte_size();
            if (size == 0) return true;
            if (size > static_cast<size_t>(std::numeric_limits<int>::max()) - AV_INPUT_BUFFER_PADDING_SIZE)
                return fail_media_write(L"preparing a video packet for", AVERROR(EINVAL));
            ReplayMemoryReservation scratch(save_budget, ReplayMemoryUse::Save);
            if (!prepare_packet(size, size, scratch, timing ? &timing->video : nullptr))
                return false;
            const auto read_started = frame_pacing_trace::Now(timing != nullptr);
            const bool read = pkt.ReadInto(av_pkt->data);
            if (timing) {
                timing->video.read_copy_ticks += frame_pacing_trace::Qpc() - read_started;
                ++timing->video.packets;
                timing->video.bytes += size;
            }
            if (!read)
                return fail_media_write(L"reading video replay data for", AVERROR(EIO));

            // Keep decoder pre-roll negative; MP4 presents from the logical
            // replay cutoff at t=0 via an edit list.
            int64_t rebased_pts = 0;
            if (!CheckedSignedSubtract(pkt.pts, pts_offset, rebased_pts))
                return fail_media_write(
                    L"rebasing video timestamps for", -1);
            av_pkt->pts = rebased_pts;
            av_pkt->dts = rebased_pts;
            av_pkt->duration = 1;
            av_pkt->stream_index = video_stream->index;
            av_pkt->flags = pkt.is_keyframe ? AV_PKT_FLAG_KEY : 0;

            // Log first 3 packets before rescaling.
            if (video_packet_count < 3) {
                std::cout << "[VPkt #" << video_packet_count << " BEFORE rescale] "
                    << "PTS=" << av_pkt->pts
                    << " DTS=" << av_pkt->dts
                    << " duration=" << av_pkt->duration
                    << (pkt.is_keyframe ? " [KEYFRAME]" : "")
                    << std::endl;
            }

            av_packet_rescale_ts(av_pkt, encode_tb, video_stream->time_base);
            if (video_packet_count < 3) {
                std::cout << "[VPkt #" << video_packet_count << " AFTER rescale]  "
                    << "PTS=" << av_pkt->pts
                    << " DTS=" << av_pkt->dts
                    << " duration=" << av_pkt->duration
                    << " (time="
                    << (static_cast<double>(av_pkt->pts) / video_stream->time_base.den)
                    << "s)" << std::endl;
            }

            last_video_pts = av_pkt->pts;
            const auto write_started = frame_pacing_trace::Now(timing != nullptr);
            ret = av_write_frame(fmt_ctx, av_pkt);
            av_packet_unref(av_pkt);
            if (timing) timing->video.write_ticks += frame_pacing_trace::Qpc() - write_started;
            if (ret < 0)
                return fail_media_write(L"writing video data to", ret);
            video_packet_count++;
            return true;
        };

        const auto write_audio = [&](const MuxAudioTrack& audio, size_t i, int64_t pts) {
            const auto* persistent = audio.persistent_packets.empty()
                ? nullptr : audio.persistent_packets[i];
            size_t size = persistent ? persistent->byte_size() : audio.packets[i].size();
            if (persistent && audio.codec == "pcm_f32le") {
                const uint64_t frame_bytes = static_cast<uint64_t>(audio.channels) * sizeof(float);
                const int64_t samples = audio.packet_durations[i];
                if (!frame_bytes || samples <= 0 || static_cast<uint64_t>(samples) > size / frame_bytes)
                    return fail_media_write(L"preparing a lossless audio packet for", AVERROR(EINVAL));
                // The snapshot may retain only a prefix of a coalesced PCM
                // packet. Match the bytes and duration on the muxed sample.
                size = static_cast<size_t>(static_cast<uint64_t>(samples) * frame_bytes);
            }
            if (!size || size > static_cast<size_t>(std::numeric_limits<int>::max()) - AV_INPUT_BUFFER_PADDING_SIZE)
                return fail_media_write(L"preparing an audio packet for", AVERROR(EINVAL));
            ReplayMemoryReservation no_scratch;
            if (!prepare_packet(size, 0, no_scratch, timing ? &timing->audio : nullptr))
                return false;
            const auto read_started = frame_pacing_trace::Now(timing != nullptr);

            bool read = true;
            if (persistent && persistent->replay_storage) {
                read = persistent->replay_storage->ReadSlice(0, av_pkt->data, size);
            } else {
                const auto spooled = persistent && persistent->storage
                    ? persistent->storage->Read() : nullptr;
                read = !(persistent && persistent->storage && !spooled);
                if (read) {
                    const auto& data = spooled ? *spooled
                        : persistent ? persistent->data : audio.packets[i];
                    std::memcpy(av_pkt->data, data.data(), size);
                }
            }
            if (timing) {
                timing->audio.read_copy_ticks += frame_pacing_trace::Qpc() - read_started;
                ++timing->audio.packets;
                timing->audio.bytes += size;
            }
            if (!read) return fail_media_write(L"reading lossless audio data for", AVERROR(EIO));
            av_pkt->pts = pts;
            av_pkt->dts = pts;
            av_pkt->duration = audio.packet_durations[i];
            av_pkt->stream_index = audio.stream->index;
            av_pkt->flags = 0;
            av_packet_rescale_ts(av_pkt, AVRational{1, static_cast<int>(audio.sample_rate)},
                                 audio.stream->time_base);

            const auto write_started = frame_pacing_trace::Now(timing != nullptr);
            ret = av_write_frame(fmt_ctx, av_pkt);
            av_packet_unref(av_pkt);
            if (timing) timing->audio.write_ticks += frame_pacing_trace::Qpc() - write_started;
            if (ret < 0)
                return fail_media_write(L"writing audio data to", ret);
            return true;
        };

        // The input streams already carry monotonic DTS. Merge their cursors
        // here so FFmpeg never accumulates a clip's worth of payloads while
        // waiting for a different track (including tracks that start late).
        std::vector<size_t> audio_cursors(mux_audio_tracks.size(), 0);
        size_t video_cursor = keyframe_start;
        size_t audio_packet_count = 0;
        while (true) {
            size_t next_audio = mux_audio_tracks.size();
            int64_t next_audio_pts = 0;
            for (size_t track = 0; track < mux_audio_tracks.size(); ++track) {
                const auto& audio = mux_audio_tracks[track];
                const size_t cursor = audio_cursors[track];
                if (!audio.stream || cursor >= audio.packets.size()) continue;
                const int64_t pts = audio.packet_pts[cursor] + audio.output_pts_offset;
                if (next_audio == mux_audio_tracks.size()
                    || av_compare_ts(pts, AVRational{1, static_cast<int>(audio.sample_rate)}, next_audio_pts,
                        AVRational{1, static_cast<int>(mux_audio_tracks[next_audio].sample_rate)}) < 0) {
                    next_audio = track;
                    next_audio_pts = pts;
                }
            }
            if (video_cursor < snap.packets.size()) {
                const auto& packet = snap.packets[video_cursor];
                int64_t pts = 0;
                if (!CheckedSignedSubtract(packet.pts, pts_offset, pts))
                    return fail_media_write(L"rebasing video timestamps for", AVERROR(EINVAL));
                if (next_audio == mux_audio_tracks.size()
                    || av_compare_ts(pts, encode_tb, next_audio_pts,
                        AVRational{1, static_cast<int>(mux_audio_tracks[next_audio].sample_rate)}) <= 0) {
                    if (!write_video(packet)) return false;
                    ++video_cursor;
                    continue;
                }
            }
            if (next_audio == mux_audio_tracks.size()) break;
            if (!write_audio(mux_audio_tracks[next_audio], audio_cursors[next_audio]++, next_audio_pts))
                return false;
            ++audio_packet_count;
        }
        if (timing) timing->packet_loop_end_qpc = frame_pacing_trace::Qpc();
        if (video_packet_count == 0)
            return fail_media_write(L"writing video data to", -1);
        av_packet_free(&av_pkt);
        release_packet_bytes();

        const double actual_duration_s = (last_video_pts >= 0)
            ? (static_cast<double>(last_video_pts) / 90000.0)
                + video_frame_seconds
            : 0.0;

        std::cout << "\n[MuxEncodedClip] Summary:" << std::endl;
        std::cout << "  Video packets written   : " << video_packet_count << std::endl;
        std::cout << "  Audio packets written   : " << audio_packet_count << std::endl;
        std::cout << "  Last video PTS          : " << last_video_pts
            << " (" << (static_cast<double>(last_video_pts)
                / video_stream->time_base.den) << "s)" << std::endl;
        std::cout << "  Calculated clip duration: " << actual_duration_s << "s" << std::endl;
        std::cout << "  Requested duration      : " << task.duration_seconds << "s" << std::endl;
        if (!snap.full_history)
            std::cout << "  NOTE: partial history after startup/recovery; saved "
                "all decodable media currently available." << std::endl;
        std::cout << "========================================\n" << std::endl;

        if (timing) timing->trailer_begin_qpc = frame_pacing_trace::Qpc();
        ret = av_write_trailer(fmt_ctx);
        if (timing) timing->trailer_end_qpc = frame_pacing_trace::Qpc();
        if (ret < 0)
            return fail_media_write(L"finalizing the container for", ret);

        // The trailer is complete before the top-level uuid box is appended.
        // This keeps the marker payload outside stream offsets and lets the
        // transactional save publish one self-contained MP4.
        if (!snap.highlights.empty()) {
            std::vector<uint8_t> highlight_box;
            if (!BuildHighlightUuidBox(snap.highlights, highlight_box)
                || highlight_box.size() > static_cast<size_t>(std::numeric_limits<int>::max())) {
                return fail_media_write(L"appending highlight metadata to", -1);
            }
            avio_write(fmt_ctx->pb, highlight_box.data(),
                static_cast<int>(highlight_box.size()));
            avio_flush(fmt_ctx->pb);
            if (fmt_ctx->pb->error < 0)
                return fail_media_write(L"flushing highlight metadata for", fmt_ctx->pb->error);
        }

        if (timing) timing->close_begin_qpc = frame_pacing_trace::Qpc();
        ret = avio_closep(&fmt_ctx->pb);
        if (timing) timing->close_end_qpc = frame_pacing_trace::Qpc();
        if (ret < 0) {
            avformat_free_context(fmt_ctx);
            wchar_t message[512];
            _snwprintf_s(
                message,
                _TRUNCATE,
                L"Failed while closing the temporary clip (error %d). "
                L"The incomplete file was not published.",
                ret);
            SetEngineError(task.shared_memory, message);
            return false;
        }
        avformat_free_context(fmt_ctx);

        std::wcout << L"[MuxEncodedClip] Done: " << output_path
            << L" (" << video_packet_count << L" video, "
            << audio_packet_count << L" audio, "
            << actual_duration_s << L"s)" << std::endl;
        mux_diagnostic.success = true;
        return true;
    }


    // Encode and mux a legacy raw-frame snapshot.

    bool CaptureEngine::EncodeRawClip(
        const SaveClipTask& task, const std::wstring& output_path) {
        EncoderConfig enc_cfg;
        enc_cfg.src_width = task.src_width;
        enc_cfg.src_height = task.src_height;
        enc_cfg.enc_width = task.enc_width;
        enc_cfg.enc_height = task.enc_height;
        enc_cfg.fps = task.fps;
        enc_cfg.bitrate_kbps = task.bitrate_kbps;
        enc_cfg.preset = "superfast";
        enc_cfg.tune = nullptr;
        enc_cfg.scaling_mode = task.scaling_mode;

        VideoEncoder encoder;

        // Feed audio snapshot so it gets muxed alongside the video.
        // SetAudioData must be called before Initialize() so the audio stream
        // and its AAC extradata can be registered before avformat_write_header.
        if (task.has_audio && task.audio_snapshot.valid
            && !task.audio_snapshot.samples.empty())
        {
            // Use the tail of the snapshot matching the actual clip duration.
            // x264 frames have no per-frame QPC timestamps, so we take the last
            // (frame_count / fps) seconds of audio, aligned to the clip end.
            const uint32_t sr = task.audio_snapshot.sample_rate;
            const uint32_t ch = task.audio_snapshot.channels;
            const double   clip_duration_s =
                (task.fps > 0)
                ? static_cast<double>(task.frame_count) / static_cast<double>(task.fps)
                : static_cast<double>(task.duration_seconds);
            const int64_t total_frames =
                static_cast<int64_t>(task.audio_snapshot.samples.size())
                / static_cast<int64_t>(ch);
            const int64_t frames_needed =
                static_cast<int64_t>(clip_duration_s * static_cast<double>(sr) + 0.5);
            const int64_t start_frame =
                std::max<int64_t>(0, total_frames - frames_needed);
            const size_t  skip_samples =
                static_cast<size_t>(start_frame) * static_cast<size_t>(ch);

            std::vector<float> aligned_pcm(
                task.audio_snapshot.samples.begin() + static_cast<ptrdiff_t>(skip_samples),
                task.audio_snapshot.samples.end());

            if (!aligned_pcm.empty()) {
                encoder.SetAudioData(std::move(aligned_pcm), sr, ch, task.audio_bitrate_kbps);
                std::cout << "[EncodeRawClip] Audio: "
                          << frames_needed << " frames from snapshot (clip="
                          << clip_duration_s << "s)" << std::endl;
            }
        }

        if (!encoder.Initialize(output_path.c_str(), enc_cfg)) {
            std::cerr << "[EncodeRawClip] Encoder initialization failed" << std::endl;
            SetEngineError(task.shared_memory,
                L"Failed to initialise the video encoder. The selected codec "
                L"may be unavailable on this GPU.");
            return false;
        }

        bool encode_ok = true;
        for (size_t i = 0; i < task.frame_count; i++) {
            size_t slot_idx = (task.start_frame_idx + i) % max_frames_;
            if (!encoder.EncodeFrame(frame_pool_.GetSlot(slot_idx))) {
                encode_ok = false;
                break;
            }
        }

        const bool finalize_ok = encoder.Finalize();
        if (!encode_ok) {
            SetEngineError(task.shared_memory,
                L"The software encoder failed while writing video frames. "
                L"The incomplete clip was not published.");
            return false;
        }
        if (!finalize_ok) {
            SetEngineError(task.shared_memory,
                L"The software encoder could not finalize or close the clip. "
                L"The incomplete clip was not published.");
            return false;
        }
        std::wcout << L"[EncodeRawClip] Done: " << output_path << std::endl;
        return true;
    }



    CaptureEngine::Stats CaptureEngine::GetStats() const {
        Stats s{};
        s.frames_captured = frames_captured_.load(std::memory_order_relaxed);
        s.frames_dropped = frames_dropped_.load(std::memory_order_relaxed);
        s.capture_fps = static_cast<float>(fps_);

        if (nvenc_active_ && encoded_ring_) {
            s.ring_used_frames = encoded_ring_->GetCount();
            s.ring_max_frames = encoded_ring_->GetCapacity();
            s.pool_memory_mb = 0;  // no raw frame pool
            s.effective_buffer_seconds = (fps_ > 0)
                ? static_cast<float>(s.ring_used_frames) / static_cast<float>(fps_)
                : 0.0f;
        }
        else {
            s.ring_used_frames = ring_count_.load(std::memory_order_relaxed);
            s.ring_max_frames = max_frames_;
            s.pool_memory_mb = frame_pool_.TotalBytes() / (1024 * 1024);
            s.effective_buffer_seconds = (fps_ > 0)
                ? static_cast<float>(max_frames_) / static_cast<float>(fps_)
                : 0.0f;
        }

        return s;
    }

    void CaptureEngine::PublishContentMetrics(
        uint64_t sum, uint64_t sum_sq, uint32_t count) {
        if (count == 0) return;
        const float mean = static_cast<float>(sum) / count;
        const float variance = std::max(
            0.0f, static_cast<float>(sum_sq) / count - mean * mean);
        const bool suspicious_sample =
            (mean <= 8.0f && variance <= 6.0f) || variance <= 2.0f;
        const uint32_t streak = suspicious_sample
            ? content_suspicious_streak_.fetch_add(1) + 1
            : 0;
        if (!suspicious_sample) content_suspicious_streak_.store(0);
        content_luma_mean_.store(mean);
        content_luma_variance_.store(variance);
        content_sample_sequence_.fetch_add(1);
        if (streak >= 12)
            capture_health_flags_.fetch_or(CAPTURE_HEALTH_CONTENT_SUSPECT);
        else
            capture_health_flags_.fetch_and(~CAPTURE_HEALTH_CONTENT_SUSPECT);
    }

    void CaptureEngine::SampleContentBGRA(
        const uint8_t* data, uint32_t stride, uint32_t width,
        uint32_t height, uint64_t produced_frame) {
        if (!data || width == 0 || height == 0 || stride < width * 4 ||
            produced_frame % std::max<uint32_t>(1, fps_) != 0)
            return;
        constexpr uint32_t kColumns = 16;
        constexpr uint32_t kRows = 9;
        uint64_t sum = 0;
        uint64_t sum_sq = 0;
        for (uint32_t row = 0; row < kRows; ++row) {
            const uint32_t y = std::min(
                height - 1, ((2 * row + 1) * height) / (2 * kRows));
            for (uint32_t column = 0; column < kColumns; ++column) {
                const uint32_t x = std::min(
                    width - 1, ((2 * column + 1) * width) / (2 * kColumns));
                const uint8_t* pixel = data + static_cast<size_t>(y) * stride + x * 4;
                const uint32_t luma =
                    (19u * pixel[0] + 183u * pixel[1] + 54u * pixel[2]) >> 8;
                sum += luma;
                sum_sq += luma * luma;
            }
        }
        PublishContentMetrics(sum, sum_sq, kColumns * kRows);
    }

    void CaptureEngine::SampleContentTexture(
        ID3D11Texture2D* texture, uint64_t produced_frame) {
        content_sampler_.Tick(device_, context_, texture,
            produced_frame % std::max<uint32_t>(1, fps_) == 0, this,
            [](void* owner, const ContentSampleMetrics& metrics) {
                static_cast<CaptureEngine*>(owner)->PublishContentMetrics(
                    metrics.sum, metrics.sum_sq, metrics.count);
            });
    }

    void CaptureEngine::ClearReplayForRecovery(int64_t recovery_cutoff_qpc) {
        // Stop the writer before invalidating the ring generation.  This keeps
        // its final marker batch on the old AVIO while preventing any stale
        // packet from crossing into the next generation.
        std::shared_ptr<ContinuousRecordingWriter> recovery_writer;
        {
            std::unique_lock<std::mutex> lock(record_writer_mutex_);
            // A second recovery request must not take ownership from the
            // recovery that already owns the lifecycle boundary.
            if (recording_recovery_in_progress_) return;
            recording_recovery_in_progress_ = true;
            record_writer_cv_.notify_all();
            record_writer_cv_.wait(lock, [this] {
                return !recording_stop_in_progress_;
            });
            // Move the owner out before joining so a concurrent capture
            // callback cannot retain a writer that recovery is clearing.
            recovery_writer = std::move(record_writer_);
            if (recovery_writer)
                is_recording_.store(false, std::memory_order_release);
        }
        bool recovery_saved = true;
        std::string recovery_error;
        if (recovery_writer) {
            recovery_saved = recovery_writer->Stop();
            if (!recovery_saved) {
                recovery_error = recovery_writer->LastError();
                if (recovery_error.empty())
                    recovery_error =
                        "The recording contained no complete video fragment.";
            }
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            // Preserve an earlier unconsumed recovery result. A concurrent
            // Start is rejected while it is pending, and normal Stop consumes
            // it after this recovery has released the lifecycle boundary.
            if (!recovery_saved && !recovery_stop_result_valid_) {
                recovery_stop_result_valid_ = true;
                recovery_stop_result_ = false;
                recovery_stop_error_ = recovery_error;
            } else if (!recovery_saved) {
                recovery_stop_result_ = false;
                if (recovery_stop_error_.empty())
                    recovery_stop_error_ = recovery_error;
            }
            if (!recovery_saved)
                last_recording_error_ = recovery_error;
        }
        content_sampler_.Reset();
        if (recovery_cutoff_qpc <= 0) {
            LARGE_INTEGER now{};
            if (QueryPerformanceCounter(&now)) {
                recovery_cutoff_qpc = now.QuadPart;
            }
        }
        if (replay_pause_clock_) {
            LARGE_INTEGER frequency{};
            QueryPerformanceFrequency(&frequency);
            recovery_cutoff_qpc = replay_pause_clock_->MapQpc(
                recovery_cutoff_qpc, frequency.QuadPart);
        }
        if (encoded_ring_) encoded_ring_->Clear(recovery_cutoff_qpc);
        replay_config_publish_failed_.store(false);
        ring_head_.store(0, std::memory_order_release);
        ring_count_.store(0, std::memory_order_release);
        content_suspicious_streak_.store(0);
        capture_health_flags_.fetch_and(~CAPTURE_HEALTH_CONTENT_SUSPECT);
        {
            std::lock_guard<std::mutex> lock(record_writer_mutex_);
            // Keep recovery_in_progress_ true until Stop/Join and the ring
            // generation clear both complete. Every waiter is released only
            // after stale packets can no longer enter the old writer.
            recording_recovery_in_progress_ = false;
            record_writer_cv_.notify_all();
        }
    }

    bool CaptureEngine::WaitForDxgiRecoveryBackoff(
        uint32_t delay_ms) const {
        uint32_t remaining = delay_ms;
        while (remaining > 0) {
            if (!running_.load(std::memory_order_relaxed)) return false;
            const uint32_t slice = std::min<uint32_t>(remaining, 10);
            std::this_thread::sleep_for(std::chrono::milliseconds(slice));
            remaining -= slice;
        }
        return running_.load(std::memory_order_relaxed);
    }

    dxgi::RecoveryAttemptResult CaptureEngine::TryRecreateDxgiDuplication(
        uint32_t attempt, std::string& detail) {
        if (!running_.load(std::memory_order_relaxed)) {
            detail = "shutdown requested";
            return dxgi::RecoveryAttemptResult::StopRequested;
        }
        if (!device_ || !capture_device_adapter_luid_available_) {
            detail = "live capture device identity is unavailable";
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        const HRESULT removed_reason = device_->GetDeviceRemovedReason();
        if (removed_reason != S_OK) {
            detail = diagnostics::FormatHResultFailure(
                "ID3D11Device::GetDeviceRemovedReason", removed_reason);
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        const auto current_monitor = monitor_resolver_.Resolve(
            monitor_device_path_);
        if (!current_monitor.ok()) {
            detail = std::string(monitor::ToString(current_monitor.error))
                + ": " + current_monitor.diagnostic;
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        IDXGIFactory1* factory = nullptr;
        IDXGIAdapter1* adapter = nullptr;
        IDXGIOutput* output = nullptr;
        IDXGIOutput1* output1 = nullptr;
        IDXGIOutputDuplication* candidate = nullptr;
        const auto cleanup = [&] {
            if (candidate) { candidate->Release(); candidate = nullptr; }
            if (output1) { output1->Release(); output1 = nullptr; }
            if (output) { output->Release(); output = nullptr; }
            if (adapter) { adapter->Release(); adapter = nullptr; }
            if (factory) { factory->Release(); factory = nullptr; }
        };

        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory));
        if (FAILED(hr) || !factory) {
            detail = diagnostics::FormatHResultFailure(
                "CreateDXGIFactory1(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        monitor::DxgiOutputIdentity output_identity;
        std::string output_diagnostic;
        if (!monitor::OpenSelectedDxgiOutput(
                factory, current_monitor.monitor, &adapter, &output,
                &output_identity, output_diagnostic)) {
            detail = output_diagnostic.empty()
                ? "selected monitor output is temporarily unavailable"
                : output_diagnostic;
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        DXGI_OUTPUT_DESC output_desc{};
        hr = output->GetDesc(&output_desc);
        if (FAILED(hr)) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput::GetDesc(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        if (!IsSupportedDxgiOutputRotation(
                static_cast<uint32_t>(output_desc.Rotation))) {
            std::ostringstream rotation_detail;
            rotation_detail << "recovery selected output rotation "
                            << static_cast<uint32_t>(output_desc.Rotation)
                            << " is unsupported by the capture geometry contract";
            detail = rotation_detail.str();
            capture_health_flags_.fetch_or(
                CAPTURE_HEALTH_RECONFIGURE_REQUIRED,
                std::memory_order_acq_rel);
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }
        const uint32_t current_width = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.right
            - output_desc.DesktopCoordinates.left);
        const uint32_t current_height = static_cast<uint32_t>(
            output_desc.DesktopCoordinates.bottom
            - output_desc.DesktopCoordinates.top);

        const dxgi::RecoveryIdentity active_identity{
            monitor_device_path_, capture_device_adapter_luid_, width_, height_};
        const dxgi::RecoveryObservation observation{
            running_.load(std::memory_order_relaxed),
            true,
            {current_monitor.monitor.monitor_device_path,
             current_monitor.monitor.adapter_luid,
             current_width,
             current_height},
            removed_reason};
        const auto decision = dxgi::EvaluateRecovery(
            active_identity, observation);
        std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                  << "\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_dimensions_observed\","
                  << "\"attempt\":" << attempt << ','
                  << "\"decision\":\""
                  << dxgi::RecoveryDecisionName(decision) << "\","
                  << "\"active_width\":" << active_identity.width << ','
                  << "\"active_height\":" << active_identity.height << ','
                  << "\"current_width\":" << current_width << ','
                  << "\"current_height\":" << current_height << "}"
                  << std::endl;
        if (decision == dxgi::RecoveryDecision::DimensionsChanged)
            capture_health_flags_.fetch_or(CAPTURE_HEALTH_RECONFIGURE_REQUIRED);
        if (decision != dxgi::RecoveryDecision::RecreateDuplication) {
            detail = std::string("recovery contract rejected: ")
                + dxgi::RecoveryDecisionName(decision);
            cleanup();
            return dxgi::RecoveryAttemptForDecision(decision);
        }

        DXGI_ADAPTER_DESC1 adapter_desc{};
        hr = adapter->GetDesc1(&adapter_desc);
        if (FAILED(hr)) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::GetDesc1(recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        const monitor::AdapterLuid reopened_luid{
            adapter_desc.AdapterLuid.LowPart,
            adapter_desc.AdapterLuid.HighPart};
        if (reopened_luid != capture_device_adapter_luid_) {
            detail = "reopened output adapter LUID differs from capture device";
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }

        hr = output->QueryInterface(
            __uuidof(IDXGIOutput1), reinterpret_cast<void**>(&output1));
        if (FAILED(hr) || !output1) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput::QueryInterface(IDXGIOutput1,recovery)", hr);
            cleanup();
            return dxgi::RecoveryAttemptResult::RetryableFailure;
        }

        if (QueryHdrDisplay(current_monitor.monitor, adapter).value_or(false)) {
            detail = "display switched to HDR; restart with WGC scRGB capture";
            capture_health_flags_.fetch_or(CAPTURE_HEALTH_RECONFIGURE_REQUIRED);
            cleanup();
            return dxgi::RecoveryAttemptResult::FatalFailure;
        }
        hr = output1->DuplicateOutput(device_, &candidate);
        if (FAILED(hr) || !candidate) {
            detail = diagnostics::FormatHResultFailure(
                "IDXGIOutput1::DuplicateOutput(recovery)", hr);
            const bool fatal = dxgi::IsFatalDuplicationRecreateFailure(hr);
            cleanup();
            return fatal
                ? dxgi::RecoveryAttemptResult::FatalFailure
                : dxgi::RecoveryAttemptResult::RetryableFailure;
        }
        if (!running_.load(std::memory_order_relaxed)) {
            detail = "shutdown requested after duplication recreation";
            cleanup();
            return dxgi::RecoveryAttemptResult::StopRequested;
        }

        duplication_ = candidate;
        candidate = nullptr;
        {
            std::lock_guard<std::mutex> lock(dxgi_context_mutex_);
            resolved_monitor_ = current_monitor.monitor;
            resolved_dxgi_output_ = output_identity;
            resolved_desktop_coordinates_ = output_desc.DesktopCoordinates;
            resolved_desktop_coordinates_available_ = true;
        }
        detail = "duplication recreated on attempt " + std::to_string(attempt);
        cleanup();
        return dxgi::RecoveryAttemptResult::Recovered;
    }

    bool CaptureEngine::RecoverDxgiDuplication(
        const char* api_call, HRESULT trigger) {
        // Recovery is a generation boundary: the cached texture and all
        // completed-call evidence belong to the old duplication object.
        // The scaler retains no source frame. Its scratch and staging can be
        // reused only because recreation requires the same live D3D device and geometry.
        dxgi_cursor_composer_.InvalidateGeneration();
        InvalidateDxgiReplayCache();
        last_dxgi_completed_qpc_.store(0, std::memory_order_release);
        last_dxgi_pointer_only_completed_qpc_.store(
            0, std::memory_order_release);
        // Mark the boundary before clearing the ring. A delayed encoder
        // callback must not be mistaken for current history while recovery is
        // in progress; ClearReplayForRecovery also records this QPC cutoff.
        capture_health_flags_.store(CAPTURE_HEALTH_RECOVERING);
        LARGE_INTEGER recovery_start_qpc{};
        QueryPerformanceCounter(&recovery_start_qpc);
        EmitRecentDxgiEvidence("dxgi_recovery_started");
        // A duplication recovery starts a new replay history. Clear before
        // releasing/recreating DXGI so a save racing this boundary can never
        // publish packets from two device/output generations.
        ClearReplayForRecovery(recovery_start_qpc.QuadPart);
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_started\","
                  << "\"state\":\"RECOVERING\","
                  << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                  << "\"native_failure\":"
                  << diagnostics::HResultFailureJson(
                         api_call, trigger)
                  << ",\"monitor_id\":\""
                  << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                         monitor_device_path_)) << "\","
                  << "\"capture_device_luid\":"
                  << (capture_device_adapter_luid_available_
                      ? AdapterLuidJson(capture_device_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << "}"
                  << std::endl;

        // Any outstanding frame has already been released by the caller. The
        // invalid interface must be released before DuplicateOutput is tried.
        if (duplication_) {
            duplication_->Release();
            duplication_ = nullptr;
        }

        std::string last_detail;
        const auto result = dxgi::RunBoundedRecovery(
            [this] {
                return running_.load(std::memory_order_relaxed);
            },
            [this](uint32_t delay_ms) {
                return WaitForDxgiRecoveryBackoff(delay_ms);
            },
            [this, &last_detail](uint32_t attempt) {
                const auto attempt_result = TryRecreateDxgiDuplication(
                    attempt, last_detail);
                if (attempt_result != dxgi::RecoveryAttemptResult::Recovered) {
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                              << "\"subsystem\":\"capture\","
                              << "\"event\":\"recovery_attempt_failed\","
                              << "\"attempt\":" << attempt << ','
                              << "\"detail\":\""
                              << diagnostics::JsonEscape(last_detail)
                              << "\"}" << std::endl;
                }
                return attempt_result;
            });
        capture_recovery_attempts_.fetch_add(
            result.attempts, std::memory_order_relaxed);

        if (result.outcome == dxgi::RecoveryOutcome::Recovered) {
            const uint32_t generation = capture_generation_.fetch_add(
                1, std::memory_order_acq_rel) + 1;
            LARGE_INTEGER generation_start_qpc{};
            if (QueryPerformanceCounter(&generation_start_qpc)) {
                generation_start_qpc_.store(generation_start_qpc.QuadPart,
                    std::memory_order_release);
            }
            last_source_frame_qpc_.store(0, std::memory_order_release);
            last_encoder_submission_qpc_.store(0, std::memory_order_release);
            last_encoded_output_qpc_.store(0, std::memory_order_release);
            last_ring_insertion_qpc_.store(0, std::memory_order_release);
            const uint32_t restart = capture_restart_count_.fetch_add(
                1, std::memory_order_relaxed) + 1;
            last_capture_hresult_.store(S_OK, std::memory_order_relaxed);
            capture_health_flags_.store(CAPTURE_HEALTH_ACTIVE);
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"recovery_completed\","
                      << "\"state\":\"ACTIVE\","
                      << "\"attempts\":" << result.attempts << ','
                      << "\"capture_restart_count\":" << restart << ','
                      << "\"encoder_restart_count\":0,"
                      << "\"stream_generation_preserved\":false,"
                      << "\"replay_ring_reset\":true,"
                      << "\"capture_generation\":" << generation << ','
                      << "\"monitor_id\":\""
                      << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                             monitor_device_path_)) << "\","
                      << "\"dxgi_output\":{\"index\":"
                      << resolved_dxgi_output_.output_index << "},"
                      << "\"capture_device_luid\":"
                      << AdapterLuidJson(capture_device_adapter_luid_) << "}"
                      << std::endl;
            return true;
        }

        if (result.outcome == dxgi::RecoveryOutcome::StopRequested) {
            return false;
        }

        capture_recovery_failures_.fetch_add(1, std::memory_order_relaxed);
        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
            | (capture_health_flags_.load() & CAPTURE_HEALTH_RECONFIGURE_REQUIRED));
        SetCaptureFailure(last_detail.empty()
            ? "DXGI duplication recovery failed without backend detail"
            : last_detail);
        std::cerr << "[CaptureThread] DXGI recovery failed: "
                  << dxgi::RecoveryOutcomeName(result.outcome)
                  << " attempts=" << result.attempts
                  << " detail=" << last_capture_failure_detail_ << std::endl;
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"recovery_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                  << "\"outcome\":\""
                  << dxgi::RecoveryOutcomeName(result.outcome) << "\","
                  << "\"attempts\":" << result.attempts << ','
                  << "\"detail\":\""
                  << diagnostics::JsonEscape(last_capture_failure_detail_)
                  << "\",\"native_failure\":"
                  << diagnostics::HResultFailureJson(
                         api_call, trigger)
                  << "}" << std::endl;
        return false;
    }

    bool CaptureEngine::ValidateCaptureTexture(
        ID3D11Texture2D* texture, const char* backend_name) {
        if (!texture) {
            SetCaptureFailure("capture backend returned a null D3D11 texture");
            return false;
        }
        D3D11_TEXTURE2D_DESC description{};
        texture->GetDesc(&description);
        const dxgi::TextureContract expected{
            width_, height_,
            static_cast<uint32_t>(source_hdr_ ? DXGI_FORMAT_R16G16B16A16_FLOAT
                                              : DXGI_FORMAT_B8G8R8A8_UNORM), 1};
        const dxgi::TextureContract actual{
            description.Width, description.Height,
            static_cast<uint32_t>(description.Format),
            description.SampleDesc.Count};
        const bool valid = dxgi::IsCompatibleTexture(expected, actual);
        if (valid) return true;
        if (description.Width != width_ || description.Height != height_) {
            // Drivers can deliver a resized surface without ACCESS_LOST or
            // ContentSize changing first. The old encoder cannot accept it.
            capture_health_flags_.fetch_or(CAPTURE_HEALTH_RECONFIGURE_REQUIRED);
        }

        std::ostringstream failure;
        failure << "capture texture contract mismatch expected="
                << width_ << 'x' << height_
                << (source_hdr_ ? "/scRGB/sample1 actual=" : "/BGRA8/sample1 actual=")
                << description.Width << 'x' << description.Height
                << "/format" << static_cast<uint32_t>(description.Format)
                << "/sample" << description.SampleDesc.Count;
        SetCaptureFailure(failure.str());
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"texture_contract_rejected\","
                  << "\"state\":\"FAILED\",\"error_code\":\""
                  << (backend_name && std::strcmp(backend_name, "WGC") == 0
                      ? "CAPTURE_WGC_RUNTIME_FAILED"
                      : "CAPTURE_DXGI_RUNTIME_FAILED") << "\","
                  << "\"backend\":\""
                  << diagnostics::JsonEscape(
                         backend_name ? backend_name : "unknown") << "\","
                  << "\"expected_width\":" << width_ << ','
                  << "\"expected_height\":" << height_ << ','
                  << "\"actual_width\":" << description.Width << ','
                  << "\"actual_height\":" << description.Height << ','
                  << "\"actual_format\":"
                  << static_cast<uint32_t>(description.Format) << ','
                  << "\"actual_sample_count\":"
                  << description.SampleDesc.Count << "}" << std::endl;
        return false;
    }

    bool CaptureEngine::ValidateMappedCaptureRowPitch(
        uint32_t expected_width, uint32_t row_pitch, const char* backend_name) {
        if (dxgi::IsValidBgraRowPitch(expected_width, row_pitch)) return true;
        std::ostringstream failure;
        failure << "capture row pitch is smaller than BGRA frame width: "
                << row_pitch << " < " << (static_cast<uint64_t>(expected_width) * 4u);
        SetCaptureFailure(failure.str());
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"texture_contract_rejected\","
                  << "\"state\":\"FAILED\",\"error_code\":\""
                  << (backend_name && std::strcmp(backend_name, "WGC") == 0
                      ? "CAPTURE_WGC_RUNTIME_FAILED"
                      : "CAPTURE_DXGI_RUNTIME_FAILED") << "\","
                  << "\"backend\":\""
                  << diagnostics::JsonEscape(
                         backend_name ? backend_name : "unknown") << "\","
                  << "\"expected_minimum_row_pitch\":"
                  << (static_cast<uint64_t>(expected_width) * 4u) << ','
                  << "\"actual_row_pitch\":" << row_pitch << "}"
                  << std::endl;
        return false;
    }


    // Capture DXGI frames at the QPC-driven cadence and submit to the replay
    // encoder. Its callback publishes packets to EncodedRingBuffer; only the
    // legacy raw path updates frame_pool_ and its indices.

    void CaptureEngine::CaptureThread() {
        // WGC disabled — DXGI-only path. No WinRT apartment needed.

        std::cout << "[CaptureThread] Started (DXGI path)." << std::endl;

        LARGE_INTEGER qpc_freq{};
        if (!QueryPerformanceFrequency(&qpc_freq)
            || qpc_freq.QuadPart <= 0) {
            SetCaptureFailure("QueryPerformanceFrequency failed for DXGI cadence");
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
            running_.store(false);
            InvalidateDxgiReplayCache();
            return;
        }
        dxgi_replay_cadence_.Configure(fps_, qpc_freq.QuadPart);
        const int64_t processing_period_qpc = std::max<int64_t>(
            1, qpc_freq.QuadPart / std::max<uint32_t>(1, fps_));
        int64_t processing_budget_qpc = 0;
        // Keep a decaying recent high-water mark of post-acquisition work.
        // Timeouts/pointer-only polls do not erase the real processing cost;
        // a single driver stall is bounded to one period and ages out.
        struct ProcessingBudgetSample {
            int64_t& budget;
            int64_t start, period, frequency;
            std::atomic<uint64_t>& diagnostic;
            ~ProcessingBudgetSample() {
                LARGE_INTEGER end{};
                if (start <= 0 || !QueryPerformanceCounter(&end)
                    || end.QuadPart < start) return;
                const int64_t elapsed = std::min(period, end.QuadPart - start);
                budget = std::max(elapsed, budget - budget / 8);
                diagnostic.store(static_cast<uint64_t>(
                    static_cast<long double>(budget) * 1000000.0L / frequency),
                    std::memory_order_relaxed);
            }
        };
        uint32_t previous_generation = capture_generation_.load(
            std::memory_order_relaxed);
        dxgi_replay_cadence_.SetGeneration(previous_generation);

        // Keep timer precision local to this paced DXGI thread. Avoid the
        // process-wide timeBeginPeriod side effect, which would also affect
        // unrelated capture/audio workers. Resolve CreateWaitableTimerExW at
        // runtime so older SDK declarations still build; the normal timer and
        // sleep paths remain safe fallbacks when high-resolution timers are
        // unavailable on the host.
        using CreateWaitableTimerExFn = HANDLE (WINAPI*)(
            LPSECURITY_ATTRIBUTES, LPCWSTR, DWORD, DWORD);
        HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
        auto create_waitable_timer_ex = kernel32
            ? reinterpret_cast<CreateWaitableTimerExFn>(GetProcAddress(
                  kernel32, "CreateWaitableTimerExW"))
            : nullptr;
        DWORD high_resolution_create_error = ERROR_SUCCESS;
        if (!kernel32) {
            high_resolution_create_error = GetLastError();
        } else if (!create_waitable_timer_ex) {
            high_resolution_create_error = ERROR_PROC_NOT_FOUND;
        }
        HANDLE cadence_timer = create_waitable_timer_ex
            ? (SetLastError(ERROR_SUCCESS), create_waitable_timer_ex(
                  nullptr, nullptr, 0x00000002u, TIMER_ALL_ACCESS))
            : nullptr;
        if (create_waitable_timer_ex && !cadence_timer) {
            high_resolution_create_error = GetLastError();
        }
        DWORD ordinary_create_error = ERROR_SUCCESS;
        const char* cadence_timer_mode = cadence_timer
            ? "high_resolution" : "sleep";
        if (!cadence_timer) {
            SetLastError(ERROR_SUCCESS);
            cadence_timer = CreateWaitableTimerW(nullptr, FALSE, nullptr);
            if (cadence_timer) {
                cadence_timer_mode = "ordinary";
            } else {
                ordinary_create_error = GetLastError();
            }
        }
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"dxgi_cadence_timer_initialized\","
                  << "\"timer_mode\":\"" << cadence_timer_mode << "\","
                  << "\"high_resolution_create_error\":"
                  << high_resolution_create_error << ','
                  << "\"ordinary_create_error\":"
                  << ordinary_create_error << "}" << std::endl;

        std::cout << "[CaptureThread] " << fps_ << " fps ("
            << (1000.0 / static_cast<double>(std::max<uint32_t>(1, fps_)))
            << " ms/frame, sampled DXGI acquisition)  "
            << (nvenc_active_ ? GetActiveEncoderName() : "software")
            << " path" << std::endl;

        uint32_t consecutive_acquire_errors = 0;
        int64_t previous_present_qpc = 0;
        if (replay_encoder_) {
            replay_encoder_->SetCaptureGeneration(previous_generation);
        }
        IDXGIResource* previous_resource_identity = nullptr;
        uint64_t next_resource_token = 0;
        auto next_color_check = std::chrono::steady_clock::now();

        while (running_.load(std::memory_order_relaxed)) {
            const auto now = std::chrono::steady_clock::now();
            if (now >= next_color_check) {
                next_color_check = now + std::chrono::seconds(1);
                if (QueryHdrDisplay(resolved_monitor_).value_or(false)) {
                    SetCaptureFailure("Display switched to HDR; restart with WGC scRGB capture.");
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                        | CAPTURE_HEALTH_RECONFIGURE_REQUIRED);
                    ClearReplayForRecovery();
                    running_.store(false);
                    break;
                }
            }

            const uint32_t generation = capture_generation_.load(
                std::memory_order_relaxed);
            if (generation != previous_generation) {
                // A recovery starts a fresh history. Do not report a
                // presentation gap across the old and new duplication.
                dxgi_cursor_composer_.InvalidateGeneration();
                InvalidateDxgiReplayCache();
                dxgi_replay_cadence_.SetGeneration(generation);
                previous_generation = generation;
                processing_budget_qpc = 0;
                dxgi_processing_budget_us_.store(0, std::memory_order_relaxed);
                previous_present_qpc = 0;
                previous_resource_identity = nullptr;
                if (replay_encoder_) {
                    replay_encoder_->SetCaptureGeneration(generation);
                }
            }

            LARGE_INTEGER cadence_now{};
            if (!QueryPerformanceCounter(&cadence_now)) {
                SetCaptureFailure("QueryPerformanceCounter failed for DXGI cadence");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                InvalidateDxgiReplayCache();
                break;
            }
            const auto schedule = dxgi_replay_cadence_.Schedule(
                cadence_now.QuadPart);
            if (!schedule.due) {
                if (schedule.wait_qpc > 0) {
                    // Every blocking slice is bounded so shutdown and QPC
                    // deadlines are rechecked at least every 50 ms. A timer
                    // timeout merely returns to Schedule; it never authorizes
                    // an acquire or a held presentation by itself.
                    const int64_t max_wait_slice_qpc = std::max<int64_t>(
                        1, qpc_freq.QuadPart / 20);
                    const int64_t wait_slice_qpc = std::min<int64_t>(
                        schedule.wait_qpc, max_wait_slice_qpc);
                    if (cadence_timer) {
                        const long double wait_100ns = std::ceil(
                            static_cast<long double>(wait_slice_qpc)
                            * 10000000.0L
                            / static_cast<long double>(qpc_freq.QuadPart));
                        if (wait_100ns > 0.0L
                            && wait_100ns
                                <= static_cast<long double>(
                                    (std::numeric_limits<int64_t>::max)())) {
                            LARGE_INTEGER due_time{};
                            // Also cap the relative Win32 timer to 50 ms; the
                            // QPC slice cap above keeps the scheduler interval
                            // bounded even when timer conversion rounds up.
                            const int64_t bounded_wait_100ns =
                                std::min<int64_t>(500000, std::max<int64_t>(
                                    1, static_cast<int64_t>(wait_100ns)));
                            due_time.QuadPart = -std::max<int64_t>(
                                1, bounded_wait_100ns);
                            if (SetWaitableTimer(
                                    cadence_timer, &due_time, 0, nullptr,
                                    nullptr, FALSE)) {
                                const DWORD wait_result = WaitForSingleObject(
                                    cadence_timer, 50);
                                if (wait_result == WAIT_OBJECT_0
                                    || wait_result == WAIT_TIMEOUT) {
                                    continue;
                                }
                                const DWORD wait_error = wait_result == WAIT_FAILED
                                    ? GetLastError() : ERROR_GEN_FAILURE;
                                std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                                          << "\"subsystem\":\"capture\","
                                          << "\"event\":\"dxgi_cadence_timer_disabled\","
                                          << "\"timer_mode\":\""
                                          << cadence_timer_mode << "\","
                                          << "\"win32_error\":"
                                          << wait_error << "}" << std::endl;
                            } else {
                                const DWORD timer_error = GetLastError();
                                std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                                          << "\"subsystem\":\"capture\","
                                          << "\"event\":\"dxgi_cadence_timer_disabled\","
                                          << "\"timer_mode\":\""
                                          << cadence_timer_mode << "\","
                                          << "\"win32_error\":"
                                          << timer_error << "}" << std::endl;
                            }
                        }
                    }
                    if (cadence_timer) {
                        // A set/wait error is reported once, then the timer is
                        // retired so the bounded sleep fallback cannot spam.
                        // (Successful signals and timeouts returned above.)
                        CancelWaitableTimer(cadence_timer);
                        CloseHandle(cadence_timer);
                        cadence_timer = nullptr;
                    }
                    if (!cadence_timer) {
                        const double wait_seconds =
                            static_cast<double>(wait_slice_qpc)
                            / static_cast<double>(qpc_freq.QuadPart);
                        std::this_thread::sleep_for(
                            std::min(std::chrono::duration<double>(wait_seconds),
                                std::chrono::duration<double>(0.050)));
                    }
                }
                continue;
            }
            dxgi_skipped_capture_slots_.fetch_add(
                schedule.skipped_slots, std::memory_order_relaxed);
            capture_loop_iterations_.fetch_add(1, std::memory_order_relaxed);

            DXGI_OUTDUPL_FRAME_INFO info{};
            IDXGIResource* resource = nullptr;

            capture_thread_stage_.store(1, std::memory_order_relaxed);
            capture_acquire_attempts_.fetch_add(1, std::memory_order_relaxed);
            if (!duplication_) {
                SetCaptureFailure(
                    "DXGI duplication is unavailable outside recovery");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }
            LARGE_INTEGER acquire_start_qpc{};
            QueryPerformanceCounter(&acquire_start_qpc);
            // A zero-timeout poll can miss a desktop update arriving just after
            // this tick. Wait only inside the remaining deadline budget, with
            // processing headroom and a small cancellation-latency ceiling.
            const uint32_t acquire_wait_ms = dxgi_replay_cadence_.AcquireTimeoutMs(
                schedule.tick, acquire_start_qpc.QuadPart, processing_budget_qpc);
            dxgi_acquire_wait_ms_.store(acquire_wait_ms, std::memory_order_relaxed);
            HRESULT hr = duplication_->AcquireNextFrame(
                acquire_wait_ms, &info, &resource);
            LARGE_INTEGER acquire_end_qpc{};
            QueryPerformanceCounter(&acquire_end_qpc);
            last_capture_hresult_.store(
                static_cast<int32_t>(hr), std::memory_order_relaxed);

            const int64_t present_qpc = info.LastPresentTime.QuadPart;
            int64_t presentation_gap_qpc = 0;
            if (present_qpc > 0 && previous_present_qpc > 0) {
                presentation_gap_qpc = present_qpc - previous_present_qpc;
            }
            if (present_qpc > previous_present_qpc) {
                previous_present_qpc = present_qpc;
            }
            dxgi::RecentDxgiEvent recent_event{};
            recent_event.acquire_start_qpc = acquire_start_qpc.QuadPart;
            recent_event.acquire_end_qpc = acquire_end_qpc.QuadPart;
            recent_event.last_present_qpc = present_qpc;
            recent_event.presentation_gap_qpc = presentation_gap_qpc;
            recent_event.accumulated_frames = info.AccumulatedFrames;
            if (info.AccumulatedFrames > 1) {
                dxgi_coalesced_extra_frames_.fetch_add(
                    static_cast<uint64_t>(info.AccumulatedFrames) - 1,
                    std::memory_order_relaxed);
            }
            recent_event.acquire_hresult = static_cast<int32_t>(hr);
            if (resource && resource != previous_resource_identity) {
                previous_resource_identity = resource;
                ++next_resource_token;
            }
            recent_event.resource_token = resource ? next_resource_token : 0;
            recent_event.generation = generation;
            recent_event.width = width_;
            recent_event.height = height_;
            recent_event.format = static_cast<uint32_t>(
                DXGI_FORMAT_B8G8R8A8_UNORM);
            recent_event.output_index = resolved_dxgi_output_.output_index;
            recent_event.acquired = SUCCEEDED(hr);
            recent_event.timed_out = hr == DXGI_ERROR_WAIT_TIMEOUT;
            // A successful acquire is recorded once, after ReleaseFrame has
            // completed, so one event contains the complete ownership
            // boundary. Timeouts/errors have no ReleaseFrame ownership and
            // are recorded immediately.
            if (FAILED(hr)) {
                dxgi_recent_evidence_.Record(recent_event);
            }

            dxgi_replay::Presentation presentation;
            int64_t presentation_qpc = 0;
            ID3D11Texture2D* source_texture = nullptr;
            std::string cursor_detail;
            if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
                capture_timeouts_.fetch_add(1, std::memory_order_relaxed);
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                result.call_end_qpc = acquire_end_qpc.QuadPart;
                result.observation = dxgi_replay::Observation::WaitTimeout;
                presentation = dxgi_replay_cadence_.Complete(
                    schedule.tick, result);
                dxgi_replay_cache_armed_.store(
                    dxgi_replay_cadence_.HasSeed(),
                    std::memory_order_release);
                last_dxgi_completed_qpc_.store(
                    acquire_end_qpc.QuadPart, std::memory_order_release);
                if (presentation.present) {
                    source_texture = dxgi_replay_texture_;
                    presentation_qpc = presentation.qpc;
                } else if (!dxgi_replay_cadence_.HasSeed()) {
                    InvalidateDxgiReplayCache();
                }
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!presentation.present) continue;
            }

            // WAIT_TIMEOUT can still authorize presenting the already-owned
            // texture, but it did not acquire a frame/resource. Keep every
            // successful-acquire path and its FrameLease entirely separate so
            // a held timeout never reaches failed-HRESULT recovery handling or
            // attempts ReleaseFrame on a frame it does not own.
            if (hr != DXGI_ERROR_WAIT_TIMEOUT) {
            if (hr == DXGI_ERROR_ACCESS_LOST) {
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                result.call_end_qpc = acquire_end_qpc.QuadPart;
                result.observation = dxgi_replay::Observation::Failure;
                dxgi_replay_cadence_.Complete(schedule.tick, result);
                InvalidateDxgiReplayCache();
                last_dxgi_completed_qpc_.store(0, std::memory_order_release);
                last_dxgi_pointer_only_completed_qpc_.store(
                    0, std::memory_order_release);
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (RecoverDxgiDuplication(
                        "IDXGIOutputDuplication::AcquireNextFrame", hr)) {
                    consecutive_acquire_errors = 0;
                    continue;
                }
                running_.store(false);
                break;
            }

            if (FAILED(hr)) {
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                result.call_end_qpc = acquire_end_qpc.QuadPart;
                result.observation = dxgi_replay::Observation::Failure;
                dxgi_replay_cadence_.Complete(schedule.tick, result);
                InvalidateDxgiReplayCache();
                last_dxgi_completed_qpc_.store(0, std::memory_order_release);
                last_dxgi_pointer_only_completed_qpc_.store(
                    0, std::memory_order_release);
                std::cerr << "[CaptureThread] AcquireNextFrame failed: 0x"
                    << std::hex << hr << std::dec << std::endl;
                if (++consecutive_acquire_errors >= 100) {
                    std::cerr << "[CaptureThread] Too many consecutive acquisition errors"
                              << std::endl;
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     "IDXGIOutputDuplication::AcquireNextFrame", hr)
                              << ",\"consecutive_failures\":"
                              << consecutive_acquire_errors << "}"
                              << std::endl;
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                continue;
            }
            consecutive_acquire_errors = 0;
            capture_acquire_successes_.fetch_add(1, std::memory_order_relaxed);
            capture_thread_stage_.store(2, std::memory_order_relaxed);
            dxgi::FrameLease<IDXGIResource, IDXGIOutputDuplication> frame(
                resource, duplication_);
            int64_t release_completion_qpc = 0;
            const auto fail_scheduled_acquire = [&]() {
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                LARGE_INTEGER failed_completion{};
                QueryPerformanceCounter(&failed_completion);
                result.call_end_qpc = failed_completion.QuadPart;
                result.observation = dxgi_replay::Observation::Failure;
                dxgi_replay_cadence_.Complete(schedule.tick, result);
                InvalidateDxgiReplayCache();
                last_dxgi_completed_qpc_.store(0, std::memory_order_release);
                last_dxgi_pointer_only_completed_qpc_.store(
                    0, std::memory_order_release);
            };
            const auto finish_frame = [this, &frame, &recent_event]() {
                frame.ReleaseResource();
                const bool owned = frame.owns_frame();
                const HRESULT release = frame.ReleaseFrame();
                recent_event.release_attempted = true;
                recent_event.release_hresult = static_cast<int32_t>(release);
                dxgi_recent_evidence_.Record(recent_event);
                if (owned) {
                    capture_frames_released_.fetch_add(
                        1, std::memory_order_relaxed);
                }
                if (release == DXGI_ERROR_ACCESS_LOST) {
                    InvalidateDxgiReplayCache();
                    last_dxgi_completed_qpc_.store(
                        0, std::memory_order_release);
                    last_dxgi_pointer_only_completed_qpc_.store(
                        0, std::memory_order_release);
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    return RecoverDxgiDuplication(
                        "IDXGIOutputDuplication::ReleaseFrame", release);
                }
                if (FAILED(release)) {
                    InvalidateDxgiReplayCache();
                    last_dxgi_completed_qpc_.store(
                        0, std::memory_order_release);
                    last_dxgi_pointer_only_completed_qpc_.store(
                        0, std::memory_order_release);
                    SetCaptureFailure(diagnostics::FormatHResultFailure(
                        "IDXGIOutputDuplication::ReleaseFrame", release));
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {"
                              << "\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_DXGI_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     "IDXGIOutputDuplication::ReleaseFrame",
                                     release) << "}" << std::endl;
                    ClearReplayForRecovery();
                    capture_health_flags_.store(
                        CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    return false;
                }
                return true;
            };

            // Pointer position/shape metadata is valid only while the
            // duplication frame is owned.  Observe it before every release,
            // including pointer-only frames, so the later GPU composition can
            // use a clean replay background without losing a cursor update.
            if (capture_cursor_ && !dxgi_cursor_composer_.ObserveFrame(
                    duplication_, info, cursor_detail)) {
                SetCaptureFailure(
                    cursor_detail.empty()
                        ? "DXGI cursor metadata could not be processed"
                        : cursor_detail);
                std::cerr << "[CaptureThread] "
                          << last_capture_failure_detail_ << std::endl;
                const bool released = finish_frame();
                if (!released) break;
                fail_scheduled_acquire();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }

            if (!resource) {
                SetCaptureFailure(
                    "AcquireNextFrame succeeded without a desktop resource");
                const bool released = finish_frame();
                if (released && capture_generation_.load(
                        std::memory_order_acquire) != generation) {
                    dxgi_cursor_composer_.InvalidateGeneration();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
                fail_scheduled_acquire();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }

            // Pointer-only updates carry no newly presented desktop image.
            // Encoding the returned old surface as a new frame creates a false
            // freeze and introduces a zero presentation timestamp.
            if (!dxgi::HasNewDesktopImage(info.LastPresentTime.QuadPart)) {
                recent_event.pointer_only = true;
                pointer_only_frames_.fetch_add(1, std::memory_order_relaxed);
                frames_dropped_.fetch_add(1, std::memory_order_relaxed);
                const bool released = finish_frame();
                if (!released) {
                    fail_scheduled_acquire();
                    break;
                }
                if (capture_generation_.load(std::memory_order_acquire)
                    != generation) {
                    dxgi_cursor_composer_.InvalidateGeneration();
                    InvalidateDxgiReplayCache();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
                LARGE_INTEGER completed{};
                QueryPerformanceCounter(&completed);
                release_completion_qpc = completed.QuadPart;
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                result.call_end_qpc = release_completion_qpc;
                result.observation = dxgi_replay::Observation::PointerOnly;
                result.release_succeeded = true;
                presentation = dxgi_replay_cadence_.Complete(
                    schedule.tick, result);
                dxgi_replay_cache_armed_.store(
                    dxgi_replay_cadence_.HasSeed(),
                    std::memory_order_release);
                last_dxgi_completed_qpc_.store(
                    release_completion_qpc, std::memory_order_release);
                last_dxgi_pointer_only_completed_qpc_.store(
                    release_completion_qpc, std::memory_order_release);
                if (presentation.present) {
                    source_texture = dxgi_replay_texture_;
                    presentation_qpc = presentation.qpc;
                } else if (!dxgi_replay_cadence_.HasSeed()) {
                    InvalidateDxgiReplayCache();
                }
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!presentation.present) continue;
            } else {
                recent_event.pacing_accepted = true;
                winrt::com_ptr<ID3D11Texture2D> acquired_texture;
                hr = frame.resource()->QueryInterface(
                    __uuidof(ID3D11Texture2D), acquired_texture.put_void());
                if (FAILED(hr) || !acquired_texture) {
                    const bool released = finish_frame();
                    if (released && capture_generation_.load(
                            std::memory_order_acquire) != generation) {
                        dxgi_cursor_composer_.InvalidateGeneration();
                        InvalidateDxgiReplayCache();
                        capture_thread_stage_.store(0,
                            std::memory_order_relaxed);
                        continue;
                    }
                    dxgi_replay::AcquireResult result;
                    result.generation = generation;
                    result.call_start_qpc = acquire_start_qpc.QuadPart;
                    result.call_end_qpc = release_completion_qpc;
                    result.observation = dxgi_replay::Observation::Image;
                    result.release_succeeded = released;
                    result.cache_copy_succeeded = false;
                    if (released) {
                        LARGE_INTEGER completed{};
                        QueryPerformanceCounter(&completed);
                        result.call_end_qpc = completed.QuadPart;
                        last_dxgi_completed_qpc_.store(
                            completed.QuadPart, std::memory_order_release);
                    } else {
                        last_dxgi_completed_qpc_.store(0,
                            std::memory_order_release);
                    }
                    dxgi_replay_cadence_.Complete(schedule.tick, result);
                    InvalidateDxgiReplayCache();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    if (!released) break;
                    continue;
                }

                D3D11_TEXTURE2D_DESC recent_texture_desc{};
                acquired_texture->GetDesc(&recent_texture_desc);
                recent_event.width = recent_texture_desc.Width;
                recent_event.height = recent_texture_desc.Height;
                recent_event.format = static_cast<uint32_t>(
                    recent_texture_desc.Format);
                if (!ValidateCaptureTexture(acquired_texture.get(), "DXGI")) {
                    acquired_texture = nullptr;
                    const bool released = finish_frame();
                    if (!released) {
                        fail_scheduled_acquire();
                        break;
                    }
                    if (capture_generation_.load(std::memory_order_acquire)
                        != generation) {
                        dxgi_cursor_composer_.InvalidateGeneration();
                        InvalidateDxgiReplayCache();
                        capture_thread_stage_.store(0,
                            std::memory_order_relaxed);
                        continue;
                    }
                    fail_scheduled_acquire();
                    ClearReplayForRecovery();
                    capture_health_flags_.fetch_or(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }
                source_textures_received_.fetch_add(1,
                    std::memory_order_relaxed);
                if (present_qpc > 0) {
                    // Preserve the source's actual presentation timestamp.
                    // Resampling, when needed, affects only encoder presentation.
                    last_source_frame_qpc_.store(
                        present_qpc, std::memory_order_release);
                }
                capture_thread_stage_.store(3, std::memory_order_relaxed);
                if (!EnsureDxgiReplayTexture(recent_texture_desc)) {
                    acquired_texture = nullptr;
                    const bool released = finish_frame();
                    if (!released) {
                        fail_scheduled_acquire();
                        break;
                    }
                    if (capture_generation_.load(std::memory_order_acquire)
                        != generation) {
                        dxgi_cursor_composer_.InvalidateGeneration();
                        InvalidateDxgiReplayCache();
                        capture_thread_stage_.store(0,
                            std::memory_order_relaxed);
                        continue;
                    }
                    fail_scheduled_acquire();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }
                context_->CopyResource(dxgi_replay_texture_, acquired_texture.get());
                const HRESULT device_reason = device_->GetDeviceRemovedReason();
                acquired_texture = nullptr;
                const bool released = finish_frame();
                if (!released) {
                    fail_scheduled_acquire();
                    break;
                }
                if (capture_generation_.load(std::memory_order_acquire)
                    != generation) {
                    dxgi_cursor_composer_.InvalidateGeneration();
                    InvalidateDxgiReplayCache();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
                LARGE_INTEGER completed{};
                QueryPerformanceCounter(&completed);
                release_completion_qpc = completed.QuadPart;
                if (FAILED(device_reason)) {
                    SetCaptureFailure(diagnostics::FormatHResultFailure(
                        "ID3D11Device::GetDeviceRemovedReason(DXGI replay copy)",
                        device_reason));
                    fail_scheduled_acquire();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }
                dxgi_replay::AcquireResult result;
                result.generation = generation;
                result.call_start_qpc = acquire_start_qpc.QuadPart;
                result.call_end_qpc = release_completion_qpc;
                result.source_qpc = present_qpc;
                result.observation = dxgi_replay::Observation::Image;
                result.release_succeeded = true;
                result.cache_copy_succeeded = true;
                presentation = dxgi_replay_cadence_.Complete(
                    schedule.tick, result);
                dxgi_replay_cache_armed_.store(
                    dxgi_replay_cadence_.HasSeed(),
                    std::memory_order_release);
                last_dxgi_completed_qpc_.store(
                    release_completion_qpc, std::memory_order_release);
                last_dxgi_pointer_only_completed_qpc_.store(
                    0, std::memory_order_release);
                if (presentation.present) {
                    source_texture = dxgi_replay_texture_;
                    presentation_qpc = presentation.qpc;
                } else if (!dxgi_replay_cadence_.HasSeed()) {
                    InvalidateDxgiReplayCache();
                }
                if (!presentation.present) {
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
            }
            }

            if (!source_texture || presentation_qpc <= 0) {
                InvalidateDxgiReplayCache();
                SetCaptureFailure(
                    "DXGI replay policy selected a missing texture or invalid presentation QPC");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }

            ProcessingBudgetSample processing_sample{
                processing_budget_qpc, acquire_end_qpc.QuadPart,
                processing_period_qpc, qpc_freq.QuadPart, dxgi_processing_budget_us_};

            // Compose once, after the common presentation decision and before
            // GPU, hybrid CPU-input, or legacy raw encoder branches.  The
            // replay cache remains a clean background texture; the compositor
            // owns the separate output texture used by this presentation.
            ID3D11Texture2D* composed_texture = source_texture;
            cursor_detail.clear();
            if (capture_cursor_ && !dxgi_cursor_composer_.Compose(
                    source_texture, &composed_texture, cursor_detail)) {
                SetCaptureFailure(
                    cursor_detail.empty()
                        ? "DXGI cursor composition failed"
                        : cursor_detail);
                std::cerr << "[CaptureThread] "
                          << last_capture_failure_detail_ << std::endl;
                InvalidateDxgiReplayCache();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }
            source_texture = composed_texture;

            if (nvenc_active_ && !replay_encoder_cpu_input_) {
                // Same-adapter compressed replay path (native NVENC or AMF).
                // CopyResource is a pure GPU op; there is no full-frame CPU
                // readback or upload on the normal AMD path.
                capture_thread_stage_.store(4, std::memory_order_relaxed);
                conversion_submissions_.fetch_add(1, std::memory_order_relaxed);
                ID3D11Texture2D* encode_texture =
                    PrepareEncodeTexture(source_texture);
                if (encode_texture)
                    conversion_completions_.fetch_add(1, std::memory_order_relaxed);
                capture_thread_stage_.store(5, std::memory_order_relaxed);
                const bool encoded = encode_texture && EncodeGpuReplayTexture(
                    encode_texture,
                    presentation_qpc,
                    frames_captured_.load(std::memory_order_relaxed) + 1);
                capture_thread_stage_.store(0, std::memory_order_relaxed);
                if (!encoded) {
                    InvalidateDxgiReplayCache();
                    break;
                }
            }
            else if (nvenc_active_ && replay_encoder_cpu_input_) {
                // Hybrid NVENC still transfers through CPU memory. When the
                // scaler is active, only the completed encoded canvas is mapped.
                capture_thread_stage_.store(4, std::memory_order_relaxed);
                ID3D11Texture2D* readback_source = PrepareHybridReadbackTexture(source_texture, "DXGI");
                if (!readback_source) break;
                context_->CopyResource(staging_texture_, readback_source);

                D3D11_MAPPED_SUBRESOURCE mapped{};
                hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                if (FAILED(hr)) {
                    if (hybrid_bgra_scaler_) {
                        FailHybridReadback("DXGI", "scaled staging Map",
                            diagnostics::FormatHResultFailure("ID3D11DeviceContext::Map", hr));
                        break;
                    }
                    std::cerr << "[CaptureThread] Texture map failed (Optimus): 0x"
                        << std::hex << hr << std::dec << std::endl;
                    InvalidateDxgiReplayCache();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
                const uint32_t readback_width = hybrid_bgra_scaler_ ? target_width_ : width_;
                if (!ValidateMappedCaptureRowPitch(readback_width, mapped.RowPitch, "DXGI")) {
                    context_->Unmap(staging_texture_, 0);
                    InvalidateDxgiReplayCache();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }

                const auto* input = static_cast<const uint8_t*>(mapped.pData);
                if (!hybrid_bgra_scaler_) {
                    SampleContentBGRA(input, mapped.RowPitch, width_, height_,
                        frames_captured_.load(std::memory_order_relaxed) + 1);
                }
                const uint8_t* encode_data = hybrid_bgra_scaler_ ? input
                    : CropMappedData(input, mapped.RowPitch);
                bool encoded = false;
                for (uint32_t attempt = 1; attempt <= 3 && !encoded; ++attempt) {
                    encoded = replay_encoder_->EncodeFrameCPU(
                        encode_data,
                        mapped.RowPitch,
                        presentation_qpc);
                    if (!encoded && attempt < 3) {
                        std::cerr << "[CaptureThread] Transient hybrid encoder "
                                  << "submission failure; retrying (" << attempt
                                  << "/3)." << std::endl;
                        std::this_thread::sleep_for(std::chrono::milliseconds(2));
                    }
                }
                context_->Unmap(staging_texture_, 0);
                conversion_submissions_.fetch_add(1, std::memory_order_relaxed);
                conversion_completions_.fetch_add(1, std::memory_order_relaxed);
                if (!encoded) {
                    InvalidateDxgiReplayCache();
                    FailReplayEncoder("hybrid NVENC CPU-input submission");
                    break;
                }
                last_encoder_submission_qpc_.store(
                    presentation_qpc, std::memory_order_release);
            }
            else {
                // Legacy raw path: copy to staging, then map and copy to the frame pool.
                context_->CopyResource(staging_texture_, source_texture);
                capture_thread_stage_.store(4, std::memory_order_relaxed);

                D3D11_MAPPED_SUBRESOURCE mapped{};
                hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                if (FAILED(hr)) {
                    std::cerr << "[CaptureThread] Texture map failed: 0x"
                        << std::hex << hr << std::dec << std::endl;
                    InvalidateDxgiReplayCache();
                    capture_thread_stage_.store(0, std::memory_order_relaxed);
                    continue;
                }
                if (!ValidateMappedCaptureRowPitch(width_, mapped.RowPitch, "DXGI")) {
                    context_->Unmap(staging_texture_, 0);
                    InvalidateDxgiReplayCache();
                    ClearReplayForRecovery();
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                    running_.store(false);
                    break;
                }

                const uint8_t* full_src = static_cast<const uint8_t*>(mapped.pData);
                const uint8_t* src     = CropMappedData(full_src, mapped.RowPitch);
                const size_t   row     = static_cast<size_t>(crop_width_) * 4;
                const bool     pitched = (mapped.RowPitch != static_cast<UINT>(row));

                const size_t write_pos = ring_head_.load(std::memory_order_relaxed);
                const size_t slot_idx  = write_pos % max_frames_;
                uint8_t*     dst       = frame_pool_.GetSlot(slot_idx);

                SampleContentBGRA(
                    full_src, mapped.RowPitch, width_, height_,
                    frames_captured_.load(std::memory_order_relaxed) + 1);

                if (!pitched) {
                    std::memcpy(dst, src, row * crop_height_);
                }
                else {
                    for (uint32_t y = 0; y < crop_height_; y++) {
                        std::memcpy(dst + y * row, src + y * mapped.RowPitch, row);
                    }
                }

                context_->Unmap(staging_texture_, 0);

                ring_head_.fetch_add(1, std::memory_order_release);
                size_t prev = ring_count_.load(std::memory_order_relaxed);
                if (prev < max_frames_)
                    ring_count_.fetch_add(1, std::memory_order_relaxed);
            }

            if (!dxgi_replay_cadence_.CommitPresentation(presentation_qpc)) {
                InvalidateDxgiReplayCache();
                SetCaptureFailure(
                    "DXGI replay presentation timestamp was not increasing");
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                running_.store(false);
                break;
            }
            last_dxgi_presentation_qpc_.store(
                presentation_qpc, std::memory_order_release);
            if (presentation.held) {
                dxgi_held_presentations_.fetch_add(
                    1, std::memory_order_relaxed);
            }
            if (presentation.resampled_source_qpc) {
                dxgi_resampled_presentations_.fetch_add(
                    1, std::memory_order_relaxed);
            }

            uint64_t fc = frames_captured_.fetch_add(1, std::memory_order_relaxed) + 1;
            capture_thread_stage_.store(0, std::memory_order_relaxed);
            if (fc == 1 || fc == 10 || fc == 100 || (fc % 500 == 0)) {
                std::cout << "[CaptureThread] Frames captured: " << fc << std::endl;
            }
        }

        std::cout << "[CaptureThread] Stopped. Total frames: "
                  << frames_captured_.load() << std::endl;
        InvalidateDxgiReplayCache();
        if (cadence_timer) {
            CancelWaitableTimer(cadence_timer);
            CloseHandle(cadence_timer);
        }
        if (!(capture_health_flags_.load() & CAPTURE_HEALTH_BACKEND_FAILED))
            capture_health_flags_.store(CAPTURE_HEALTH_NONE);
    }

    bool CaptureEngine::ConfigureCrop(const CaptureConfig& config) {
        crop_enabled_ = false;
        crop_x_ = 0;
        crop_y_ = 0;
        crop_width_ = width_;
        crop_height_ = height_;
        if (!config.crop_enabled) return true;

        const bool normalized = std::isfinite(config.crop_x)
            && std::isfinite(config.crop_y)
            && std::isfinite(config.crop_width)
            && std::isfinite(config.crop_height)
            && config.crop_x >= 0.0 && config.crop_y >= 0.0
            && config.crop_width > 0.0 && config.crop_height > 0.0
            && config.crop_x + config.crop_width <= 1.000001
            && config.crop_y + config.crop_height <= 1.000001;
        if (!normalized || width_ < 2 || height_ < 2) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_PROFILE_INVALID: "
                         "The saved crop was outside the source frame; the full "
                         "frame is being captured." << std::endl;
            return true;
        }

        const uint32_t left = std::min<uint32_t>(
            width_ - 1, static_cast<uint32_t>(std::floor(config.crop_x * width_)));
        const uint32_t top = std::min<uint32_t>(
            height_ - 1, static_cast<uint32_t>(std::floor(config.crop_y * height_)));
        const uint32_t right = std::min<uint32_t>(
            width_, static_cast<uint32_t>(std::ceil(
                (config.crop_x + config.crop_width) * width_)));
        const uint32_t bottom = std::min<uint32_t>(
            height_, static_cast<uint32_t>(std::ceil(
                (config.crop_y + config.crop_height) * height_)));
        const uint32_t crop_width = (right > left) ? ((right - left) & ~1u) : 0;
        const uint32_t crop_height = (bottom > top) ? ((bottom - top) & ~1u) : 0;
        if (crop_width < 2 || crop_height < 2
            || left + crop_width > width_ || top + crop_height > height_) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_PROFILE_INVALID: "
                         "The saved crop became too small at this resolution; "
                         "the full frame is being captured." << std::endl;
            return true;
        }
        if (left == 0 && top == 0
            && crop_width == width_ && crop_height == height_) return true;

        D3D11_TEXTURE2D_DESC description{};
        description.Width = crop_width;
        description.Height = crop_height;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_DEFAULT;
        // The crop feeds both shader sampling and encoder video-processor input views.
        description.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        const HRESULT result = device_->CreateTexture2D(
            &description, nullptr, &crop_texture_);
        if (FAILED(result)) {
            std::cerr << "FTHR_STARTUP_WARNING: CROP_GPU_UNAVAILABLE: "
                         "The crop texture could not be created; the full frame "
                         "is being captured." << std::endl;
            return true;
        }

        crop_enabled_ = true;
        crop_x_ = left;
        crop_y_ = top;
        crop_width_ = crop_width;
        crop_height_ = crop_height;

        // A resolution preset is a bounding box. Cropped sources retain their
        // own aspect ratio instead of being stretched back to the old preset.
        if (target_width_ > 0 && target_height_ > 0) {
            const double scale = std::min(
                static_cast<double>(target_width_) / crop_width_,
                static_cast<double>(target_height_) / crop_height_);
            target_width_ = std::max<uint32_t>(2,
                static_cast<uint32_t>(std::floor(crop_width_ * scale)) & ~1u);
            target_height_ = std::max<uint32_t>(2,
                static_cast<uint32_t>(std::floor(crop_height_ * scale)) & ~1u);
        }
        std::cout << "[Crop] Encoder input " << crop_width_ << 'x'
                  << crop_height_ << " at " << crop_x_ << ',' << crop_y_
                  << " (normalized per-game profile)" << std::endl;
        return true;
    }

    ID3D11Texture2D* CaptureEngine::PrepareEncodeTexture(
        ID3D11Texture2D* source) {
        if (!crop_enabled_) return source;
        if (!source || !crop_texture_ || !context_) return nullptr;
        const D3D11_BOX box{
            crop_x_, crop_y_, 0,
            crop_x_ + crop_width_, crop_y_ + crop_height_, 1};
        context_->CopySubresourceRegion(
            crop_texture_, 0, 0, 0, 0, source, 0, &box);
        return crop_texture_;
    }

    ID3D11Texture2D* CaptureEngine::PrepareHybridReadbackTexture(
        ID3D11Texture2D* source, const char* backend_name) {
        if (!hybrid_bgra_scaler_) return source;
        ID3D11Texture2D* cropped = PrepareEncodeTexture(source);
        ID3D11Texture2D* scaled = nullptr;
        std::string detail;
        if (!cropped || !hybrid_bgra_scaler_->Scale(cropped, scaled, detail) || !scaled) {
            FailHybridReadback(backend_name, "BGRA downscale",
                detail.empty() ? "The GPU scaler did not produce an encoded canvas" : detail);
            return nullptr;
        }
        // Keep content-health evidence tied to the capture source. The
        // bounded GPU sampler avoids a second full-frame readback.
        SampleContentTexture(source, frames_captured_.load(std::memory_order_relaxed) + 1);
        return scaled;
    }

    void CaptureEngine::FailHybridReadback(const char* backend_name, const char* operation,
                                           std::string detail) {
        SetCaptureFailure(detail);
        ClearReplayForRecovery();
        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
        running_.store(false);
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"runtime_failed\",\"state\":\"FAILED\",\"error_code\":\""
                  << (wgc_active_ ? "CAPTURE_WGC_RUNTIME_FAILED" : "CAPTURE_DXGI_RUNTIME_FAILED")
                  << "\",\"backend\":\"" << backend_name
                  << "\",\"operation\":\"" << operation
                  << "\",\"detail\":\"" << diagnostics::JsonEscape(detail) << "\"}" << std::endl;
    }

    const uint8_t* CaptureEngine::CropMappedData(
        const uint8_t* data, uint32_t stride) const {
        if (!data || !crop_enabled_) return data;
        return data + static_cast<size_t>(crop_y_) * stride
            + static_cast<size_t>(crop_x_) * 4;
    }

    bool CaptureEngine::CreateStagingTexture(uint32_t width, uint32_t height) {
        if (!device_ || width == 0 || height == 0) return false;

        D3D11_TEXTURE2D_DESC description{};
        description.Width = width;
        description.Height = height;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_STAGING;
        description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        winrt::com_ptr<ID3D11Texture2D> staging;
        const HRESULT result = device_->CreateTexture2D(&description, nullptr, staging.put());
        if (FAILED(result)) {
            std::cerr << "[CaptureEngine] D3D11 readback texture creation failed: 0x"
                      << std::hex << result << std::dec << std::endl;
            return false;
        }
        if (staging_texture_) staging_texture_->Release();
        staging_texture_ = staging.detach();
        return true;
    }

    bool CaptureEngine::EncodeGpuReplayTexture(
        ID3D11Texture2D* source,
        int64_t present_qpc,
        uint64_t produced_frame) {
        if (!source || !replay_encoder_ || !context_) {
            FailReplayEncoder("D3D11 replay input setup");
            return false;
        }
        if (!replay_hdr_) SampleContentTexture(source, produced_frame);
        if (replay_encoder_->RequiresBackendGpuPreparation()) {
            if (!replay_encoder_->PrepareGpuFrame(source, 0)) {
                FailReplayEncoder("backend GPU conversion");
                return false;
            }
        } else {
            ID3D11Texture2D* input = replay_encoder_->GetCurrentInputTexture();
            if (!input) {
                FailReplayEncoder("D3D11 replay input acquisition");
                return false;
            }

            D3D11_TEXTURE2D_DESC source_description{};
            D3D11_TEXTURE2D_DESC input_description{};
            source->GetDesc(&source_description);
            input->GetDesc(&input_description);
            if (source_description.Width != input_description.Width
                || source_description.Height != input_description.Height
                || source_description.Format != input_description.Format) {
                FailReplayEncoder("D3D11 replay texture compatibility check");
                return false;
            }
            context_->CopySubresourceRegion(
                input,
                replay_encoder_->GetCurrentInputSubresource(),
                0, 0, 0,
                source,
                0,
                nullptr);
        }
        if (present_qpc > 0) {
            LARGE_INTEGER submission{};
            QueryPerformanceCounter(&submission);
            last_encoder_submission_qpc_.store(
                replay_pause_clock_ ? submission.QuadPart : present_qpc,
                std::memory_order_release);
        }
        if (!replay_encoder_->EncodeFrame(present_qpc)) {
            FailReplayEncoder("hardware frame submission");
            return false;
        }
        if (replay_config_publish_failed_.load()) {
            FailReplayEncoder("encoded stream configuration publication");
            return false;
        }
        return true;
    }

    void CaptureEngine::FailReplayEncoder(const char* operation) {
        if (HasReplayMemoryFailure()) {
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
            running_.store(false);
            return;
        }
        encoder_failure_count_.fetch_add(1, std::memory_order_relaxed);
        ClearReplayForRecovery();
        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
        running_.store(false);
        const std::string detail = replay_encoder_
            ? replay_encoder_->GetLastError() : std::string{};
        std::cerr << "[CaptureEngine] " << operation << " failed";
        if (!detail.empty()) std::cerr << ": " << detail;
        std::cerr << "; a fresh capture generation is required" << std::endl;
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"encoder\","
                  << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                  << "\"error_code\":\"ENCODER_SUBMIT_FAILED\","
                  << "\"operation\":\""
                  << diagnostics::JsonEscape(operation ? operation : "unknown")
                  << "\",\"detail\":\""
                  << diagnostics::JsonEscape(
                         detail.empty() ? "unavailable:no_backend_detail" : detail)
                  << "\",\"encoder_backend\":\""
                  << diagnostics::JsonEscape(startup_encoder_backend_) << "\","
                  << "\"codec\":\"" << diagnostics::JsonEscape(startup_codec_)
                  << "\",\"encoder_adapter_luid\":"
                  << (encoder_adapter_luid_available_
                      ? AdapterLuidJson(encoder_adapter_luid_)
                      : "\"unavailable:not_resolved\"") << "}"
                  << std::endl;
    }


    bool CaptureEngine::ResolveSelectedMonitor(const char* backend_name) {
        const auto result = monitor_resolver_.Resolve(monitor_device_path_);
        if (!result.ok()) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(result.error) << ": "
                      << result.diagnostic << std::endl;
            SetCaptureFailure(result.diagnostic);
            return false;
        }

        if (!resolved_monitor_.monitor_device_path.empty()
            && !(resolved_monitor_ == result.monitor)) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::MonitorTopologyChanged)
                      << ": selected monitor transient mapping changed; "
                         "starting a fresh capture generation" << std::endl;
        }
        resolved_monitor_ = result.monitor;
        std::cout << '[' << backend_name << "] Monitor resolved: ";
        std::wcout << resolved_monitor_.friendly_name << L"  "
                   << resolved_monitor_.source_gdi_name << L"  "
                   << resolved_monitor_.monitor_device_path << std::endl;
        std::cout << '[' << backend_name << "] Topology: LUID="
                  << resolved_monitor_.adapter_luid.high_part << ':'
                  << resolved_monitor_.adapter_luid.low_part
                  << " source=" << resolved_monitor_.source_id
                  << " target=" << resolved_monitor_.target_id
                  << " generation=" << resolved_monitor_.topology_generation
                  << std::endl;
        return true;
    }

    bool CaptureEngine::InitializeMonitorCaptureDevice(
        const char* backend_name, IDXGIOutput** selected_output) {
        if (!selected_output) return false;
        *selected_output = nullptr;
        if (!ResolveSelectedMonitor(backend_name)) return false;

        IDXGIFactory1* factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory));
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "CreateDXGIFactory1", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            return false;
        }

        IDXGIAdapter1* capture_adapter = nullptr;
        std::string output_diagnostic;
        const bool wgc = std::strcmp(backend_name, "WGC") == 0;
        // WGC binds to the selected adapter and HMONITOR item. It does not
        // require a resolvable IDXGIOutput, while Desktop Duplication does.
        const bool opened = wgc
            ? monitor::OpenSelectedDxgiAdapter(
                  factory, resolved_monitor_, &capture_adapter,
                  output_diagnostic)
            : monitor::OpenSelectedDxgiOutput(
                  factory, resolved_monitor_, &capture_adapter, selected_output,
                  &resolved_dxgi_output_, output_diagnostic);
        if (!opened) {
            std::cerr << '[' << backend_name << "] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::OutputResolutionFailed)
                      << ": " << output_diagnostic << std::endl;
            SetCaptureFailure(output_diagnostic);
            factory->Release();
            return false;
        }

        source_hdr_ = QueryHdrDisplay(resolved_monitor_, capture_adapter).value_or(false);
        sdr_reference_white_ = source_hdr_ ? QuerySdrReferenceWhite(resolved_monitor_) : 1.0f;
        DXGI_ADAPTER_DESC1 capture_desc{};
        hr = capture_adapter->GetDesc1(&capture_desc);
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIAdapter1::GetDesc1(selected capture adapter)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            if (*selected_output) (*selected_output)->Release();
            *selected_output = nullptr;
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            capture_adapter->Release();
            factory->Release();
            return false;
        }
        D3D_FEATURE_LEVEL feature_level{};
        hr = D3D11CreateDevice(
            capture_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
            nullptr, 0, D3D11_SDK_VERSION,
            &device_, &feature_level, &context_);
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "D3D11CreateDevice(selected monitor adapter)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << backend_name << "] " << failure << std::endl;
            if (*selected_output) (*selected_output)->Release();
            *selected_output = nullptr;
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            capture_adapter->Release();
            factory->Release();
            return false;
        }

        capture_adapter_vendor_ = EncoderVendorFromPciVendorId(
            capture_desc.VendorId);
        capture_device_adapter_luid_ = {
            capture_desc.AdapterLuid.LowPart,
            capture_desc.AdapterLuid.HighPart};
        capture_device_adapter_luid_available_ = true;
        nvidia_device_ = capture_adapter_vendor_ == EncoderVendor::Nvidia;
        if (wgc) {
            width_ = resolved_monitor_.width;
            height_ = resolved_monitor_.height;
            resolved_desktop_coordinates_ = {
                resolved_monitor_.left, resolved_monitor_.top,
                resolved_monitor_.left + static_cast<LONG>(width_),
                resolved_monitor_.top + static_cast<LONG>(height_)};
            resolved_desktop_coordinates_available_ = true;
        } else {
            DXGI_OUTPUT_DESC output_desc{};
            hr = (*selected_output)->GetDesc(&output_desc);
            if (FAILED(hr)) {
                const std::string failure = diagnostics::FormatHResultFailure(
                    "IDXGIOutput::GetDesc(selected output)", hr);
                SetCaptureFailure(failure);
                std::cerr << '[' << backend_name << "] " << failure << std::endl;
                if (*selected_output) (*selected_output)->Release();
                *selected_output = nullptr;
                if (context_) { context_->Release(); context_ = nullptr; }
                if (device_) { device_->Release(); device_ = nullptr; }
                capture_adapter->Release();
                factory->Release();
                return false;
            }
            if (!IsSupportedDxgiOutputRotation(
                    static_cast<uint32_t>(output_desc.Rotation))) {
                std::ostringstream failure;
                failure << "selected output rotation "
                        << static_cast<uint32_t>(output_desc.Rotation)
                        << " is unsupported by the capture geometry contract";
                SetCaptureFailure(failure.str());
                std::cerr << '[' << backend_name << "] " << failure.str()
                          << std::endl;
                if (*selected_output) (*selected_output)->Release();
                *selected_output = nullptr;
                if (context_) { context_->Release(); context_ = nullptr; }
                if (device_) { device_->Release(); device_ = nullptr; }
                capture_adapter->Release();
                factory->Release();
                return false;
            }
            width_ = static_cast<uint32_t>(
                output_desc.DesktopCoordinates.right
                - output_desc.DesktopCoordinates.left);
            height_ = static_cast<uint32_t>(
                output_desc.DesktopCoordinates.bottom
                - output_desc.DesktopCoordinates.top);
            resolved_desktop_coordinates_ = output_desc.DesktopCoordinates;
            resolved_desktop_coordinates_available_ = true;
        }
        capture_adapter->Release();
        factory->Release();
        std::cout << '[' << backend_name << "] Selected output ready: "
                  << width_ << 'x' << height_
                  << " [" << EncoderVendorName(capture_adapter_vendor_)
                  << " display-owning adapter]"
                  << std::endl;
        return true;
    }


    // Create WGC monitor capture and D3D11 on the selected monitor adapter.
    // Set the source dimensions without DXGI duplication or a staging texture.

    bool CaptureEngine::InitializeWGC() {
        startup_capture_backend_ = "WGC_MONITOR";
        // WGC requires Windows 10 1903+ (build 18362)
        try {
            if (!winrt::Windows::Graphics::Capture::GraphicsCaptureSession::IsSupported()) {
                std::cerr << "[WGC] GraphicsCaptureSession not supported on this system" << std::endl;
                SetCaptureFailure(
                    "api_call=GraphicsCaptureSession::IsSupported result=false");
                return false;
            }
        } catch (winrt::hresult_error const& error) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::IsSupported", error.code().value);
            SetCaptureFailure(failure);
            std::cerr << "[WGC] " << failure << std::endl;
            return false;
        } catch (...) {
            SetCaptureFailure(
                "api_call=GraphicsCaptureSession::IsSupported exception=unknown");
            std::cerr << "[WGC] IsSupported() threw — WGC unavailable" << std::endl;
            return false;
        }

        // Resolve the persistent monitor device path into the current topology,
        // then create D3D11 on the adapter that actually owns that monitor.
        IDXGIOutput* selected_output = nullptr;
        if (!InitializeMonitorCaptureDevice("WGC", &selected_output)) {
            return false;
        }
        if (selected_output) selected_output->Release();
        selected_output = nullptr;
        last_capture_failure_detail_.clear();
        const HMONITOR hmonitor = reinterpret_cast<HMONITOR>(
            resolved_monitor_.hmonitor);
        HRESULT hr = S_OK;

        // WinRT session setup
        wgc_state_ = std::make_unique<WGCState>();
        wgc_state_->monitor_item = true;
        monitor_source_invalidated_.store(false, std::memory_order_release);
        const char* current_api = "ID3D11Device::QueryInterface(IDXGIDevice)";
        try {
            // 4a. Wrap ID3D11Device as WinRT IDirect3DDevice
            winrt::com_ptr<IDXGIDevice> dxgi_dev;
            winrt::check_hresult(device_->QueryInterface(dxgi_dev.put()));

            winrt::com_ptr<IInspectable> insp;
            current_api = "CreateDirect3D11DeviceFromDXGIDevice";
            winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev.get(), insp.put()));

            current_api = "IInspectable::as(IDirect3DDevice)";
            wgc_state_->winrt_device =
                insp.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();

            current_api = "RoGetActivationFactory(GraphicsCaptureItem)";
            auto item_interop = winrt::get_activation_factory<
                winrt::Windows::Graphics::Capture::GraphicsCaptureItem,
                IGraphicsCaptureItemInterop>();

            current_api = "IGraphicsCaptureItemInterop::CreateForMonitor";
            winrt::check_hresult(item_interop->CreateForMonitor(
                hmonitor,
                winrt::guid_of<winrt::Windows::Graphics::Capture::GraphicsCaptureItem>(),
                winrt::put_abi(wgc_state_->item)));

            current_api = "GraphicsCaptureItem::Closed(add handler)";
            wgc_state_->close_signal =
                std::make_shared<wgc::ItemClosedSignal>();
            const auto close_signal = wgc_state_->close_signal;
            wgc_state_->item_closed_token = wgc_state_->item.Closed(
                [close_signal](auto&, auto&) {
                    close_signal->closed.store(true, std::memory_order_release);
                });
            wgc_state_->item_closed_registered = true;

            // Poll a two-slot free-threaded pool at video cadence; no dispatcher or
            // message pump is required.
            current_api = "GraphicsCaptureItem::Size";
            auto sz = wgc_state_->item.Size();
            if (sz.Width <= 0 || sz.Height <= 0) throw winrt::hresult_error(E_INVALIDARG);
            width_ = static_cast<uint32_t>(sz.Width);
            height_ = static_cast<uint32_t>(sz.Height);
            wgc_state_->pool_width = width_;
            wgc_state_->pool_height = height_;
            current_api = "Direct3D11CaptureFramePool::CreateFreeThreaded";
            wgc_state_->frame_pool =
                winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
                    wgc_state_->winrt_device,
                    WgcPixelFormat(source_hdr_),
                    2,
                    sz);

            // Apply the optional border opt-out before StartCapture. A missing
            // or denied opt-out retains this WGC target and the OS privacy border;
            // only a required session failure rejects the WGC attempt.
            current_api = "Direct3D11CaptureFramePool::CreateCaptureSession";
            wgc_state_->session =
                wgc_state_->frame_pool.CreateCaptureSession(wgc_state_->item);
            if (!ApplyCaptureBorderPolicy("monitor")) {
                std::cerr << "[WGC] Required monitor capture session unavailable; "
                             "the WGC attempt remains fatal."
                          << std::endl;
                ShutdownWGC();
                ShutdownD3D11();
                return false;
            }


            current_api = "GraphicsCaptureSession::IsCursorCaptureEnabled";
            ApplyCaptureCursorPolicy();

            ConfigureWgcSessionCadence(wgc_state_->session, fps_, "monitor");
            current_api = "GraphicsCaptureSession::StartCapture";
            wgc_state_->session.StartCapture();

        } catch (winrt::hresult_error const& e) {
            std::string failure = diagnostics::FormatHResultFailure(
                current_api, e.code().value);
            const std::string winrt_message = winrt::to_string(e.message());
            if (!winrt_message.empty()) {
                failure += " winrt_message=\"";
                failure += diagnostics::JsonEscape(winrt_message);
                failure += '\"';
            }
            SetCaptureFailure(failure);
            std::cerr << "[WGC] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::CaptureItemCreationFailed)
                      << ": " << failure << std::endl;
            wgc_state_.reset();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_)  { device_->Release();  device_  = nullptr; }
            nvidia_device_ = false;
            capture_adapter_vendor_ = EncoderVendor::Software;
            return false;
        }

        std::cout << "[WGC] Ready. Capture started ("
                  << width_ << "x" << height_ << ", "
                  << EncoderVendorName(capture_adapter_vendor_)
                  << " capture adapter"
                  << ")" << std::endl;
        last_capture_failure_detail_.clear();
        return true;
    }


    void CaptureEngine::ApplyCaptureCursorPolicy() {
        auto session2 = wgc_state_->session.try_as<
            winrt::Windows::Graphics::Capture::IGraphicsCaptureSession2>();
        if (!session2) {
            if (!capture_cursor_) {
                throw winrt::hresult_error(E_NOINTERFACE,
                    L"Cursor exclusion requires Windows 10 version 2004 or newer.");
            }
            return; // Older WGC sessions include the cursor by default.
        }
        session2.IsCursorCaptureEnabled(capture_cursor_);
        if (session2.IsCursorCaptureEnabled() != capture_cursor_) {
            throw winrt::hresult_error(E_FAIL,
                L"Windows did not apply the requested cursor capture setting.");
        }
    }

    // Request the optional border suppression before StartCapture and verify the
    // live session. An optional interface/property failure retains the OS privacy
    // border and the requested WGC target; only an unavailable required session
    // returns false.

    bool CaptureEngine::ApplyCaptureBorderPolicy(const char* capture_target) {
        CaptureBorderPolicyInput input;
        input.session_available = wgc_state_ && static_cast<bool>(wgc_state_->session);
        input.session_interface_checked = true;
        std::string optional_failure_detail;
        if (!input.session_available) {
            if (last_capture_failure_detail_.empty()) {
                SetCaptureFailure(
                    "api_call=Direct3D11CaptureFramePool::CreateCaptureSession "
                    "result=session_unavailable");
            }
            const auto decision = EvaluateCaptureBorderPolicy(input);
            std::cout << "[CaptureBorderPolicy] target="
                      << (capture_target ? capture_target : "unknown")
                      << " capture_allowed="
                      << (decision.capture_allowed ? "true" : "false")
                      << " border_status=required_session_unavailable"
                      << " reason=" << CaptureBorderPolicyReasonName(decision.reason)
                      << std::endl;
            return decision.capture_allowed;
        }
        try {
            auto session3 = wgc_state_->session.try_as<
                winrt::Windows::Graphics::Capture::IGraphicsCaptureSession3>();
            input.session_interface_available = static_cast<bool>(session3);
            if (session3) {
                input.property_attempted = true;
                try {
                    // This is the same session-level opt-out used by the old
                    // build and must happen before StartCapture().
                    session3.IsBorderRequired(false);
                    input.property_set_succeeded = true;
                    input.border_required_after_attempt =
                        session3.IsBorderRequired();
                } catch (winrt::hresult_error const& error) {
                    optional_failure_detail = diagnostics::FormatHResultFailure(
                        "IGraphicsCaptureSession3::IsBorderRequired",
                        error.code().value);
                    input.property_set_succeeded = false;
                } catch (...) {
                    optional_failure_detail =
                        "api_call=IGraphicsCaptureSession3::IsBorderRequired exception=unknown";
                    input.property_set_succeeded = false;
                }
            }
        } catch (winrt::hresult_error const& error) {
            optional_failure_detail = diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::QueryInterface(IGraphicsCaptureSession3)",
                error.code().value);
            input.session_interface_available = false;
        } catch (...) {
            optional_failure_detail =
                "api_call=GraphicsCaptureSession::QueryInterface(IGraphicsCaptureSession3) exception=unknown";
            input.session_interface_available = false;
        }
        const auto decision = EvaluateCaptureBorderPolicy(input);
        const char* border_status = decision.effective_borderless
            ? "borderless_requested_os_policy_decides"
            : "os_privacy_border_retained";

        std::cout << "[CaptureBorderPolicy] target="
                  << (capture_target ? capture_target : "unknown")
                  << " session_interface="
                  << (input.session_interface_available ? "available" : "unavailable")
                  << " property_attempted=" << (input.property_attempted ? "true" : "false")
                  << " property_set=" << (input.property_set_succeeded ? "true" : "false")
                  << " border_required="
                  << (input.border_required_after_attempt ? "true" : "false")
                  << " capture_allowed="
                  << (decision.capture_allowed ? "true" : "false")
                  << " border_status=" << border_status
                  << " effective_borderless_requested="
                  << (decision.effective_borderless ? "true" : "false")
                  << " reason=" << CaptureBorderPolicyReasonName(decision.reason)
                  << (optional_failure_detail.empty() ? "" : " optional_failure=")
                  << optional_failure_detail
                  << std::endl;

        if (!decision.capture_allowed) {
            SetCaptureFailure(
                std::string("api_call=IGraphicsCaptureSession3::IsBorderRequired result=")
                + CaptureBorderPolicyReasonName(decision.reason));
        }

        return decision.capture_allowed;
    }



    void CaptureEngine::ShutdownWGC() {
        if (!wgc_state_) return;
        try {
            if (wgc_state_->item && wgc_state_->item_closed_registered) {
                wgc_state_->item.Closed(wgc_state_->item_closed_token);
                wgc_state_->item_closed_registered = false;
            }
            if (wgc_state_->session)    wgc_state_->session.Close();
            if (wgc_state_->frame_pool) {
                wgc_state_->frame_pool.Close();
            }
        } catch (...) {}
        wgc_state_.reset();
        std::cout << "[WGC] Shutdown complete." << std::endl;
    }


    // Sample the newest WGC image at video cadence and copy it before releasing
    // the borrowed frame. Reuse the owned image on idle ticks so video timing
    // does not depend on desktop change notifications.

    void CaptureEngine::CaptureThreadWGC() {
        const bool trace_enabled = frame_pacing_trace::Enabled();
        // WinRT calls (TryGetNextFrame, surface access) require the thread to be
        // in a COM apartment. Multi-threaded apartment is correct here — we have
        // no message pump and don't need the STA marshaling overhead.
        winrt::init_apartment(winrt::apartment_type::multi_threaded);

        std::cout << "[CaptureThread/WGC] Started ("
                  << fps_ << " fps, "
                  << (nvenc_active_ ? GetActiveEncoderName() : "software")
                  << " path, "
                  << (replay_encoder_cpu_input_ ? "hybrid CPU-input"
                      : (nvenc_active_ ? "same-adapter GPU input" : "readback"))
                  << ")" << std::endl;

        // WGC publishes changes, not a continuous video clock. Keep one
        // owned image and sample it at the encoder cadence, including an idle
        // desktop. Never retain a surface borrowed from the WGC frame pool.
        const auto interval = std::chrono::microseconds(1000000 / fps_);
        auto next_tick = std::chrono::steady_clock::now();
        WgcCadenceWait cadence_wait;
        wgc::ReadbackQueue readback_queue;
        winrt::com_ptr<ID3D11Texture2D> alternate_staging;
        if (fps_ >= 120 && replay_encoder_cpu_input_ && staging_texture_
            && active_replay_capability_.active_backend == ReplayEncoderBackend::NativeNvenc
            && !source_hdr_ && !hybrid_bgra_scaler_) {
            // Sustained cadence varied under warm load even with overlapping
            // staging copies. Keep synchronous readback as the default.
            wchar_t opt_in[2]{};
            const bool requested = GetEnvironmentVariableW(
                L"FTHR_EXPERIMENTAL_WGC_READBACK", opt_in, ARRAYSIZE(opt_in)) == 1
                && opt_in[0] == L'1';
            HRESULT result = S_OK;
            if (requested) {
                D3D11_TEXTURE2D_DESC staging_desc{};
                staging_texture_->GetDesc(&staging_desc);
                result = device_->CreateTexture2D(
                    &staging_desc, nullptr, alternate_staging.put());
            }
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"wgc_hybrid_readback_initialized\","
                      << "\"experimental_requested\":" << (requested ? "true" : "false") << ','
                      << "\"staging_slots\":" << (alternate_staging ? 2 : 1) << ','
                      << "\"hr\":" << result << "}" << std::endl;
        }
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"wgc_cadence_timer_initialized\","
                  << "\"timer_mode\":\""
                  << (cadence_wait.HighResolution() ? "high_resolution" : "condition_variable")
                  << "\"}" << std::endl;
        winrt::com_ptr<ID3D11Texture2D> latest_texture;
        CaptureScaleGeometry hdr_geometry;
        if (replay_hdr_) {
            BuildCaptureScaleGeometry(crop_width_, crop_height_,
                target_width_, target_height_, scaling_mode_ == 1, hdr_geometry);
        }
        auto color_monitor = resolved_monitor_;
        auto next_color_check = std::chrono::steady_clock::now();
        bool converted_image_valid = false;
        bool initial_seed_pending = false;
        if (wgc_state_ && !wgc_state_->monitor_item
            && wgc_state_->initial_owned_image_pending) {
            latest_texture.copy_from(wgc_state_->CanvasTexture());
            initial_seed_pending = latest_texture != nullptr;
            wgc_state_->initial_owned_image_pending = false;
        }
        if (replay_encoder_) {
            replay_encoder_->SetCaptureGeneration(
                capture_generation_.load(std::memory_order_relaxed));
        }
        uint32_t consecutive_frame_errors = 0;
        CaptureFocusPolicy focus_policy;
        WgcMonitorGeometry monitor_geometry(
            static_cast<int32_t>(width_), static_cast<int32_t>(height_));
        const WgcCaptureSize canvas{static_cast<int32_t>(width_), static_cast<int32_t>(height_)};
        const bool adapt_monitor = wgc_state_->monitor_item && focus_gated_
            && !source_hdr_ && !crop_enabled_;
        WgcProtectedMonitorResize protected_geometry(canvas);
        int64_t pool_recreated_at = 0;
        int64_t last_monitor_frame_time = 0;
        bool focus_was_paused = false;
        std::optional<std::chrono::steady_clock::time_point> resize_started;
        const auto log_monitor_event = [&](const char* event, const char* reason,
                WgcCaptureSize source, bool retained, int64_t elapsed_ms) {
            std::ostringstream log;
            log << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\",\"event\":\"" << event
                << "\",\"reason\":\"" << diagnostics::JsonEscape(reason)
                << "\",\"source_width\":" << source.width << ",\"source_height\":" << source.height
                << ",\"pool_width\":" << wgc_state_->pool_width << ",\"pool_height\":" << wgc_state_->pool_height
                << ",\"canvas_width\":" << width_ << ",\"canvas_height\":" << height_
                << ",\"encoded_width\":" << target_width_ << ",\"encoded_height\":" << target_height_
                << ",\"elapsed_ms\":" << elapsed_ms << ",\"capture_generation\":" << capture_generation_.load()
                << ",\"history_retained\":" << (retained ? "true" : "false") << '}';
            std::cout << log.str() << std::endl;
        };
        const auto fail_monitor_resize = [&](const std::string& reason, WgcCaptureSize observed,
                                              int64_t elapsed_ms) {
            SetCaptureFailure("WGC monitor resize: " + reason);
            capture_health_flags_.store(WgcSizeMismatchHealthFlags(true));
            ClearReplayForRecovery();
            log_monitor_event("wgc_monitor_reconfiguration_required", reason.c_str(),
                              observed, false, elapsed_ms);
            running_.store(false);
        };
        LARGE_INTEGER focus_frequency{};
        QueryPerformanceFrequency(&focus_frequency);
        int64_t foreground_since_100ns = 0;
        int64_t focus_paused_at = 0;
        auto captured_monitor = reinterpret_cast<HMONITOR>(resolved_monitor_.hmonitor);
        int64_t next_monitor_lookup = 0;
        const auto game_on_captured_monitor = [&](int64_t at) {
            const auto game_monitor = MonitorFromWindow(reinterpret_cast<HWND>(target_hwnd_),
                                                         MONITOR_DEFAULTTONULL);
            if (!game_monitor) return false;
            if (game_monitor == captured_monitor) return true;
            // HMONITORs can change across a mode set. Verify the stable display
            // identity and adapter before allowing a replacement handle.
            if (at >= next_monitor_lookup) {
                next_monitor_lookup = at + 5'000'000; // 500 ms while on a different display
                const auto current = monitor_resolver_.Resolve(resolved_monitor_.monitor_device_path);
                if (current.ok() && current.monitor.adapter_luid == resolved_monitor_.adapter_luid)
                    captured_monitor = reinterpret_cast<HMONITOR>(current.monitor.hmonitor);
            }
            return game_monitor == captured_monitor;
        };
        const auto observe_game_focus = [&](int64_t at) {
            if (!focus_gated_ || !target_hwnd_) return focus_policy.Observe(false);
            const bool foreground = GetForegroundWindow() == reinterpret_cast<HWND>(target_hwnd_);
            const char* reason = foreground ? window_source_identity_.RenderabilityFailure()
                                            : "foreground_changed";
            if (!reason && !game_on_captured_monitor(at)) reason = "game_off_capture_monitor";
            const bool paused = reason != nullptr;
            if (paused != focus_was_paused) {
                if (paused) focus_paused_at = at;
                // Audio-enabled focus resumes start a new generation below.
                const auto source = protected_geometry.AcceptedSource();
                std::ostringstream log;
                log << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\",\"event\":\"wgc_game_focus_changed\""
                    << ",\"paused\":" << (paused ? "true" : "false")
                    << ",\"foreground_match\":" << (foreground ? "true" : "false")
                    << ",\"reason\":\"" << (reason ? reason : "foreground_restored")
                    << "\",\"pause_ms\":" << (at - focus_paused_at) / 10'000
                    << ",\"source_width\":" << source.width << ",\"source_height\":" << source.height
                    << ",\"pool_width\":" << wgc_state_->pool_width << ",\"pool_height\":" << wgc_state_->pool_height
                    << ",\"canvas_width\":" << width_ << ",\"canvas_height\":" << height_
                    << ",\"capture_generation\":" << capture_generation_.load()
                    << ",\"history_retained\":" << (paused || replay_pause_clock_ ? "true" : "false") << '}';
                std::cout << log.str() << std::endl;
                focus_was_paused = paused;
            }
            return focus_policy.Observe(paused);
        };
        int64_t window_resume_since_100ns = 0;
        bool window_source_was_paused = false;
        bool window_restore_pending = false;
        wgc::RecreateAttemptBudget window_recreate_budget(3);
        enum class PoolRecreateResult { Recreated, RetryPending, Fatal };
        const auto fail_window_capture = [this](
            const std::string& detail, bool reconfigure_required) {
            SetCaptureFailure(detail);
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                | (reconfigure_required
                    ? CAPTURE_HEALTH_RECONFIGURE_REQUIRED : 0u));
            ClearReplayForRecovery();
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                      << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                      << "\"source_kind\":\"window\",\"detail\":\""
                      << diagnostics::JsonEscape(detail) << "\"}"
                      << std::endl;
            running_.store(false);
        };
        const auto recreate_window_pool = [this, &window_recreate_budget,
                                             &window_restore_pending,
                                             &latest_texture,
                                             &fail_window_capture](
            uint32_t width, uint32_t height) {
            if (!window_recreate_budget.CanAttempt()) {
                fail_window_capture(
                    "WGC frame-pool resize exceeded its bounded retry budget",
                    false);
                return PoolRecreateResult::Fatal;
            }
            const char* recreate_api = "Direct3D11CaptureFramePool::Recreate";
            try {
                wgc_state_->frame_pool.Recreate(
                    wgc_state_->winrt_device,
                    WgcPixelFormat(source_hdr_),
                    2,
                    winrt::Windows::Graphics::SizeInt32{
                        static_cast<int32_t>(width),
                        static_cast<int32_t>(height)});
                wgc_state_->pool_width = width;
                wgc_state_->pool_height = height;
                wgc_state_->owned_canvas.ResetProcessor();
                latest_texture = nullptr;
                window_restore_pending = true;
                capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"wgc_frame_pool_recreated\","
                          << "\"source_kind\":\"window\",\"width\":" << width
                          << ",\"height\":" << height << '}'
                          << std::endl;
                return PoolRecreateResult::Recreated;
            } catch (const winrt::hresult_error& error) {
                window_recreate_budget.RecordFailure();
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"wgc_frame_pool_recreate_failed\","
                          << "\"source_kind\":\"window\",\"attempt\":"
                          << window_recreate_budget.Attempts()
                          << ",\"api\":\"" << diagnostics::JsonEscape(recreate_api)
                          << "\",\"hr\":" << error.code().value << '}'
                          << std::endl;
                if (wgc::ClassifyRecreateFailure(error.code().value)
                    == wgc::RecreateFailureAction::ReconfigureRequired) {
                    fail_window_capture(
                        "api_call=Direct3D11CaptureFramePool::Recreate "
                        "result=RPC_E_WRONG_THREAD hr=0x8001010E; "
                        "in-place WGC pool resize is unavailable on this WinRT "
                        "apartment; restart capture with a fresh generation",
                        true);
                    return PoolRecreateResult::Fatal;
                }
                if (!window_recreate_budget.CanAttempt()) {
                    fail_window_capture(
                        "WGC frame-pool resize retry budget exhausted", false);
                    return PoolRecreateResult::Fatal;
                }
                return PoolRecreateResult::RetryPending;
            }
        };

        while (running_.load(std::memory_order_relaxed)) {

            next_tick += interval;
            {
                std::unique_lock<std::mutex> lk(wgc_frame_mutex_);
                cadence_wait.WaitUntil(next_tick, wgc_frame_cv_, lk, [this] {
                    return monitor_source_invalidated_.load(std::memory_order_acquire)
                        || (wgc_state_ && wgc_state_->close_signal
                            && wgc_state_->close_signal->closed.load(
                                std::memory_order_acquire))
                        || !running_.load(std::memory_order_relaxed);
                });
            }
            const auto tick_now = std::chrono::steady_clock::now();
            if (tick_now > next_tick + interval) {
                wgc_missed_output_deadlines_.fetch_add(
                    (tick_now - next_tick) / interval, std::memory_order_relaxed);
                next_tick = tick_now;
            }

            if (!running_.load(std::memory_order_relaxed)) break;
            if (target_hwnd_ != 0 && (focus_gated_ || !wgc_state_->monitor_item)
                && !window_source_identity_.Matches(reinterpret_cast<HWND>(target_hwnd_))) {
                SetCaptureFailure("The selected capture window closed or changed process/thread identity.");
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"window_source_identity_lost\",\"state\":\"FAILED\"}"
                          << std::endl;
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                ClearReplayForRecovery();
                running_.store(false);
                break;
            }
            const HMONITOR current_display = wgc_state_->monitor_item
                ? reinterpret_cast<HMONITOR>(resolved_monitor_.hmonitor)
                : MonitorFromWindow(reinterpret_cast<HWND>(target_hwnd_), MONITOR_DEFAULTTONULL);
            const bool display_changed = current_display
                && reinterpret_cast<uintptr_t>(current_display) != color_monitor.hmonitor;
            if (display_changed || tick_now >= next_color_check) {
                next_color_check = tick_now + std::chrono::seconds(1);
                if (display_changed) {
                    const auto topology = monitor_topology_source_.QueryActiveTopology();
                    const auto monitor = std::find_if(topology.monitors.begin(),
                        topology.monitors.end(), [current_display](const auto& entry) {
                            return entry.hmonitor == reinterpret_cast<uintptr_t>(current_display);
                        });
                    if (monitor != topology.monitors.end()) color_monitor = *monitor;
                }
                const auto display_hdr = QueryHdrDisplay(color_monitor);
                if (display_hdr && *display_hdr != source_hdr_) {
                    SetCaptureFailure("Capture source dynamic range changed; restart required.");
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"capture_dynamic_range_changed\","
                              << "\"reconfigure_required\":true}" << std::endl;
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                        | CAPTURE_HEALTH_RECONFIGURE_REQUIRED);
                    ClearReplayForRecovery();
                    running_.store(false);
                    break;
                }
                if (source_hdr_ && !replay_hdr_) {
                    const float white = QuerySdrReferenceWhite(color_monitor);
                    if (white != sdr_reference_white_) {
                        sdr_reference_white_ = white;
                        converted_image_valid = false;
                    }
                }
            }
            if (wgc_state_ && wgc_state_->close_signal
                && wgc_state_->close_signal->closed.load(
                    std::memory_order_acquire)) {
                monitor_source_invalidated_.store(
                    true, std::memory_order_release);
            }
            if (wgc_state_ && !wgc_state_->monitor_item) {
                const HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
                if (!IsWindow(hwnd)) {
                    monitor_source_invalidated_.store(
                        true, std::memory_order_release);
                }
            }
            if (monitor_source_invalidated_.load(std::memory_order_acquire)) {
                const char* source_kind = wgc_state_->monitor_item
                    ? "monitor" : "window";
                std::cerr << "[CaptureThread/WGC] "
                          << monitor::ToString(
                                 monitor::MonitorResolveError::MonitorDisconnected)
                          << ": selected " << source_kind
                          << " capture item closed" << std::endl;
                std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                          << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                          << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                          << "\"api_call\":\"GraphicsCaptureItem::Closed\","
                          << "\"source_kind\":\"" << source_kind << "\","
                          << "\"monitor_device_path\":\""
                          << diagnostics::JsonEscape(diagnostics::WideToUtf8(
                                 resolved_monitor_.monitor_device_path))
                          << "\",\"monitor_adapter_luid\":"
                          << AdapterLuidJson(resolved_monitor_.adapter_luid) << ','
                          << "\"detail\":\"selected capture item closed\"}"
                          << std::endl;
                SetCaptureFailure(std::string("selected WGC ") + source_kind
                    + " capture item closed");
                monitor::WindowsMonitorTopologySource topology;
                monitor::MonitorResolver resolver(topology);
                const bool monitor_still_live = wgc_state_->monitor_item
                    && resolver.Resolve(monitor_device_path_).ok();
                capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                    | (monitor_still_live ? CAPTURE_HEALTH_RECONFIGURE_REQUIRED : 0));
                ClearReplayForRecovery();
                running_.store(false);
                break;
            }

            const char* current_frame_api =
                "Direct3D11CaptureFramePool::TryGetNextFrame";
            try {
                current_frame_api = "ID3D11Device::GetDeviceRemovedReason";
                winrt::check_hresult(device_->GetDeviceRemovedReason());
                LARGE_INTEGER focus_now{};
                QueryPerformanceCounter(&focus_now);
                const auto raw_focus_time = ReplayPauseClock::Scale(
                    focus_now.QuadPart, 10'000'000, focus_frequency.QuadPart);
                const auto focus = observe_game_focus(raw_focus_time);
                if (focus.discard_queued_frames) {
                    content_sampler_.Reset();
                    content_suspicious_streak_.store(0);
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_CONTENT_SUSPECT);
                }
                auto window_state = wgc::WindowState::Active;
                if (!wgc_state_->monitor_item) {
                    const HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
                    RECT client{};
                    const bool has_client_area = GetClientRect(hwnd, &client)
                        && client.right > client.left
                        && client.bottom > client.top
                        && IsWindowVisible(hwnd);
                    window_state = wgc::ClassifyWindowState(
                        window_source_identity_.Matches(hwnd),
                        IsIconic(hwnd) != FALSE,
                        has_client_area);
                    if (window_state == wgc::WindowState::Closed) {
                        monitor_source_invalidated_.store(
                            true, std::memory_order_release);
                        continue;
                    }
                    if (window_state == wgc::WindowState::Paused) {
                        if (!window_source_was_paused) {
                            latest_texture = nullptr;
                            initial_seed_pending = false;
                            window_restore_pending = true;
                            ClearReplayForRecovery();
                            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                                      << "\"event\":\"wgc_window_paused\","
                                      << "\"reason\":\"minimized_or_nonrendering\"}"
                                      << std::endl;
                        }
                        window_source_was_paused = true;
                    } else {
                        const int32_t client_width = client.right - client.left;
                        const int32_t client_height = client.bottom - client.top;
                        const bool client_size_changed =
                            wgc_state_->last_client_width != 0
                            && wgc_state_->last_client_height != 0
                            && (client_width != wgc_state_->last_client_width
                                || client_height != wgc_state_->last_client_height);
                        const bool restored = window_source_was_paused;
                        if (restored || client_size_changed) {
                            if (!window_restore_pending) ClearReplayForRecovery();
                            initial_seed_pending = false;
                            latest_texture = nullptr;
                            window_restore_pending = true;
                            window_resume_since_100ns = raw_focus_time;
                        }
                        window_source_was_paused = false;
                        wgc_state_->last_client_width = client_width;
                        wgc_state_->last_client_height = client_height;
                        if (restored) {
                            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                                      << "\"event\":\"wgc_window_restore_warmup\"}"
                                      << std::endl;
                        }
                    }
                }
                const bool window_paused = window_state == wgc::WindowState::Paused;
                const bool source_paused = focus.paused || window_paused;
                if (replay_pause_clock_ && !wgc_state_->monitor_item) {
                    replay_pause_clock_->Observe(source_paused, raw_focus_time);
                }
                // Keep this raw boundary independent of compact replay time so
                // audio-enabled generations also reject pre-resume WGC frames.
                if (source_paused) foreground_since_100ns = 0;
                else if (!foreground_since_100ns) foreground_since_100ns = raw_focus_time;
                bool focus_resume_started = false;
                const auto begin_replay_after_resume = [this] {
                    if (!replay_pause_clock_) {
                        ClearReplayForRecovery();
                        capture_generation_.fetch_add(1, std::memory_order_acq_rel);
                        LARGE_INTEGER generation_start_qpc{};
                        if (QueryPerformanceCounter(&generation_start_qpc)) {
                            generation_start_qpc_.store(
                                generation_start_qpc.QuadPart,
                                std::memory_order_release);
                        }
                        last_source_frame_qpc_.store(0, std::memory_order_release);
                        last_encoder_submission_qpc_.store(0, std::memory_order_release);
                        last_encoded_output_qpc_.store(0, std::memory_order_release);
                        last_ring_insertion_qpc_.store(0, std::memory_order_release);
                        if (replay_encoder_) replay_encoder_->SetCaptureGeneration(
                            capture_generation_.load(std::memory_order_acquire));
                        if (replay_encoder_) replay_encoder_->RequestKeyframe();
                    } else if (replay_encoder_) {
                        replay_encoder_->RequestKeyframe();
                    }
                };
                if (source_paused || window_restore_pending) {
                    readback_queue.Reset();
                    latest_texture = nullptr;
                    capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                } else if (focus.resumed) {
                    readback_queue.Reset();
                    focus_resume_started = true;
                    latest_texture = nullptr;
                    begin_replay_after_resume();
                    if (focus_gated_) {
                        std::cout << "[CaptureThread/WGC] Game focus restored. "
                                  << (replay_pause_clock_ ? "Continuing video replay."
                                                         : "Starting a fresh audio/video replay generation.")
                                  << std::endl;
                    }
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_PAUSED);
                } else if (!window_restore_pending) {
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_PAUSED);
                }
                bool new_image = false;
                std::optional<winrt::Windows::Graphics::SizeInt32> monitor_frame_size;
                WgcCaptureSize item{}, pool{};
                std::optional<WgcProtectedMonitorResizeDecision> monitor_decision;
                if (adapt_monitor && !source_paused) {
                    current_frame_api = "GraphicsCaptureItem::Size(monitor)";
                    const auto size = wgc_state_->item.Size();
                    item = {size.Width, size.Height};
                    pool = {static_cast<int32_t>(wgc_state_->pool_width),
                            static_cast<int32_t>(wgc_state_->pool_height)};
                }
                uint32_t requested_pool_width = 0;
                uint32_t requested_pool_height = 0;
                // Drain both slots at each tick. Notifications can coalesce;
                // TryGetNextFrame is the sole authority for queued images.
                const auto acquisition_started = CaptureStageTiming::Clock::now();
                for (int slot = 0; slot < (adapt_monitor ? 1 : 2); ++slot) {
                    current_frame_api = "Direct3D11CaptureFramePool::TryGetNextFrame";
                    auto frame = wgc_state_->NextFrame(adapt_monitor);
                    if (!frame) break;
                    wgc::FrameLease frame_lease(frame);
                    if (focus.discard_queued_frames || source_paused) continue;
                    if (window_restore_pending
                        && frame.SystemRelativeTime().count()
                            < window_resume_since_100ns) continue;
                    if (focus_gated_ && frame.SystemRelativeTime().count()
                            < foreground_since_100ns) continue;
                    if (adapt_monitor) {
                        const auto stamp = frame.SystemRelativeTime().count();
                        if (stamp < pool_recreated_at || stamp <= last_monitor_frame_time) continue;
                        last_monitor_frame_time = stamp;
                    }
                    current_frame_api = "Direct3D11CaptureFrame::ContentSize";
                    const auto content_size = frame.ContentSize();
                    if (wgc_state_->monitor_item) {
                        monitor_frame_size = content_size;
                        if (!adapt_monitor && !monitor_geometry.Matches(content_size.Width, content_size.Height))
                            continue;
                        if (adapt_monitor && (content_size.Width <= 0 || content_size.Height <= 0)) {
                            monitor_decision = protected_geometry.Observe(item, pool, {}, {}, true, tick_now);
                            continue;
                        }
                    }
                    if (!wgc_state_->monitor_item) {
                        if (content_size.Width <= 0 || content_size.Height <= 0) {
                            continue;
                        }
                        if (wgc::RequiresEncoderReconfigureForResize(
                                static_cast<uint32_t>(content_size.Width),
                                static_cast<uint32_t>(content_size.Height),
                                width_, height_, configured_target_width_,
                                configured_target_height_, crop_enabled_)) {
                            std::cerr << "[CaptureThread/WGC] window dimensions changed from "
                                      << width_ << "x" << height_ << " to "
                                      << content_size.Width << "x" << content_size.Height
                                      << "; RECONFIGURE_REQUIRED" << std::endl;
                            std::ostringstream detail;
                            detail << "WGC window content requires a fresh encoder "
                                   << "geometry (source=" << content_size.Width << 'x'
                                   << content_size.Height << ", canvas=" << width_ << 'x'
                                   << height_ << ')';
                            fail_window_capture(detail.str(), true);
                            break;
                        }
                        const auto frame_action = wgc::ClassifyFrameAction(
                            static_cast<uint32_t>(content_size.Width),
                            static_cast<uint32_t>(content_size.Height),
                            wgc_state_->pool_width, wgc_state_->pool_height,
                            wgc_state_->pool_width, wgc_state_->pool_height,
                            width_, height_, scaling_mode_ == 1);
                        if (frame_action == wgc::FrameAction::RequiresReconfigure) {
                            std::ostringstream detail;
                            detail << "WGC window source aspect changed in FIT mode; "
                                   << "fresh encoder geometry required (canvas="
                                   << width_ << 'x' << height_ << ", source="
                                   << content_size.Width << 'x' << content_size.Height << ')';
                            fail_window_capture(detail.str(), true);
                            break;
                        }
                        if (frame_action == wgc::FrameAction::RecreatePool) {
                            requested_pool_width = (std::max)(
                                requested_pool_width,
                                static_cast<uint32_t>(content_size.Width));
                            requested_pool_height = (std::max)(
                                requested_pool_height,
                                static_cast<uint32_t>(content_size.Height));
                            latest_texture = nullptr;
                            window_restore_pending = true;
                            continue;
                        }
                        if (frame_action == wgc::FrameAction::Discard) continue;
                    }

                    // Extract ID3D11Texture2D from the WGC surface
                    current_frame_api = "Direct3D11CaptureFrame::Surface";
                    auto surface = frame.Surface();

                    // IDirect3DDxgiInterfaceAccess is a COM interface in
                    // Windows::Graphics::DirectX::Direct3D11 — not a WinRT-projected
                    // type, so winrt::as<>() cannot be used. QueryInterface directly.
                    using DxgiAccess = Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess;
                    winrt::com_ptr<DxgiAccess> interop;
                    current_frame_api =
                        "IDirect3DSurface::QueryInterface(IDirect3DDxgiInterfaceAccess)";
                    winrt::check_hresult(
                        reinterpret_cast<IUnknown*>(winrt::get_abi(surface))->QueryInterface(
                            __uuidof(DxgiAccess), reinterpret_cast<void**>(interop.put())));

                    winrt::com_ptr<ID3D11Texture2D> tex;
                    current_frame_api =
                        "IDirect3DDxgiInterfaceAccess::GetInterface(ID3D11Texture2D)";
                    winrt::check_hresult(interop->GetInterface(IID_PPV_ARGS(tex.put())));
                    if (adapt_monitor) {
                        D3D11_TEXTURE2D_DESC desc{};
                        tex->GetDesc(&desc);
                        const WgcCaptureSize content{content_size.Width, content_size.Height};
                        monitor_decision = protected_geometry.Observe(item, pool, content,
                            {static_cast<int32_t>(desc.Width), static_cast<int32_t>(desc.Height)},
                            true, tick_now);
                        if (monitor_decision->action != WgcProtectedMonitorResizeAction::Capture) continue;
                        // The owned canvas is also the verified in-focus cache. On a 1:1
                        // frame this is a GPU CopyResource; padding is never encoded.
                        auto& normalizer = wgc_state_->owned_canvas;
                        std::string error;
                        if ((!normalizer.Texture() && !normalizer.Initialize(device_, width_, height_, fps_, error))
                            || !normalizer.Copy(context_, tex.get(), content.width, content.height,
                                pool.width, pool.height, scaling_mode_ == 1, error)) {
                            fail_monitor_resize(error, content, 0);
                            break;
                        }
                        latest_texture.copy_from(normalizer.Texture());
                    } else if (wgc_state_->monitor_item) {
                        if (!ValidateCaptureTexture(tex.get(), "WGC")) {
                            ClearReplayForRecovery();
                            capture_health_flags_.fetch_or(CAPTURE_HEALTH_BACKEND_FAILED);
                            running_.store(false);
                            break;
                        }
                        if (!latest_texture) {
                            D3D11_TEXTURE2D_DESC desc{};
                            tex->GetDesc(&desc);
                            desc.Usage = D3D11_USAGE_DEFAULT;
                            // Intel's video processor rejects SRV-only monitor textures.
                            desc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
                            desc.CPUAccessFlags = 0;
                            desc.MiscFlags = 0;
                            current_frame_api = "ID3D11Device::CreateTexture2D(latest WGC image)";
                            winrt::check_hresult(device_->CreateTexture2D(
                                &desc, nullptr, latest_texture.put()));
                        }
                        context_->CopyResource(latest_texture.get(), tex.get());
                    } else {
                        D3D11_TEXTURE2D_DESC source_description{};
                        tex->GetDesc(&source_description);
                        const auto source_action = wgc::ClassifyFrameAction(
                            static_cast<uint32_t>(content_size.Width),
                            static_cast<uint32_t>(content_size.Height),
                            wgc_state_->pool_width, wgc_state_->pool_height,
                            source_description.Width, source_description.Height,
                            width_, height_, scaling_mode_ == 1);
                        if (source_action == wgc::FrameAction::RequiresReconfigure) {
                            std::ostringstream detail;
                            detail << "WGC window source aspect changed in FIT mode; "
                                   << "fresh encoder geometry required (canvas="
                                   << width_ << 'x' << height_ << ", source="
                                   << content_size.Width << 'x' << content_size.Height << ')';
                            fail_window_capture(detail.str(), true);
                            break;
                        }
                        if (source_action == wgc::FrameAction::RecreatePool) {
                            requested_pool_width = (std::max)(
                                requested_pool_width,
                                static_cast<uint32_t>(content_size.Width));
                            requested_pool_height = (std::max)(
                                requested_pool_height,
                                static_cast<uint32_t>(content_size.Height));
                            latest_texture = nullptr;
                            window_restore_pending = true;
                            continue;
                        }
                        if (source_action == wgc::FrameAction::Discard) continue;
                        current_frame_api = "OwnedCanvasNormalizer::Copy";
                        std::string normalize_error;
                        if (!wgc_state_->CopyToCanvas(
                                context_, tex.get(),
                                static_cast<uint32_t>(content_size.Width),
                                static_cast<uint32_t>(content_size.Height),
                                scaling_mode_ == 1, normalize_error)) {
                            fail_window_capture(
                                "api_call=OwnedCanvasNormalizer::Copy detail="
                                    + normalize_error,
                                false);
                            break;
                        }
                        latest_texture.copy_from(
                            wgc_state_->CanvasTexture());
                    }
                    source_textures_received_.fetch_add(1, std::memory_order_relaxed);
                    new_image = true;
                } // Close every borrowed frame before encoding the owned copy.
                wgc_acquisition_timings_.RecordSince(acquisition_started);
                if (source_paused) initial_seed_pending = false;
                if (initial_seed_pending && !source_paused) {
                    if (!new_image && latest_texture) {
                        // The one startup frame was already copied into owned
                        // storage on the creation thread. Count it as one
                        // source observation if no newer frame arrived first.
                        source_textures_received_.fetch_add(
                            1, std::memory_order_relaxed);
                        new_image = true;
                    }
                    initial_seed_pending = false;
                }
                if (!running_.load(std::memory_order_relaxed)) break;
                if (requested_pool_width != 0 && requested_pool_height != 0) {
                    readback_queue.Reset();
                    latest_texture = nullptr;
                    const auto result = recreate_window_pool(
                        requested_pool_width, requested_pool_height);
                    if (result == PoolRecreateResult::Fatal) break;
                    continue;
                }
                if (source_paused) {
                    if (wgc_state_->monitor_item) {
                        monitor_geometry.Reset();
                        protected_geometry.Reset();
                        resize_started.reset();
                        capture_health_flags_.fetch_and(~CAPTURE_HEALTH_RECOVERING);
                        if (replay_pause_clock_)
                            replay_pause_clock_->Observe(true, raw_focus_time);
                    }
                    continue;
                }
                if (adapt_monitor) {
                    if (!monitor_decision) {
                        monitor_decision = protected_geometry.Observe(item, pool, {}, {}, false, tick_now);
                    }
                    const auto& decision = *monitor_decision;
                    const auto observed = monitor_frame_size
                        ? WgcCaptureSize{monitor_frame_size->Width, monitor_frame_size->Height} : item;
                    const auto elapsed_ms = resize_started
                        ? std::chrono::duration_cast<std::chrono::milliseconds>(tick_now - *resize_started).count() : 0;
                    if (decision.action == WgcProtectedMonitorResizeAction::Reconfigure) {
                        fail_monitor_resize(decision.reason, observed, elapsed_ms);
                        break;
                    }
                    if (protected_geometry.Pending() && !resize_started) {
                        resize_started = tick_now;
                        log_monitor_event("wgc_monitor_resize_pending", "waiting_for_geometry",
                                          observed, true, 0);
                    }
                    if (decision.recreate_pool.Positive()) {
                        // No WGC frame or borrowed surface is live here. Preserve
                        // the independent canvas while growing the source pool.
                        const auto requested = decision.recreate_pool;
                        current_frame_api = "Direct3D11CaptureFramePool::Recreate(monitor)";
                        try {
                            wgc_state_->frame_pool.Recreate(wgc_state_->winrt_device,
                                WgcPixelFormat(source_hdr_), 2, {requested.width, requested.height});
                            wgc_state_->pool_width = requested.width;
                            wgc_state_->pool_height = requested.height;
                            wgc_state_->owned_canvas.ResetProcessor();
                            readback_queue.Reset();
                            converted_image_valid = false;
                            LARGE_INTEGER recreated{};
                            QueryPerformanceCounter(&recreated);
                            pool_recreated_at = ReplayPauseClock::Scale(recreated.QuadPart, 10'000'000,
                                                                        focus_frequency.QuadPart);
                            log_monitor_event("wgc_monitor_pool_recreated", "content_exceeds_pool",
                                              requested, true, elapsed_ms);
                        } catch (winrt::hresult_error const& e) {
                            winrt::check_hresult(device_->GetDeviceRemovedReason());
                            const auto error = diagnostics::FormatHResultFailure(current_frame_api, e.code().value);
                            log_monitor_event("wgc_monitor_pool_recreate_failed", error.c_str(),
                                              requested, true, elapsed_ms);
                        }
                    }
                    if (new_image && (decision.source_changed || (resize_started && !protected_geometry.Pending()))) {
                        const auto source = protected_geometry.AcceptedSource();
                        protected_monitor_source_size_.store(source, std::memory_order_relaxed);
                        readback_queue.Reset();
                        converted_image_valid = false;
                        if (replay_encoder_) replay_encoder_->RequestKeyframe();
                        log_monitor_event("wgc_monitor_resize_accepted",
                            decision.source_changed ? "source_size_changed" : "fresh_source_restored",
                            source, true, elapsed_ms);
                    }
                    if (!protected_geometry.Pending()) resize_started.reset();
                    // Only real focus loss compacts a video-only replay. During
                    // resize, cadence and audio both continue on the active clock.
                    if (replay_pause_clock_) replay_pause_clock_->Observe(false, raw_focus_time);
                    if (!new_image && !decision.allow_cached_frame) continue;
                } else if (wgc_state_->monitor_item) {
                    current_frame_api = "GraphicsCaptureItem::Size(monitor)";
                    const auto item_size = wgc_state_->item.Size();
                    // Either source can lead the display transition. Require
                    // both to agree before accepting a matching fresh frame.
                    const auto observed = monitor_geometry.Matches(
                        item_size.Width, item_size.Height) && monitor_frame_size
                        ? *monitor_frame_size : item_size;
                    const auto geometry = monitor_geometry.Observe(
                        observed.Width, observed.Height, new_image, tick_now);
                    if (replay_pause_clock_)
                        replay_pause_clock_->Observe(
                            geometry == WgcMonitorGeometryAction::Wait
                                || geometry == WgcMonitorGeometryAction::Reconfigure,
                            raw_focus_time);
                    if (geometry == WgcMonitorGeometryAction::Wait) {
                        capture_health_flags_.fetch_or(CAPTURE_HEALTH_RECOVERING);
                        latest_texture = nullptr;
                        readback_queue.Reset();
                        converted_image_valid = false;
                        continue;
                    }
                    if (geometry == WgcMonitorGeometryAction::Reconfigure) {
                        SetCaptureFailure(
                            "WGC monitor geometry requires a fresh capture generation.");
                        capture_health_flags_.store(WgcSizeMismatchHealthFlags(true));
                        ClearReplayForRecovery();
                        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                                  << "\"event\":\"wgc_monitor_reconfiguration_required\","
                                  << "\"state\":\"RECOVERING\","
                                  << "\"expected_width\":" << width_ << ','
                                  << "\"expected_height\":" << height_ << ','
                                  << "\"actual_width\":" << observed.Width << ','
                                  << "\"actual_height\":" << observed.Height << '}'
                                  << std::endl;
                        running_.store(false);
                        break;
                    }
                    if (geometry == WgcMonitorGeometryAction::Resume) {
                        // Matching geometry keeps the encoder and any active
                        // recording on the same generation.
                        if (replay_encoder_) replay_encoder_->RequestKeyframe();
                        capture_health_flags_.fetch_and(~CAPTURE_HEALTH_RECOVERING);
                    }
                }
                if (window_restore_pending && !new_image) {
                    capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                    continue;
                }
                if (window_restore_pending && new_image) {
                    if (!focus_resume_started) begin_replay_after_resume();
                    window_restore_pending = false;
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_PAUSED);
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"wgc_window_first_valid_frame_after_resume\","
                              << "\"canvas_width\":" << width_
                              << ",\"canvas_height\":" << height_
                              << ",\"configured_output_width\":" << target_width_
                              << ",\"configured_output_height\":" << target_height_
                              << ",\"replay_warmup_required\":true"
                              << '}'
                              << std::endl;
                }
                if (!latest_texture) continue;
                // Focus may change while draining the frame pool. Never submit
                // that tick's monitor image after the protected game loses focus.
                if (target_hwnd_ != 0 && (focus_gated_ || !wgc_state_->monitor_item)
                    && !window_source_identity_.Matches(reinterpret_cast<HWND>(target_hwnd_))) {
                    latest_texture = nullptr;
                    continue;
                }
                LARGE_INTEGER before_submit{};
                if (focus_gated_) QueryPerformanceCounter(&before_submit);
                const auto submit_focus_time = focus_gated_
                    ? ReplayPauseClock::Scale(before_submit.QuadPart, 10'000'000, focus_frequency.QuadPart) : 0;
                if (focus_gated_ && observe_game_focus(submit_focus_time).paused) {
                    latest_texture = nullptr;
                    // This loss occurred after the early observation. Feed it
                    // back so a rapid return next tick still performs resume
                    // warmup and rejects images queued while out of focus.
                    protected_geometry.Reset();
                    resize_started.reset();
                    readback_queue.Reset();
                    converted_image_valid = false;
                    foreground_since_100ns = 0;
                    capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                    content_sampler_.Reset();
                    if (replay_pause_clock_) replay_pause_clock_->Observe(true, submit_focus_time);
                    continue;
                }

                if (!wgc_state_->monitor_item) {
                    const HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
                    RECT client{};
                    const bool has_client_area = GetClientRect(hwnd, &client)
                        && client.right > client.left
                        && client.bottom > client.top
                        && IsWindowVisible(hwnd);
                    const auto state = wgc::ClassifyWindowState(
                        window_source_identity_.Matches(hwnd),
                        IsIconic(hwnd) != FALSE,
                        has_client_area);
                    if (state == wgc::WindowState::Closed) {
                        monitor_source_invalidated_.store(
                            true, std::memory_order_release);
                        continue;
                    }
                    if (state == wgc::WindowState::Paused) {
                        readback_queue.Reset();
                        latest_texture = nullptr;
                        initial_seed_pending = false;
                        if (!window_source_was_paused) {
                            ClearReplayForRecovery();
                            window_restore_pending = true;
                            window_source_was_paused = true;
                            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                                      << "\"event\":\"wgc_window_paused\","
                                      << "\"reason\":\"minimized_or_nonrendering\"}"
                                      << std::endl;
                        }
                        capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                        continue;
                    }
                    const int32_t client_width = client.right - client.left;
                    const int32_t client_height = client.bottom - client.top;
                    const bool client_size_changed =
                        wgc_state_->last_client_width != 0
                        && wgc_state_->last_client_height != 0
                        && (client_width != wgc_state_->last_client_width
                            || client_height != wgc_state_->last_client_height);
                    if (client_size_changed) {
                        readback_queue.Reset();
                        wgc_state_->last_client_width = client_width;
                        wgc_state_->last_client_height = client_height;
                        latest_texture = nullptr;
                        initial_seed_pending = false;
                        if (!window_restore_pending) ClearReplayForRecovery();
                        window_restore_pending = true;
                        window_resume_since_100ns = raw_focus_time;
                        capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                        continue;
                    }
                    if (window_source_was_paused) {
                        readback_queue.Reset();
                        window_source_was_paused = false;
                        window_restore_pending = true;
                        window_resume_since_100ns = raw_focus_time;
                        latest_texture = nullptr;
                        capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                        continue;
                    }
                    wgc_state_->last_client_width = client_width;
                    wgc_state_->last_client_height = client_height;
                }
                capture_health_flags_.fetch_or(CAPTURE_HEALTH_ACTIVE);
                if (new_image && !wgc_state_->monitor_item) {
                    window_recreate_budget.ObserveStableFrameProgress();
                }
                if (!new_image) {
                    wgc_repeated_frame_observations_.fetch_add(
                        1, std::memory_order_relaxed);
                }
                ID3D11Texture2D* encode_source = latest_texture.get();
                if (source_hdr_) {
                    if (replay_hdr_) SampleContentTexture(encode_source,
                        frames_captured_.load(std::memory_order_relaxed) + 1);
                    auto& converter = *wgc_state_->color_converter;
                    if (new_image || !converted_image_valid) {
                        const CaptureScaleRect full{0, 0, width_, height_};
                        const CaptureScaleRect source = replay_hdr_
                            ? CaptureScaleRect{crop_x_, crop_y_, crop_width_, crop_height_} : full;
                        const auto destination = replay_hdr_ ? hdr_geometry.destination : full;
                        const auto scale_started = CaptureStageTiming::Clock::now();
                        const HRESULT hr = converter.Convert(context_, encode_source,
                            source, destination, sdr_reference_white_);
                        wgc_scale_timings_.RecordSince(scale_started);
                        if (FAILED(hr)) {
                            const auto error = diagnostics::FormatHResultFailure(
                                "scRGB frame conversion", hr);
                            SetCaptureFailure(error);
                            FailReplayEncoder("scRGB frame conversion");
                            break;
                        }
                        converted_image_valid = true;
                    }
                    encode_source = converter.Texture();
                }
                LARGE_INTEGER now;
                QueryPerformanceCounter(&now);
                const auto replay_qpc = replay_pause_clock_
                    ? replay_pause_clock_->MapQpc(now.QuadPart, focus_frequency.QuadPart)
                    : now.QuadPart;
                if (new_image && now.QuadPart > 0) {
                    // Cached WGC images are encoded repeatedly for pacing, but
                    // only a newly acquired image advances source progress.
                    last_source_frame_qpc_.store(
                        now.QuadPart, std::memory_order_release);
                }

                if (nvenc_active_ && !replay_encoder_cpu_input_) {
                    // Same-adapter compressed replay (native NVENC or AMF).
                    // A GPU CopyResource feeds the encoder-owned texture.
                    const auto scale_started = CaptureStageTiming::Clock::now();
                    ID3D11Texture2D* encode_texture = replay_hdr_ ? encode_source
                        : PrepareEncodeTexture(encode_source);
                    if (!replay_hdr_) wgc_scale_timings_.RecordSince(scale_started);
                    if (!encode_texture) break;
                    const auto submit_started = CaptureStageTiming::Clock::now();
                    const bool encoded = EncodeGpuReplayTexture(encode_texture, replay_qpc,
                        frames_captured_.load(std::memory_order_relaxed) + 1);
                    wgc_submit_timings_.RecordSince(submit_started);
                    if (!encoded) break;
                }
                else if (nvenc_active_ && replay_encoder_cpu_input_
                    && staging_texture_) {
                    // NVENC CPU-input path (Optimus).
                    // Read back from the capture adapter for CPU transfer to NVIDIA.
                    ID3D11Texture2D* staging = replay_hdr_
                        ? wgc_state_->hdr_staging.get() : staging_texture_;
                    const auto scale_started = CaptureStageTiming::Clock::now();
                    ID3D11Texture2D* readback_source = PrepareHybridReadbackTexture(encode_source, "WGC");
                    if (hybrid_bgra_scaler_) wgc_scale_timings_.RecordSince(scale_started);
                    if (!readback_source) break;
                    const auto readback_started = CaptureStageTiming::Clock::now();
                    auto input_qpc = replay_qpc;
                    if (alternate_staging) {
                        ID3D11Texture2D* slots[] = {staging, alternate_staging.get()};
                        const auto work = readback_queue.Queue(replay_qpc,
                            capture_generation_.load(std::memory_order_acquire));
                        const auto copy_started = trace_enabled
                            ? CaptureStageTiming::Clock::now() : CaptureStageTiming::Clock::time_point{};
                        context_->CopyResource(slots[work.write_slot], readback_source);
                        if (trace_enabled) wgc_readback_copy_timings_.RecordSince(copy_started);
                        // Send this tick's copy before waiting for the previous
                        // one, so the GPU can complete it during cadence wait.
                        context_->Flush();
                        if (!work.read) continue;
                        staging = slots[work.read->slot];
                        input_qpc = work.read->qpc;
                    } else {
                        const auto copy_started = trace_enabled
                            ? CaptureStageTiming::Clock::now() : CaptureStageTiming::Clock::time_point{};
                        context_->CopyResource(staging, readback_source);
                        if (trace_enabled) wgc_readback_copy_timings_.RecordSince(copy_started);
                    }

                    D3D11_MAPPED_SUBRESOURCE mapped{};
                    const auto map_started = trace_enabled
                        ? CaptureStageTiming::Clock::now() : CaptureStageTiming::Clock::time_point{};
                    HRESULT hr = context_->Map(staging, 0, D3D11_MAP_READ, 0, &mapped);
                    if (trace_enabled) wgc_readback_map_timings_.RecordSince(map_started);
                    wgc_readback_timings_.RecordSince(readback_started);
                    if (FAILED(hr)) {
                        if (hybrid_bgra_scaler_) {
                            FailHybridReadback("WGC", "scaled staging Map",
                                diagnostics::FormatHResultFailure("ID3D11DeviceContext::Map", hr));
                            break;
                        }
                        std::cerr << "[CaptureThread/WGC] Texture map failed (Optimus): 0x"
                                  << std::hex << hr << std::dec << std::endl;
                        continue;
                    }
                    const uint32_t readback_width = hybrid_bgra_scaler_ ? target_width_ : width_;
                    if (replay_hdr_ ? mapped.RowPitch < target_width_ * 2
                                    : !ValidateMappedCaptureRowPitch(readback_width, mapped.RowPitch, "WGC")) {
                        context_->Unmap(staging, 0);
                        ClearReplayForRecovery();
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                        break;
                    }

                    const auto* input = static_cast<const uint8_t*>(mapped.pData);
                    if (!replay_hdr_ && !hybrid_bgra_scaler_) {
                        SampleContentBGRA(input, mapped.RowPitch, width_, height_,
                            frames_captured_.load(std::memory_order_relaxed) + 1);
                    }
                    const uint8_t* encode_data = (replay_hdr_ || hybrid_bgra_scaler_) ? input
                        : CropMappedData(input, mapped.RowPitch);
                    if (now.QuadPart > 0) {
                        last_encoder_submission_qpc_.store(
                            now.QuadPart, std::memory_order_release);
                    }
                    bool encoded = false;
                    const auto submit_started = CaptureStageTiming::Clock::now();
                    for (uint32_t attempt = 1; attempt <= 3 && !encoded; ++attempt) {
                        encoded = replay_encoder_->EncodeFrameCPU(
                            encode_data,
                            mapped.RowPitch, input_qpc);
                        if (!encoded && attempt < 3) {
                            std::cerr << "[CaptureThread/WGC] Transient hybrid encoder "
                                      << "submission failure; retrying (" << attempt
                                      << "/3)." << std::endl;
                            std::this_thread::sleep_for(std::chrono::milliseconds(2));
                        }
                    }
                    wgc_submit_timings_.RecordSince(submit_started);
                    context_->Unmap(staging, 0);
                    if (!encoded) {
                        FailReplayEncoder("hybrid NVENC CPU-input submission");
                        break;
                    }
                }
                else {
                    // Legacy raw-frame readback
                    const auto readback_started = CaptureStageTiming::Clock::now();
                    context_->CopyResource(staging_texture_, encode_source);

                    D3D11_MAPPED_SUBRESOURCE mapped{};
                    HRESULT hr = context_->Map(staging_texture_, 0, D3D11_MAP_READ, 0, &mapped);
                    wgc_readback_timings_.RecordSince(readback_started);
                    if (FAILED(hr)) {
                        std::cerr << "[CaptureThread/WGC] Texture map failed: 0x"
                                  << std::hex << hr << std::dec << std::endl;
                        continue;
                    }
                    if (!ValidateMappedCaptureRowPitch(width_, mapped.RowPitch, "WGC")) {
                        context_->Unmap(staging_texture_, 0);
                        ClearReplayForRecovery();
                        capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
                        running_.store(false);
                        break;
                    }

                    const uint8_t* full_src = static_cast<const uint8_t*>(mapped.pData);
                    const uint8_t* src = CropMappedData(full_src, mapped.RowPitch);
                    const size_t   row = static_cast<size_t>(crop_width_) * 4;
                    const size_t   write_pos = ring_head_.load(std::memory_order_relaxed);
                    const size_t   slot_idx  = write_pos % max_frames_;
                    uint8_t*       dst = frame_pool_.GetSlot(slot_idx);

                    SampleContentBGRA(
                        full_src, mapped.RowPitch, width_, height_,
                        frames_captured_.load(std::memory_order_relaxed) + 1);

                    if (mapped.RowPitch == static_cast<UINT>(row)) {
                        std::memcpy(dst, src, row * crop_height_);
                    } else {
                        for (uint32_t y = 0; y < crop_height_; y++) {
                            std::memcpy(dst + y * row, src + y * mapped.RowPitch, row);
                        }
                    }

                    context_->Unmap(staging_texture_, 0);

                    ring_head_.fetch_add(1, std::memory_order_release);
                    size_t prev = ring_count_.load(std::memory_order_relaxed);
                    if (prev < max_frames_)
                        ring_count_.fetch_add(1, std::memory_order_relaxed);
                }

                frames_captured_.fetch_add(1, std::memory_order_relaxed);
                consecutive_frame_errors = 0;

            } catch (winrt::hresult_error const& e) {
                const HRESULT hr = e.code().value;
                const bool reconfigure = hr == DXGI_ERROR_DEVICE_REMOVED
                    || hr == DXGI_ERROR_DEVICE_RESET
                    || hr == DXGI_ERROR_ACCESS_LOST;
                std::cerr << "[CaptureThread/WGC] Frame error: 0x"
                          << std::hex << hr << std::dec << std::endl;
                if (++consecutive_frame_errors >= 30 || reconfigure) {
                    SetCaptureFailure(diagnostics::FormatHResultFailure(current_frame_api, hr));
                    std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                              << "\"event\":\"runtime_failed\","
                              << "\"state\":\"FAILED\","
                              << "\"error_code\":\"CAPTURE_WGC_RUNTIME_FAILED\","
                              << "\"native_failure\":"
                              << diagnostics::HResultFailureJson(
                                     current_frame_api, hr)
                              << ",\"reconfigure_required\":"
                              << (reconfigure ? "true" : "false")
                              << ",\"consecutive_failures\":"
                              << consecutive_frame_errors << "}"
                              << std::endl;
                    capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                        | (reconfigure ? CAPTURE_HEALTH_RECONFIGURE_REQUIRED : 0u));
                    ClearReplayForRecovery();
                    running_.store(false);
                }
            }
        }

        std::cout << "[CaptureThread/WGC] Stopped. Frames captured: "
                  << frames_captured_.load() << " repeated idle frames: "
                  << wgc_repeated_frame_observations_.load(
                         std::memory_order_relaxed) << std::endl;
        if (!(capture_health_flags_.load() & CAPTURE_HEALTH_BACKEND_FAILED))
            capture_health_flags_.store(CAPTURE_HEALTH_NONE);

        winrt::uninit_apartment();
    }


    bool CaptureEngine::InitializeMinecraftJavaCapture() {
        startup_capture_backend_ = WindowsCaptureBackendName(
            WindowsCaptureBackend::MinecraftJavaGame);
        const HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
        if (!window_source_identity_.HasProcessWatch() || !window_source_identity_.IsRenderable()) {
            SetCaptureFailure("The selected Minecraft Java window is unavailable. "
                              "Restore the game window and try again.");
            return false;
        }
        const auto [width, height] = MinecraftJavaClientSize(hwnd);
        if (!width || !height) {
            SetCaptureFailure("The selected Minecraft Java window has unsupported dimensions.");
            return false;
        }
        width_ = width;
        height_ = height;
        if (!InitializeWindowCaptureDevice(hwnd, false)) return false;

        auto state = std::make_unique<MinecraftJavaState>();
        D3D11_TEXTURE2D_DESC description{};
        description.Width = width_;
        description.Height = height_;
        description.MipLevels = 1;
        description.ArraySize = 1;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.Usage = D3D11_USAGE_DEFAULT;
        description.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        const HRESULT texture_result = device_->CreateTexture2D(&description, nullptr,
            state->upload.put());
        if (FAILED(texture_result)) {
            SetCaptureFailure(diagnostics::FormatHResultFailure(
                "CreateTexture2D for Minecraft Java Game Capture", texture_result));
            return false;
        }
        std::string error;
        if (!state->Start(hwnd, width_, height_, fps_, capture_cursor_, error)) {
            SetCaptureFailure(std::move(error));
            return false;
        }

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(6);
        do {
            if (!window_source_identity_.IsRenderable()) {
                SetCaptureFailure("The selected Minecraft Java window closed or stopped rendering.");
                return false;
            }
            error = state->Failure();
            if (!error.empty()) {
                SetCaptureFailure(std::move(error));
                return false;
            }
            if (state->Ready()) {
                minecraft_java_state_ = std::move(state);
                return true;
            }
            if (WaitForSingleObject(state->frame_event.get(), 50) == WAIT_FAILED) {
                SetCaptureFailure("Could not wait for the first Minecraft Java Game Capture frame.");
                return false;
            }
        } while (std::chrono::steady_clock::now() < deadline);
        SetCaptureFailure("Minecraft Java did not provide a Game Capture frame in time. "
            "Bring the game to the foreground and check that it uses the same graphics adapter.");
        return false;
    }

    void CaptureEngine::CaptureThreadMinecraftJava() {
        auto& state = *minecraft_java_state_;
        const HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
        const uint32_t stride = width_ * 4;
        const auto fail = [this](std::string message, bool reconfigure = false) {
            SetCaptureFailure(std::move(message));
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED
                | (reconfigure ? CAPTURE_HEALTH_RECONFIGURE_REQUIRED : 0u));
            ClearReplayForRecovery();
            running_.store(false);
            std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                      << "\"event\":\"runtime_failed\",\"state\":\"FAILED\","
                      << "\"error_code\":\"CAPTURE_TARGET_UNAVAILABLE\","
                      << "\"backend\":\"OBS_GAME_CAPTURE\",\"reconfigure_required\":"
                      << (reconfigure ? "true" : "false") << ",\"detail\":\""
                      << diagnostics::JsonEscape(last_capture_failure_detail_) << "\"}"
                      << std::endl;
        };

        // The readiness frame arrived before the encoder and audio were ready.
        if (state.TakeFrame()) state.ReleaseFrame();
        bool paused = false;
        const auto canvas_size = std::make_pair(width_, height_);
        std::pair<uint32_t, uint32_t> pending_size{};
        auto stable_since = std::chrono::steady_clock::now();
        while (running_.load(std::memory_order_acquire)) {
            capture_loop_iterations_.fetch_add(1, std::memory_order_relaxed);
            if (auto error = state.Failure(); !error.empty()) {
                fail(std::move(error));
                break;
            }
            if (!window_source_identity_.Matches(hwnd)) {
                fail("The selected Minecraft Java window closed or changed owner.");
                break;
            }

            const auto size = MinecraftJavaClientSize(hwnd);
            const bool renderable = size.first && size.second
                && !IsIconic(hwnd) && IsWindowVisible(hwnd);
            if (renderable && size != canvas_size) {
                const auto now = std::chrono::steady_clock::now();
                if (size != pending_size) {
                    pending_size = size;
                    stable_since = now;
                } else if (now - stable_since >= std::chrono::milliseconds(400)) {
                    fail("Minecraft Java resized from " + std::to_string(width_) + 'x'
                        + std::to_string(height_) + " to " + std::to_string(size.first)
                        + 'x' + std::to_string(size.second) + ".", true);
                    break;
                }
            } else {
                pending_size = {};
            }

            capture_acquire_attempts_.fetch_add(1, std::memory_order_relaxed);
            const auto* frame = state.TakeFrame();
            if (!renderable && !paused) {
                paused = true;
                capture_health_flags_.fetch_or(CAPTURE_HEALTH_PAUSED);
                ClearReplayForRecovery();
            }
            if (!frame) {
                const DWORD result = WaitForSingleObject(state.frame_event.get(), 50);
                if (result == WAIT_FAILED) {
                    fail("Could not wait for Minecraft Java Game Capture frames.");
                    break;
                }
                if (result == WAIT_TIMEOUT) {
                    capture_timeouts_.fetch_add(1, std::memory_order_relaxed);
                }
                continue;
            }

            if (!renderable || paused) {
                state.ReleaseFrame();
                if (renderable) {
                    paused = false;
                    replay_encoder_->RequestKeyframe();
                    capture_health_flags_.fetch_and(~CAPTURE_HEALTH_PAUSED);
                }
                continue;
            }

            LARGE_INTEGER now{};
            QueryPerformanceCounter(&now);
            capture_acquire_successes_.fetch_add(1, std::memory_order_relaxed);
            source_textures_received_.fetch_add(1, std::memory_order_relaxed);
            last_source_frame_qpc_.store(now.QuadPart, std::memory_order_release);
            const uint64_t frame_number = frames_captured_.load(std::memory_order_relaxed) + 1;
            bool encoded = false;
            if (replay_encoder_cpu_input_) {
                SampleContentBGRA(frame, stride, width_, height_, frame_number);
                last_encoder_submission_qpc_.store(now.QuadPart, std::memory_order_release);
                encoded = replay_encoder_->EncodeFrameCPU(
                    CropMappedData(frame, stride), stride, now.QuadPart);
                state.ReleaseFrame();
                capture_frames_released_.fetch_add(1, std::memory_order_relaxed);
                if (!encoded || replay_config_publish_failed_.load()) {
                    FailReplayEncoder("Minecraft Java BGRA frame submission");
                    break;
                }
            } else {
                context_->UpdateSubresource(state.upload.get(), 0, nullptr, frame, stride, 0);
                state.ReleaseFrame();
                capture_frames_released_.fetch_add(1, std::memory_order_relaxed);
                encoded = EncodeGpuReplayTexture(PrepareEncodeTexture(state.upload.get()),
                    now.QuadPart, frame_number);
                if (!encoded) break;
            }
            frames_captured_.fetch_add(1, std::memory_order_relaxed);
        }
        if (!(capture_health_flags_.load() & CAPTURE_HEALTH_BACKEND_FAILED)) {
            capture_health_flags_.store(CAPTURE_HEALTH_NONE);
        }
    }

    bool CaptureEngine::InitializeWindowCaptureDevice(HWND hwnd, bool observe_display_hdr) {
        // A window can move between GPUs on hybrid systems. Resolve the
        // monitor Windows currently assigns to this HWND and bind the capture
        // D3D11 device to that monitor's adapter LUID. Never use adapter 0 or
        // the primary display as an implicit fallback.
        const HMONITOR window_monitor = MonitorFromWindow(
            hwnd, MONITOR_DEFAULTTONULL);
        if (!window_monitor) {
            SetCaptureFailure("api_call=MonitorFromWindow result=null");
            return false;
        }
        const auto topology = monitor_topology_source_.QueryActiveTopology();
        const auto window_monitor_it = std::find_if(
            topology.monitors.begin(), topology.monitors.end(),
            [window_monitor](const monitor::MonitorTopologyEntry& entry) {
                return entry.hmonitor == reinterpret_cast<uintptr_t>(window_monitor);
            });
        if (!topology.ok() || window_monitor_it == topology.monitors.end()) {
            SetCaptureFailure(
                "api_call=WindowsMonitorTopologySource::QueryActiveTopology "
                "reason=window_monitor_not_resolved");
            return false;
        }
        resolved_monitor_ = *window_monitor_it;
        // The HWND's monitor is authoritative for this capture generation.
        // Persist its identity before starting the source so every later
        // backend resolves the same physical display instead of the originally
        // configured or primary monitor.
        monitor_device_path_ = monitor::NormalizeMonitorDevicePath(
            resolved_monitor_.monitor_device_path);

        // Create one D3D11 device and keep replay encoding on that exact adapter.
        // Do not enumerate another vendor merely because it exists elsewhere in
        // the machine; cross-adapter window capture is not alpha-qualified.
        D3D_FEATURE_LEVEL feature_level{};
        IDXGIFactory1* window_factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(
            __uuidof(IDXGIFactory1),
            reinterpret_cast<void**>(&window_factory));
        IDXGIAdapter1* window_adapter = nullptr;
        if (SUCCEEDED(hr) && window_factory) {
            for (UINT index = 0; ; ++index) {
                IDXGIAdapter1* candidate = nullptr;
                const HRESULT enumerated = window_factory->EnumAdapters1(index, &candidate);
                if (enumerated == DXGI_ERROR_NOT_FOUND) break;
                if (FAILED(enumerated) || !candidate) {
                    hr = enumerated;
                    if (candidate) candidate->Release();
                    break;
                }
                DXGI_ADAPTER_DESC1 description{};
                const HRESULT described = candidate->GetDesc1(&description);
                if (SUCCEEDED(described)
                    && !(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                    && description.AdapterLuid.LowPart
                        == static_cast<LONG>(resolved_monitor_.adapter_luid.low_part)
                    && description.AdapterLuid.HighPart
                        == resolved_monitor_.adapter_luid.high_part) {
                    window_adapter = candidate;
                    break;
                }
                candidate->Release();
            }
        }
        if (!window_adapter) {
            if (window_factory) window_factory->Release();
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIFactory1::EnumAdapters1(window monitor adapter)",
                FAILED(hr) ? hr : DXGI_ERROR_NOT_FOUND);
            SetCaptureFailure(failure + " reason=window_monitor_adapter_not_found");
            return false;
        }
        source_hdr_ = observe_display_hdr
            && QueryHdrDisplay(resolved_monitor_, window_adapter).value_or(false);
        sdr_reference_white_ = source_hdr_ ? QuerySdrReferenceWhite(resolved_monitor_) : 1.0f;
        hr = D3D11CreateDevice(
            window_adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0,
            nullptr, 0, D3D11_SDK_VERSION,
            &device_, &feature_level, &context_);
        window_adapter->Release();
        window_factory->Release();
        if (FAILED(hr)) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "D3D11CreateDevice(window capture)", hr);
            SetCaptureFailure(failure);
            std::cerr << '[' << startup_capture_backend_ << "] " << failure << std::endl;
            return false;
        }
        capture_adapter_vendor_ = QueryD3D11DeviceVendor(device_);
        nvidia_device_ = capture_adapter_vendor_ == EncoderVendor::Nvidia;
        std::string capture_luid_diagnostic;
        capture_device_adapter_luid_available_ = QueryD3D11DeviceAdapterLuid(
            device_, capture_device_adapter_luid_, capture_luid_diagnostic);
        if (!capture_device_adapter_luid_available_) {
            std::cerr << '[' << startup_capture_backend_ << "] Could not resolve capture device adapter LUID: "
                      << capture_luid_diagnostic << std::endl;
        }

        return true;
    }

    // Create WGC capture for target_hwnd_ using the window content dimensions.
    // The engine must restart if resizing changes the encoder input dimensions.

    bool CaptureEngine::InitializeWindowCapture() {
        startup_capture_backend_ = "WGC_WINDOW";
        try {
            if (!winrt::Windows::Graphics::Capture::GraphicsCaptureSession::IsSupported()) {
                std::cerr << "[WinCapture] GraphicsCaptureSession not supported" << std::endl;
                SetCaptureFailure(
                    "api_call=GraphicsCaptureSession::IsSupported result=false");
                return false;
            }
        } catch (winrt::hresult_error const& error) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "GraphicsCaptureSession::IsSupported", error.code().value);
            SetCaptureFailure(failure);
            std::cerr << "[WinCapture] " << failure << std::endl;
            return false;
        } catch (...) {
            SetCaptureFailure(
                "api_call=GraphicsCaptureSession::IsSupported exception=unknown");
            std::cerr << "[WinCapture] IsSupported() threw — WGC unavailable" << std::endl;
            return false;
        }

        HWND hwnd = reinterpret_cast<HWND>(target_hwnd_);
        if (!window_source_identity_.Matches(hwnd)) {
            SetCaptureFailure("api_call=IsWindow result=false");
            std::cerr << "[WinCapture] HWND 0x" << std::hex << target_hwnd_
                      << std::dec << " is not a valid window" << std::endl;
            return false;
        }
        RECT initial_client{};
        const bool initially_renderable = GetClientRect(hwnd, &initial_client)
            && initial_client.right > initial_client.left
            && initial_client.bottom > initial_client.top
            && IsWindowVisible(hwnd);
        if (wgc::ClassifyWindowState(IsWindow(hwnd) != FALSE,
                IsIconic(hwnd) != FALSE, initially_renderable)
            != wgc::WindowState::Active) {
            SetCaptureFailure(
                "selected window is minimized, hidden, or has no client area; "
                "restore it and retry capture");
            return false;
        }

        if (!InitializeWindowCaptureDevice(hwnd, true)) return false;

        // Steps 3-8: WinRT capture session (exception-safe)
        wgc_state_ = std::make_unique<WGCState>();
        const char* current_api = "ID3D11Device::QueryInterface(IDXGIDevice)";
        try {
            winrt::com_ptr<IDXGIDevice> dxgi_dev;
            winrt::check_hresult(device_->QueryInterface(dxgi_dev.put()));
            winrt::com_ptr<IInspectable> insp;
            current_api = "CreateDirect3D11DeviceFromDXGIDevice";
            winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi_dev.get(), insp.put()));
            current_api = "IInspectable::as(IDirect3DDevice)";
            wgc_state_->winrt_device =
                insp.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();

            current_api = "RoGetActivationFactory(GraphicsCaptureItem)";
            auto item_interop = winrt::get_activation_factory<
                winrt::Windows::Graphics::Capture::GraphicsCaptureItem,
                IGraphicsCaptureItemInterop>();
            current_api = "IGraphicsCaptureItemInterop::CreateForWindow";
            winrt::check_hresult(item_interop->CreateForWindow(
                hwnd,
                winrt::guid_of<winrt::Windows::Graphics::Capture::GraphicsCaptureItem>(),
                winrt::put_abi(wgc_state_->item)));
            current_api = "GraphicsCaptureItem::Closed(add handler)";
            monitor_source_invalidated_.store(false, std::memory_order_release);
            wgc_state_->close_signal =
                std::make_shared<wgc::ItemClosedSignal>();
            const auto close_signal = wgc_state_->close_signal;
            wgc_state_->item_closed_token = wgc_state_->item.Closed(
                [close_signal](auto&, auto&) {
                    close_signal->closed.store(true, std::memory_order_release);
                });
            wgc_state_->item_closed_registered = true;

            current_api = "GraphicsCaptureItem::Size";
            auto sz = wgc_state_->item.Size();
            RECT current_client{};
            const bool currently_renderable = GetClientRect(hwnd, &current_client)
                && current_client.right > current_client.left
                && current_client.bottom > current_client.top
                && IsWindowVisible(hwnd);
            if (wgc::ClassifyWindowState(IsWindow(hwnd) != FALSE,
                    IsIconic(hwnd) != FALSE, currently_renderable)
                != wgc::WindowState::Active) {
                SetCaptureFailure(
                    "selected window became minimized, hidden, or nonrendering "
                    "during startup; restore it and retry capture");
                wgc_state_.reset();
                if (context_) { context_->Release(); context_ = nullptr; }
                if (device_) { device_->Release(); device_ = nullptr; }
                nvidia_device_ = false;
                capture_adapter_vendor_ = EncoderVendor::Software;
                return false;
            }
            // GraphicsCaptureItem::Size is an allocation estimate only. The
            // first delivered frame's ContentSize is authoritative for the
            // encoder canvas and may differ from both this estimate and the
            // underlying texture dimensions.
            if (sz.Width <= 0 || sz.Height <= 0) {
                SetCaptureFailure(
                    "api_call=GraphicsCaptureItem::Size result=invalid_pool_estimate");
                wgc_state_.reset();
                if (context_) { context_->Release(); context_ = nullptr; }
                if (device_) { device_->Release(); device_ = nullptr; }
                nvidia_device_ = false;
                capture_adapter_vendor_ = EncoderVendor::Software;
                return false;
            }
            wgc_state_->pool_width = static_cast<uint32_t>(sz.Width);
            wgc_state_->pool_height = static_cast<uint32_t>(sz.Height);
            wgc_state_->last_client_width = current_client.right - current_client.left;
            wgc_state_->last_client_height = current_client.bottom - current_client.top;
            std::cout << "[WinCapture] WGC pool allocation estimate: "
                      << wgc_state_->pool_width << "x" << wgc_state_->pool_height
                      << std::endl;

            // Use the same free-threaded, two-slot pool as monitor capture.
            current_api = "Direct3D11CaptureFramePool::CreateFreeThreaded";
            wgc_state_->frame_pool =
                winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool::CreateFreeThreaded(
                    wgc_state_->winrt_device,
                    WgcPixelFormat(source_hdr_),
                    2,
                    sz);

            // Apply the optional border opt-out before StartCapture. A missing
            // or denied opt-out retains this requested HWND and the OS privacy
            // border; only a required session failure rejects window capture.
            current_api = "Direct3D11CaptureFramePool::CreateCaptureSession";
            wgc_state_->session =
                wgc_state_->frame_pool.CreateCaptureSession(wgc_state_->item);
            if (!ApplyCaptureBorderPolicy("window")) {
                std::cerr << "[WinCapture] Required WGC window session unavailable; "
                             "the requested HWND remains required; no monitor/DXGI fallback."
                          << std::endl;
                ShutdownWGC();
                ShutdownD3D11();
                return false;
            }


            current_api = "GraphicsCaptureSession::IsCursorCaptureEnabled";
            ApplyCaptureCursorPolicy();

            ConfigureWgcSessionCadence(wgc_state_->session, fps_, "window");
            current_api = "GraphicsCaptureSession::StartCapture";
            wgc_state_->session.StartCapture();

            // Establish canonical encoder geometry from an actual WGC frame
            // before the encoder is initialized. Size() above only sized the
            // first pool allocation and is intentionally never polled by the
            // capture thread for window sources.
            current_api = "Initial WGC frame ContentSize wait";
            const auto first_frame_deadline = std::chrono::steady_clock::now()
                + std::chrono::milliseconds(1500);
            uint32_t pool_recreate_attempts = 0;
            bool got_initial_frame = false;
            while (std::chrono::steady_clock::now() < first_frame_deadline
                   && !got_initial_frame) {
                RECT client{};
                const bool renderable = GetClientRect(hwnd, &client)
                    && client.right > client.left
                    && client.bottom > client.top
                    && IsWindowVisible(hwnd);
                if (wgc::ClassifyWindowState(IsWindow(hwnd) != FALSE,
                        IsIconic(hwnd) != FALSE, renderable)
                    != wgc::WindowState::Active) {
                    current_api = "Initial WGC window state";
                    throw winrt::hresult_error(E_ABORT,
                        winrt::hstring(
                            L"selected window became minimized, hidden, or closed "
                            L"before the first valid capture frame"));
                }
                auto frame = wgc_state_->frame_pool.TryGetNextFrame();
                if (!frame) {
                    Sleep(5);
                    continue;
                }

                uint32_t requested_pool_width = 0;
                uint32_t requested_pool_height = 0;
                std::string initial_copy_error;
                {
                    wgc::FrameLease frame_lease(frame);
                    current_api = "Direct3D11CaptureFrame::ContentSize(initial)";
                    const auto content_size = frame.ContentSize();
                    if (content_size.Width > 0 && content_size.Height > 0) {
                        current_api = "Direct3D11CaptureFrame::Surface(initial)";
                        auto surface = frame.Surface();
                        using DxgiAccess = Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess;
                        winrt::com_ptr<DxgiAccess> interop;
                        current_api = "IDirect3DSurface::QueryInterface(initial)";
                        winrt::check_hresult(
                            reinterpret_cast<IUnknown*>(winrt::get_abi(surface))->QueryInterface(
                                __uuidof(DxgiAccess),
                                reinterpret_cast<void**>(interop.put())));
                        winrt::com_ptr<ID3D11Texture2D> texture;
                        current_api = "IDirect3DDxgiInterfaceAccess::GetInterface(initial)";
                        winrt::check_hresult(
                            interop->GetInterface(IID_PPV_ARGS(texture.put())));
                        D3D11_TEXTURE2D_DESC texture_description{};
                        texture->GetDesc(&texture_description);
                        const auto content_width =
                            static_cast<uint32_t>(content_size.Width);
                        const auto content_height =
                            static_cast<uint32_t>(content_size.Height);
                        if (content_width > wgc_state_->pool_width
                            || content_height > wgc_state_->pool_height
                            || content_width > texture_description.Width
                            || content_height > texture_description.Height) {
                            requested_pool_width = content_width;
                            requested_pool_height = content_height;
                        } else {
                            width_ = content_width;
                            height_ = content_height;
                            current_api = "OwnedCanvasNormalizer::Initialize(initial)";
                            if (source_hdr_) {
                                wgc_state_->hdr_canvas = std::make_unique<HdrFrameConverter>();
                                winrt::check_hresult(wgc_state_->hdr_canvas->Initialize(
                                    device_, width_, height_, HdrFrameConverter::Output::ScRgb));
                            } else if (!wgc_state_->owned_canvas.Initialize(
                                    device_, width_, height_, fps_, initial_copy_error)) {
                                throw winrt::hresult_error(
                                    E_FAIL, winrt::to_hstring(initial_copy_error));
                            }
                            current_api = "OwnedCanvasNormalizer::Copy(initial)";
                            if (!wgc_state_->CopyToCanvas(
                                    context_, texture.get(), content_width,
                                    content_height, scaling_mode_ == 1, initial_copy_error)) {
                                throw winrt::hresult_error(
                                    E_FAIL, winrt::to_hstring(initial_copy_error));
                            }
                            wgc_state_->initial_owned_image_pending = true;
                            got_initial_frame = true;
                        }
                    }
                }
                // Do not keep a frame, surface, or source texture reference
                // across Recreate; WGC requires every checked-out lease closed.
                frame = nullptr;
                if (requested_pool_width != 0 && requested_pool_height != 0) {
                    if (pool_recreate_attempts >= 3) {
                        current_api = "Direct3D11CaptureFramePool::Recreate(initial)";
                        throw winrt::hresult_error(E_FAIL, winrt::hstring(
                            L"initial ContentSize exceeded the frame pool after "
                            L"three bounded recreates"));
                    }
                    ++pool_recreate_attempts;
                    current_api = "Direct3D11CaptureFramePool::Recreate(initial)";
                    wgc_state_->frame_pool.Recreate(
                        wgc_state_->winrt_device,
                        WgcPixelFormat(source_hdr_),
                        2,
                        winrt::Windows::Graphics::SizeInt32{
                            static_cast<int32_t>(requested_pool_width),
                            static_cast<int32_t>(requested_pool_height)});
                    wgc_state_->pool_width = requested_pool_width;
                    wgc_state_->pool_height = requested_pool_height;
                    continue;
                }
            }
            if (!got_initial_frame) {
                current_api = "Initial WGC frame ContentSize wait";
                throw winrt::hresult_error(
                    HRESULT_FROM_WIN32(ERROR_TIMEOUT),
                    winrt::hstring(
                        L"no valid WGC frame arrived within the bounded 1500 ms startup wait"));
            }
            RECT first_frame_client{};
            if (GetClientRect(hwnd, &first_frame_client)
                && first_frame_client.right > first_frame_client.left
                && first_frame_client.bottom > first_frame_client.top) {
                wgc_state_->last_client_width =
                    first_frame_client.right - first_frame_client.left;
                wgc_state_->last_client_height =
                    first_frame_client.bottom - first_frame_client.top;
            }
            std::cout << "[WinCapture] First valid WGC ContentSize: "
                      << width_ << "x" << height_ << std::endl;

        } catch (winrt::hresult_error const& e) {
            std::string failure = diagnostics::FormatHResultFailure(
                current_api, e.code().value);
            const std::string winrt_message = winrt::to_string(e.message());
            if (!winrt_message.empty()) {
                failure += " winrt_message=\"";
                failure += diagnostics::JsonEscape(winrt_message);
                failure += '\"';
            }
            SetCaptureFailure(failure);
            std::cerr << "[WinCapture] " << failure << std::endl;
            wgc_state_.reset();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_)  { device_->Release();  device_  = nullptr; }
            nvidia_device_ = false;
            capture_adapter_vendor_ = EncoderVendor::Software;
            return false;
        }

        std::cout << "[WinCapture] Ready  "
                  << width_ << "x" << height_ << "  "
                  << EncoderVendorName(capture_adapter_vendor_)
                  << " capture adapter"
                  << std::endl;
        last_capture_failure_detail_.clear();
        return true;
    }


    // InitializeD3D11 - exact selected adapter/output fallback

    bool CaptureEngine::InitializeD3D11() {
        startup_capture_backend_ = "DXGI_OUTPUT_DUPLICATION";
        nvidia_device_ = false;
        capture_adapter_vendor_ = EncoderVendor::Software;
        IDXGIOutput* selected_output = nullptr;
        if (!InitializeMonitorCaptureDevice("DXGI", &selected_output)) {
            return false;
        }
        last_capture_failure_detail_.clear();

        if (source_hdr_) {
            selected_output->Release();
            SetCaptureFailure("The selected HDR display requires WGC scRGB capture.");
            return false;
        }
        IDXGIOutput1* output1 = nullptr;
        HRESULT hr = selected_output->QueryInterface(
            __uuidof(IDXGIOutput1), reinterpret_cast<void**>(&output1));
        if (FAILED(hr) || !output1) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIOutput::QueryInterface(IDXGIOutput1)", hr);
            SetCaptureFailure(failure);
            std::cerr << "[D3D11] " << failure << std::endl;
            selected_output->Release();
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            return false;
        }
        hr = output1->DuplicateOutput(device_, &duplication_);
        output1->Release();
        selected_output->Release();
        if (FAILED(hr) || !duplication_) {
            const std::string failure = diagnostics::FormatHResultFailure(
                "IDXGIOutput1::DuplicateOutput", hr);
            SetCaptureFailure(failure);
            std::cerr << "[D3D11] "
                      << monitor::ToString(
                             monitor::MonitorResolveError::OutputResolutionFailed)
                      << ": " << failure << std::endl;
            if (context_) { context_->Release(); context_ = nullptr; }
            if (device_) { device_->Release(); device_ = nullptr; }
            return false;
        }

        std::cout << "[D3D11] Ready - " << width_ << "x" << height_
                  << (nvidia_device_
                      ? "  [selected NVIDIA adapter - GPU zero-copy enabled]"
                      : "  [selected display-owning adapter]")
                  << std::endl;
        std::cout << "[D3D11] Duplication: " << (duplication_ ? "OK" : "NULL") << std::endl;
        std::cout << "[D3D11] Readback texture: deferred until fallback policy"
                  << std::endl;
        std::string cursor_detail;
        if (!dxgi_cursor_composer_.Initialize(
                device_, context_, width_, height_, cursor_detail)) {
            SetCaptureFailure(
                cursor_detail.empty()
                    ? "DXGI cursor compositor initialization failed"
                    : cursor_detail);
            std::cerr << "[D3D11] " << last_capture_failure_detail_
                      << std::endl;
            ShutdownD3D11();
            return false;
        }
        last_capture_failure_detail_.clear();
        return true;
    }


    // ShutdownD3D11 - unchanged

    void CaptureEngine::ShutdownD3D11() {
        InvalidateDxgiReplayCache();
        dxgi_cursor_composer_.Reset();
        content_sampler_.Reset();
        hybrid_bgra_scaler_.reset();
        if (crop_texture_) { crop_texture_->Release(); crop_texture_ = nullptr; }
        if (staging_texture_) { staging_texture_->Release(); staging_texture_ = nullptr; }
        if (duplication_)     { duplication_->Release();     duplication_ = nullptr; }
        if (context_)         { context_->Release();         context_ = nullptr; }
        if (device_)          { device_->Release();          device_ = nullptr; }
        nvidia_device_ = false;
        capture_adapter_vendor_ = EncoderVendor::Software;
        // nvenc_device_ / nvenc_context_ are intentionally NOT released here.
        // They must outlive the NVENC encoder session (which is finalized in Shutdown()
        // before this is called). On ACCESS_LOST reinit they stay valid.
    }


} // namespace fthr
