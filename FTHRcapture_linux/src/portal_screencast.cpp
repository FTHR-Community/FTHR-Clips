#include "portal_screencast.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <thread>

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace fthr {

namespace {

constexpr const char* kPortalBusName = "org.freedesktop.portal.Desktop";
constexpr const char* kPortalPath = "/org/freedesktop/portal/desktop";
constexpr const char* kScreenCastIface = "org.freedesktop.portal.ScreenCast";
constexpr const char* kRequestIface = "org.freedesktop.portal.Request";
constexpr const char* kSessionIface = "org.freedesktop.portal.Session";
constexpr const char* kPropertiesIface = "org.freedesktop.DBus.Properties";

// Bus polling granularity while waiting for a Response signal. Bounds how
// long a cancelled capture keeps the thread inside Open().
constexpr int kPollSliceMs = 50;

// Method replies only carry the request handle; the portal answers them
// without user interaction.
constexpr int kMethodReplyTimeoutMs = 5000;

// SelectSources `types`: 1 = MONITOR. Window and virtual sources are not
// offered because the engine encodes one desktop output.
constexpr uint32_t kSourceTypeMonitor = 1;

struct DictWriter {
    const DBusApi& api;
    DBusMessageIter* parent;
    DBusMessageIter dict{};
    bool ok = true;

    DictWriter(const DBusApi& a, DBusMessageIter* p) : api(a), parent(p) {
        ok = api.message_iter_open_container(parent, DBUS_TYPE_ARRAY, "{sv}", &dict);
    }
    void Add(const char* key, int type, const char* signature, const void* value) {
        if (!ok) return;
        DBusMessageIter entry{}, variant{};
        const char* key_ptr = key;
        ok = api.message_iter_open_container(&dict, DBUS_TYPE_DICT_ENTRY, nullptr, &entry)
            && api.message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key_ptr)
            && api.message_iter_open_container(&entry, DBUS_TYPE_VARIANT, signature, &variant)
            && api.message_iter_append_basic(&variant, type, value)
            && api.message_iter_close_container(&entry, &variant)
            && api.message_iter_close_container(&dict, &entry);
    }
    void AddString(const char* key, const std::string& value) {
        const char* ptr = value.c_str();
        Add(key, DBUS_TYPE_STRING, "s", &ptr);
    }
    void AddUint32(const char* key, uint32_t value) {
        dbus_uint32_t v = value;
        Add(key, DBUS_TYPE_UINT32, "u", &v);
    }
    void AddBool(const char* key, bool value) {
        dbus_bool_t v = value ? TRUE : FALSE;
        Add(key, DBUS_TYPE_BOOLEAN, "b", &v);
    }
    bool Close() {
        if (ok) ok = api.message_iter_close_container(parent, &dict);
        return ok;
    }
};

bool ReadInt32Pair(const DBusApi& api, DBusMessageIter* variant, int32_t& a, int32_t& b) {
    if (api.message_iter_get_arg_type(variant) != DBUS_TYPE_STRUCT) return false;
    DBusMessageIter fields{};
    api.message_iter_recurse(variant, &fields);
    if (api.message_iter_get_arg_type(&fields) != DBUS_TYPE_INT32) return false;
    dbus_int32_t first = 0, second = 0;
    api.message_iter_get_basic(&fields, &first);
    if (!api.message_iter_next(&fields) ||
            api.message_iter_get_arg_type(&fields) != DBUS_TYPE_INT32)
        return false;
    api.message_iter_get_basic(&fields, &second);
    a = first;
    b = second;
    return true;
}

std::string ReadString(const DBusApi& api, DBusMessageIter* it) {
    const int type = api.message_iter_get_arg_type(it);
    if (type != DBUS_TYPE_STRING && type != DBUS_TYPE_OBJECT_PATH) return {};
    const char* value = nullptr;
    api.message_iter_get_basic(it, &value);
    return value ? value : "";
}

// Walks one a{sv}, handing each (key, variant iterator) to visit.
void ForEachDictEntry(const DBusApi& api, DBusMessageIter* array,
                      const std::function<void(const std::string&, DBusMessageIter*)>& visit) {
    if (api.message_iter_get_arg_type(array) != DBUS_TYPE_ARRAY) return;
    DBusMessageIter entry{};
    api.message_iter_recurse(array, &entry);
    while (api.message_iter_get_arg_type(&entry) == DBUS_TYPE_DICT_ENTRY) {
        DBusMessageIter kv{};
        api.message_iter_recurse(&entry, &kv);
        const std::string key = ReadString(api, &kv);
        if (!key.empty() && api.message_iter_next(&kv) &&
                api.message_iter_get_arg_type(&kv) == DBUS_TYPE_VARIANT) {
            DBusMessageIter variant{};
            api.message_iter_recurse(&kv, &variant);
            visit(key, &variant);
        }
        api.message_iter_next(&entry);
    }
}

void ParseStreams(const DBusApi& api, DBusMessageIter* variant,
                  std::vector<PortalStream>& out) {
    if (api.message_iter_get_arg_type(variant) != DBUS_TYPE_ARRAY) return;
    DBusMessageIter item{};
    api.message_iter_recurse(variant, &item);
    while (api.message_iter_get_arg_type(&item) == DBUS_TYPE_STRUCT) {
        DBusMessageIter fields{};
        api.message_iter_recurse(&item, &fields);
        if (api.message_iter_get_arg_type(&fields) == DBUS_TYPE_UINT32) {
            PortalStream stream;
            dbus_uint32_t node = 0;
            api.message_iter_get_basic(&fields, &node);
            stream.node_id = node;
            if (api.message_iter_next(&fields)) {
                ForEachDictEntry(api, &fields,
                    [&](const std::string& key, DBusMessageIter* value) {
                        int32_t a = 0, b = 0;
                        if (key == "size" && ReadInt32Pair(api, value, a, b)) {
                            stream.width = a > 0 ? static_cast<uint32_t>(a) : 0;
                            stream.height = b > 0 ? static_cast<uint32_t>(b) : 0;
                        } else if (key == "position" && ReadInt32Pair(api, value, a, b)) {
                            stream.x = a;
                            stream.y = b;
                        } else if (key == "source_type" &&
                                   api.message_iter_get_arg_type(value) == DBUS_TYPE_UINT32) {
                            dbus_uint32_t type = 0;
                            api.message_iter_get_basic(value, &type);
                            stream.source_type = type;
                        }
                    });
            }
            out.push_back(stream);
        }
        api.message_iter_next(&item);
    }
}

std::string RandomHex(size_t bytes) {
    std::random_device device;
    std::ostringstream hex;
    hex << std::hex;
    for (size_t i = 0; i < bytes; ++i)
        hex.width(2), hex.fill('0'), hex << (device() & 0xffu);
    return hex.str();
}

} // namespace

// Library loading

bool LoadDBusApi(DBusApi& api, std::string* error) {
    static void* handle = nullptr;
    if (!handle) {
        handle = dlopen("libdbus-1.so.3", RTLD_NOW | RTLD_LOCAL);
        if (!handle) {
            if (error) *error = std::string("libdbus-1.so.3 not loadable: ") + dlerror();
            return false;
        }
    }
    auto resolve = [&](auto& member, const char* name) {
        if (!member) {
            member = reinterpret_cast<std::remove_reference_t<decltype(member)>>(
                dlsym(handle, name));
        }
        if (!member && error && error->empty())
            *error = std::string("libdbus-1 lacks ") + name;
        return member != nullptr;
    };
    if (error) error->clear();
    bool ok = true;
    ok &= resolve(api.error_init, "dbus_error_init");
    ok &= resolve(api.error_free, "dbus_error_free");
    ok &= resolve(api.error_is_set, "dbus_error_is_set");
    ok &= resolve(api.bus_get_private, "dbus_bus_get_private");
    ok &= resolve(api.bus_get_unique_name, "dbus_bus_get_unique_name");
    ok &= resolve(api.bus_add_match, "dbus_bus_add_match");
    ok &= resolve(api.connection_set_exit_on_disconnect, "dbus_connection_set_exit_on_disconnect");
    ok &= resolve(api.connection_close, "dbus_connection_close");
    ok &= resolve(api.connection_unref, "dbus_connection_unref");
    ok &= resolve(api.connection_send_with_reply_and_block, "dbus_connection_send_with_reply_and_block");
    ok &= resolve(api.connection_send, "dbus_connection_send");
    ok &= resolve(api.connection_flush, "dbus_connection_flush");
    ok &= resolve(api.connection_read_write, "dbus_connection_read_write");
    ok &= resolve(api.connection_pop_message, "dbus_connection_pop_message");
    ok &= resolve(api.message_new_method_call, "dbus_message_new_method_call");
    ok &= resolve(api.message_unref, "dbus_message_unref");
    ok &= resolve(api.message_is_signal, "dbus_message_is_signal");
    ok &= resolve(api.message_get_path, "dbus_message_get_path");
    ok &= resolve(api.message_iter_init, "dbus_message_iter_init");
    ok &= resolve(api.message_iter_init_append, "dbus_message_iter_init_append");
    ok &= resolve(api.message_iter_open_container, "dbus_message_iter_open_container");
    ok &= resolve(api.message_iter_close_container, "dbus_message_iter_close_container");
    ok &= resolve(api.message_iter_append_basic, "dbus_message_iter_append_basic");
    ok &= resolve(api.message_iter_get_arg_type, "dbus_message_iter_get_arg_type");
    ok &= resolve(api.message_iter_get_basic, "dbus_message_iter_get_basic");
    ok &= resolve(api.message_iter_recurse, "dbus_message_iter_recurse");
    ok &= resolve(api.message_iter_next, "dbus_message_iter_next");
    return ok;
}

// Pure helpers

std::string PortalSenderToken(const std::string& unique_name) {
    std::string token = unique_name;
    if (!token.empty() && token[0] == ':') token.erase(0, 1);
    for (char& c : token)
        if (c == '.') c = '_';
    return token;
}

std::string PortalRequestPath(const std::string& unique_name, const std::string& token) {
    return std::string(kPortalPath) + "/request/" + PortalSenderToken(unique_name) + "/" + token;
}

std::string PortalSessionPath(const std::string& unique_name, const std::string& token) {
    return std::string(kPortalPath) + "/session/" + PortalSenderToken(unique_name) + "/" + token;
}

std::string PortalRestoreTokenPath(const char* home) {
    if (!home || !*home) return {};
    return std::string(home) + "/.fthr/portal_screencast_token";
}

std::string LoadPortalRestoreToken(const std::string& path) {
    if (path.empty()) return {};
    std::ifstream in(path);
    if (!in) return {};
    std::string token;
    std::getline(in, token);
    // Tokens are opaque UUID-like strings; anything with whitespace or
    // control characters is a damaged file, not a token.
    for (unsigned char c : token)
        if (c <= ' ' || c == 0x7f) return {};
    return token;
}

bool SavePortalRestoreToken(const std::string& path, const std::string& token) {
    if (path.empty()) return false;
    if (token.empty()) {
        return unlink(path.c_str()) == 0 || errno == ENOENT;
    }
    const size_t slash = path.rfind('/');
    if (slash != std::string::npos) {
        // Settings normally exist already; this only covers a first run
        // where the engine starts before the UI wrote settings.json.
        mkdir(path.substr(0, slash).c_str(), 0700);
    }
    const std::string tmp = path + ".tmp";
    const int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    bool ok = fchmod(fd, 0600) == 0;
    const std::string line = token + "\n";
    size_t written = 0;
    while (ok && written < line.size()) {
        const ssize_t n = write(fd, line.data() + written, line.size() - written);
        if (n < 0) { if (errno == EINTR) continue; ok = false; break; }
        written += static_cast<size_t>(n);
    }
    ok = (fsync(fd) == 0) && ok;
    ok = (close(fd) == 0) && ok;
    if (ok) ok = rename(tmp.c_str(), path.c_str()) == 0;
    if (!ok) unlink(tmp.c_str());
    return ok;
}

bool ParsePortalResponse(const DBusApi& api, DBusMessage* message, PortalResponse& out) {
    out = PortalResponse{};
    DBusMessageIter it{};
    if (!api.message_iter_init(message, &it)) return false;
    if (api.message_iter_get_arg_type(&it) != DBUS_TYPE_UINT32) return false;
    dbus_uint32_t code = 0;
    api.message_iter_get_basic(&it, &code);
    out.code = code;
    if (!api.message_iter_next(&it)) return false;
    if (api.message_iter_get_arg_type(&it) != DBUS_TYPE_ARRAY) return false;
    ForEachDictEntry(api, &it, [&](const std::string& key, DBusMessageIter* value) {
        if (key == "session_handle") {
            out.session_handle = ReadString(api, value);
        } else if (key == "restore_token") {
            out.restore_token = ReadString(api, value);
        } else if (key == "streams") {
            ParseStreams(api, value, out.streams);
        }
    });
    return true;
}

const char* PortalOutcomeName(PortalOutcome outcome) noexcept {
    switch (outcome) {
    case PortalOutcome::Ok: return "ok";
    case PortalOutcome::Unavailable: return "unavailable";
    case PortalOutcome::Cancelled: return "cancelled";
    case PortalOutcome::TimedOut: return "timed out";
    case PortalOutcome::Failed: return "failed";
    case PortalOutcome::Interrupted: return "interrupted";
    }
    return "unknown";
}

// PortalScreenCastSession

PortalScreenCastSession::PortalScreenCastSession(const DBusApi& api) : api_(api) {}

PortalScreenCastSession::~PortalScreenCastSession() {
    CloseSession();
    if (connection_) {
        // Private connections must be closed explicitly before the last unref.
        api_.connection_close(connection_);
        api_.connection_unref(connection_);
        connection_ = nullptr;
    }
}

std::string PortalScreenCastSession::NewToken(const char* prefix) {
    std::ostringstream token;
    token << prefix << RandomHex(6) << ++token_counter_;
    return token.str();
}

bool PortalScreenCastSession::Connect(std::string* error) {
    if (connection_) return true;
    DBusError err;
    api_.error_init(&err);
    // A private connection keeps this poll loop independent of any shared
    // connection another part of the process might dispatch.
    connection_ = api_.bus_get_private(DBUS_BUS_SESSION, &err);
    if (!connection_) {
        if (error) *error = std::string("session bus: ") + (err.message ? err.message : "unknown error");
        api_.error_free(&err);
        return false;
    }
    api_.connection_set_exit_on_disconnect(connection_, FALSE);
    const char* name = api_.bus_get_unique_name(connection_);
    unique_name_ = name ? name : "";
    // sender= is resolved by the bus daemon against the well-known name, so
    // only the portal's own signals reach this connection's queue.
    const std::string rules[] = {
        std::string("type='signal',sender='") + kPortalBusName +
            "',interface='" + kRequestIface + "',member='Response'",
        std::string("type='signal',sender='") + kPortalBusName +
            "',interface='" + kSessionIface + "',member='Closed'",
    };
    for (const auto& rule : rules) {
        api_.bus_add_match(connection_, rule.c_str(), &err);
        if (api_.error_is_set(&err)) {
            if (error) *error = std::string("AddMatch: ") + (err.message ? err.message : "");
            api_.error_free(&err);
            return false;
        }
    }
    return true;
}

void PortalScreenCastSession::HandleSignal(DBusMessage* message) {
    const char* path = api_.message_get_path(message);
    if (!path) return;
    if (api_.message_is_signal(message, kRequestIface, "Response")) {
        if (pending_request_path_.empty() || pending_request_path_ != path) return;
        PortalResponse response;
        if (!ParsePortalResponse(api_, message, response)) {
            std::cerr << "[Portal] Ignoring malformed Response on " << path << std::endl;
            return;
        }
        pending_response_ = std::move(response);
        pending_response_ready_ = true;
    } else if (api_.message_is_signal(message, kSessionIface, "Closed")) {
        if (!session_handle_.empty() && session_handle_ == path) closed_ = true;
    }
}

void PortalScreenCastSession::CloseRequest() {
    if (pending_request_path_.empty()) return;
    // Best effort: tells the portal to drop the dialog it may still show.
    if (DBusMessage* close = api_.message_new_method_call(
            kPortalBusName, pending_request_path_.c_str(), kRequestIface, "Close")) {
        api_.connection_send(connection_, close, nullptr);
        api_.connection_flush(connection_);
        api_.message_unref(close);
    }
    pending_request_path_.clear();
}

PortalOutcome PortalScreenCastSession::Call(
        const char* method,
        const std::function<bool(DBusMessageIter&)>& append_args,
        std::chrono::milliseconds response_timeout,
        const KeepRunning& keep_running,
        PortalResponse& response,
        std::string* error) {
    pending_token_ = NewToken("fthrreq");
    pending_request_path_ = PortalRequestPath(unique_name_, pending_token_);
    pending_response_ready_ = false;
    pending_response_ = PortalResponse{};

    DBusMessage* msg = api_.message_new_method_call(
        kPortalBusName, kPortalPath, kScreenCastIface, method);
    if (!msg) { if (error) *error = "out of memory"; return PortalOutcome::Failed; }
    DBusMessageIter it{};
    api_.message_iter_init_append(msg, &it);
    // append_args adds the method's own arguments and the trailing options
    // dict, whose handle_token must be pending_token_ so the Response path
    // computed above is the one the portal emits on.
    if (!append_args(it)) {
        api_.message_unref(msg);
        pending_request_path_.clear();
        if (error) *error = std::string(method) + ": could not build arguments";
        return PortalOutcome::Failed;
    }
    DBusError err;
    api_.error_init(&err);
    DBusMessage* reply = api_.connection_send_with_reply_and_block(
        connection_, msg, kMethodReplyTimeoutMs, &err);
    api_.message_unref(msg);
    if (!reply) {
        const std::string name = err.name ? err.name : "";
        if (error) *error = std::string(method) + ": " + name + " " + (err.message ? err.message : "");
        api_.error_free(&err);
        pending_request_path_.clear();
        const bool missing = name == DBUS_ERROR_SERVICE_UNKNOWN ||
            name == DBUS_ERROR_UNKNOWN_METHOD || name == DBUS_ERROR_UNKNOWN_INTERFACE ||
            name == DBUS_ERROR_UNKNOWN_OBJECT || name == DBUS_ERROR_NAME_HAS_NO_OWNER;
        return missing ? PortalOutcome::Unavailable : PortalOutcome::Failed;
    }
    // Portals older than the handle_token convention return a path they
    // chose themselves; honour it so the Response is still recognised.
    DBusMessageIter reply_it{};
    if (api_.message_iter_init(reply, &reply_it)) {
        const std::string handle = ReadString(api_, &reply_it);
        if (!handle.empty() && handle != pending_request_path_) {
            std::cerr << "[Portal] Request handle differs from token path: "
                      << handle << std::endl;
            pending_request_path_ = handle;
        }
    }
    api_.message_unref(reply);

    const auto deadline = std::chrono::steady_clock::now() + response_timeout;
    while (!pending_response_ready_) {
        if (!keep_running()) {
            CloseRequest();
            if (error) *error = std::string(method) + ": capture stopped while waiting";
            return PortalOutcome::Interrupted;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            CloseRequest();
            if (error) *error = std::string(method) + ": no Response within " +
                std::to_string(response_timeout.count()) + " ms";
            return PortalOutcome::TimedOut;
        }
        if (!api_.connection_read_write(connection_, kPollSliceMs)) {
            if (error) *error = std::string(method) + ": session bus disconnected";
            pending_request_path_.clear();
            return PortalOutcome::Failed;
        }
        while (DBusMessage* incoming = api_.connection_pop_message(connection_)) {
            HandleSignal(incoming);
            api_.message_unref(incoming);
        }
    }
    pending_request_path_.clear();
    response = std::move(pending_response_);
    pending_response_ready_ = false;
    return PortalOutcome::Ok;
}

PortalOutcome PortalScreenCastSession::Open(const PortalScreenCastOptions& options,
                                            const KeepRunning& keep_running,
                                            std::string* error) {
    if (error) error->clear();
    if (!Connect(error)) return PortalOutcome::Unavailable;

    // Probe the interface first: xdg-desktop-portal only exports ScreenCast
    // when a backend implements it, and a missing interface should fall
    // through to the "no capture protocol" diagnostic rather than a retry.
    {
        DBusMessage* probe = api_.message_new_method_call(
            kPortalBusName, kPortalPath, kPropertiesIface, "Get");
        if (!probe) { if (error) *error = "out of memory"; return PortalOutcome::Failed; }
        DBusMessageIter it{};
        api_.message_iter_init_append(probe, &it);
        const char* iface = kScreenCastIface;
        const char* prop = "version";
        api_.message_iter_append_basic(&it, DBUS_TYPE_STRING, &iface);
        api_.message_iter_append_basic(&it, DBUS_TYPE_STRING, &prop);
        DBusError err;
        api_.error_init(&err);
        DBusMessage* reply = api_.connection_send_with_reply_and_block(
            connection_, probe, static_cast<int>(options.request_timeout.count()), &err);
        api_.message_unref(probe);
        if (!reply) {
            if (error) *error = std::string("ScreenCast portal not available: ") +
                (err.name ? err.name : "") + " " + (err.message ? err.message : "");
            api_.error_free(&err);
            return PortalOutcome::Unavailable;
        }
        DBusMessageIter reply_it{}, variant{};
        dbus_uint32_t version = 0;
        if (api_.message_iter_init(reply, &reply_it) &&
                api_.message_iter_get_arg_type(&reply_it) == DBUS_TYPE_VARIANT) {
            api_.message_iter_recurse(&reply_it, &variant);
            if (api_.message_iter_get_arg_type(&variant) == DBUS_TYPE_UINT32)
                api_.message_iter_get_basic(&variant, &version);
        }
        api_.message_unref(reply);
        std::cerr << "[Portal] ScreenCast interface version " << version << std::endl;
    }

    PortalResponse response;
    std::string session_token = NewToken("fthrsess");
    const std::string expected_session = PortalSessionPath(unique_name_, session_token);
    auto outcome = Call("CreateSession",
        [&](DBusMessageIter& it) {
            DictWriter dict(api_, &it);
            dict.AddString("handle_token", pending_token_);
            dict.AddString("session_handle_token", session_token);
            return dict.Close();
        },
        options.request_timeout, keep_running, response, error);
    if (outcome != PortalOutcome::Ok) return outcome;
    if (response.code != 0) {
        if (error) *error = "CreateSession refused (response " + std::to_string(response.code) + ")";
        return PortalOutcome::Failed;
    }
    session_handle_ = response.session_handle.empty() ? expected_session : response.session_handle;

    outcome = Call("SelectSources",
        [&](DBusMessageIter& it) {
            const char* session = session_handle_.c_str();
            if (!api_.message_iter_append_basic(&it, DBUS_TYPE_OBJECT_PATH, &session))
                return false;
            DictWriter dict(api_, &it);
            dict.AddString("handle_token", pending_token_);
            dict.AddUint32("types", kSourceTypeMonitor);
            dict.AddBool("multiple", false);
            dict.AddUint32("cursor_mode", options.cursor_mode);
            dict.AddUint32("persist_mode", options.persist_mode);
            if (!options.restore_token.empty())
                dict.AddString("restore_token", options.restore_token);
            return dict.Close();
        },
        options.request_timeout, keep_running, response, error);
    if (outcome != PortalOutcome::Ok) return outcome;
    if (response.code == 1) return PortalOutcome::Cancelled;
    if (response.code != 0) {
        if (error) *error = "SelectSources failed (response " + std::to_string(response.code) + ")";
        return PortalOutcome::Failed;
    }

    // Start() is where the desktop shows its picker unless the restore token
    // was accepted, hence the much longer wait.
    outcome = Call("Start",
        [&](DBusMessageIter& it) {
            const char* session = session_handle_.c_str();
            const char* parent_window = "";
            if (!api_.message_iter_append_basic(&it, DBUS_TYPE_OBJECT_PATH, &session) ||
                    !api_.message_iter_append_basic(&it, DBUS_TYPE_STRING, &parent_window))
                return false;
            DictWriter dict(api_, &it);
            dict.AddString("handle_token", pending_token_);
            return dict.Close();
        },
        options.dialog_timeout, keep_running, response, error);
    if (outcome != PortalOutcome::Ok) return outcome;
    if (response.code == 1) return PortalOutcome::Cancelled;
    if (response.code != 0) {
        if (error) *error = "Start failed (response " + std::to_string(response.code) + ")";
        return PortalOutcome::Failed;
    }
    if (response.streams.empty()) {
        if (error) *error = "Start succeeded without any stream";
        return PortalOutcome::Failed;
    }
    streams_ = std::move(response.streams);
    restore_token_ = std::move(response.restore_token);
    return PortalOutcome::Ok;
}

int PortalScreenCastSession::OpenPipeWireRemote(std::string* error) {
    if (!connection_ || session_handle_.empty()) {
        if (error) *error = "no session";
        return -1;
    }
    DBusMessage* msg = api_.message_new_method_call(
        kPortalBusName, kPortalPath, kScreenCastIface, "OpenPipeWireRemote");
    if (!msg) { if (error) *error = "out of memory"; return -1; }
    DBusMessageIter it{};
    api_.message_iter_init_append(msg, &it);
    const char* session = session_handle_.c_str();
    api_.message_iter_append_basic(&it, DBUS_TYPE_OBJECT_PATH, &session);
    DictWriter dict(api_, &it);
    dict.Close();
    DBusError err;
    api_.error_init(&err);
    DBusMessage* reply = api_.connection_send_with_reply_and_block(connection_, msg, 10000, &err);
    api_.message_unref(msg);
    if (!reply) {
        if (error) *error = std::string("OpenPipeWireRemote: ") + (err.message ? err.message : "");
        api_.error_free(&err);
        return -1;
    }
    int fd = -1;
    DBusMessageIter reply_it{};
    if (api_.message_iter_init(reply, &reply_it) &&
            api_.message_iter_get_arg_type(&reply_it) == DBUS_TYPE_UNIX_FD) {
        // libdbus dup()s the descriptor for us; the reply's copy is released
        // with the message and ours stays valid.
        api_.message_iter_get_basic(&reply_it, &fd);
    }
    api_.message_unref(reply);
    if (fd < 0 && error) *error = "OpenPipeWireRemote returned no descriptor";
    return fd;
}

bool PortalScreenCastSession::PollClosed() {
    if (!connection_ || closed_) return closed_;
    if (!api_.connection_read_write(connection_, 0)) {
        closed_ = true;   // the bus went away, which ends the session as well
        return true;
    }
    while (DBusMessage* incoming = api_.connection_pop_message(connection_)) {
        HandleSignal(incoming);
        api_.message_unref(incoming);
    }
    return closed_;
}

void PortalScreenCastSession::CloseSession() {
    if (!connection_ || session_handle_.empty() || closed_) return;
    if (DBusMessage* close = api_.message_new_method_call(
            kPortalBusName, session_handle_.c_str(), kSessionIface, "Close")) {
        api_.connection_send(connection_, close, nullptr);
        api_.connection_flush(connection_);
        api_.message_unref(close);
    }
    session_handle_.clear();
}

} // namespace fthr
