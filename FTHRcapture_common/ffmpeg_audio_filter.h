#pragma once

extern "C" {
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/channel_layout.h>
#include <libavutil/samplefmt.h>
}
#include <cstring>
#include <string>
#include <vector>

// The caller supplies the same validated filter description used for export.
// All graph execution belongs to the decoder worker, never the audio callback.
class FthrAudioFilter {
public:
    ~FthrAudioFilter() { avfilter_graph_free(&graph); }
    bool Open(const std::string& description) {
        avfilter_graph_free(&graph);
        source = sink = nullptr;
        graph = avfilter_graph_alloc();
        if (!graph) return false;
        graph->nb_threads = 1;
        if (avfilter_graph_create_filter(&source, avfilter_get_by_name("abuffer"), "in",
                "time_base=1/48000:sample_rate=48000:sample_fmt=flt:channel_layout=stereo",
                nullptr, graph) < 0
                || avfilter_graph_create_filter(&sink, avfilter_get_by_name("abuffersink"),
                    "out", nullptr, nullptr, graph) < 0) return false;
        AVFilterInOut* inputs = avfilter_inout_alloc();
        AVFilterInOut* outputs = avfilter_inout_alloc();
        if (!inputs || !outputs) {
            avfilter_inout_free(&inputs); avfilter_inout_free(&outputs);
            return false;
        }
        inputs->name = av_strdup("out"); inputs->filter_ctx = sink; inputs->pad_idx = 0;
        outputs->name = av_strdup("in"); outputs->filter_ctx = source; outputs->pad_idx = 0;
        const std::string chain = description
            + ",aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=stereo";
        const int parsed = avfilter_graph_parse_ptr(graph, chain.c_str(), &inputs, &outputs, nullptr);
        const bool connected = !inputs && !outputs;
        avfilter_inout_free(&inputs); avfilter_inout_free(&outputs);
        return parsed >= 0 && connected && avfilter_graph_config(graph, nullptr) >= 0;
    }
    int Send(const float* samples, int frames, int64_t pts) {
        if (!samples) return av_buffersrc_add_frame_flags(source, nullptr, 0);
        AVFrame* frame = av_frame_alloc();
        if (!frame) return AVERROR(ENOMEM);
        frame->format = AV_SAMPLE_FMT_FLT; frame->sample_rate = 48000;
        frame->nb_samples = frames; frame->pts = pts;
        av_channel_layout_default(&frame->ch_layout, 2);
        int result = av_frame_get_buffer(frame, 0);
        if (result >= 0) {
            std::memcpy(frame->data[0], samples, static_cast<size_t>(frames) * 2 * sizeof(float));
            result = av_buffersrc_add_frame_flags(source, frame, AV_BUFFERSRC_FLAG_KEEP_REF);
        }
        av_frame_free(&frame);
        return result;
    }
    int Read(std::vector<float>& samples, int64_t& pts) {
        AVFrame* frame = av_frame_alloc();
        if (!frame) return AVERROR(ENOMEM);
        const int result = av_buffersink_get_frame(sink, frame);
        if (result >= 0) {
            samples.resize(static_cast<size_t>(frame->nb_samples) * 2);
            std::memcpy(samples.data(), frame->data[0], samples.size() * sizeof(float));
            pts = frame->pts;
        }
        av_frame_free(&frame);
        return result;
    }
    AVFilterGraph* graph = nullptr;
    AVFilterContext* source = nullptr;
    AVFilterContext* sink = nullptr;
};
