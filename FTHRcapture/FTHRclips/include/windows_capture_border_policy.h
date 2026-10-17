// Compatibility policy for the Windows Graphics Capture session border.

#pragma once
#ifndef FTHR_WINDOWS_CAPTURE_BORDER_POLICY_H
#define FTHR_WINDOWS_CAPTURE_BORDER_POLICY_H

#include <cstdint>

namespace fthr {

enum class CaptureBorderPolicyReason : uint8_t {
    NotWgc,
    SessionUnavailable,
    SessionOptOutAttemptRequired,
    SessionInterfaceUnavailable,
    SessionPropertyFailed,
    BorderStillRequired,
    BorderlessEnabled,
};

struct CaptureBorderPolicyInput {
    bool wgc_session = true;
    bool session_available = true;
    bool session_interface_checked = false;
    bool session_interface_available = false;
    bool property_attempted = false;
    bool property_set_succeeded = false;
    bool border_required_after_attempt = true;
};

struct CaptureBorderPolicyDecision {
    bool capture_allowed = false;
    bool attempt_disable_border = false;
    bool effective_borderless = false;
    CaptureBorderPolicyReason reason =
        CaptureBorderPolicyReason::SessionOptOutAttemptRequired;
};

CaptureBorderPolicyDecision EvaluateCaptureBorderPolicy(
    const CaptureBorderPolicyInput& input);
const char* CaptureBorderPolicyReasonName(CaptureBorderPolicyReason reason);

}  // namespace fthr

#endif  // FTHR_WINDOWS_CAPTURE_BORDER_POLICY_H
