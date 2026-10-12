#include "ext_repeat_policy.h"

#include <cassert>

int main() {
    using R = fthr::WaylandWaitResult;
    assert(fthr::ShouldRepeatLastFrame(R::Timeout, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Timeout, false));
    assert(!fthr::ShouldRepeatLastFrame(R::EventReceived, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Disconnected, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Error, true));
    assert(!fthr::ShouldRepeatLastFrame(R::Cancelled, true));
    return 0;
}
