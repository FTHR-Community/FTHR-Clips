// Match Python's isolated dlopen path: no FFmpeg libraries are preloaded by
// linking this test to the bridge, so missing DT_NEEDED entries cannot hide.
#include <dlfcn.h>
#include <cstdio>

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    void* bridge = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!bridge) {
        std::fprintf(stderr, "Cannot load playback bridge: %s\n", dlerror());
        return 1;
    }
    const char* exports[] = {
        "fthr_playback_open", "fthr_playback_pull",
        "fthr_playback_set_playback_rate", "fthr_playback_seek",
        "fthr_playback_is_eof", "fthr_playback_source_failed",
        "fthr_playback_close",
    };
    for (const char* name : exports) {
        dlerror();
        dlsym(bridge, name);
        if (const char* error = dlerror()) {
            std::fprintf(stderr, "Missing playback API %s: %s\n", name, error);
            dlclose(bridge);
            return 1;
        }
    }
    return dlclose(bridge) == 0 ? 0 : 1;
}
