#pragma once

// Shared bounds for the Windows capture startup contract. Keep these pure and
// dependency-free so native startup validation and deterministic tests use the
// same boundary rather than silently diverging.

#include <cstdint>
#include <limits>
#include <string_view>

namespace fthr::capture_config_limits {

inline constexpr uint32_t kMinBitrateKbps = 500;
inline constexpr uint32_t kMaxBitrateKbps = 200'000;

struct U32ParseResult {
    bool valid = false;
    uint32_t value = 0;
};

// Parse only an unsigned decimal token.  Unlike atoi/strtoul this has no
// signed intermediate, locale behavior, or implementation-defined overflow.
inline U32ParseResult ParseU32(std::string_view text) noexcept {
    if (text.empty()) return {};

    uint64_t value = 0;
    constexpr uint64_t max_value =
        static_cast<uint64_t>(std::numeric_limits<uint32_t>::max());
    for (const char character : text) {
        if (character < '0' || character > '9') return {};
        const uint64_t digit = static_cast<uint64_t>(character - '0');
        if (value > (max_value - digit) / 10) return {};
        value = value * 10 + digit;
    }
    return {true, static_cast<uint32_t>(value)};
}

inline constexpr bool IsValidBitrateKbps(uint32_t bitrate_kbps) noexcept {
    return bitrate_kbps >= kMinBitrateKbps
        && bitrate_kbps <= kMaxBitrateKbps;
}

} // namespace fthr::capture_config_limits
