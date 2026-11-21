#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>

#include <cstdint>
#include <mutex>
#include <sstream>

namespace fthr::frame_pacing_trace {

inline int64_t Qpc() noexcept {
    LARGE_INTEGER counter{};
    QueryPerformanceCounter(&counter);
    return counter.QuadPart;
}

inline int64_t Frequency() noexcept {
    static const int64_t frequency = [] {
        LARGE_INTEGER counter{};
        QueryPerformanceFrequency(&counter);
        return counter.QuadPart;
    }();
    return frequency;
}

class TraceFile {
public:
    TraceFile() noexcept {
        wchar_t path[32768];
        const DWORD length = GetEnvironmentVariableW(
            L"FTHR_FRAME_PACING_TRACE", path, static_cast<DWORD>(_countof(path)));
        if (length > 0 && length < _countof(path)) {
            file_ = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ, nullptr,
                OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        }
    }

    ~TraceFile() {
        if (Enabled()) CloseHandle(file_);
    }

    bool Enabled() const noexcept { return file_ != INVALID_HANDLE_VALUE; }

    void Write(const std::string& line) {
        std::lock_guard<std::mutex> lock(mutex_);
        DWORD written = 0;
        WriteFile(file_, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
    }

private:
    HANDLE file_ = INVALID_HANDLE_VALUE;
    std::mutex mutex_;
};

inline TraceFile& File() noexcept {
    static TraceFile trace;
    return trace;
}

inline bool Enabled() noexcept { return File().Enabled(); }

inline int64_t Now(bool enabled) noexcept { return enabled ? Qpc() : 0; }

template <typename Fields>
void Emit(const char* event, uint32_t task_id, int64_t completed_qpc, Fields fields) noexcept {
    if (!Enabled()) return;
    try {
        std::ostringstream line;
        line << "{\"event\":\"" << event << "\",\"pid\":" << GetCurrentProcessId()
             << ",\"tid\":" << GetCurrentThreadId() << ",\"task_id\":" << task_id
             << ",\"qpc\":" << completed_qpc << ",\"qpc_frequency\":" << Frequency();
        fields(line);
        line << "}\n";
        File().Write(line.str());
    } catch (...) {
        // Diagnostics must never change save or capture behavior.
    }
}

}  // namespace fthr::frame_pacing_trace
