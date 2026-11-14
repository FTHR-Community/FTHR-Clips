#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <random>
#include <string>

namespace fthr {

inline constexpr const char* kCaptureTimelineMetadataTag = "fthr_capture_timeline";

// Half-open interval in the original capture generation's presentation clock.
// start_tick maps to playback zero. This clock may compact pauses; it is not
// UTC or a promise of a linear mapping to QPC/monotonic wall time.
struct CaptureTimeline {
    std::string generation_id;
    int64_t start_tick = 0;
    int64_t end_tick = 0;
    int32_t time_base_num = 0;
    int32_t time_base_den = 0;
};

inline bool IsCanonicalCaptureGenerationId(const std::string& value) noexcept {
    if (value.size() != 36) return false;
    bool nonzero = false;
    for (size_t i = 0; i < value.size(); ++i) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (value[i] != '-') return false;
        } else {
            const char c = value[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
            nonzero = nonzero || c != '0';
        }
    }
    return nonzero;
}

// Failure leaves provenance unavailable rather than manufacturing a shared ID.
inline std::string NewCaptureGenerationId() noexcept {
    try {
        std::random_device random;
        std::uniform_int_distribution<unsigned int> byte(0, 255);
        std::array<uint8_t, 16> bytes{};
        for (auto& value : bytes) value = static_cast<uint8_t>(byte(random));
        bytes[6] = static_cast<uint8_t>((bytes[6] & 0x0f) | 0x40);
        bytes[8] = static_cast<uint8_t>((bytes[8] & 0x3f) | 0x80);
        constexpr char hex[] = "0123456789abcdef";
        std::string result;
        result.reserve(36);
        for (size_t i = 0; i < bytes.size(); ++i) {
            if (i == 4 || i == 6 || i == 8 || i == 10) result.push_back('-');
            result.push_back(hex[bytes[i] >> 4]);
            result.push_back(hex[bytes[i] & 0xf]);
        }
        return result;
    } catch (...) {
        return {};
    }
}

inline bool IsValidCaptureTimeline(const CaptureTimeline& value) noexcept {
    return IsCanonicalCaptureGenerationId(value.generation_id)
        && value.start_tick >= 0 && value.end_tick > value.start_tick
        && value.time_base_num > 0 && value.time_base_den > 0;
}

inline CaptureTimeline MakeCaptureTimeline(
    const std::string& generation_id, int64_t start_tick, int64_t last_tick,
    int32_t time_base_num, int32_t time_base_den) {
    if (last_tick == std::numeric_limits<int64_t>::max()) return {};
    CaptureTimeline value{generation_id, start_tick, last_tick + 1,
        time_base_num, time_base_den};
    return IsValidCaptureTimeline(value) ? value : CaptureTimeline{};
}

inline std::string SerializeCaptureTimeline(const CaptureTimeline& value) {
    if (!IsValidCaptureTimeline(value)) return {};
    return "{\"version\":1,\"generation_id\":\"" + value.generation_id
        + "\",\"start_tick\":" + std::to_string(value.start_tick)
        + ",\"end_tick\":" + std::to_string(value.end_tick)
        + ",\"time_base_num\":" + std::to_string(value.time_base_num)
        + ",\"time_base_den\":" + std::to_string(value.time_base_den) + "}";
}

} // namespace fthr
