#include "encoder.h"

#include <cassert>
#include <string>
#include <vector>

namespace {

std::vector<std::string> Names(fthr::CodecPref codec, fthr::EncoderPref pref) {
    std::vector<std::string> names;
    for (const char* name : fthr::EncoderCandidates(codec, pref)) {
        names.emplace_back(name);
    }
    return names;
}

bool Contains(const std::vector<std::string>& names, const std::string& name) {
    for (const auto& candidate : names) {
        if (candidate == name) return true;
    }
    return false;
}

}  // namespace

int main() {
    using fthr::CodecPref;
    using fthr::EncoderPref;

    // Auto is backend-first: NVENC, VA-API/AMF, QSV, then software.
    const auto automatic = Names(CodecPref::H264, EncoderPref::Auto);
    assert((automatic == std::vector<std::string>{
        "h264_nvenc", "h264_vaapi", "h264_amf", "h264_qsv", "libopenh264"}));

    // A hardware preference narrows the hardware candidates to that vendor
    // but keeps the software encoder last, so a missing driver does not end
    // capture.
    assert((Names(CodecPref::H264, EncoderPref::Nvidia) ==
            std::vector<std::string>{"h264_nvenc", "libopenh264"}));
    assert((Names(CodecPref::H264, EncoderPref::Amd) ==
            std::vector<std::string>{"h264_vaapi", "h264_amf", "libopenh264"}));
    assert((Names(CodecPref::H264, EncoderPref::Intel) ==
            std::vector<std::string>{"h264_qsv", "libopenh264"}));

    // The codec preference still holds for the software fallback.
    assert((Names(CodecPref::HEVC, EncoderPref::Nvidia) ==
            std::vector<std::string>{"hevc_nvenc", "libkvazaar"}));
    const auto av1_amd = Names(CodecPref::AV1, EncoderPref::Amd);
    assert(av1_amd.front() == "av1_vaapi");
    assert(av1_amd.back() == "librav1e");
    assert(Contains(av1_amd, "libsvtav1"));
    assert(!Contains(av1_amd, "av1_nvenc"));

    // Software never includes a hardware encoder.
    for (const auto& name : Names(CodecPref::Auto, EncoderPref::Software)) {
        assert(name.rfind("lib", 0) == 0);
    }
    return 0;
}
