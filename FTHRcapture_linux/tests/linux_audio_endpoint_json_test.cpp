// `--list-playback-devices` / `--list-microphones` output: schema 1, one line,
// the shape FTHR_UI/core/windows_microphone_devices.py accepts.

#include "audio_devices.h"

#include <cassert>
#include <string>
#include <vector>

int main() {
    using fthr::AudioEndpoint;

    assert(fthr::FormatEndpointListJson({}) ==
           "{\"schema_version\":1,\"microphones\":[]}");

    std::vector<AudioEndpoint> sinks;
    sinks.push_back({"alsa_output.pci-0000_0b_00.4.analog-stereo",
                     "Starship/Matisse HD Audio Controller Analog Stereo",
                     fthr::kEndpointStateActive, true});
    sinks.push_back({"bluez_output.AA_BB.1", "", fthr::kEndpointStateActive, false});
    assert(fthr::FormatEndpointListJson(sinks) ==
           "{\"schema_version\":1,\"microphones\":["
           "{\"endpoint_id\":\"alsa_output.pci-0000_0b_00.4.analog-stereo\","
           "\"display_name\":\"Starship/Matisse HD Audio Controller Analog Stereo\","
           "\"device_state\":1,\"is_default\":true},"
           // The UI drops entries without a display name, so the sink name
           // stands in for a missing description.
           "{\"endpoint_id\":\"bluez_output.AA_BB.1\","
           "\"display_name\":\"bluez_output.AA_BB.1\","
           "\"device_state\":1,\"is_default\":false}]}");

    // Escaping: quotes, backslashes and control characters cannot break the
    // line; UTF-8 passes through.
    assert(fthr::JsonEscape("a\"b\\c") == "a\\\"b\\\\c");
    assert(fthr::JsonEscape("x\ny\tz\r") == "x\\ny\\tz\\r");
    assert(fthr::JsonEscape(std::string("\x01\x1f\x7f", 3)) == "\\u0001\\u001f\\u007f");
    assert(fthr::JsonEscape("Kopfh\xC3\xB6rer") == "Kopfh\xC3\xB6rer");
    const std::string line = fthr::FormatEndpointListJson(
        {{"s\nink", "Desc \"quoted\"", 1, false}});
    assert(line.find('\n') == std::string::npos);
    assert(line.find("\"display_name\":\"Desc \\\"quoted\\\"\"") != std::string::npos);

    assert(fthr::FormatEndpointListError("PulseAudio server unreachable") ==
           "{\"schema_version\":1,\"error\":\"PulseAudio server unreachable\"}");
    return 0;
}
