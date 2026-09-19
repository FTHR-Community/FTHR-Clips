#include "backend_portal.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <iostream>

#include <dlfcn.h>
#include <time.h>
#include <unistd.h>

#include <spa/param/format-utils.h>
#include <spa/param/video/format-utils.h>
#include <spa/pod/builder.h>
#include <spa/buffer/meta.h>

namespace fthr {

// libpipewire entry points resolved at runtime. Declared with the header
// prototypes so a mismatch is a compile error rather than a bad call.
struct PipeWireApi {
    decltype(&pw_init) init = nullptr;
    decltype(&pw_get_library_version) get_library_version = nullptr;
    decltype(&pw_thread_loop_new) thread_loop_new = nullptr;
    decltype(&pw_thread_loop_destroy) thread_loop_destroy = nullptr;
    decltype(&pw_thread_loop_start) thread_loop_start = nullptr;
    decltype(&pw_thread_loop_stop) thread_loop_stop = nullptr;
    decltype(&pw_thread_loop_lock) thread_loop_lock = nullptr;
    decltype(&pw_thread_loop_unlock) thread_loop_unlock = nullptr;
    decltype(&pw_thread_loop_get_loop) thread_loop_get_loop = nullptr;
    decltype(&pw_context_new) context_new = nullptr;
    decltype(&pw_context_destroy) context_destroy = nullptr;
    decltype(&pw_context_connect_fd) context_connect_fd = nullptr;
    decltype(&pw_core_disconnect) core_disconnect = nullptr;
    decltype(&pw_properties_new) properties_new = nullptr;
    decltype(&pw_stream_new) stream_new = nullptr;
    decltype(&pw_stream_destroy) stream_destroy = nullptr;
    decltype(&pw_stream_add_listener) stream_add_listener = nullptr;
    decltype(&pw_stream_connect) stream_connect = nullptr;
    decltype(&pw_stream_disconnect) stream_disconnect = nullptr;
    decltype(&pw_stream_update_params) stream_update_params = nullptr;
    decltype(&pw_stream_dequeue_buffer) stream_dequeue_buffer = nullptr;
    decltype(&pw_stream_queue_buffer) stream_queue_buffer = nullptr;
    decltype(&pw_stream_get_state) stream_get_state = nullptr;
    decltype(&pw_stream_state_as_string) stream_state_as_string = nullptr;
};

namespace {

// Loaded once per process; pw_init() is not meant to be repeated.
const PipeWireApi* LoadPipeWireApi(std::string* error) {
    static PipeWireApi api;
    static bool attempted = false;
    static bool loaded = false;
    static std::string load_error;
    if (!attempted) {
        attempted = true;
        void* handle = dlopen("libpipewire-0.3.so.0", RTLD_NOW | RTLD_LOCAL);
        if (!handle) {
            load_error = std::string("libpipewire-0.3.so.0 not loadable: ") + dlerror();
        } else {
            bool ok = true;
            auto resolve = [&](auto& member, const char* name) {
                member = reinterpret_cast<std::remove_reference_t<decltype(member)>>(
                    dlsym(handle, name));
                if (!member) {
                    ok = false;
                    if (load_error.empty())
                        load_error = std::string("libpipewire-0.3 lacks ") + name;
                }
            };
            resolve(api.init, "pw_init");
            resolve(api.get_library_version, "pw_get_library_version");
            resolve(api.thread_loop_new, "pw_thread_loop_new");
            resolve(api.thread_loop_destroy, "pw_thread_loop_destroy");
            resolve(api.thread_loop_start, "pw_thread_loop_start");
            resolve(api.thread_loop_stop, "pw_thread_loop_stop");
            resolve(api.thread_loop_lock, "pw_thread_loop_lock");
            resolve(api.thread_loop_unlock, "pw_thread_loop_unlock");
            resolve(api.thread_loop_get_loop, "pw_thread_loop_get_loop");
            resolve(api.context_new, "pw_context_new");
            resolve(api.context_destroy, "pw_context_destroy");
            resolve(api.context_connect_fd, "pw_context_connect_fd");
            resolve(api.core_disconnect, "pw_core_disconnect");
            resolve(api.properties_new, "pw_properties_new");
            resolve(api.stream_new, "pw_stream_new");
            resolve(api.stream_destroy, "pw_stream_destroy");
            resolve(api.stream_add_listener, "pw_stream_add_listener");
            resolve(api.stream_connect, "pw_stream_connect");
            resolve(api.stream_disconnect, "pw_stream_disconnect");
            resolve(api.stream_update_params, "pw_stream_update_params");
            resolve(api.stream_dequeue_buffer, "pw_stream_dequeue_buffer");
            resolve(api.stream_queue_buffer, "pw_stream_queue_buffer");
            resolve(api.stream_get_state, "pw_stream_get_state");
            resolve(api.stream_state_as_string, "pw_stream_state_as_string");
            if (ok) {
                api.init(nullptr, nullptr);
                loaded = true;
                std::cerr << "[PortalBackend] libpipewire " << api.get_library_version()
                          << std::endl;
            }
        }
    }
    if (!loaded && error) *error = load_error;
    return loaded ? &api : nullptr;
}

int64_t MonotonicNowNs() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1'000'000'000LL + ts.tv_nsec;
}

void StreamStateChanged(void* data, pw_stream_state old_state,
                        pw_stream_state state, const char* error) {
    static_cast<PortalBackend*>(data)->OnStreamStateChanged(old_state, state, error);
}
void StreamParamChanged(void* data, uint32_t id, const spa_pod* param) {
    static_cast<PortalBackend*>(data)->OnStreamParamChanged(id, param);
}
void StreamProcess(void* data) {
    static_cast<PortalBackend*>(data)->OnStreamProcess();
}
void CoreError(void* data, uint32_t id, int seq, int res, const char* message) {
    static_cast<PortalBackend*>(data)->OnCoreError(id, seq, res, message);
}

const pw_stream_events kStreamEvents = [] {
    pw_stream_events events{};
    events.version = PW_VERSION_STREAM_EVENTS;
    events.state_changed = StreamStateChanged;
    events.param_changed = StreamParamChanged;
    events.process = StreamProcess;
    return events;
}();

const pw_core_events kCoreEvents = [] {
    pw_core_events events{};
    events.version = PW_VERSION_CORE_EVENTS;
    events.error = CoreError;
    return events;
}();

// EnumFormat offer: raw video in one of the CPU-readable formats, any size,
// and a frame rate hint the compositor may use to throttle delivery. No
// modifier property is offered, so DMA-BUF-only formats fail negotiation and
// the compositor falls back to memfd buffers we can mmap.
const spa_pod* BuildFormatParam(spa_pod_builder* b, uint32_t fps) {
    const auto& formats = PreferredSpaVideoFormats();
    spa_pod_frame object{}, choice{};
    spa_pod_builder_push_object(b, &object, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat);
    spa_pod_builder_add(b,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        0);
    spa_pod_builder_prop(b, SPA_FORMAT_VIDEO_format, 0);
    spa_pod_builder_push_choice(b, &choice, SPA_CHOICE_Enum, 0);
    spa_pod_builder_id(b, formats.front());   // default comes first
    for (uint32_t format : formats) spa_pod_builder_id(b, format);
    spa_pod_builder_pop(b, &choice);

    spa_rectangle size_default{1920, 1080}, size_min{1, 1}, size_max{16384, 16384};
    spa_fraction rate_default{fps, 1}, rate_min{0, 1}, rate_max{1000, 1};
    spa_fraction max_rate_min{1, 1};
    spa_pod_builder_add(b,
        SPA_FORMAT_VIDEO_size,
            SPA_POD_CHOICE_RANGE_Rectangle(&size_default, &size_min, &size_max),
        SPA_FORMAT_VIDEO_framerate,
            SPA_POD_CHOICE_RANGE_Fraction(&rate_default, &rate_min, &rate_max),
        SPA_FORMAT_VIDEO_maxFramerate,
            SPA_POD_CHOICE_RANGE_Fraction(&rate_default, &max_rate_min, &rate_max),
        0);
    return static_cast<const spa_pod*>(spa_pod_builder_pop(b, &object));
}

} // namespace

bool PortalBackend::KeepRunning() const noexcept {
    return !running_ || running_->load();
}

void PortalBackend::Fail(const std::string& reason) {
    {
        std::lock_guard<std::mutex> lk(frame_mutex_);
        if (!failed_) stream_error_ = reason;
        failed_ = true;
    }
    frame_cv_.notify_all();
}

// PipeWire callbacks (loop thread)

void PortalBackend::OnStreamStateChanged(pw_stream_state old_state,
                                         pw_stream_state state,
                                         const char* error) {
    std::cerr << "[PortalBackend] Stream " << pw_->stream_state_as_string(old_state)
              << " -> " << pw_->stream_state_as_string(state)
              << (error ? std::string(": ") + error : std::string()) << std::endl;
    if (state == PW_STREAM_STATE_ERROR) {
        Fail(std::string("PipeWire stream error: ") + (error ? error : "unknown"));
    } else if (state == PW_STREAM_STATE_UNCONNECTED &&
               old_state != PW_STREAM_STATE_UNCONNECTED && !shutting_down_) {
        // Either the connect never took (node gone) or the portal session
        // ended later (user stopped sharing, output removed).
        Fail("PipeWire stream disconnected");
    }
}

void PortalBackend::OnStreamParamChanged(uint32_t id, const spa_pod* param) {
    if (id != SPA_PARAM_Format || !param) return;
    uint32_t media_type = 0, media_subtype = 0;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0 ||
            media_type != SPA_MEDIA_TYPE_video ||
            media_subtype != SPA_MEDIA_SUBTYPE_raw)
        return;
    spa_video_info_raw info{};
    if (spa_format_video_raw_parse(param, &info) < 0) {
        Fail("PipeWire format could not be parsed");
        return;
    }
    if (spa_pod_find_prop(param, nullptr, SPA_FORMAT_VIDEO_modifier)) {
        Fail("compositor offers this screen only as DMA-BUF, which this build does not import");
        return;
    }
    PixelLayout layout;
    if (!DescribeSpaVideoFormat(info.format, layout)) {
        Fail("compositor negotiated an unsupported pixel format (" +
             std::to_string(info.format) + ")");
        return;
    }
    if (info.size.width == 0 || info.size.height == 0) {
        Fail("compositor negotiated an empty frame size");
        return;
    }
    {
        std::lock_guard<std::mutex> lk(frame_mutex_);
        if (have_format_ &&
                (info.size.width != native_w_ || info.size.height != native_h_)) {
            // The encoder is sized once per generation; a resolution change
            // restarts the generation through the normal recovery path.
            stream_error_ = "screen size changed";
            failed_ = true;
        } else {
            native_w_ = info.size.width;
            native_h_ = info.size.height;
            layout_ = layout;
            const size_t bytes = static_cast<size_t>(native_w_) * native_h_ * 4;
            pending_.assign(bytes, 0);
            front_.assign(bytes, 0);
            have_format_ = true;
        }
    }
    // Log before waking the capture thread, which logs "Ready" right away.
    std::cerr << "[PortalBackend] Format " << SpaVideoFormatName(info.format)
              << " " << info.size.width << "x" << info.size.height
              << " @ " << info.framerate.num << "/" << info.framerate.denom
              << " (max " << info.max_framerate.num << "/" << info.max_framerate.denom
              << ")" << std::endl;
    frame_cv_.notify_all();

    // Buffer negotiation: memfd or plain memory only (matches the format
    // offer), plus the header meta that carries the presentation timestamp.
    uint8_t buffer[1024];
    spa_pod_builder b{};
    spa_pod_builder_init(&b, buffer, sizeof(buffer));
    const int32_t stride = static_cast<int32_t>(native_w_ * 4);
    const int32_t size = stride * static_cast<int32_t>(native_h_);
    const spa_pod* params[2];
    params[0] = static_cast<const spa_pod*>(spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 16),
        SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
        SPA_PARAM_BUFFERS_size, SPA_POD_Int(size),
        SPA_PARAM_BUFFERS_stride, SPA_POD_Int(stride),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int(
            (1 << SPA_DATA_MemFd) | (1 << SPA_DATA_MemPtr))));
    params[1] = static_cast<const spa_pod*>(spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
        SPA_PARAM_META_size, SPA_POD_Int(sizeof(spa_meta_header))));
    pw_->stream_update_params(stream_, params, 2);
}

void PortalBackend::OnStreamProcess() {
    pw_buffer* pwb = pw_->stream_dequeue_buffer(stream_);
    if (!pwb) return;
    spa_buffer* buf = pwb->buffer;
    const spa_data& d = buf->datas[0];
    const auto* header = static_cast<const spa_meta_header*>(
        spa_buffer_find_meta_data(buf, SPA_META_Header, sizeof(spa_meta_header)));
    const bool corrupted = header && (header->flags & SPA_META_HEADER_FLAG_CORRUPTED);
    if (d.data && d.chunk && d.chunk->size > 0 && !corrupted &&
            d.type != SPA_DATA_DmaBuf && !(d.chunk->flags & SPA_CHUNK_FLAG_CORRUPTED)) {
        std::lock_guard<std::mutex> lk(frame_mutex_);
        if (have_format_ && !failed_) {
            const size_t stride = d.chunk->stride > 0
                ? static_cast<size_t>(d.chunk->stride)
                : static_cast<size_t>(native_w_) * 4;
            const size_t needed = d.chunk->offset +
                stride * (native_h_ - 1) + static_cast<size_t>(native_w_) * 4;
            if (stride >= static_cast<size_t>(native_w_) * 4 && needed <= d.maxsize) {
                CopyFrameRows(pending_.data(), static_cast<size_t>(native_w_) * 4,
                              static_cast<const uint8_t*>(d.data) + d.chunk->offset,
                              stride, native_w_, native_h_, layout_.swap_rb);
                // Compositors stamp pts from CLOCK_MONOTONIC; anything far
                // from now (a different clock, or zero) falls back to ours.
                const int64_t now = MonotonicNowNs();
                const int64_t pts = header ? header->pts : 0;
                pending_pts_ns_ = (pts > 0 && std::llabs(now - pts) < 1'000'000'000LL)
                    ? pts : now;
                ++frame_seq_;
            }
        }
    }
    pw_->stream_queue_buffer(stream_, pwb);
    frame_cv_.notify_all();
}

void PortalBackend::OnCoreError(uint32_t id, int seq, int res, const char* message) {
    std::cerr << "[PortalBackend] PipeWire core error id=" << id << " seq=" << seq
              << " res=" << res << ": " << (message ? message : "") << std::endl;
    if (id == PW_ID_CORE) Fail(std::string("PipeWire connection lost: ") + (message ? message : ""));
}

// Setup

bool PortalBackend::OpenPortalSession(const CaptureConfig& cfg) {
    session_ = std::make_unique<PortalScreenCastSession>(dbus_);
    PortalScreenCastOptions options;
    restore_token_path_ = PortalRestoreTokenPath(std::getenv("HOME"));
    options.restore_token = LoadPortalRestoreToken(restore_token_path_);
    if (!options.restore_token.empty())
        std::cerr << "[PortalBackend] Presenting saved restore token" << std::endl;
    if (!cfg.target_output.empty()) {
        std::cerr << "[PortalBackend] The portal dialog chooses the screen; "
                  << "the configured output '" << cfg.target_output
                  << "' cannot be preselected" << std::endl;
    }

    std::string detail;
    const PortalOutcome outcome = session_->Open(
        options, [this] { return KeepRunning(); }, &detail);
    if (!detail.empty())
        std::cerr << "[PortalBackend] " << PortalOutcomeName(outcome) << ": " << detail << std::endl;
    switch (outcome) {
    case PortalOutcome::Ok:
        break;
    case PortalOutcome::Unavailable:
        failure_reason_ = "the ScreenCast portal is not available (is xdg-desktop-portal "
                          "with a backend for this desktop installed?)";
        return false;
    case PortalOutcome::Cancelled:
        failure_reason_ = "Screen sharing was declined in the desktop's dialog. "
                          "Restart the engine and pick a screen to capture.";
        retry_pointless_ = true;
        return false;
    case PortalOutcome::TimedOut:
        failure_reason_ = "The desktop's screen sharing dialog was not answered in time. "
                          "Restart the engine and pick a screen to capture.";
        retry_pointless_ = true;
        return false;
    case PortalOutcome::Failed:
        failure_reason_ = "the ScreenCast portal refused the capture session: " + detail;
        return false;
    case PortalOutcome::Interrupted:
        return false;
    }

    // The token that came back supersedes the one we presented; an empty one
    // means the backend does not persist and the file must not linger.
    if (session_->RestoreToken() != options.restore_token &&
            !SavePortalRestoreToken(restore_token_path_, session_->RestoreToken())) {
        std::cerr << "[PortalBackend] Could not persist the restore token at "
                  << restore_token_path_ << std::endl;
    }
    for (const auto& stream : session_->Streams()) {
        std::cerr << "[PortalBackend] Stream node " << stream.node_id
                  << " size " << stream.width << "x" << stream.height
                  << " at " << stream.x << "," << stream.y
                  << " source_type " << stream.source_type << std::endl;
    }
    return true;
}

bool PortalBackend::ConnectStream(int pipewire_fd, uint32_t node_id, uint32_t fps) {
    loop_ = pw_->thread_loop_new("fthr-portal-capture", nullptr);
    if (!loop_) { close(pipewire_fd); failure_reason_ = "PipeWire loop could not be created"; return false; }
    context_ = pw_->context_new(pw_->thread_loop_get_loop(loop_), nullptr, 0);
    if (!context_) { close(pipewire_fd); failure_reason_ = "PipeWire context could not be created"; return false; }
    if (pw_->thread_loop_start(loop_) < 0) {
        close(pipewire_fd);
        failure_reason_ = "PipeWire loop thread could not be started";
        return false;
    }

    pw_->thread_loop_lock(loop_);
    // The core owns the descriptor from here on, including on failure.
    core_ = pw_->context_connect_fd(context_, pipewire_fd, nullptr, 0);
    if (!core_) {
        pw_->thread_loop_unlock(loop_);
        failure_reason_ = std::string("PipeWire connection over the portal socket failed: ")
            + strerror(errno);
        return false;
    }
    pw_core_add_listener(core_, &core_listener_, &kCoreEvents, this);

    stream_ = pw_->stream_new(core_, "FTHR Clips capture",
        pw_->properties_new(
            PW_KEY_MEDIA_TYPE, "Video",
            PW_KEY_MEDIA_CATEGORY, "Capture",
            PW_KEY_MEDIA_ROLE, "Screen",
            nullptr));
    if (!stream_) {
        pw_->thread_loop_unlock(loop_);
        failure_reason_ = "PipeWire stream could not be created";
        return false;
    }
    pw_->stream_add_listener(stream_, &stream_listener_, &kStreamEvents, this);

    uint8_t buffer[1024];
    spa_pod_builder b{};
    spa_pod_builder_init(&b, buffer, sizeof(buffer));
    const spa_pod* params[1] = { BuildFormatParam(&b, fps) };
    // target_id is the portal's node id; MAP_BUFFERS gives the process
    // callback CPU pointers to the memfd buffers the compositor allocates.
    const int res = pw_->stream_connect(stream_, PW_DIRECTION_INPUT, node_id,
        static_cast<pw_stream_flags>(PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS),
        params, 1);
    pw_->thread_loop_unlock(loop_);
    if (res < 0) {
        failure_reason_ = std::string("PipeWire stream connect failed: ") + strerror(-res);
        return false;
    }

    const auto deadline = std::chrono::steady_clock::now() + kStreamTimeout;
    std::unique_lock<std::mutex> lk(frame_mutex_);
    while (!have_format_ && !failed_) {
        if (!KeepRunning()) return false;
        if (std::chrono::steady_clock::now() >= deadline) {
            failure_reason_ = "PipeWire stream did not negotiate a format within 10 s";
            return false;
        }
        frame_cv_.wait_for(lk, kWaitSlice);
    }
    if (failed_) {
        failure_reason_ = stream_error_;
        return false;
    }
    return true;
}

bool PortalBackend::Initialize(const CaptureConfig& cfg) {
    failure_reason_.clear();
    retry_pointless_ = false;
    frame_interval_ = std::chrono::nanoseconds(
        1'000'000'000LL / std::max<uint32_t>(1, cfg.fps));

    std::string load_error;
    if (!LoadDBusApi(dbus_, &load_error)) {
        std::cerr << "[PortalBackend] " << load_error << std::endl;
        failure_reason_ = "the ScreenCast portal needs libdbus-1, which is not installed";
        return false;
    }
    pw_ = LoadPipeWireApi(&load_error);
    if (!pw_) {
        std::cerr << "[PortalBackend] " << load_error << std::endl;
        failure_reason_ = "the ScreenCast portal needs libpipewire-0.3, which is not installed";
        return false;
    }

    if (!OpenPortalSession(cfg)) { Shutdown(); return false; }

    std::string detail;
    const int fd = session_->OpenPipeWireRemote(&detail);
    if (fd < 0) {
        std::cerr << "[PortalBackend] " << detail << std::endl;
        failure_reason_ = "the ScreenCast portal did not hand out a PipeWire connection: " + detail;
        Shutdown();
        return false;
    }
    if (!ConnectStream(fd, session_->Streams().front().node_id, cfg.fps)) {
        if (!failure_reason_.empty())
            std::cerr << "[PortalBackend] " << failure_reason_ << std::endl;
        Shutdown();
        return false;
    }
    std::cerr << "[PortalBackend] Ready: " << native_w_ << "x" << native_h_ << std::endl;
    return true;
}

bool PortalBackend::CaptureFrame(RawFrame& out) {
    // Session.Closed arrives over D-Bus; checking twice a second at 60 fps is
    // plenty, and PipeWire reports the lost node on its own anyway.
    if (session_ && (++poll_counter_ % 30 == 0) && session_->PollClosed()) {
        std::cerr << "[PortalBackend] Portal session closed by the desktop" << std::endl;
        return false;
    }

    std::unique_lock<std::mutex> lk(frame_mutex_);
    // Compositors only push frames when something changed. Once a frame
    // exists, wait until one frame interval after the previous delivery and
    // then repeat the last image, so a static desktop still fills the replay
    // buffer at the target rate. Anchoring on the previous delivery rather
    // than on this call keeps the encoder's own time out of the cadence.
    const auto start = std::chrono::steady_clock::now();
    const auto deadline = have_front_
        ? last_delivery_ + std::chrono::duration_cast<std::chrono::steady_clock::duration>(frame_interval_)
        : start + std::chrono::duration_cast<std::chrono::steady_clock::duration>(kFirstFrameTimeout);
    while (frame_seq_ == consumed_seq_ && !failed_) {
        if (!KeepRunning()) return false;
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) break;
        frame_cv_.wait_until(lk, std::min(deadline, now + kWaitSlice));
    }
    if (failed_) {
        std::cerr << "[PortalBackend] " << stream_error_ << std::endl;
        return false;
    }
    int64_t timestamp_ns;
    if (frame_seq_ != consumed_seq_) {
        std::swap(front_, pending_);
        consumed_seq_ = frame_seq_;
        timestamp_ns = pending_pts_ns_;
        have_front_ = true;
    } else if (have_front_) {
        timestamp_ns = MonotonicNowNs();
    } else {
        std::cerr << "[PortalBackend] No frame arrived within 5 s of stream start" << std::endl;
        return false;
    }
    last_delivery_ = std::chrono::steady_clock::now();
    out.data = front_.data();
    out.stride = native_w_ * 4;
    out.width = native_w_;
    out.height = native_h_;
    out.av_pix_fmt = layout_.av_pix_fmt;
    out.timestamp_ns = timestamp_ns;
    return true;
}

void PortalBackend::DestroyStream() {
    if (!pw_) return;
    if (loop_) {
        pw_->thread_loop_lock(loop_);
        shutting_down_ = true;
        if (stream_) {
            pw_->stream_disconnect(stream_);
            pw_->stream_destroy(stream_);
            stream_ = nullptr;
        }
        pw_->thread_loop_unlock(loop_);
        pw_->thread_loop_stop(loop_);
    }
    if (core_) { pw_->core_disconnect(core_); core_ = nullptr; }
    if (context_) { pw_->context_destroy(context_); context_ = nullptr; }
    if (loop_) { pw_->thread_loop_destroy(loop_); loop_ = nullptr; }
    spa_zero(stream_listener_);
    spa_zero(core_listener_);
}

void PortalBackend::Shutdown() {
    DestroyStream();
    // Closing the D-Bus session tells the desktop to drop its sharing indicator.
    session_.reset();
    std::lock_guard<std::mutex> lk(frame_mutex_);
    have_format_ = false;
    have_front_ = false;
    failed_ = false;
    shutting_down_ = false;
    frame_seq_ = consumed_seq_ = 0;
    pending_.clear();
    front_.clear();
}

} // namespace fthr
