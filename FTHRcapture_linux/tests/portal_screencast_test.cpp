// Covers the portal helpers that need no desktop: object-path derivation,
// restore-token persistence, Response parsing, and the pixel-format glue.
// The D-Bus messages are assembled in-process with the real libdbus, so the
// parser is exercised through the same DBusApi table the engine fills by
// dlsym(); no bus connection is involved.

#include "pipewire_frame.h"
#include "portal_screencast.h"

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <spa/param/video/raw.h>
#include <sys/stat.h>
#include <unistd.h>
extern "C" {
#include <libavutil/pixfmt.h>
}

namespace {

fthr::DBusApi DirectApi() {
    fthr::DBusApi api;
    api.error_init = dbus_error_init;
    api.error_free = dbus_error_free;
    api.error_is_set = dbus_error_is_set;
    api.message_unref = dbus_message_unref;
    api.message_iter_init = dbus_message_iter_init;
    api.message_iter_init_append = dbus_message_iter_init_append;
    api.message_iter_open_container = dbus_message_iter_open_container;
    api.message_iter_close_container = dbus_message_iter_close_container;
    api.message_iter_append_basic = dbus_message_iter_append_basic;
    api.message_iter_get_arg_type = dbus_message_iter_get_arg_type;
    api.message_iter_get_basic = dbus_message_iter_get_basic;
    api.message_iter_recurse = dbus_message_iter_recurse;
    api.message_iter_next = dbus_message_iter_next;
    return api;
}

void AppendVariantString(DBusMessageIter* dict, const char* key, const char* value) {
    DBusMessageIter entry, variant;
    dbus_message_iter_open_container(dict, DBUS_TYPE_DICT_ENTRY, nullptr, &entry);
    dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key);
    dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "s", &variant);
    dbus_message_iter_append_basic(&variant, DBUS_TYPE_STRING, &value);
    dbus_message_iter_close_container(&entry, &variant);
    dbus_message_iter_close_container(dict, &entry);
}

void AppendVariantUint32(DBusMessageIter* dict, const char* key, uint32_t value) {
    DBusMessageIter entry, variant;
    dbus_uint32_t v = value;
    dbus_message_iter_open_container(dict, DBUS_TYPE_DICT_ENTRY, nullptr, &entry);
    dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key);
    dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "u", &variant);
    dbus_message_iter_append_basic(&variant, DBUS_TYPE_UINT32, &v);
    dbus_message_iter_close_container(&entry, &variant);
    dbus_message_iter_close_container(dict, &entry);
}

void AppendVariantPair(DBusMessageIter* dict, const char* key, int32_t a, int32_t b) {
    DBusMessageIter entry, variant, pair;
    dbus_int32_t x = a, y = b;
    dbus_message_iter_open_container(dict, DBUS_TYPE_DICT_ENTRY, nullptr, &entry);
    dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key);
    dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "(ii)", &variant);
    dbus_message_iter_open_container(&variant, DBUS_TYPE_STRUCT, nullptr, &pair);
    dbus_message_iter_append_basic(&pair, DBUS_TYPE_INT32, &x);
    dbus_message_iter_append_basic(&pair, DBUS_TYPE_INT32, &y);
    dbus_message_iter_close_container(&variant, &pair);
    dbus_message_iter_close_container(&entry, &variant);
    dbus_message_iter_close_container(dict, &entry);
}

struct StreamSpec {
    uint32_t node;
    int32_t w, h, x, y;
    uint32_t source_type;
};

// Builds Response(u code, a{sv} results) the way xdg-desktop-portal emits it
// for Start(): results = {streams: a(ua{sv}), restore_token: s}.
DBusMessage* BuildStartResponse(uint32_t code, const std::vector<StreamSpec>& streams,
                                const char* restore_token) {
    DBusMessage* msg = dbus_message_new_signal(
        "/org/freedesktop/portal/desktop/request/1_7/tok",
        "org.freedesktop.portal.Request", "Response");
    DBusMessageIter it, results;
    dbus_message_iter_init_append(msg, &it);
    dbus_uint32_t c = code;
    dbus_message_iter_append_basic(&it, DBUS_TYPE_UINT32, &c);
    dbus_message_iter_open_container(&it, DBUS_TYPE_ARRAY, "{sv}", &results);
    if (!streams.empty()) {
        DBusMessageIter entry, variant, array;
        const char* key = "streams";
        dbus_message_iter_open_container(&results, DBUS_TYPE_DICT_ENTRY, nullptr, &entry);
        dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key);
        dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "a(ua{sv})", &variant);
        dbus_message_iter_open_container(&variant, DBUS_TYPE_ARRAY, "(ua{sv})", &array);
        for (const auto& s : streams) {
            DBusMessageIter item, props;
            dbus_uint32_t node = s.node;
            dbus_message_iter_open_container(&array, DBUS_TYPE_STRUCT, nullptr, &item);
            dbus_message_iter_append_basic(&item, DBUS_TYPE_UINT32, &node);
            dbus_message_iter_open_container(&item, DBUS_TYPE_ARRAY, "{sv}", &props);
            AppendVariantPair(&props, "size", s.w, s.h);
            AppendVariantPair(&props, "position", s.x, s.y);
            AppendVariantUint32(&props, "source_type", s.source_type);
            // Unknown keys must be skipped, not rejected.
            AppendVariantString(&props, "id", "DP-2");
            dbus_message_iter_close_container(&item, &props);
            dbus_message_iter_close_container(&array, &item);
        }
        dbus_message_iter_close_container(&variant, &array);
        dbus_message_iter_close_container(&entry, &variant);
        dbus_message_iter_close_container(&results, &entry);
    }
    if (restore_token) AppendVariantString(&results, "restore_token", restore_token);
    dbus_message_iter_close_container(&it, &results);
    return msg;
}

void TestPaths() {
    assert(fthr::PortalSenderToken(":1.42") == "1_42");
    assert(fthr::PortalSenderToken(":1.149209") == "1_149209");
    assert(fthr::PortalRequestPath(":1.42", "fthrreq1") ==
           "/org/freedesktop/portal/desktop/request/1_42/fthrreq1");
    assert(fthr::PortalSessionPath(":1.42", "fthrsess1") ==
           "/org/freedesktop/portal/desktop/session/1_42/fthrsess1");
    assert(fthr::PortalRestoreTokenPath("/home/u") == "/home/u/.fthr/portal_screencast_token");
    assert(fthr::PortalRestoreTokenPath(nullptr).empty());
    assert(fthr::PortalRestoreTokenPath("").empty());
}

void TestRestoreTokenPersistence() {
    char dir[] = "/tmp/fthr-portal-token-XXXXXX";
    assert(mkdtemp(dir) != nullptr);
    const std::string path = std::string(dir) + "/nested/portal_screencast_token";

    assert(fthr::LoadPortalRestoreToken(path).empty());
    assert(fthr::SavePortalRestoreToken(path, "0d5e1a52-4a4f-4d7a-9c1e-1e5c3ef7b9a0"));
    struct stat st{};
    assert(stat(path.c_str(), &st) == 0);
    assert((st.st_mode & 0777) == 0600);
    assert(fthr::LoadPortalRestoreToken(path) == "0d5e1a52-4a4f-4d7a-9c1e-1e5c3ef7b9a0");
    assert(access((path + ".tmp").c_str(), F_OK) != 0);

    // Damaged content is treated as no token rather than sent to the portal.
    FILE* f = fopen(path.c_str(), "wb");
    assert(f);
    fputs("bad token\twith control\x01chars\n", f);
    fclose(f);
    assert(fthr::LoadPortalRestoreToken(path).empty());

    // An empty token removes the file (and succeeds when it is already gone).
    assert(fthr::SavePortalRestoreToken(path, ""));
    assert(access(path.c_str(), F_OK) != 0);
    assert(fthr::SavePortalRestoreToken(path, ""));

    assert(!fthr::SavePortalRestoreToken("", "token"));
    assert(fthr::LoadPortalRestoreToken("").empty());

    std::string cleanup = "rm -rf " + std::string(dir);
    assert(system(cleanup.c_str()) == 0);
}

void TestResponseParsing() {
    const fthr::DBusApi api = DirectApi();
    fthr::PortalResponse response;

    DBusMessage* ok = BuildStartResponse(0,
        {{78, 2560, 1440, 0, 0, 1}, {79, 1920, 1080, 2560, 360, 1}},
        "restore-me");
    assert(fthr::ParsePortalResponse(api, ok, response));
    assert(response.code == 0);
    assert(response.restore_token == "restore-me");
    assert(response.session_handle.empty());
    assert(response.streams.size() == 2);
    assert(response.streams[0].node_id == 78);
    assert(response.streams[0].width == 2560 && response.streams[0].height == 1440);
    assert(response.streams[1].node_id == 79);
    assert(response.streams[1].x == 2560 && response.streams[1].y == 360);
    assert(response.streams[1].source_type == 1);
    dbus_message_unref(ok);

    // Cancellation carries no results.
    DBusMessage* cancelled = BuildStartResponse(1, {}, nullptr);
    assert(fthr::ParsePortalResponse(api, cancelled, response));
    assert(response.code == 1);
    assert(response.streams.empty() && response.restore_token.empty());
    dbus_message_unref(cancelled);

    // CreateSession's results carry the session handle as a string.
    DBusMessage* created = dbus_message_new_signal(
        "/org/freedesktop/portal/desktop/request/1_7/tok",
        "org.freedesktop.portal.Request", "Response");
    {
        DBusMessageIter it, results;
        dbus_message_iter_init_append(created, &it);
        dbus_uint32_t code = 0;
        dbus_message_iter_append_basic(&it, DBUS_TYPE_UINT32, &code);
        dbus_message_iter_open_container(&it, DBUS_TYPE_ARRAY, "{sv}", &results);
        AppendVariantString(&results, "session_handle",
                            "/org/freedesktop/portal/desktop/session/1_7/s1");
        dbus_message_iter_close_container(&it, &results);
    }
    assert(fthr::ParsePortalResponse(api, created, response));
    assert(response.session_handle == "/org/freedesktop/portal/desktop/session/1_7/s1");
    dbus_message_unref(created);

    // A message that is not (u a{sv}) is rejected outright.
    DBusMessage* wrong = dbus_message_new_signal(
        "/org/freedesktop/portal/desktop/request/1_7/tok",
        "org.freedesktop.portal.Request", "Response");
    {
        DBusMessageIter it;
        dbus_message_iter_init_append(wrong, &it);
        const char* text = "nope";
        dbus_message_iter_append_basic(&it, DBUS_TYPE_STRING, &text);
    }
    assert(!fthr::ParsePortalResponse(api, wrong, response));
    dbus_message_unref(wrong);
}

void TestPixelFormats() {
    const auto& preferred = fthr::PreferredSpaVideoFormats();
    assert(preferred.front() == SPA_VIDEO_FORMAT_BGRx);

    fthr::PixelLayout layout;
    assert(fthr::DescribeSpaVideoFormat(SPA_VIDEO_FORMAT_BGRx, layout));
    assert(layout.av_pix_fmt == AV_PIX_FMT_BGR0 && !layout.swap_rb);
    assert(fthr::DescribeSpaVideoFormat(SPA_VIDEO_FORMAT_BGRA, layout));
    assert(layout.av_pix_fmt == AV_PIX_FMT_BGRA && !layout.swap_rb);
    assert(fthr::DescribeSpaVideoFormat(SPA_VIDEO_FORMAT_RGBx, layout));
    assert(layout.av_pix_fmt == AV_PIX_FMT_BGR0 && layout.swap_rb);
    assert(fthr::DescribeSpaVideoFormat(SPA_VIDEO_FORMAT_RGBA, layout));
    assert(layout.av_pix_fmt == AV_PIX_FMT_BGRA && layout.swap_rb);
    assert(!fthr::DescribeSpaVideoFormat(SPA_VIDEO_FORMAT_NV12, layout));
    for (uint32_t format : preferred)
        assert(fthr::DescribeSpaVideoFormat(format, layout));

    // Two rows of three pixels with 4 bytes of padding per source row.
    const uint8_t src[2][16] = {
        {1, 2, 3, 4,  5, 6, 7, 8,  9, 10, 11, 12,  0xEE, 0xEE, 0xEE, 0xEE},
        {13, 14, 15, 16,  17, 18, 19, 20,  21, 22, 23, 24,  0xEE, 0xEE, 0xEE, 0xEE},
    };
    uint8_t dst[2 * 12];
    fthr::CopyFrameRows(dst, 12, &src[0][0], 16, 3, 2, false);
    assert(std::memcmp(dst, src[0], 12) == 0);
    assert(std::memcmp(dst + 12, src[1], 12) == 0);

    fthr::CopyFrameRows(dst, 12, &src[0][0], 16, 3, 2, true);
    const uint8_t swapped_row0[12] = {3, 2, 1, 4,  7, 6, 5, 8,  11, 10, 9, 12};
    const uint8_t swapped_row1[12] = {15, 14, 13, 16,  19, 18, 17, 20,  23, 22, 21, 24};
    assert(std::memcmp(dst, swapped_row0, 12) == 0);
    assert(std::memcmp(dst + 12, swapped_row1, 12) == 0);
}

} // namespace

int main() {
    TestPaths();
    TestRestoreTokenPersistence();
    TestResponseParsing();
    TestPixelFormats();
    std::puts("portal screencast helpers: ok");
    return 0;
}
