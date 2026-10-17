#pragma once

#include <Windows.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <condition_variable>
#include <mutex>
#include <iostream>

namespace fthr {

// Condition-variable timeouts on Windows can round a 4-7 ms frame wait to
// a system timer tick. Use a thread-owned timer without timeBeginPeriod's
// process-wide effects. The CV remains the interruptible fallback.
class WgcCadenceWait {
public:
    explicit WgcCadenceWait(bool enable_high_resolution = true) {
        if (enable_high_resolution) {
            using CreateTimer = HANDLE (WINAPI*)(
                LPSECURITY_ATTRIBUTES, LPCWSTR, DWORD, DWORD);
            const auto module = GetModuleHandleW(L"kernel32.dll");
            const auto create = module ? reinterpret_cast<CreateTimer>(
                GetProcAddress(module, "CreateWaitableTimerExW")) : nullptr;
            if (create) timer_ = create(nullptr, nullptr, 0x00000002u,
                                       TIMER_ALL_ACCESS);
        }
    }
    ~WgcCadenceWait() { CloseTimer(); }
    WgcCadenceWait(const WgcCadenceWait&) = delete;
    WgcCadenceWait& operator=(const WgcCadenceWait&) = delete;

    bool HighResolution() const { return timer_ != nullptr; }

    template<class Interrupted>
    void WaitUntil(std::chrono::steady_clock::time_point deadline,
                   std::condition_variable& cv,
                   std::unique_lock<std::mutex>& lock,
                   Interrupted interrupted) {
        using namespace std::chrono;
        while (!interrupted()) {
            const auto now = steady_clock::now();
            if (now >= deadline) return;
            if (!timer_) {
                cv.wait_until(lock, deadline, interrupted);
                return;
            }
            // Bound shutdown/source-close latency even at very low FPS.
            const auto remaining = (std::min)(deadline - now,
                duration_cast<steady_clock::duration>(milliseconds(10)));
            LARGE_INTEGER due{};
            due.QuadPart = -(std::max)(int64_t{1},
                static_cast<int64_t>(ceil<duration<int64_t, std::ratio<1, 10000000>>>(
                    remaining).count()));
            if (!SetWaitableTimer(timer_, &due, 0, nullptr, nullptr, FALSE)) {
                DisableTimer(GetLastError());
                continue;
            }
            lock.unlock();
            const DWORD result = WaitForSingleObject(timer_, 10);
            const DWORD error = result == WAIT_FAILED ? GetLastError() : ERROR_GEN_FAILURE;
            lock.lock();
            if (result != WAIT_OBJECT_0 && result != WAIT_TIMEOUT) {
                DisableTimer(error);
            }
            // A signal/timeout never authorizes an early frame. Recheck the
            // deadline and interruption predicate on every bounded wait.
        }
    }

private:
    void CloseTimer() {
        if (timer_) {
            CancelWaitableTimer(timer_);
            CloseHandle(timer_);
            timer_ = nullptr;
        }
    }
    void DisableTimer(DWORD error) {
        std::cout << "FTHR_DIAGNOSTIC_EVENT {\"subsystem\":\"capture\","
                  << "\"event\":\"wgc_cadence_timer_disabled\","
                  << "\"win32_error\":" << error << "}" << std::endl;
        CloseTimer();
    }
    HANDLE timer_ = nullptr;
};

} // namespace fthr
