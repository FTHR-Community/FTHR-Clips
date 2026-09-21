/*
 * vaapi_symbol_compat_test.cpp — CTest unit test for Linux VA-API symbol resolution.
 */

#include <iostream>
#include <cassert>
#include <dlfcn.h>

int main() {
    std::cout << "[Test] Probing VA-API symbol resolution..." << std::endl;
    void* h = dlopen("libva.so.2", RTLD_LAZY | RTLD_LOCAL);
    if (!h) {
        std::cout << "[Test] libva.so.2 not present on host — test skipped cleanly." << std::endl;
        return 0;
    }

    void* map2 = dlsym(h, "vaMapBuffer2");
    void* map = dlsym(h, "vaMapBuffer");

    std::cout << "[Test] vaMapBuffer2: " << map2 << ", vaMapBuffer: " << map << std::endl;
    // At least one of vaMapBuffer or vaMapBuffer2 must be present if libva.so.2 loads
    assert((map2 || map) && "Neither vaMapBuffer nor vaMapBuffer2 could be resolved from libva.so.2");

    dlclose(h);
    std::cout << "[Test] VA-API symbol resolution test PASSED." << std::endl;
    return 0;
}
