#pragma once
// org.freedesktop.portal.ScreenCast client for compositors that expose screen
// capture only through the desktop portal (KWin is the reference case). The
// portal hands out a PipeWire node and a socket; backend_portal.cpp turns those
// into frames. libdbus-1 is loaded with dlopen so the engine still starts on
// systems without it; nothing here links the library directly.
//
// Protocol reference: https://flatpak.github.io/xdg-desktop-portal/docs/
// doc-org.freedesktop.portal.ScreenCast.html

#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <dbus/dbus.h>

namespace fthr {

// Entry points resolved from libdbus-1.so.3 at runtime. The member types are
// the header prototypes, so a signature drift fails to compile instead of
// corrupting the call.
struct DBusApi {
    decltype(&dbus_error_init) error_init = nullptr;
    decltype(&dbus_error_free) error_free = nullptr;
    decltype(&dbus_error_is_set) error_is_set = nullptr;
    decltype(&dbus_bus_get_private) bus_get_private = nullptr;
    decltype(&dbus_bus_get_unique_name) bus_get_unique_name = nullptr;
    decltype(&dbus_bus_add_match) bus_add_match = nullptr;
    decltype(&dbus_connection_set_exit_on_disconnect) connection_set_exit_on_disconnect = nullptr;
    decltype(&dbus_connection_close) connection_close = nullptr;
    decltype(&dbus_connection_unref) connection_unref = nullptr;
    decltype(&dbus_connection_send) connection_send = nullptr;
    decltype(&dbus_connection_flush) connection_flush = nullptr;
    decltype(&dbus_connection_read_write) connection_read_write = nullptr;
    decltype(&dbus_connection_pop_message) connection_pop_message = nullptr;
    decltype(&dbus_message_new_method_call) message_new_method_call = nullptr;
    decltype(&dbus_message_unref) message_unref = nullptr;
    decltype(&dbus_message_is_signal) message_is_signal = nullptr;
    decltype(&dbus_message_get_path) message_get_path = nullptr;
    decltype(&dbus_message_get_type) message_get_type = nullptr;
    decltype(&dbus_message_get_reply_serial) message_get_reply_serial = nullptr;
    decltype(&dbus_message_get_error_name) message_get_error_name = nullptr;
    decltype(&dbus_message_iter_init) message_iter_init = nullptr;
    decltype(&dbus_message_iter_init_append) message_iter_init_append = nullptr;
    decltype(&dbus_message_iter_open_container) message_iter_open_container = nullptr;
    decltype(&dbus_message_iter_close_container) message_iter_close_container = nullptr;
    decltype(&dbus_message_iter_append_basic) message_iter_append_basic = nullptr;
    decltype(&dbus_message_iter_get_arg_type) message_iter_get_arg_type = nullptr;
    decltype(&dbus_message_iter_get_basic) message_iter_get_basic = nullptr;
    decltype(&dbus_message_iter_recurse) message_iter_recurse = nullptr;
    decltype(&dbus_message_iter_next) message_iter_next = nullptr;
};

// Resolves every DBusApi member from the already-installed libdbus-1. Returns
// false with a reason when the library or a symbol is missing; the handle is
// kept for the life of the process.
bool LoadDBusApi(DBusApi& api, std::string* error);

// Object paths the portal derives from the caller's unique bus name.
// ":1.42" + "tok" -> "/org/freedesktop/portal/desktop/request/1_42/tok".
std::string PortalSenderToken(const std::string& unique_name);
std::string PortalRequestPath(const std::string& unique_name, const std::string& token);
std::string PortalSessionPath(const std::string& unique_name, const std::string& token);

// The restore token lets a persistent session skip the source-selection dialog
// on later runs. It is stored beside settings.json with owner-only permissions
// because it grants screen access to whoever presents it.
std::string PortalRestoreTokenPath(const char* home);
std::string LoadPortalRestoreToken(const std::string& path);
// An empty token removes the file. Writes go through a temporary file so a
// crash never leaves a truncated token behind.
bool SavePortalRestoreToken(const std::string& path, const std::string& token);

// One entry of the Start() results' `streams a(ua{sv})`.
struct PortalStream {
    uint32_t node_id = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    int32_t x = 0;
    int32_t y = 0;
    uint32_t source_type = 0;
};

// Parsed org.freedesktop.portal.Request.Response(u response, a{sv} results).
struct PortalResponse {
    uint32_t code = 0;   // 0 success, 1 cancelled by the user, 2 other failure
    std::string session_handle;
    std::string restore_token;
    std::vector<PortalStream> streams;
};

// Unknown keys and unexpected types are skipped rather than rejected, since
// portal backends add result fields over time. Returns false only when the
// message does not even start with the (u a{sv}) the interface promises.
bool ParsePortalResponse(const DBusApi& api, DBusMessage* message, PortalResponse& out);

enum class PortalOutcome {
    Ok,
    Unavailable,   // no bus, no portal, or no ScreenCast interface: try other backends
    Cancelled,     // the user dismissed the source dialog
    TimedOut,      // the dialog stayed unanswered
    Failed,        // the portal reported an error
    Interrupted,   // capture was cancelled while waiting
};

const char* PortalOutcomeName(PortalOutcome outcome) noexcept;

struct PortalScreenCastOptions {
    uint32_t cursor_mode = 2;    // EMBEDDED: the cursor is painted into the frames
    uint32_t persist_mode = 2;   // persistent across sessions when the backend allows it
    std::string restore_token;
    // Session setup calls answer without user interaction.
    std::chrono::milliseconds request_timeout{10000};
    // Start() may wait for the user to pick a screen in the portal dialog.
    std::chrono::milliseconds dialog_timeout{120000};
};

// Owns one private session-bus connection and one ScreenCast session. All
// methods run on the caller's thread and poll the bus in short slices so
// keep_running() can abort a wait within ~50 ms.
class PortalScreenCastSession {
public:
    using KeepRunning = std::function<bool()>;

    explicit PortalScreenCastSession(const DBusApi& api);
    ~PortalScreenCastSession();
    PortalScreenCastSession(const PortalScreenCastSession&) = delete;
    PortalScreenCastSession& operator=(const PortalScreenCastSession&) = delete;

    // CreateSession -> SelectSources -> Start. On Ok, Streams() and
    // RestoreToken() are populated. Details for the log land in *error.
    PortalOutcome Open(const PortalScreenCastOptions& options,
                       const KeepRunning& keep_running,
                       std::string* error);

    // OpenPipeWireRemote(); the caller owns the returned descriptor. -1 on error.
    int OpenPipeWireRemote(const KeepRunning& keep_running, std::string* error);

    // Non-blocking. True once the portal emitted Session.Closed, e.g. because
    // the user stopped sharing from the desktop's indicator.
    bool PollClosed();

    const std::vector<PortalStream>& Streams() const noexcept { return streams_; }
    const std::string& RestoreToken() const noexcept { return restore_token_; }
    const std::string& SessionHandle() const noexcept { return session_handle_; }

private:
    bool Connect(std::string* error);
    // Sends `message` (consumed) and waits for its reply in kPollSliceMs
    // slices so keep_running() can abort; signals seen meanwhile go through
    // HandleSignal. Returns the reply, or nullptr with *outcome set to
    // Interrupted / TimedOut / Failed / Unavailable and *error explained.
    DBusMessage* SendAndWait(DBusMessage* message,
                             std::chrono::milliseconds timeout,
                             const KeepRunning& keep_running,
                             PortalOutcome* outcome,
                             std::string* error);
    PortalOutcome Call(const char* method,
                       const std::function<bool(DBusMessageIter&)>& append_args,
                       std::chrono::milliseconds response_timeout,
                       const KeepRunning& keep_running,
                       PortalResponse& response,
                       std::string* error);
    void CloseRequest();
    void CloseSession();
    void HandleSignal(DBusMessage* message);
    std::string NewToken(const char* prefix);

    const DBusApi& api_;
    DBusConnection* connection_ = nullptr;
    std::string unique_name_;
    std::string session_handle_;
    std::string restore_token_;
    std::vector<PortalStream> streams_;
    // Set by HandleSignal when a Response for pending_request_path_ arrives.
    std::string pending_token_;
    std::string pending_request_path_;
    bool pending_response_ready_ = false;
    PortalResponse pending_response_;
    bool closed_ = false;
    uint32_t token_counter_ = 0;
};

} // namespace fthr
