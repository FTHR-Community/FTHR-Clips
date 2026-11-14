#include "nvenc_input_lifecycle.h"
#include "nvenc_buffer_policy.h"

#include <cstdlib>
#include <iostream>

namespace {

void Require(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "NVENC lifecycle test failed: " << message << std::endl;
        std::exit(1);
    }
}

} // namespace

int RunNvencInputLifecycleTests() {
    int checks = 0;

    Require(fthr::NvencAsyncBufferCount(1, false, 32) == 4,
        "no-B-frame replay must use four slots and ignore disabled lookahead depth"); ++checks;
    Require(fthr::NvencAsyncBufferCount(0, false, 0) == 4,
        "intra-only encoding must retain the four-slot async minimum"); ++checks;
    Require(fthr::NvencAsyncBufferCount(3, false, 0) == 6,
        "B-frame reordering must receive extra slots"); ++checks;
    Require(fthr::NvencAsyncBufferCount(3, true, 16) == 22,
        "enabled lookahead must not exhaust the pool before output is ready"); ++checks;

    fthr::NvencInputSlotLifecycle slot;
    Require(slot.OnMapped(), "available slot must map"); ++checks;
    Require(slot.OnSubmitted(), "mapped slot must submit"); ++checks;

    // This was the production bug: unmapping immediately after submission must
    // be rejected until nvEncLockBitstream has confirmed output completion.
    Require(!slot.OnCompletedInputUnmapped(),
        "submitted input must not unmap before output lock returns"); ++checks;
    Require(!slot.is_available(), "premature unmap must not recycle the slot"); ++checks;

    Require(!slot.OnOutputLocked(),
        "worker must not lock output before its completion event"); ++checks;
    Require(slot.OnCompletionSignaled(),
        "submitted slot must accept its completion event"); ++checks;
    Require(slot.OnOutputLocked(), "submitted slot must accept output completion"); ++checks;
    Require(!slot.OnCompletedInputUnmapped(),
        "locked output must be consumed before input unmap"); ++checks;
    Require(slot.OnOutputConsumed(),
        "unlocked output must become ready for submit-thread unmap"); ++checks;
    Require(slot.is_ready_to_unmap(),
        "consumed output must advertise submit-thread unmap readiness"); ++checks;
    Require(slot.OnCompletedInputUnmapped(),
        "completed slot must unmap and recycle"); ++checks;
    Require(slot.is_available(), "completed slot must return to the pool"); ++checks;

    for (int frame = 0; frame < 1000; ++frame) {
        Require(slot.OnMapped(), "1000-cycle map transition"); ++checks;
        Require(slot.OnSubmitted(), "1000-cycle submit transition"); ++checks;
        Require(slot.OnCompletionSignaled(), "1000-cycle completion transition"); ++checks;
        Require(slot.OnOutputLocked(), "1000-cycle output transition"); ++checks;
        Require(slot.OnOutputConsumed(), "1000-cycle output-consumed transition"); ++checks;
        Require(slot.OnCompletedInputUnmapped(), "1000-cycle recycle transition"); ++checks;
    }

    return checks;
}
