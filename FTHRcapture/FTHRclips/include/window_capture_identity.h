#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

namespace fthr {

// A numeric HWND can be reused after game exit. Retain the selected process
// object where access permits it, and always verify HWND/PID/TID together.
class WindowCaptureIdentity {
public:
    ~WindowCaptureIdentity() { Reset(); }
    WindowCaptureIdentity() = default;
    WindowCaptureIdentity(const WindowCaptureIdentity&) = delete;
    WindowCaptureIdentity& operator=(const WindowCaptureIdentity&) = delete;

    bool Bind(HWND window) noexcept {
        Reset();
        if (!window || !IsWindow(window)) return false;
        thread_ = GetWindowThreadProcessId(window, &process_);
        if (!thread_ || !process_) { Reset(); return false; }
        window_ = window;
        // Protected processes may deny this optional lifetime observation.
        // The WGC capture-item Closed signal and PID/TID checks still apply.
        process_handle_ = OpenProcess(SYNCHRONIZE, FALSE, process_);
        return Matches(window);
    }

    bool Matches(HWND window) const noexcept {
        if (!window_ || window != window_ || !IsWindow(window)) return false;
        DWORD process = 0;
        const DWORD thread = GetWindowThreadProcessId(window, &process);
        if (process != process_ || thread != thread_) return false;
        return !process_handle_ || WaitForSingleObject(process_handle_, 0) == WAIT_TIMEOUT;
    }

    bool HasProcessWatch() const noexcept { return process_handle_ != nullptr; }
    bool IsRenderable() const noexcept {
        RECT client{};
        return Matches(window_) && !IsIconic(window_) && IsWindowVisible(window_)
            && GetClientRect(window_, &client) && client.right > client.left
            && client.bottom > client.top;
    }
    void Reset() noexcept {
        if (process_handle_) CloseHandle(process_handle_);
        process_handle_ = nullptr;
        window_ = nullptr;
        process_ = thread_ = 0;
    }

private:
    HWND window_ = nullptr;
    DWORD process_ = 0, thread_ = 0;
    HANDLE process_handle_ = nullptr;
};

} // namespace fthr
