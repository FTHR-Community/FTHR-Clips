#include "audio_devices.h"

#include <cstdio>

namespace fthr {

std::string JsonEscape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (const unsigned char c : value) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20 || c == 0x7f) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                out += buf;
            } else {
                out.push_back(static_cast<char>(c));
            }
            break;
        }
    }
    return out;
}

std::string FormatEndpointListJson(const std::vector<AudioEndpoint>& endpoints) {
    std::string out = "{\"schema_version\":1,\"microphones\":[";
    bool first = true;
    for (const auto& endpoint : endpoints) {
        if (!first) out += ',';
        first = false;
        out += "{\"endpoint_id\":\"" + JsonEscape(endpoint.endpoint_id) +
               "\",\"display_name\":\"" +
               JsonEscape(endpoint.display_name.empty() ? endpoint.endpoint_id
                                                        : endpoint.display_name) +
               "\",\"device_state\":" + std::to_string(endpoint.device_state) +
               ",\"is_default\":" + (endpoint.is_default ? "true" : "false") +
               "}";
    }
    out += "]}";
    return out;
}

std::string FormatEndpointListError(const std::string& error) {
    return "{\"schema_version\":1,\"error\":\"" + JsonEscape(error) + "\"}";
}

} // namespace fthr
