#include "capture_engine.h"
#include <winrt/base.h>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

int wmain(int argc, wchar_t** argv) {
    if (argc < 3) {
        std::wcerr << L"Usage: capture-smoke.exe <monitor> <clip.mp4> "
                      L"[--backend auto|dxgi|wgc] [--manual]" << std::endl;
        return 2;
    }
    bool test_recording = false;
    std::wstring requested_backend = L"auto";
    for (int index = 3; index < argc; ++index) {
        if (std::wstring(argv[index]) == L"--manual") {
            test_recording = true;
        } else if (std::wstring(argv[index]) == L"--backend"
                   && index + 1 < argc) {
            requested_backend = argv[++index];
        } else {
            std::wcerr << L"SMOKE_ERROR stage=arguments unknown_or_incomplete="
                       << argv[index] << std::endl;
            return 2;
        }
    }
    winrt::init_apartment(winrt::apartment_type::multi_threaded);
    fthr::CaptureConfig config;
    config.monitor_device_path = argv[1];
    config.encoder_preference = fthr::EncoderPreference::Nvidia;
    if (requested_backend == L"auto") {
        config.capture_backend_preference =
            fthr::WindowsCaptureBackendPreference::Auto;
    } else if (requested_backend == L"dxgi") {
        config.capture_backend_preference =
            fthr::WindowsCaptureBackendPreference::Dxgi;
    } else if (requested_backend == L"wgc") {
        config.capture_backend_preference =
            fthr::WindowsCaptureBackendPreference::Wgc;
    } else {
        std::wcerr << L"SMOKE_ERROR stage=arguments invalid_backend="
                   << requested_backend << std::endl;
        return 2;
    }
    config.buffer_seconds = 15;
    config.bitrate_kbps = 16000;
    config.audio_enabled = true;
    fthr::CaptureEngine engine;
    if (!engine.Initialize(config)) {
        std::cerr << "SMOKE_ERROR stage=initialize native_startup_code="
                  << fthr::ReplayStartupErrorName(
                         engine.GetLastStartupErrorCode())
                  << " detail=" << engine.GetLastStartupError() << std::endl;
        return 3;
    }
    std::cout << "SMOKE requested_backend="
              << fthr::WindowsCaptureBackendPreferenceName(
                     config.capture_backend_preference)
              << " capture_backend=" << engine.GetStartupCaptureBackend()
              << std::endl;
    auto recording = std::filesystem::path(argv[2]);
    recording.replace_extension(L".recording.mp4");
    uint64_t last = 0;
    for (int second = 0; second < 15; ++second) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        const auto stats = engine.GetStats();
        std::cout << "SMOKE second=" << second + 1
                  << " frames=" << stats.frames_captured
                  << " packets=" << stats.ring_used_frames
                  << " health=" << engine.GetCaptureHealthFlags() << std::endl;
        if (second > 2 && stats.frames_captured <= last) {
            std::cerr << "SMOKE_ERROR stage=capture_progress no_new_frames="
                      << stats.frames_captured << " second=" << second + 1
                      << std::endl;
            engine.Shutdown();
            return 4;
        }
        last = stats.frames_captured;
        if (test_recording && second == 2 && !engine.StartRecording(recording.c_str())) {
            std::cerr << "SMOKE_ERROR stage=manual_recording_start detail="
                      << engine.GetLastRecordingError() << std::endl;
            engine.Shutdown();
            return 7;
        }
        if (test_recording && second == 10) {
            const auto start = std::chrono::steady_clock::now();
            const bool stopped = engine.StopRecording();
            std::cout << "SMOKE manual_stopped=" << stopped << " close_ms="
                      << std::chrono::duration_cast<std::chrono::milliseconds>(
                             std::chrono::steady_clock::now() - start).count() << std::endl;
            if (!stopped) {
                std::cerr << "SMOKE_ERROR stage=manual_recording_stop detail="
                          << engine.GetLastRecordingError() << std::endl;
                engine.Shutdown();
                return 8;
            }
        }
    }
    fthr::SharedMemoryLayout response{};
    if (!engine.SaveClip(argv[2], 10, &response)) {
        std::wcerr << L"SMOKE_ERROR stage=save_admission detail="
                   << response.engine_string << std::endl;
        engine.Shutdown();
        return 5;
    }
    for (int tick = 0; tick < 200 && response.engine_response == fthr::ResponseType::NONE; ++tick)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    const bool saved = response.engine_response == fthr::ResponseType::CLIP_SAVED;
    std::cout << "SMOKE saved=" << saved << std::endl;
    if (!saved) {
        std::wcerr << L"SMOKE_ERROR stage=save_completion response="
                   << static_cast<uint32_t>(response.engine_response)
                   << L" detail=" << response.engine_string << std::endl;
    }
    engine.Shutdown();
    return saved ? 0 : 6;
}
