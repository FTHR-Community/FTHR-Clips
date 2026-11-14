// audio_device_tracks.h -- user-chosen playback devices recorded as their own
// clip tracks.
//
// The UI passes the list as one launch argument: entries joined by '|', each
// entry "<name>;<endpoint id>". Endpoint IDs never contain either separator.

#pragma once
#ifndef FTHR_AUDIO_DEVICE_TRACKS_H
#define FTHR_AUDIO_DEVICE_TRACKS_H

#include "audio_source_model.h"

#include <cstddef>
#include <string>
#include <vector>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

namespace fthr {

inline constexpr size_t kMaxAudioDeviceTracks = kMaxDeviceAudioTracks;
inline constexpr size_t kMaxAudioDeviceTrackNameBytes = 48;

struct AudioDeviceTrackConfig {
    std::wstring endpoint_id;
    std::string name;
};

inline std::string TrimAudioDeviceTrackText(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

inline std::vector<AudioDeviceTrackConfig> ParseAudioDeviceTracks(
    const std::string& argument) {
    std::vector<AudioDeviceTrackConfig> tracks;
    size_t begin = 0;
    while (begin <= argument.size() && tracks.size() < kMaxAudioDeviceTracks) {
        size_t end = argument.find('|', begin);
        if (end == std::string::npos) end = argument.size();
        const std::string entry = argument.substr(begin, end - begin);
        begin = end + 1;
        const size_t split = entry.find(';');
        if (split == std::string::npos) continue;
        const std::string id = TrimAudioDeviceTrackText(entry.substr(split + 1));
        if (id.empty()) continue;
        const int length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
            id.c_str(), static_cast<int>(id.size()), nullptr, 0);
        if (length <= 0) continue;
        AudioDeviceTrackConfig track;
        track.endpoint_id.assign(static_cast<size_t>(length), L'\0');
        MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, id.c_str(),
            static_cast<int>(id.size()), track.endpoint_id.data(), length);
        bool duplicate = false;
        for (const auto& existing : tracks)
            duplicate = duplicate || existing.endpoint_id == track.endpoint_id;
        if (duplicate) continue;
        // Manifests accept only this character set; anything else would make
        // the sidecar unwritable.
        std::string name;
        for (const char c : entry.substr(0, split)) {
            const bool safe = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9') || c == ' ' || c == '.' || c == '_'
                || c == '-' || c == '+' || c == '(' || c == ')' || c == '\'';
            const char out = safe ? c : ' ';
            if (out == ' ' && !name.empty() && name.back() == ' ') continue;
            name += out;
        }
        track.name = TrimAudioDeviceTrackText(name);
        if (track.name.size() > kMaxAudioDeviceTrackNameBytes) {
            size_t cut = kMaxAudioDeviceTrackNameBytes;
            // Never cut inside a UTF-8 continuation sequence.
            while (cut > 0 && (static_cast<unsigned char>(track.name[cut]) & 0xC0) == 0x80)
                --cut;
            track.name.resize(cut);
        }
        if (track.name.empty())
            track.name = "Audio Device " + std::to_string(tracks.size() + 1);
        tracks.push_back(std::move(track));
    }
    return tracks;
}

}  // namespace fthr

#endif  // FTHR_AUDIO_DEVICE_TRACKS_H
