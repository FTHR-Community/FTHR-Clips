#include "windows_capture_border_policy.h"

#include <cstdlib>
#include <iostream>

namespace {

int border_policy_checks = 0;

void CheckCaptureBorder(bool condition, const char* message) {
    ++border_policy_checks;
    if (!condition) {
        std::cerr << "FAILED: " << message << std::endl;
        std::exit(1);
    }
}

fthr::CaptureBorderPolicyInput WgcInput() {
    return fthr::CaptureBorderPolicyInput{};
}

void WgcAlwaysAttemptsTheOldSessionOptOut() {
    const auto decision = fthr::EvaluateCaptureBorderPolicy(WgcInput());
    CheckCaptureBorder(decision.capture_allowed
            && decision.attempt_disable_border
            && !decision.effective_borderless
            && decision.reason
                == fthr::CaptureBorderPolicyReason::SessionOptOutAttemptRequired,
        "every WGC session attempts the old direct border opt-out");
}

void MissingSessionInterfaceKeepsWgcAllowed() {
    auto input = WgcInput();
    input.session_interface_checked = true;
    input.session_interface_available = false;
    const auto decision = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(decision.capture_allowed
            && !decision.attempt_disable_border
            && !decision.effective_borderless
            && decision.reason
                == fthr::CaptureBorderPolicyReason::SessionInterfaceUnavailable,
        "an absent optional WGC border interface keeps capture allowed");
}

void PropertyFailureKeepsPrivacyBorder() {
    auto input = WgcInput();
    input.session_interface_checked = true;
    input.session_interface_available = true;
    input.property_attempted = true;
    input.property_set_succeeded = false;
    const auto decision = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(decision.capture_allowed
            && !decision.effective_borderless
            && decision.reason
                == fthr::CaptureBorderPolicyReason::SessionPropertyFailed,
        "a failed optional border property keeps the OS privacy border");
}

void ReadbackMustConfirmTheBorderIsOff() {
    auto input = WgcInput();
    input.session_interface_checked = true;
    input.session_interface_available = true;
    input.property_attempted = true;
    input.property_set_succeeded = true;

    const auto still_required = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(still_required.capture_allowed
            && !still_required.effective_borderless
            && still_required.reason
                == fthr::CaptureBorderPolicyReason::BorderStillRequired,
        "a session that still requires the border remains capture-allowed");

    input.border_required_after_attempt = false;
    const auto disabled = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(disabled.capture_allowed
            && disabled.effective_borderless
            && disabled.reason == fthr::CaptureBorderPolicyReason::BorderlessEnabled,
        "a successful setter and false readback enable WGC");
}

void NonWgcPathsDoNotUseThePolicy() {
    auto input = WgcInput();
    input.wgc_session = false;
    const auto decision = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(decision.capture_allowed
            && !decision.attempt_disable_border
            && !decision.effective_borderless
            && decision.reason == fthr::CaptureBorderPolicyReason::NotWgc,
        "DXGI does not participate in the WGC border policy");
}

void InvalidSessionRemainsFatal() {
    auto input = WgcInput();
    input.session_available = false;
    const auto decision = fthr::EvaluateCaptureBorderPolicy(input);
    CheckCaptureBorder(!decision.capture_allowed
            && !decision.effective_borderless
            && decision.reason == fthr::CaptureBorderPolicyReason::SessionUnavailable,
        "an unavailable required WGC session remains fatal");
}

}  // namespace

int RunWindowsCaptureBorderPolicyTests() {
    WgcAlwaysAttemptsTheOldSessionOptOut();
    MissingSessionInterfaceKeepsWgcAllowed();
    PropertyFailureKeepsPrivacyBorder();
    ReadbackMustConfirmTheBorderIsOff();
    NonWgcPathsDoNotUseThePolicy();
    InvalidSessionRemainsFatal();
    std::cout << "Windows capture border policy tests: " << border_policy_checks
              << " checks passed" << std::endl;
    return border_policy_checks;
}
