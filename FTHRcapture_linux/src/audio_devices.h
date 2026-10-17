#pragma once
// Audio endpoint inventory for `--list-playback-devices` and
// `--list-microphones`: schema 1, the same JSON line the Windows engine
// prints and FTHR_UI/core/windows_microphone_devices.py parses. Both listings
// use the "microphones" key, as on Windows.

#include <cstdint>
#include <string>
#include <vector>

namespace fthr {

inline constexpr uint32_t kEndpointStateActive = 1;   // DEVICE_STATE_ACTIVE

struct AudioEndpoint {
    std::string endpoint_id;    // PulseAudio sink name for playback devices
    std::string display_name;   // sink description; never empty
    uint32_t    device_state = kEndpointStateActive;
    bool        is_default = false;
};

// JSON string body (no quotes). UTF-8 passes through; control characters
// are escaped so a device description can never break the line.
std::string JsonEscape(const std::string& value);

// {"schema_version":1,"microphones":[{...},...]}, without a newline.
std::string FormatEndpointListJson(const std::vector<AudioEndpoint>& endpoints);

// {"schema_version":1,"error":"..."}, without a newline.
std::string FormatEndpointListError(const std::string& error);

// PulseAudio (or PipeWire-Pulse) sinks, the default sink flagged. False with
// *error set when the server cannot be reached or does not answer in time.
bool EnumeratePlaybackSinks(std::vector<AudioEndpoint>* endpoints,
                            std::string* error);

} // namespace fthr
