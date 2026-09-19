#include "capture_engine.h"
#include "capture_backend.h"
#include "recovery_policy.h"
#include "save_clip.h"
#include <chrono>
#include <iostream>
#include <cstring>
#include <cstdio>
#include <thread>
#include <mutex>
#include <algorithm>
#include <time.h>

namespace fthr {

// CaptureEngine constructor/destructor — defined here so ICaptureBackend is complete

CaptureEngine::CaptureEngine() = default;
CaptureEngine::~CaptureEngine() { Shutdown(); }

// CaptureEngine::Initialize

bool CaptureEngine::Initialize(const CaptureConfig& cfg) {
    cfg_ = cfg;

    // Allocate ring buffer (buffer_seconds + small margin)
    size_t ring_ms = (static_cast<size_t>(cfg.buffer_seconds) + 5) * 1000;
    ring_ = new EncodedRingBuffer(ring_ms, cfg.fps);

    // Start audio capture (loopback via PulseAudio monitor)
    if (cfg.audio_enabled) {
        if (cfg.multiband_enabled && !cfg.audio_categories.empty()) {
            multi_audio_.Start(cfg.audio_categories);
        } else {
            if (!audio_.Start("")) {
                std::cerr << "FTHR_STARTUP_WARNING: DESKTOP_AUDIO_UNAVAILABLE: "
                          << "Default output monitor could not be opened; "
                          << "capture continues video-only." << std::endl;
            }
        }
    }

    running_.store(true);
    cap_thread_ = std::thread(&CaptureEngine::CaptureLoop, this);

    return true;
}

// CaptureEngine::Shutdown

void CaptureEngine::Shutdown() {
    running_.store(false);
    if (cap_thread_.joinable())
        cap_thread_.join();
    if (cfg_.audio_enabled) {
        audio_.Stop();
        multi_audio_.Stop();
    }
    encoder_.Close();
    delete ring_;
    ring_ = nullptr;
}

// CaptureEngine::CaptureLoop — backend-agnostic main loop

void CaptureEngine::CaptureLoop() {
    CaptureRecoveryPolicy recovery(static_cast<uint64_t>(cfg_.fps) * 10);

    while (running_.load()) {
        const uint64_t frames_before = frame_count_.load();
        if (recovery.Attempts() > 0)
            capture_health_flags_.store(CAPTURE_HEALTH_RECOVERING);

        const GenerationEnd end = RunCaptureGeneration();
        if (end == GenerationEnd::Stopped) break;
        if (!running_.load()) break;

        if (ring_) ring_->Clear();
        content_suspicious_streak_.store(0);
        const auto decision = recovery.OnGenerationFailed(
            frame_count_.load() - frames_before);
        if (decision.exhausted || end == GenerationEnd::FailedForGood) {
            if (decision.exhausted)
                std::cerr << "[Capture] Recovery exhausted after 3 attempts" << std::endl;
            else
                std::cerr << "[Capture] Not retrying: " << GetCaptureFailureReason()
                          << std::endl;
            // The reason is already stored; the flag is what the UI acts on.
            capture_health_flags_.store(CAPTURE_HEALTH_BACKEND_FAILED);
            // AUDIT-023: capture-thread state must become false on terminal
            // backend failure.  The command loop stays alive to publish the
            // typed failure and reject stale replay data.
            running_.store(false);
            break;
        }

        capture_health_flags_.store(CAPTURE_HEALTH_RECOVERING);
        const auto backoff = decision.backoff;
        std::cerr << "[Capture] Recovery attempt " << decision.attempt << "/3 in "
                  << backoff.count() << " ms"
                  << std::endl;
        const bool continue_recovery = WaitForRecoveryBackoff(
            backoff,
            [this] { return running_.load(); },
            [](std::chrono::milliseconds slice) {
                std::this_thread::sleep_for(slice);
            });
        if (!continue_recovery) break;
    }

    if (capture_health_flags_.load() != CAPTURE_HEALTH_BACKEND_FAILED)
        capture_health_flags_.store(CAPTURE_HEALTH_NONE);
}

void CaptureEngine::SetCaptureFailureReason(const std::string& reason) {
    std::lock_guard<std::mutex> lk(codec_mutex_);
    capture_failure_reason_ = reason;
}

CaptureEngine::GenerationEnd CaptureEngine::RunCaptureGeneration() {
    BackendFailure failure;
    backend_ = CreateBestBackend(cfg_, &running_, &failure);
    if (!backend_) {
        if (!running_.load()) return GenerationEnd::Stopped;
        std::cerr << "[Capture] No capture backend available — exiting" << std::endl;
        SetCaptureFailureReason(failure.reason.empty()
            ? "No capture backend is available" : failure.reason);
        return failure.retry_pointless ? GenerationEnd::FailedForGood
                                       : GenerationEnd::Failed;
    }

    uint32_t native_w = backend_->NativeWidth();
    uint32_t native_h = backend_->NativeHeight();

    // Determine encode dimensions
    uint32_t enc_w = (cfg_.target_width  == 0) ? native_w : cfg_.target_width;
    uint32_t enc_h = (cfg_.target_height == 0) ? native_h : cfg_.target_height;

    if (cfg_.scaling_mode == 1 && (enc_w != native_w || enc_h != native_h)) {
        double ar = static_cast<double>(native_w) / native_h;
        uint32_t fit_h = static_cast<uint32_t>(enc_w / ar);
        if (fit_h <= enc_h) {
            enc_h = fit_h & ~1u;
        } else {
            enc_w = static_cast<uint32_t>(enc_h * ar) & ~1u;
        }
    }
    enc_w &= ~1u;
    enc_h &= ~1u;

    EncoderConfig enc_cfg;
    enc_cfg.src_width    = native_w;
    enc_cfg.src_height   = native_h;
    enc_cfg.enc_width    = enc_w;
    enc_cfg.enc_height   = enc_h;
    enc_cfg.fps          = cfg_.fps;
    enc_cfg.bitrate_kbps = cfg_.bitrate_kbps;
    enc_cfg.codec_pref   = cfg_.codec_pref;
    enc_cfg.encoder_pref = cfg_.encoder_pref;
    enc_cfg.preset       = cfg_.preset;

    std::string codec_used;
    if (!encoder_.Open(enc_cfg, codec_used)) {
        std::cerr << "[Capture] Encoder open failed" << std::endl;
        SetCaptureFailureReason("No video encoder could be opened");
        backend_->Shutdown();
        backend_.reset();
        return GenerationEnd::Failed;
    }
    SetCaptureFailureReason("");
    nvenc_active_.store(codec_used.find("nvenc") != std::string::npos);
    { std::lock_guard<std::mutex> lk(codec_mutex_); active_codec_ = codec_used; }
    capture_generation_.fetch_add(1);
    capture_health_flags_.store(CAPTURE_HEALTH_ACTIVE);
    if (ring_) ring_->Clear();
    content_suspicious_streak_.store(0);

    int64_t frame_ns       = 1'000'000'000LL / cfg_.fps;
    int64_t next_encode_ns = 0;

    std::cout << "[Capture] Loop started: "
              << enc_w << "x" << enc_h
              << " @ " << cfg_.fps << "fps  codec=" << codec_used << std::endl;

    while (running_.load()) {
        if (paused_.load()) {
            capture_health_flags_.store(
                CAPTURE_HEALTH_ACTIVE | CAPTURE_HEALTH_PAUSED);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }
        capture_health_flags_.store(
            CAPTURE_HEALTH_ACTIVE |
            (capture_health_flags_.load() & CAPTURE_HEALTH_CONTENT_SUSPECT));

        RawFrame raw;
        if (!backend_->CaptureFrame(raw)) {
            if (running_.load()) {
                std::cerr << "[Capture] CaptureFrame failed — exiting loop" << std::endl;
                SetCaptureFailureReason("The capture backend stopped delivering frames");
            }
            break;
        }

        // Frame rate throttling
        if (next_encode_ns == 0) next_encode_ns = raw.timestamp_ns;
        if (raw.timestamp_ns < next_encode_ns) continue;
        next_encode_ns += frame_ns;

        const uint64_t produced = frame_count_.load() + 1;
        SampleContent(raw, produced);
        encoder_.EncodeFrame(raw.data, raw.stride, raw.timestamp_ns,
            [this](EncodedPacket pkt) { ring_->Push(std::move(pkt)); });

        frame_count_.fetch_add(1);
    }

    backend_->Shutdown();
    backend_.reset();
    encoder_.Close();
    nvenc_active_.store(false);
    std::cout << "[Capture] Loop exited. Frames: " << frame_count_.load() << std::endl;
    return running_.load() ? GenerationEnd::Failed : GenerationEnd::Stopped;
}

void CaptureEngine::SampleContent(const RawFrame& frame, uint64_t produced_frame) {
    // 16x9 sparse samples once per configured second: 144 pixels, no retained
    // image, no logging and no full-frame scan. Repeated normal imagery is
    // allowed; only sustained black-like/uniform samples become suspect.
    if (!frame.data || frame.width == 0 || frame.height == 0 ||
            frame.stride < frame.width * 4)
        return;
    const uint64_t cadence = std::max<uint32_t>(1, cfg_.fps);
    if (produced_frame % cadence != 0) return;

    constexpr uint32_t kColumns = 16;
    constexpr uint32_t kRows = 9;
    constexpr uint32_t kCount = kColumns * kRows;
    uint64_t sum = 0;
    uint64_t sum_sq = 0;
    for (uint32_t row = 0; row < kRows; ++row) {
        const uint32_t y = std::min(
            frame.height - 1, ((2 * row + 1) * frame.height) / (2 * kRows));
        for (uint32_t column = 0; column < kColumns; ++column) {
            const uint32_t x = std::min(
                frame.width - 1, ((2 * column + 1) * frame.width) / (2 * kColumns));
            const uint8_t* pixel = frame.data +
                static_cast<size_t>(y) * frame.stride + x * 4;
            const uint32_t luma =
                (19u * pixel[0] + 183u * pixel[1] + 54u * pixel[2]) >> 8;
            sum += luma;
            sum_sq += luma * luma;
        }
    }
    const float mean = static_cast<float>(sum) / kCount;
    const float variance = std::max(
        0.0f, static_cast<float>(sum_sq) / kCount - mean * mean);
    const bool suspicious_sample =
        (mean <= 8.0f && variance <= 6.0f) || variance <= 2.0f;
    const uint32_t streak = suspicious_sample
        ? content_suspicious_streak_.fetch_add(1) + 1
        : 0;
    if (!suspicious_sample) content_suspicious_streak_.store(0);

    content_luma_mean_.store(mean);
    content_luma_variance_.store(variance);
    content_sample_sequence_.fetch_add(1);
    uint32_t flags = capture_health_flags_.load();
    if (streak >= 12) flags |= CAPTURE_HEALTH_CONTENT_SUSPECT;
    else flags &= ~CAPTURE_HEALTH_CONTENT_SUSPECT;
    capture_health_flags_.store(flags);
}

// write_pcm_wav — writes IEEE float32 WAV file

static void write_pcm_wav(const std::string& path,
                            const std::vector<float>& pcm,
                            int sample_rate, int channels) {
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) return;

    uint32_t data_bytes  = (uint32_t)(pcm.size() * sizeof(float));
    uint32_t file_size   = 36 + data_bytes;

    // RIFF header
    fwrite("RIFF", 1, 4, f);
    fwrite(&file_size,  4, 1, f);
    fwrite("WAVE", 1, 4, f);

    // fmt chunk — IEEE float PCM (format tag 3)
    fwrite("fmt ", 1, 4, f);
    uint32_t fmt_size    = 16;
    uint16_t audio_fmt   = 3;
    uint16_t ch          = (uint16_t)channels;
    uint32_t sr          = (uint32_t)sample_rate;
    uint32_t byte_rate   = sr * ch * 4;
    uint16_t block_align = (uint16_t)(ch * 4);
    uint16_t bits        = 32;
    fwrite(&fmt_size,    4, 1, f);
    fwrite(&audio_fmt,   2, 1, f);
    fwrite(&ch,          2, 1, f);
    fwrite(&sr,          4, 1, f);
    fwrite(&byte_rate,   4, 1, f);
    fwrite(&block_align, 2, 1, f);
    fwrite(&bits,        2, 1, f);

    // data chunk
    fwrite("data", 1, 4, f);
    fwrite(&data_bytes, 4, 1, f);
    fwrite(pcm.data(), sizeof(float), pcm.size(), f);
    fclose(f);
}

// CaptureEngine::SaveClip

bool CaptureEngine::SaveClip(const std::string& path, uint32_t duration_sec,
                               SharedMemoryLayout* shm,
                               std::string* error_message) {
    const uint32_t health = capture_health_flags_.load();
    if (!ring_ || !running_.load() || paused_.load() ||
            (health & (CAPTURE_HEALTH_BACKEND_FAILED |
                       CAPTURE_HEALTH_RECOVERING |
                       CAPTURE_HEALTH_PAUSED))) {
        std::cerr << "[SaveClip] Refused because capture is not producing frames"
                  << std::endl;
        if (error_message)
            *error_message = "Capture is not receiving new frames; restart capture before saving";
        return false;
    }

    const uint32_t duration_ms = duration_sec * 1000;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    const int64_t save_end_ns =
        static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
    const auto publish_timeout = std::chrono::milliseconds(
        std::max<uint32_t>(100, 3000 / std::max<uint32_t>(cfg_.fps, 1)));
    ring_->WaitUntilPublished(save_end_ns, publish_timeout);
    auto video_snapshot = ring_->TakeSnapshot(duration_ms, save_end_ns);
    const auto& video_packets = video_snapshot.packets;

    if (video_packets.empty()) {
        std::cerr << "[SaveClip] No video packets in buffer" << std::endl;
        if (error_message)
            *error_message = "Nothing to save: the replay buffer contains no video packets";
        return false;
    }

    // Get audio segment
    const uint32_t audio_duration_ms = static_cast<uint32_t>(std::max<int64_t>(
        1,
        (video_snapshot.presentation_end_ns
            - video_snapshot.presentation_start_ns + 999'999) / 1'000'000));
    std::vector<float> audio_pcm = audio_.ExtractSegment(
        video_snapshot.presentation_end_ns, audio_duration_ms);

    // Write per-category WAVs when multiband is active.
    // Python reads these, mixes with preset volumes, and deletes them.
    if (cfg_.multiband_enabled) {
        for (const auto& cat_cfg : cfg_.audio_categories) {
            std::vector<float> pcm = multi_audio_.ExtractSegment(
                cat_cfg.name,
                video_snapshot.presentation_end_ns,
                audio_duration_ms);
            if (pcm.empty()) continue;
            // Derive WAV path: strip extension, append _<sinkname>.wav
            // cat_cfg.sink_name already starts with "fthr_" (e.g. "fthr_game"),
            // so the result is "<base>_fthr_game.wav" — matching what the Python
            // mixer looks for in _multiband_mux_worker.
            std::string wav_path = path;
            size_t dot = wav_path.rfind('.');
            if (dot != std::string::npos) wav_path = wav_path.substr(0, dot);
            wav_path += "_" + cat_cfg.sink_name + ".wav";
            write_pcm_wav(wav_path, pcm,
                          AudioMultiCapture::kSampleRate,
                          AudioMultiCapture::kChannels);
        }
    }

    return save_clip_to_file(
        path,
        video_packets,
        video_snapshot.presentation_start_pts,
        audio_pcm,
        AudioCapture::kSampleRate,
        AudioCapture::kChannels,
        encoder_.GetExtradata(),
        cfg_.fps,
        encoder_.GetWidth(),
        encoder_.GetHeight(),
        encoder_.GetCodecID(),
        shm,
        error_message,
        cfg_.separate_audio_enabled,
        cfg_.bitrate_kbps
    );
}

// CaptureEngine::Reconfigure — hot-swap codec/preset without full reinit

void CaptureEngine::Reconfigure(uint32_t codec_pref, int preset) {
    Shutdown();   // stops thread, deletes ring_, stops audio
    cfg_.codec_pref = static_cast<CodecPref>(codec_pref);
    cfg_.preset     = preset;
    if (cfg_.preset < 1) cfg_.preset = 1;
    if (cfg_.preset > 7) cfg_.preset = 7;
    // Re-allocate ring (Shutdown() freed it)
    size_t ring_ms = (static_cast<size_t>(cfg_.buffer_seconds) + 5) * 1000;
    ring_ = new EncodedRingBuffer(ring_ms, cfg_.fps);
    // Restart audio (Shutdown() stopped it)
    if (cfg_.audio_enabled) {
        if (cfg_.multiband_enabled && !cfg_.audio_categories.empty()) {
            multi_audio_.Start(cfg_.audio_categories);
        } else {
            if (!audio_.Start("")) {
                std::cerr << "FTHR_STARTUP_WARNING: DESKTOP_AUDIO_UNAVAILABLE: "
                          << "Default output monitor could not be opened; "
                          << "capture continues video-only." << std::endl;
            }
        }
    }
    // Reset stale state
    nvenc_active_.store(false);
    { std::lock_guard<std::mutex> lk(codec_mutex_); active_codec_.clear(); }
    // Restart capture thread
    running_.store(true);
    cap_thread_ = std::thread(&CaptureEngine::CaptureLoop, this);
}

// CaptureEngine::GetAudioMappingsJson

static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 4);
    for (unsigned char c : s) {
        if      (c == '"')  out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c < 0x20)  { char buf[8]; snprintf(buf, sizeof(buf), "\\u%04x", c); out += buf; }
        else                out += c;
    }
    return out;
}

std::string CaptureEngine::GetAudioMappingsJson() const {
    if (!cfg_.multiband_enabled) return "{}";
    auto maps = multi_audio_.GetCurrentMappings();
    std::string json = "{";
    bool first = true;
    for (auto& [app, cat] : maps) {
        if (!first) json += ",";
        json += "\"" + json_escape(app) + "\":\"" + json_escape(cat) + "\"";
        first = false;
    }
    json += "}";
    return json;
}

} // namespace fthr
