#pragma once
#include <charconv>
#include <cstdint>
#include <string>

namespace fthr {
// A late program must not force the shared demuxer to decode minutes ahead.
struct AudioStreamTiming { int64_t first = -1; int64_t last = -1; };
inline AudioStreamTiming AudioSourceTiming(const char* handler) {
    if (!handler) return {};
    const std::string text(handler);
    if (text.size() > 512 || text.rfind("FTHR3|", 0) != 0) return {};
    size_t begin = 0;
    for (int field = 0; field < 5; ++field) {
        begin = text.find('|', begin);
        if (begin == std::string::npos) return {};
        ++begin;
    }
    const size_t end = text.find('|', begin);
    if (end == std::string::npos || end == begin) return {};
    size_t split = text.find(':', begin);
    if (split == std::string::npos || split > end) split = end;
    AudioStreamTiming timing;
    const auto first = std::from_chars(text.data() + begin, text.data() + split, timing.first);
    if (first.ec != std::errc{} || first.ptr != text.data() + split || timing.first < 0) return {};
    if (split != end) {
        const auto last = std::from_chars(text.data() + split + 1, text.data() + end, timing.last);
        if (last.ec != std::errc{} || last.ptr != text.data() + end || timing.last < timing.first) return {};
    }
    return timing;
}
} // namespace fthr
