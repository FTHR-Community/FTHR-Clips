// FTHR Clips. GPL-3.0-only. Private libobs host for a single Minecraft window.
#define NOMINMAX
#include <windows.h>
#include <dxgi1_2.h>
#include <wrl/client.h>
#include <obs.h>
#include <util/windows/window-helpers.h>
#include <graphics-hook-info.h>
#include <atomic>
#include <filesystem>
#include <string>
#include <cerrno>

namespace fs = std::filesystem;
using Microsoft::WRL::ComPtr;
struct Mailbox { volatile LONG state, error, stop, reserved; };
static_assert(sizeof(Mailbox) == 16);

// The provider's exact-title mode can match a second window with the same
// title. Reject ambiguous selections before activating or accepting the source.
struct TitleMatch {
    HWND target;
    std::wstring title;
    bool duplicate = false;
};
static std::wstring window_title(HWND hwnd) {
    const int length = GetWindowTextLengthW(hwnd);
    if (length <= 0) return {};
    std::wstring title(static_cast<size_t>(length) + 1, L'\0');
    title.resize(GetWindowTextW(hwnd, title.data(), length + 1));
    return title;
}
static BOOL CALLBACK check_title(HWND hwnd, LPARAM param) {
    auto &match = *reinterpret_cast<TitleMatch *>(param);
    if (hwnd != match.target && IsWindowVisible(hwnd) &&
        CompareStringOrdinal(window_title(hwnd).c_str(), -1,
                             match.title.c_str(), -1, TRUE) == CSTR_EQUAL) {
        match.duplicate = true;
        return FALSE;
    }
    return TRUE;
}
static bool unique_title(HWND hwnd) {
    TitleMatch match{hwnd, window_title(hwnd)};
    return !match.title.empty() &&
        EnumWindows(check_title, reinterpret_cast<LPARAM>(&match)) && !match.duplicate;
}

struct Session {
    HWND window{};
    DWORD pid{};
    HANDLE parent{}, event{}, info_mapping{};
    Mailbox *box{};
    const volatile hook_info *info{};
    uint32_t width{}, height{};
    obs_source_t *source{};
    std::atomic<bool> attached{false}, disconnected{false};

    bool target_alive() const {
        DWORD owner = 0;
        GetWindowThreadProcessId(window, &owner);
        return owner == pid && WaitForSingleObject(parent, 0) == WAIT_TIMEOUT;
    }

    // OBS's source picker is title-based. Verify the actual render window from
    // the pinned provider's read-only capture metadata before publishing frames.
    bool exact_surface() {
        if (!info) {
            std::wstring name = SHMEM_HOOK_INFO + std::to_wstring(pid);
            info_mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name.c_str());
            if (!info_mapping) return false;
            info = static_cast<const volatile hook_info *>(
                MapViewOfFile(info_mapping, FILE_MAP_READ, 0, 0, sizeof(hook_info)));
            if (!info) { CloseHandle(info_mapping); info_mapping = nullptr; return false; }
        }
        return info->window == HandleToULong(window) && info->cx && info->cy;
    }
    ~Session() {
        if (info) UnmapViewOfFile(const_cast<const hook_info *>(info));
        if (info_mapping) CloseHandle(info_mapping);
        if (box) UnmapViewOfFile(box);
    }
};
static void hooked(void *opaque, calldata_t *) {
    auto &s = *static_cast<Session *>(opaque);
    if (!s.target_alive() || !unique_title(s.window)) {
        s.disconnected.store(true);
        return;
    }
    s.attached.store(true);
}
static void unhooked(void *opaque, calldata_t *) {
    auto &s = *static_cast<Session *>(opaque);
    s.attached.store(false);
    s.disconnected.store(true);
}
static void frame(void *opaque, video_data *data) {
    auto &s = *static_cast<Session *>(opaque);
    if (!s.attached.load() || s.disconnected.load() || !s.target_alive() ||
        !s.exact_surface() || !data->data[0] || data->linesize[0] < s.width * 4 ||
        !obs_source_get_width(s.source) || !obs_source_get_height(s.source)) return;
    if (InterlockedCompareExchange(&s.box->state, 1, 0) != 0) return;
    auto *dst = reinterpret_cast<uint8_t *>(s.box + 1);
    size_t stride = size_t(s.width) * 4;
    for (uint32_t y = 0; y < s.height; ++y)
        memcpy(dst + y * stride, data->data[0] + y * size_t(data->linesize[0]), stride);
    InterlockedExchange(&s.box->state, 2);
    SetEvent(s.event);
}
static std::string escaped(const char *str) {
    std::string out;
    for (; *str; ++str) {
        if (*str == '#') out += "#22";
        else if (*str == ':') out += "#3A";
        else out += *str;
    }
    return out;
}
struct WindowName {
    dstr title{}, klass{}, exe{};
    ~WindowName() { dstr_free(&title); dstr_free(&klass); dstr_free(&exe); }
};
static uint32_t adapter_for(HWND window, bool &multiple_gpus) {
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 0;
    unsigned hardware_adapters = 0;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        if (FAILED(factory->EnumAdapters1(i, &adapter))) break;
        DXGI_ADAPTER_DESC1 desc{};
        if (SUCCEEDED(adapter->GetDesc1(&desc)) && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE))
            ++hardware_adapters;
    }
    multiple_gpus = hardware_adapters > 1;
    auto monitor = MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST);
    for (UINT i=0;; ++i) {
        ComPtr<IDXGIAdapter> adapter;
        if (FAILED(factory->EnumAdapters(i, &adapter))) break;
        for (UINT j=0;; ++j) {
            ComPtr<IDXGIOutput> output;
            if (FAILED(adapter->EnumOutputs(j, &output))) break;
            DXGI_OUTPUT_DESC desc{};
            if (SUCCEEDED(output->GetDesc(&desc)) && desc.Monitor == monitor) return i;
        }
    }
    return 0;
}
static int capture(Session &s, const fs::path &dir, uint32_t fps, bool cursor) {
    WindowName id;
    ms_get_window_title(&id.title, s.window);
    ms_get_window_class(&id.klass, s.window);
    if (!unique_title(s.window) || !ms_get_window_exe(&id.exe, s.window) || !id.title.array || !id.klass.array ||
        (_stricmp(id.exe.array,"javaw.exe") && _stricmp(id.exe.array,"java.exe")) ||
        (_stricmp(id.klass.array,"GLFW30") && _stricmp(id.klass.array,"LWJGL") &&
         _stricmp(id.klass.array,"SDL_app")) ||
        _strnicmp(id.title.array,"Minecraft",9) ||
        ms_find_window(INCLUDE_MINIMIZED, WINDOW_PRIORITY_TITLE, id.klass.array,
                       id.title.array, id.exe.array) != s.window) return 11;

    auto effects = (dir / "data/libobs").generic_u8string() + "/";
    obs_add_data_path(effects.c_str());
    obs_video_info video{};
    video.graphics_module = "libobs-d3d11.dll";
    bool multiple_gpus = false;
    video.adapter = adapter_for(s.window, multiple_gpus);
    video.base_width = video.output_width = s.width;
    video.base_height = video.output_height = s.height;
    video.fps_num = fps; video.fps_den = 1;
    video.output_format = VIDEO_FORMAT_BGRA;
    video.colorspace = VIDEO_CS_SRGB;
    video.range = VIDEO_RANGE_FULL;
    if (obs_reset_video(&video) != OBS_VIDEO_SUCCESS) return 12;

    obs_module_t *mod = nullptr;
    auto module = (dir / "win-capture.dll").u8string();
    auto assets = (dir / "data/win-capture").u8string();
    if (obs_open_module(&mod, module.c_str(), assets.c_str()) != MODULE_SUCCESS ||
        !obs_init_module(mod)) return 13;
    obs_post_load_modules();
    auto *settings = obs_data_create();
    const std::string suffix = ":" + escaped(id.klass.array) + ":" + escaped(id.exe.array);
    auto name = escaped(id.title.array) + suffix;
    obs_data_set_string(settings, "capture_mode", "window");
    obs_data_set_string(settings, "window", name.c_str());
    obs_data_set_int(settings, "priority", WINDOW_PRIORITY_TITLE);
    // Java may render on a different GPU from the selected display on hybrid laptops.
    obs_data_set_bool(settings, "sli_compatibility", multiple_gpus);
    obs_data_set_bool(settings, "capture_cursor", cursor);
    obs_data_set_bool(settings, "anti_cheat_hook", false);
    obs_data_set_bool(settings, "limit_framerate", true);
    s.source = obs_source_create_private("game_capture", "Minecraft", settings);
    obs_data_release(settings);
    if (!s.source) return 14;
    auto *signals = obs_source_get_signal_handler(s.source);
    signal_handler_connect(signals,"hooked",hooked,&s);
    signal_handler_connect(signals,"unhooked",unhooked,&s);
    auto *scene = obs_scene_create_private("Game");
    auto *item = scene ? obs_scene_add(scene, s.source) : nullptr;
    if (item) {
        vec2 bounds{}; bounds.x = float(s.width); bounds.y = float(s.height);
        obs_sceneitem_set_bounds_type(item, OBS_BOUNDS_SCALE_INNER);
        obs_sceneitem_set_bounds(item, &bounds);
        obs_set_output_source(0, obs_scene_get_source(scene));
        obs_add_raw_video_callback(nullptr, frame, &s);
        while (!s.box->stop && !s.disconnected.load() && s.target_alive()) {
            if (!s.attached.load()) {
                dstr current{};
                ms_get_window_title(&current, s.window);
                const std::string title = current.array ? current.array : "";
                dstr_free(&current);
                if (title.empty() || !unique_title(s.window) ||
                    ms_find_window(INCLUDE_MINIMIZED, WINDOW_PRIORITY_TITLE,
                                   id.klass.array, title.c_str(), id.exe.array) != s.window)
                    break;
                auto latest = escaped(title.c_str()) + suffix;
                if (latest != name) {
                    auto *update = obs_data_create();
                    obs_data_set_string(update, "window", latest.c_str());
                    obs_source_update(s.source, update);
                    obs_data_release(update);
                    name = std::move(latest);
                }
            }
            if (WaitForSingleObject(s.parent, 75) != WAIT_TIMEOUT) break;
        }
        obs_remove_raw_video_callback(frame, &s);
        obs_set_output_source(0, nullptr);
    }
    signal_handler_disconnect(signals,"hooked",hooked,&s);
    signal_handler_disconnect(signals,"unhooked",unhooked,&s);
    if (scene) obs_scene_release(scene);
    obs_source_release(s.source);
    return item && s.box->stop ? 0 : 15;
}
int wmain(int argc, wchar_t **argv) {
    if (argc != 9) return 2;
    uint64_t args[8]{};
    for (int i=0; i<8; ++i) {
        wchar_t *end = nullptr;
        errno = 0; args[i] = wcstoull(argv[i+1], &end, 10);
        if (errno || !*argv[i+1] || *end || *argv[i+1] == L'-') return 2;
    }
    if (!args[4] || args[4]>16384 || !args[5] || args[5]>16384 ||
        !args[6] || args[6]>1000 || args[7]>1) return 2;
    Session s;
    s.window = reinterpret_cast<HWND>(args[0]);
    GetWindowThreadProcessId(s.window,&s.pid);
    s.event = reinterpret_cast<HANDLE>(args[2]); s.parent = reinterpret_cast<HANDLE>(args[3]);
    s.width = uint32_t(args[4]); s.height = uint32_t(args[5]);
    s.box = static_cast<Mailbox *>(MapViewOfFile(reinterpret_cast<HANDLE>(args[1]),
        FILE_MAP_WRITE, 0,0, sizeof(Mailbox) + size_t(s.width)*s.height*4));
    if (!s.box || !s.pid || !s.target_alive()) return 3;
    wchar_t path[32768]{};
    GetModuleFileNameW(nullptr,path,32768);
    auto dir=fs::path(path).parent_path();
    SetCurrentDirectoryW(dir.c_str());
    int result=10;
    if (obs_get_version() == LIBOBS_API_VER && obs_startup("en-US", nullptr, nullptr)) {
        result = capture(s,dir,uint32_t(args[6]),args[7] != 0);
        obs_shutdown();
    }
    if (result) { InterlockedExchange(&s.box->error,1); SetEvent(s.event); }
    return result;
}
