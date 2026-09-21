/*
 * libva_compat.c — Dynamic VA-API symbol compatibility wrapper for Linux.
 *
 * BtbN LGPL FFmpeg binaries are compiled with implib-gen trampolines that expect
 * libva.so.2 to export `vaMapBuffer2` (introduced in libva 2.19). On older LTS
 * distributions (Ubuntu 22.04 LTS / Pop!_OS 22.04 LTS, libva 2.14), calling
 * `vaMapBuffer2` causes implib-gen to fail symbol resolution and abort() the
 * capture engine process with SIGABRT (Error 006).
 *
 * This wrapper provides a transparent fallback mapping `vaMapBuffer2` to `vaMapBuffer`
 * while forwarding all other VA-API entrypoints to the system's real libva.so.2.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <dlfcn.h>

typedef void* VADisplay;
typedef unsigned int VABufferID;
typedef int VAStatus;

static void* get_real_libva(void) {
    static void* handle = NULL;
    if (!handle) {
        static const char* kPaths[] = {
            "/usr/lib/x86_64-linux-gnu/libva.so.2",
            "/lib/x86_64-linux-gnu/libva.so.2",
            "/usr/lib64/libva.so.2",
            "/usr/lib/libva.so.2",
            "/lib64/libva.so.2",
            "/lib/libva.so.2",
            "libva.so.2",
            NULL
        };
        for (int i = 0; kPaths[i]; ++i) {
            handle = dlopen(kPaths[i], RTLD_NOW | RTLD_GLOBAL);
            if (handle) break;
        }
    }
    return handle;
}

__attribute__((visibility("default")))
VAStatus vaMapBuffer2(VADisplay dpy, VABufferID buf_id, void **pbuf, uint32_t flags) {
    (void)flags;
    static VAStatus (*real_map2)(VADisplay, VABufferID, void**, uint32_t) = NULL;
    static VAStatus (*real_map)(VADisplay, VABufferID, void**) = NULL;

    void* h = get_real_libva();
    if (h) {
        if (!real_map2) {
            real_map2 = (VAStatus (*)(VADisplay, VABufferID, void**, uint32_t))dlsym(h, "vaMapBuffer2");
        }
        if (real_map2) {
            return real_map2(dpy, buf_id, pbuf, flags);
        }
        if (!real_map) {
            real_map = (VAStatus (*)(VADisplay, VABufferID, void**))dlsym(h, "vaMapBuffer");
        }
        if (real_map) {
            return real_map(dpy, buf_id, pbuf);
        }
    }
    return 1; /* VA_STATUS_ERROR_UNKNOWN */
}
