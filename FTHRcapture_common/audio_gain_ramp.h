#pragma once

#include <algorithm>
#include <cmath>

namespace fthr {

// Decoder-thread state only. Parameter updates never flush decoded media.
class AudioGainRamp {
public:
    void Reset() { initialized_ = false; remaining_ = 0; }
    void SetTarget(float target, int frames = 240) {
        if (!std::isfinite(target)) target = 0.0f;
        if (!initialized_) {
            current_ = target_ = target;
            initialized_ = true;
            return;
        }
        if (target == target_) return;
        target_ = target;
        remaining_ = std::max(1, frames);
        step_ = (target_ - current_) / remaining_;
    }
    float Next() {
        if (remaining_ > 0) {
            current_ += step_;
            if (--remaining_ == 0) current_ = target_;
        }
        return current_;
    }
private:
    bool initialized_ = false;
    int remaining_ = 0;
    float current_ = 1.0f;
    float target_ = 1.0f;
    float step_ = 0.0f;
};

} // namespace fthr
