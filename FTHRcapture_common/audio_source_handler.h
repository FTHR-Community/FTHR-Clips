#pragma once
#include "audio_source_model.h"
#include <string>

namespace fthr {
inline std::string AudioSourceHandler(const AudioSourceMetadata& source,
                                     int64_t first_sample = -1, int64_t last_sample = -1) {
    const char* type = source.identity.type == AudioSourceType::Microphone ? "microphone"
        : source.identity.type == AudioSourceType::Application ? "application" : "system";
    return std::string(first_sample >= 0 ? "FTHR3|" : "FTHR2|") + source.identity.id.value + "|" + type + "|"
        + std::to_string(source.input_gain_percent) + "|" + (source.mono ? "1|" : "0|")
        + (first_sample >= 0 ? std::to_string(first_sample)
            + (last_sample >= first_sample ? ":" + std::to_string(last_sample) : "") + "|" : "")
        + source.identity.display_name;
}
} // namespace fthr
