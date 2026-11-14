// C ABI for FTHR's in-process multi-track playback decoder/mixer.
//
// The bridge intentionally knows nothing about Qt. Python owns QAudioSink and
// calls Pull from a dedicated decoder worker, so UI and audio-device callbacks
// never enter FFmpeg decoding code.
#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  define FTHR_PLAYBACK_API __declspec(dllexport)
#else
#  define FTHR_PLAYBACK_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct FTHRPlaybackMixer FTHRPlaybackMixer;

FTHR_PLAYBACK_API int fthr_playback_configure_dsp(FTHRPlaybackMixer* mixer,
    const char* const* source_filters, int source_count, const char* output_filter,
    char* error_text, size_t error_text_capacity);
FTHR_PLAYBACK_API float fthr_playback_output_peak(const FTHRPlaybackMixer* mixer);
FTHR_PLAYBACK_API int64_t fthr_playback_buffered_frames(const FTHRPlaybackMixer* mixer);

// Opens one local media container and one FFmpeg decoder per selected absolute
// stream index. Error text is always optional and NUL-terminated when present.
FTHR_PLAYBACK_API FTHRPlaybackMixer* fthr_playback_open(
    const char* media_path_utf8,
    const int* stream_indexes,
    int stream_count,
    char* error_text,
    size_t error_text_capacity);

// One path per selected stream. Local imported tracks start at clip time zero.
// All inputs share seek/speed and are summed before the final output clamp.
FTHR_PLAYBACK_API FTHRPlaybackMixer* fthr_playback_open_sources(
    const char* const* media_paths_utf8,
    const int* stream_indexes,
    int stream_count,
    char* error_text,
    size_t error_text_capacity);

// Decodes/mixes up to frame_count canonical frames (48 kHz float32 stereo).
// Returns the supplied frame_count, zero at final EOF, or -1 on a recoverable
// decoder failure. ``gains`` is one linear value per selected source and
// master_gain is applied before the zero-lookahead output clamp. Source levels
// do not change when another source is muted, matching derived exports.
FTHR_PLAYBACK_API int fthr_playback_pull(
    FTHRPlaybackMixer* mixer,
    float* interleaved_float_stereo,
    int frame_count,
    const float* gains,
    int gain_count,
    float master_gain);

// Sets the source-time to output-time ratio used by Pull. With
// ``preserve_pitch`` enabled, the bridge uses FFmpeg's atempo filter so a
// faster or slower preview keeps its original pitch. With it disabled, Pull
// retains the caller's lightweight resampling behaviour and pitch follows
// speed. This can be called between pulls; a seek also flushes its state.
FTHR_PLAYBACK_API int fthr_playback_set_playback_rate(
    FTHRPlaybackMixer* mixer,
    float playback_rate,
    int preserve_pitch,
    char* error_text,
    size_t error_text_capacity);

// Seeks every decoder to one media time and flushes stale decoded buffers.
FTHR_PLAYBACK_API int fthr_playback_seek(
    FTHRPlaybackMixer* mixer,
    int64_t position_milliseconds,
    char* error_text,
    size_t error_text_capacity);

FTHR_PLAYBACK_API int fthr_playback_is_eof(const FTHRPlaybackMixer* mixer);
// A failed source is omitted from later mixes; healthy stems continue.
FTHR_PLAYBACK_API int fthr_playback_source_failed(
    const FTHRPlaybackMixer* mixer, int source_ordinal);
FTHR_PLAYBACK_API void fthr_playback_close(FTHRPlaybackMixer* mixer);

#ifdef __cplusplus
}
#endif
