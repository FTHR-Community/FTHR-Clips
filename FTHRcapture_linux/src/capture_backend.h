#pragma once
#include "capture_engine.h"
#include "capture_backend_policy.h"
#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

namespace fthr {

// How Initialize() failed. The engine waits out the transient kinds and
// spends its bounded retry budget only on genuine errors.
enum class BackendFailureKind {
    None,
    Error,                  // genuine failure: counts toward the retry budget
    CompositorUnreachable,  // display connect or registry roundtrip failed
    OutputAbsent,           // the selected output is not advertised right now
    UserDeclined,           // portal dialog declined or unanswered: never retry
};

// Raw frame delivered synchronously by CaptureFrame().
// data pointer is valid only until the next CaptureFrame() call.
struct RawFrame {
    const uint8_t* data         = nullptr;
    uint32_t       stride       = 0;
    uint32_t       width        = 0;
    uint32_t       height       = 0;
    int            av_pix_fmt   = 0;   // AV_PIX_FMT_* constant
    int64_t        timestamp_ns = 0;   // CLOCK_MONOTONIC nanoseconds
};

class ICaptureBackend {
public:
    virtual ~ICaptureBackend() = default;

    // One-time setup. Returns false and logs on failure.
    virtual bool Initialize(const CaptureConfig& cfg) = 0;

    // Synchronously waits within the backend's bounded frame deadline. Returns
    // false on timeout, cancellation, disconnect, or another backend error.
    virtual bool CaptureFrame(RawFrame& out) = 0;

    virtual void Shutdown() = 0;

    virtual BackendType Type()       const = 0;
    virtual uint32_t NativeWidth()  const = 0;
    virtual uint32_t NativeHeight() const = 0;

    // Why the last Initialize() failed; Error unless the backend knows better.
    BackendFailureKind InitFailureKind() const noexcept { return init_failure_kind_; }
    // Words for the UI, when the backend has them. Empty otherwise.
    virtual std::string FailureReason() const { return {}; }
    // During recovery the user's output is the only acceptable source;
    // first-time selection may still fall back to the first output.
    void SetStrictOutput(bool strict) noexcept { strict_output_ = strict; }

protected:
    BackendFailureKind init_failure_kind_ = BackendFailureKind::Error;
    bool strict_output_ = false;
};

// Why CreateBestBackend() returned nullptr, worded for the UI. Empty when
// capture was merely cancelled. retry_pointless marks failures that only user
// action can resolve (a dismissed portal dialog); the engine's bounded
// recovery would otherwise re-open that dialog on every attempt.
struct BackendFailure {
    std::string reason;
    BackendFailureKind kind = BackendFailureKind::None;
    bool retry_pointless = false;   // == kind is UserDeclined
};

// Creates and initialises exactly one backend type with no fallback. Returns
// nullptr on failure or cancellation; `failure` (optional) then says why.
std::unique_ptr<ICaptureBackend> CreateBackend(
    BackendType type,
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure,
    bool strict_output);

// First-time selection: tries BackendCandidates() in order, or the single
// type named by FTHR_CAPTURE_BACKEND. Returns nullptr if no backend works or
// capture cancellation is requested; `failure` (optional) then explains why.
std::unique_ptr<ICaptureBackend> CreateBestBackend(
    const CaptureConfig& cfg,
    const std::atomic<bool>* running,
    BackendFailure* failure = nullptr);

} // namespace fthr
