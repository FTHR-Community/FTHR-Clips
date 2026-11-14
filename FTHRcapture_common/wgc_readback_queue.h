#pragma once

#include <cstdint>
#include <optional>

namespace fthr::wgc {

// One pending GPU copy, two alternating staging slots. The capture thread
// owns both the queue and the immediate context.
class ReadbackQueue {
public:
    struct Frame {
        unsigned slot;
        int64_t qpc;
        uint32_t generation;
    };

    struct Work {
        unsigned write_slot;
        std::optional<Frame> read;
    };

    Work Queue(int64_t qpc, uint32_t generation) noexcept {
        auto read = pending_;
        const unsigned write_slot = read ? 1u - read->slot : 0u;
        pending_ = Frame{write_slot, qpc, generation};
        if (read && read->generation != generation) read.reset();
        return {write_slot, read};
    }

    void Reset() noexcept { pending_.reset(); }

private:
    std::optional<Frame> pending_;
};

} // namespace fthr::wgc
