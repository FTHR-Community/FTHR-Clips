#pragma once

// Native transport contract for capture highlights.  The format deliberately
// mirrors FTHR_UI.core.highlight_markers: a bounded version-1 JSON document
// containing canonical UUIDs and exact, non-negative 100 ns positions.

#include <algorithm>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>
#include <iterator>
#include <unordered_set>
#include <vector>

namespace fthr {

constexpr uint32_t kHighlightMarkersVersion = 1;
constexpr size_t kMaxHighlightMarkers = 4096;
constexpr size_t kMaxHighlightPayloadBytes = 4u * 1024u * 1024u;
constexpr size_t kMaxHighlightLabelLength = 120;

struct HighlightMarker {
    std::string marker_id;
    int64_t position_100ns = 0;
    std::string label;
};

// A mark is owned by a replay ring until it is copied into a snapshot.  These
// fields are capture-clock evidence and never cross the shared-memory ABI.
struct CapturedHighlightMark {
    std::string marker_id;
    int64_t pts = 0;
    int64_t wall_time = 0;
    uint64_t absolute_position = std::numeric_limits<uint64_t>::max();
    uint64_t generation = 0;
};

inline bool IsCanonicalHighlightUuid(std::string_view value) noexcept {
    if (value.size() != 36) return false;
    for (size_t i = 0; i < value.size(); ++i) {
        const char c = value[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != '-') return false;
            continue;
        }
        const bool lower_hex = (c >= '0' && c <= '9')
            || (c >= 'a' && c <= 'f');
        if (!lower_hex) return false;
    }
    return true;
}

inline bool IsValidHighlightLabel(std::string_view value) noexcept {
    // Native IPC carries no label field.  Keep the native subset deliberately
    // strict; Python/SQLite owns user-facing Unicode labels.
    return value.empty();
}

inline bool IsValidHighlightMarker(const HighlightMarker& marker) noexcept {
    return IsCanonicalHighlightUuid(marker.marker_id)
        && marker.position_100ns >= 0
        && IsValidHighlightLabel(marker.label);
}

inline bool CheckedPtsTo100ns(
    int64_t pts_delta, int64_t time_base_numerator,
    int64_t time_base_denominator, int64_t& out) noexcept {
    if (pts_delta < 0 || time_base_numerator <= 0
        || time_base_denominator <= 0) return false;
    const uint64_t delta = static_cast<uint64_t>(pts_delta);
    const uint64_t den = static_cast<uint64_t>(time_base_denominator);
    const uint64_t scale = static_cast<uint64_t>(time_base_numerator);
    if (scale > std::numeric_limits<uint64_t>::max() / 10'000'000ULL)
        return false;
    const uint64_t numerator = scale * 10'000'000ULL;
    const uint64_t whole = delta / den;
    const uint64_t remainder = delta % den;
    if (whole > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
            / numerator)
        return false;
    const uint64_t whole_part = whole * numerator;
    if (remainder > std::numeric_limits<uint64_t>::max() / numerator)
        return false;
    const uint64_t fractional = (remainder * numerator) / den;
    if (whole_part > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
            - fractional)
        return false;
    out = static_cast<int64_t>(whole_part + fractional);
    return true;
}

inline bool CheckedSignedSubtract(
    int64_t left, int64_t right, int64_t& out) noexcept {
    if ((right > 0 && left < std::numeric_limits<int64_t>::min() + right)
        || (right < 0 && left > std::numeric_limits<int64_t>::max() + right)) {
        return false;
    }
    out = left - right;
    return true;
}

inline bool CheckedNonNegativeDelta(
    int64_t value, int64_t origin, int64_t& out) noexcept {
    if (value < origin) return false;
    const uint64_t delta = static_cast<uint64_t>(value)
        - static_cast<uint64_t>(origin);
    if (delta > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()))
        return false;
    out = static_cast<int64_t>(delta);
    return true;
}

inline void AppendJsonEscaped(std::string_view value, std::string& out) {
    out.push_back('"');
    for (const unsigned char c : value) {
        switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\b': out += "\\b"; break;
        case '\f': out += "\\f"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) {
                static constexpr char hex[] = "0123456789abcdef";
                out += "\\u00";
                out.push_back(hex[c >> 4]);
                out.push_back(hex[c & 0x0f]);
            } else {
                out.push_back(static_cast<char>(c));
            }
            break;
        }
    }
    out.push_back('"');
}

inline bool SerializeHighlightMarkers(
    const std::vector<HighlightMarker>& markers, std::string& out) {
    if (markers.size() > kMaxHighlightMarkers) return false;
    std::vector<HighlightMarker> ordered = markers;
    for (const auto& marker : ordered) {
        if (!IsValidHighlightMarker(marker)) return false;
    }
    std::sort(ordered.begin(), ordered.end(), [](const auto& left, const auto& right) {
        if (left.position_100ns != right.position_100ns)
            return left.position_100ns < right.position_100ns;
        return left.marker_id < right.marker_id;
    });
    std::unordered_set<std::string> marker_ids;
    marker_ids.reserve(ordered.size());
    for (const auto& marker : ordered) {
        if (!marker_ids.insert(marker.marker_id).second) return false;
    }

    out.clear();
    out.reserve(64 + ordered.size() * 92);
    out += "{\"version\":1,\"markers\":[";
    for (size_t i = 0; i < ordered.size(); ++i) {
        if (i) out.push_back(',');
        out += "{\"marker_id\":";
        AppendJsonEscaped(ordered[i].marker_id, out);
        out += ",\"position_100ns\":";
        out += std::to_string(ordered[i].position_100ns);
        out += ",\"label\":";
        AppendJsonEscaped(ordered[i].label, out);
        out.push_back('}');
    }
    out += "]}";
    return out.size() <= kMaxHighlightPayloadBytes;
}

inline bool BuildHighlightUuidBox(
    const std::vector<HighlightMarker>& markers,
    std::vector<uint8_t>& box) {
    std::string payload;
    if (!SerializeHighlightMarkers(markers, payload)) return false;
    constexpr uint8_t user_type[16] = {
        0x9a, 0x68, 0xd1, 0x74, 0x6b, 0xf0, 0x42, 0x77,
        0x9b, 0x6d, 0x8b, 0x04, 0xe3, 0x48, 0x43, 0x2a};
    const uint64_t total_size = 8u + 16u + payload.size();
    if (total_size > std::numeric_limits<uint32_t>::max()) return false;
    box.clear();
    box.reserve(static_cast<size_t>(total_size));
    const uint32_t size = static_cast<uint32_t>(total_size);
    box.push_back(static_cast<uint8_t>(size >> 24));
    box.push_back(static_cast<uint8_t>(size >> 16));
    box.push_back(static_cast<uint8_t>(size >> 8));
    box.push_back(static_cast<uint8_t>(size));
    box.insert(box.end(), {'u', 'u', 'i', 'd'});
    box.insert(box.end(), std::begin(user_type), std::end(user_type));
    box.insert(box.end(), payload.begin(), payload.end());
    return true;
}

} // namespace fthr
