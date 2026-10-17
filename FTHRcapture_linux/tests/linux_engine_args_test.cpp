// The Linux engine's command line: the 26-slot 2.0 startup contract and the
// `--` modes that must exit before shared memory or capture start.

#include "engine_args.h"

#include <cassert>
#include <string>
#include <vector>

namespace {

struct Argv {
    std::vector<std::string> storage;
    std::vector<const char*> ptrs;
    explicit Argv(std::vector<std::string> args) : storage(std::move(args)) {
        storage.insert(storage.begin(), "FTHRclips");
        for (const auto& arg : storage) ptrs.push_back(arg.c_str());
        ptrs.push_back(nullptr);
    }
    int argc() const { return static_cast<int>(storage.size()); }
    const char* const* argv() const { return ptrs.data(); }
};

fthr::CliDecision Classify(std::vector<std::string> args) {
    Argv a(std::move(args));
    return fthr::ClassifyCommandLine(a.argc(), a.argv());
}

fthr::EngineArgs Parse(std::vector<std::string> args) {
    Argv a(std::move(args));
    return fthr::ParseEngineArgs(a.argc(), a.argv());
}

// What the 2.0 UI launches (FTHR_UI/main.py), Linux values filled in.
std::vector<std::string> UiLaunch() {
    return {"60", "902", "1920", "1080", "16000", "2048",
            "0", "0", "1", "DP-1",
            "2", "5", "0", "1",
            "", "100", "2",
            "0", "0", "0", "1", "1",
            "1", "alsa_output.usb-headset.analog-stereo", "1"};
}

} // namespace

int main() {
    using fthr::CliMode;

    // --- `--` modes -------------------------------------------------------
    assert(Classify({}).mode == CliMode::Engine);
    assert(Classify(UiLaunch()).mode == CliMode::Engine);
    assert(Classify({"--list-playback-devices"}).mode == CliMode::ListPlaybackDevices);
    assert(Classify({"--list-microphones"}).mode == CliMode::ListMicrophones);
    // Unknown flags never fall through to the positional parser.
    {
        const auto d = Classify({"--help"});
        assert(d.mode == CliMode::UsageError);
        assert(d.message.find("--help") != std::string::npos);
    }
    assert(Classify({"--"}).mode == CliMode::UsageError);
    assert(Classify({"--list-playback-device"}).mode == CliMode::UsageError);
    // A flag anywhere, not just argv[1], and listing flags only alone.
    {
        auto args = UiLaunch();
        args[5] = "--max-buffer";
        assert(Classify(args).mode == CliMode::UsageError);
    }
    assert(Classify({"60", "--list-playback-devices"}).mode == CliMode::UsageError);
    assert(Classify({"--list-microphones", "x"}).mode == CliMode::UsageError);
    // A single dash is a value (negative-looking crop numbers, "-" names).
    assert(Classify({"60", "30", "0", "0", "16000", "-1"}).mode == CliMode::Engine);

    // --- defaults -----------------------------------------------------------
    {
        const auto a = Parse({});
        assert(a.fps == 60 && a.buffer_seconds == 30);
        assert(a.target_width == 0 && a.target_height == 0);
        assert(a.bitrate_kbps == 16000 && a.max_buffer_mb == 0);
        assert(a.scaling_mode == 0 && a.target_output.empty());
        assert(a.codec_pref == fthr::CodecPref::Auto && a.preset == 4);
        assert(a.audio_enabled && !a.separate_audio_enabled);
        assert(a.encoder_pref == fthr::EncoderPref::Auto);
        assert(a.playback_endpoint_id.empty() && !a.microphone_mono);
        assert(a.warnings.empty());
    }
    // Empty strings mean "default", silently, in every slot.
    {
        const auto a = Parse(std::vector<std::string>(25, ""));
        assert(a.fps == 60 && a.buffer_seconds == 30 && a.bitrate_kbps == 16000);
        assert(a.audio_enabled && a.encoder_pref == fthr::EncoderPref::Auto);
        assert(a.playback_endpoint_id.empty() && !a.microphone_mono);
        assert(a.warnings.empty());
    }

    // --- a full 2.0 UI launch -----------------------------------------------
    {
        const auto a = Parse(UiLaunch());
        assert(a.fps == 60);
        assert(a.buffer_seconds == 900);   // clamped to the 2.0 maximum
        assert(a.warnings.size() == 1);
        assert(a.target_width == 1920 && a.target_height == 1080);
        assert(a.bitrate_kbps == 16000 && a.max_buffer_mb == 2048);
        assert(a.scaling_mode == 1 && a.target_output == "DP-1");
        assert(a.codec_pref == fthr::CodecPref::HEVC && a.preset == 5);
        assert(a.audio_enabled);
        assert(a.encoder_pref == fthr::EncoderPref::Amd);
        assert(a.separate_audio_enabled);
        assert(a.playback_endpoint_id == "alsa_output.usb-headset.analog-stereo");
        assert(a.microphone_mono);
    }
    // A 24-slot launch from a pre-2.0 UI still parses: argv[24]/[25] default.
    {
        auto args = UiLaunch();
        args.resize(23);
        const auto a = Parse(args);
        assert(a.playback_endpoint_id.empty() && !a.microphone_mono);
        assert(a.separate_audio_enabled);
    }

    // --- limits ---------------------------------------------------------------
    assert(Parse({"60", "900"}).buffer_seconds == 900);
    assert(Parse({"60", "900"}).warnings.empty());
    assert(Parse({"60", "901"}).buffer_seconds == 900);
    assert(Parse({"60", "0"}).buffer_seconds == 30);
    assert(Parse({"60", "30", "0", "0", "200000"}).bitrate_kbps == 200000);
    assert(Parse({"60", "30", "0", "0", "200000"}).warnings.empty());
    assert(Parse({"60", "30", "0", "0", "200001"}).bitrate_kbps == 200000);
    assert(Parse({"60", "30", "0", "0", "100"}).bitrate_kbps == 500);
    assert(Parse({"0"}).fps == 60);
    assert(Parse({"361"}).fps == 360);
    // ParseU32: no signs, no garbage, no overflow wrap.
    assert(Parse({"-60"}).fps == 60 && !Parse({"-60"}).warnings.empty());
    assert(Parse({"60fps"}).fps == 60);
    assert(Parse({"60", "4294967296"}).buffer_seconds == 30);
    assert(Parse({"60", " 30"}).buffer_seconds == 30);
    // Half a resolution, or beyond 8K, is native.
    {
        const auto a = Parse({"60", "30", "1920", "0"});
        assert(a.target_width == 0 && a.target_height == 0 && !a.warnings.empty());
        const auto b = Parse({"60", "30", "7681", "4320"});
        assert(b.target_width == 0 && b.target_height == 0);
    }
    // Byte budget: 0 = none, otherwise 64..16384 MiB.
    assert(Parse({"60", "30", "0", "0", "16000", "0"}).max_buffer_mb == 0);
    assert(Parse({"60", "30", "0", "0", "16000", "10"}).max_buffer_mb == 64);
    assert(Parse({"60", "30", "0", "0", "16000", "99999"}).max_buffer_mb == 16384);
    assert(Parse({"60", "30", "0", "0", "16000", "512"}).max_buffer_mb == 512);
    // Presets clamp to 1..7.
    {
        auto args = UiLaunch();
        args[11] = "0";
        assert(Parse(args).preset == 1);
        args[11] = "9";
        assert(Parse(args).preset == 7);
    }
    // Unknown codec and encoder values are Auto, never Software.
    {
        auto args = UiLaunch();
        args[10] = "4";
        args[16] = "5";
        const auto a = Parse(args);
        assert(a.codec_pref == fthr::CodecPref::Auto);
        assert(a.encoder_pref == fthr::EncoderPref::Auto);
        args[16] = "4";
        assert(Parse(args).encoder_pref == fthr::EncoderPref::Software);
    }
    // audio_enabled 0 disables desktop audio.
    {
        auto args = UiLaunch();
        args[13] = "0";
        assert(!Parse(args).audio_enabled);
    }
    // UTF-8 sink names pass through untouched.
    {
        auto args = UiLaunch();
        args[23] = "bluez_output.Kopfh\xC3\xB6rer.1";
        assert(Parse(args).playback_endpoint_id == "bluez_output.Kopfh\xC3\xB6rer.1");
    }
    return 0;
}
