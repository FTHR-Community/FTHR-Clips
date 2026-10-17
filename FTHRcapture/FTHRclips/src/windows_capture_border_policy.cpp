
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "windows_capture_border_policy.h"

namespace fthr {

CaptureBorderPolicyDecision EvaluateCaptureBorderPolicy(
        const CaptureBorderPolicyInput& input) {
    CaptureBorderPolicyDecision decision;
    if (!input.wgc_session) {
        decision.capture_allowed = true;
        decision.reason = CaptureBorderPolicyReason::NotWgc;
        return decision;
    }

    if (!input.session_available) {
        decision.reason = CaptureBorderPolicyReason::SessionUnavailable;
        return decision;
    }

    decision.capture_allowed = true;

    if (!input.session_interface_checked) {
        decision.attempt_disable_border = true;
        decision.reason = CaptureBorderPolicyReason::SessionOptOutAttemptRequired;
        return decision;
    }
    if (!input.session_interface_available) {
        decision.reason = CaptureBorderPolicyReason::SessionInterfaceUnavailable;
        return decision;
    }
    if (!input.property_attempted || !input.property_set_succeeded) {
        decision.reason = CaptureBorderPolicyReason::SessionPropertyFailed;
        return decision;
    }
    if (input.border_required_after_attempt) {
        decision.reason = CaptureBorderPolicyReason::BorderStillRequired;
        return decision;
    }

    decision.effective_borderless = true;
    decision.reason = CaptureBorderPolicyReason::BorderlessEnabled;
    return decision;
}

const char* CaptureBorderPolicyReasonName(CaptureBorderPolicyReason reason) {
    switch (reason) {
    case CaptureBorderPolicyReason::NotWgc: return "not_wgc";
    case CaptureBorderPolicyReason::SessionUnavailable: return "session_unavailable";
    case CaptureBorderPolicyReason::SessionOptOutAttemptRequired:
        return "session_opt_out_attempt_required";
    case CaptureBorderPolicyReason::SessionInterfaceUnavailable: return "session_interface_unavailable";
    case CaptureBorderPolicyReason::SessionPropertyFailed: return "session_property_failed";
    case CaptureBorderPolicyReason::BorderStillRequired: return "border_still_required";
    case CaptureBorderPolicyReason::BorderlessEnabled: return "borderless_enabled";
    }
    return "unknown";
}

}  // namespace fthr
