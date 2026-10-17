#include "encoder_policy.h"

namespace fthr {

std::vector<const char*> EncoderCandidates(CodecPref codec_pref,
                                           EncoderPref encoder_pref) {
    std::vector<const char*> out;
    auto codec_allowed = [&](CodecPref codec) {
        return codec_pref == CodecPref::Auto || codec_pref == codec;
    };
    auto add_backend = [&](EncoderPref backend) {
        if (backend == EncoderPref::Nvidia) {
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_nvenc");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_nvenc");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_nvenc");
        } else if (backend == EncoderPref::Amd) {
            // VA-API is the native Linux AMD path. AMF remains a fallback for
            // systems that provide AMD's separate AMF runtime.
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_vaapi");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_vaapi");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_vaapi");
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_amf");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_amf");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_amf");
        } else if (backend == EncoderPref::Intel) {
            if (codec_allowed(CodecPref::H264)) out.push_back("h264_qsv");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("hevc_qsv");
            if (codec_allowed(CodecPref::AV1)) out.push_back("av1_qsv");
        } else if (backend == EncoderPref::Software) {
            // LGPL-compatible software encoders only.
            if (codec_allowed(CodecPref::H264)) out.push_back("libopenh264");
            if (codec_allowed(CodecPref::HEVC)) out.push_back("libkvazaar");
            if (codec_allowed(CodecPref::AV1)) {
                out.push_back("libsvtav1");
                out.push_back("libaom-av1");
                out.push_back("librav1e");
            }
        }
    };

    switch (encoder_pref) {
    case EncoderPref::Auto:
        // Backend-first so a working hardware H.264 encoder wins over a
        // hardware AV1 encoder discovered later by codec family.
        add_backend(EncoderPref::Nvidia);
        add_backend(EncoderPref::Amd);
        add_backend(EncoderPref::Intel);
        break;
    case EncoderPref::Nvidia:
    case EncoderPref::Amd:
    case EncoderPref::Intel:
    case EncoderPref::Software:
        add_backend(encoder_pref);
        break;
    }
    return out;
}

} // namespace fthr
