// Pins the Linux encoder candidate policy to the Windows 2.0 contract: Auto
// is hardware only, software only when the user chose Software.

#include "encoder_policy.h"

#include <cassert>
#include <cstring>
#include <string>
#include <vector>

namespace {

std::vector<std::string> Names(fthr::CodecPref codec, fthr::EncoderPref encoder) {
    std::vector<std::string> out;
    for (const char* name : fthr::EncoderCandidates(codec, encoder))
        out.emplace_back(name);
    return out;
}

bool IsSoftware(const std::string& name) {
    return name.rfind("lib", 0) == 0;
}

} // namespace

int main() {
    using fthr::CodecPref;
    using fthr::EncoderPref;
    using V = std::vector<std::string>;

    // Auto: every hardware backend, backend-first, and nothing else.
    assert((Names(CodecPref::Auto, EncoderPref::Auto) == V{
        "h264_nvenc", "hevc_nvenc", "av1_nvenc",
        "h264_vaapi", "hevc_vaapi", "av1_vaapi",
        "h264_amf", "hevc_amf", "av1_amf",
        "h264_qsv", "hevc_qsv", "av1_qsv"}));
    assert((Names(CodecPref::H264, EncoderPref::Auto) == V{
        "h264_nvenc", "h264_vaapi", "h264_amf", "h264_qsv"}));

    // No codec preference lets Auto reach a software encoder.
    for (CodecPref codec : {CodecPref::Auto, CodecPref::H264,
                            CodecPref::HEVC, CodecPref::AV1}) {
        const V names = Names(codec, EncoderPref::Auto);
        assert(!names.empty());
        for (const auto& name : names) assert(!IsSoftware(name));
    }

    // A named hardware backend tries that backend only.
    assert((Names(CodecPref::H264, EncoderPref::Nvidia) == V{"h264_nvenc"}));
    assert((Names(CodecPref::HEVC, EncoderPref::Amd) == V{"hevc_vaapi", "hevc_amf"}));
    assert((Names(CodecPref::AV1, EncoderPref::Intel) == V{"av1_qsv"}));
    for (EncoderPref backend : {EncoderPref::Nvidia, EncoderPref::Amd,
                                EncoderPref::Intel})
        for (const auto& name : Names(CodecPref::Auto, backend))
            assert(!IsSoftware(name));

    // Software is explicit and LGPL-compatible only (never libx264/libx265).
    assert((Names(CodecPref::H264, EncoderPref::Software) == V{"libopenh264"}));
    assert((Names(CodecPref::HEVC, EncoderPref::Software) == V{"libkvazaar"}));
    assert((Names(CodecPref::AV1, EncoderPref::Software) == V{
        "libsvtav1", "libaom-av1", "librav1e"}));
    for (const auto& name : Names(CodecPref::Auto, EncoderPref::Software)) {
        assert(IsSoftware(name));
        assert(name.find("x26") == std::string::npos);
    }
    return 0;
}
