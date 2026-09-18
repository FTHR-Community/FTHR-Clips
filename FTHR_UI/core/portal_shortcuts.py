"""Global shortcuts through the XDG Desktop Portal on Linux.

org.freedesktop.portal.GlobalShortcuts lets the compositor own the key grab:
FTHR only announces its actions and the trigger it would prefer. KDE, GNOME
(48+) and other portal backends store the binding, show their own dialog if
they need the user's consent, and deliver ``Activated`` signals over D-Bus.
Nothing here touches ``/dev/input``, so no ``input`` group or root is needed.

jeepney is used instead of QtDBus because BindShortcuts takes ``a(sa{sv})``
and PySide6 cannot marshal struct arrays from Python. All bus traffic runs on
one worker thread; results are handed to Qt through signals, which PySide6
queues to the main thread automatically.
"""
from __future__ import annotations

import queue
import secrets
import sys
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

PORTAL_BUS_NAME = 'org.freedesktop.portal.Desktop'
PORTAL_PATH = '/org/freedesktop/portal/desktop'
SHORTCUTS_IFACE = 'org.freedesktop.portal.GlobalShortcuts'
REQUEST_IFACE = 'org.freedesktop.portal.Request'
SESSION_IFACE = 'org.freedesktop.portal.Session'

# Response codes from org.freedesktop.portal.Request.Response.
RESPONSE_OK = 0
RESPONSE_CANCELLED = 1

# FTHR combo names -> xkb keysym names the shortcuts spec expects.
_MODIFIER_TOKENS = {'Ctrl': 'CTRL', 'Alt': 'ALT', 'Shift': 'SHIFT', 'Win': 'LOGO'}
_KEY_TOKENS = {
    'Esc': 'Escape', 'Enter': 'Return', 'Space': 'space', 'Tab': 'Tab',
    'Backspace': 'BackSpace', 'Delete': 'Delete', 'Insert': 'Insert',
    'Home': 'Home', 'End': 'End', 'Page Up': 'Prior', 'Page Down': 'Next',
    'Up': 'Up', 'Down': 'Down', 'Left': 'Left', 'Right': 'Right',
    'Print': 'Print', 'Pause': 'Pause', 'Scroll Lock': 'Scroll_Lock',
}

_availability: bool | None = None


def to_portal_trigger(combo: str) -> str:
    """Convert an FTHR combo such as ``Ctrl+Shift+S`` to ``CTRL+SHIFT+s``.

    The format is the freedesktop shortcuts spec: upper-case modifier names
    and an xkb keysym, joined by ``+``. Letters become lower-case keysyms
    because ``S`` would mean Shift+s to xkb.
    """
    parts = [part.strip() for part in str(combo or '').split('+') if part.strip()]
    if not parts:
        return ''
    tokens: list[str] = []
    for part in parts:
        if part in _MODIFIER_TOKENS:
            tokens.append(_MODIFIER_TOKENS[part])
        elif part in _KEY_TOKENS:
            tokens.append(_KEY_TOKENS[part])
        elif len(part) == 1 and part.isalpha():
            tokens.append(part.lower())
        else:
            tokens.append(part)
    return '+'.join(tokens)


def _sender_token(unique_name: str) -> str:
    """``:1.42`` -> ``1_42`` as used in Request and Session object paths."""
    return unique_name.lstrip(':').replace('.', '_')


def request_path(unique_name: str, token: str) -> str:
    return f'{PORTAL_PATH}/request/{_sender_token(unique_name)}/{token}'


def session_path(unique_name: str, token: str) -> str:
    return f'{PORTAL_PATH}/session/{_sender_token(unique_name)}/{token}'


def unwrap_variant(value: Any) -> Any:
    """jeepney parses ``v`` as ``(signature, value)``; return just the value."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def unwrap_vardict(value: Any) -> dict[str, Any]:
    """Unwrap every variant in an ``a{sv}`` dictionary."""
    if not isinstance(value, dict):
        return {}
    return {str(key): unwrap_variant(val) for key, val in value.items()}


def parse_bound_shortcuts(shortcuts: Any) -> dict[str, str]:
    """Reduce the portal's ``a(sa{sv})`` shortcut list to id -> trigger text."""
    bound: dict[str, str] = {}
    for entry in unwrap_variant(shortcuts) or ():
        try:
            shortcut_id, props = entry
        except (TypeError, ValueError):
            # A malformed entry from the backend is not worth failing the
            # whole bind over; the remaining shortcuts still work.
            continue
        props = unwrap_vardict(props)
        bound[str(shortcut_id)] = str(props.get('trigger_description', '') or '')
    return bound


def is_available() -> bool:
    """True when a GlobalShortcuts portal backend answers on the session bus."""
    global _availability
    if _availability is not None:
        return _availability
    if sys.platform == 'win32':
        _availability = False
        return False
    try:
        from jeepney import DBusAddress
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import Properties
    except ImportError:
        _availability = False
        return False
    try:
        with open_dbus_connection() as conn:
            props = Properties(DBusAddress(
                PORTAL_PATH, bus_name=PORTAL_BUS_NAME, interface=SHORTCUTS_IFACE))
            reply = conn.send_and_get_reply(props.get('version'), timeout=3)
            _availability = int(unwrap_variant(reply.body[0])) >= 1
    except Exception as exc:
        print(f'[Portal] GlobalShortcuts unavailable: {exc}')
        _availability = False
    return _availability


class PortalShortcuts(QObject):
    """Owns one GlobalShortcuts session for the lifetime of the app."""

    activated = Signal(str)          # shortcut id
    deactivated = Signal(str)        # shortcut id
    bound = Signal(dict)             # id -> trigger description from the compositor
    failed = Signal(str)             # human-readable reason

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._session: str | None = None
        self._pending_bind: list[tuple[str, dict]] | None = None
        self._requests: dict[str, str] = {}      # request path -> kind
        self._parent_window = ''

    # Public API (main thread)

    @property
    def session_active(self) -> bool:
        return self._session is not None

    def start(self) -> bool:
        if self._thread is not None:
            return True
        if not is_available():
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='fthr-portal-shortcuts')
        self._thread.start()
        return True

    def bind(self, shortcuts: dict[str, tuple[str, str]]) -> None:
        """Announce ``{id: (description, preferred FTHR combo)}`` to the portal.

        Replaces the previous set. Ids with an empty combo are still sent so
        the compositor keeps showing the action and lets the user bind it.
        """
        payload = []
        for shortcut_id, (description, combo) in shortcuts.items():
            props = {'description': ('s', description)}
            trigger = to_portal_trigger(combo)
            if trigger:
                props['preferred_trigger'] = ('s', trigger)
            payload.append((shortcut_id, props))
        self._jobs.put(('bind', payload))

    def configure(self) -> None:
        """Open the compositor's own shortcut editor (portal version 2)."""
        self._jobs.put(('configure', None))

    def set_parent_window(self, handle: str) -> None:
        """Portal window identifier (``wayland:...`` / ``x11:...``) for dialogs."""
        self._parent_window = handle or ''

    def stop(self) -> None:
        self._running = False
        self._jobs.put(('stop', None))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # Worker thread

    def _run(self) -> None:
        from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, message_bus, new_method_call
        from jeepney.io.threading import open_dbus_router

        portal = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS_NAME, interface=SHORTCUTS_IFACE)
        try:
            with open_dbus_router() as router:
                me = router.unique_name
                response_rule = MatchRule(
                    type='signal', interface=REQUEST_IFACE, member='Response')
                # No sender= here: the local filter compares against the
                # unique name in the header, which never equals the
                # well-known portal name. Path and interface are specific enough.
                shortcut_rule = MatchRule(
                    type='signal', path=PORTAL_PATH, interface=SHORTCUTS_IFACE)
                # Both filters feed the job queue so one loop sees everything.
                with router.filter(response_rule, queue=self._jobs), \
                        router.filter(shortcut_rule, queue=self._jobs):
                    for rule in (response_rule, shortcut_rule):
                        router.send_and_get_reply(message_bus.AddMatch(rule), timeout=3)

                    def call(method: str, signature: str, body: tuple, kind: str) -> str:
                        token = 'fthr' + secrets.token_hex(6)
                        path = request_path(me, token)
                        self._requests[path] = kind
                        options = dict(body[-1])
                        options['handle_token'] = ('s', token)
                        msg = new_method_call(
                            portal, method, signature, (*body[:-1], options))
                        reply = router.send_and_get_reply(msg, timeout=5)
                        if reply.header.message_type == MessageType.error:
                            self._requests.pop(path, None)
                            raise RuntimeError(
                                f'{method} failed: {reply.body[0] if reply.body else reply.header.fields.get(HeaderFields.error_name)}')
                        return path

                    session_token = 'fthrsess' + secrets.token_hex(6)
                    expected_session = session_path(me, session_token)
                    call('CreateSession', 'a{sv}',
                         ({'session_handle_token': ('s', session_token)},),
                         'create_session')

                    while self._running:
                        item = self._jobs.get()
                        if isinstance(item, tuple):
                            kind, payload = item
                            if kind == 'stop':
                                break
                            if kind == 'bind':
                                if self._session is None:
                                    self._pending_bind = payload
                                    continue
                                call('BindShortcuts', 'oa(sa{sv})sa{sv}',
                                     (self._session, payload, self._parent_window, {}),
                                     'bind')
                            elif kind == 'configure' and self._session is not None:
                                msg = new_method_call(
                                    portal, 'ConfigureShortcuts', 'osa{sv}',
                                    (self._session, self._parent_window, {}))
                                reply = router.send_and_get_reply(msg, timeout=5)
                                if reply.header.message_type == MessageType.error:
                                    self.failed.emit(
                                        'Your desktop portal does not offer a shortcut editor.')
                            continue

                        fields = item.header.fields
                        member = fields.get(HeaderFields.member)
                        path = fields.get(HeaderFields.path, '')
                        if member == 'Response':
                            self._handle_response(
                                self._requests.pop(path, None), item.body,
                                expected_session, call)
                        elif member == 'Activated':
                            self.activated.emit(str(item.body[1]))
                        elif member == 'Deactivated':
                            self.deactivated.emit(str(item.body[1]))
                        elif member == 'ShortcutsChanged':
                            self.bound.emit(parse_bound_shortcuts(item.body[1]))

                    if self._session is not None:
                        try:
                            router.send(new_method_call(
                                DBusAddress(self._session, bus_name=PORTAL_BUS_NAME,
                                            interface=SESSION_IFACE), 'Close'))
                        except Exception:
                            # Best-effort courtesy on shutdown; the bus drops
                            # the session anyway when our connection closes.
                            pass
        except Exception as exc:
            print(f'[Portal] Shortcut worker stopped: {exc}')
            self.failed.emit(f'Desktop portal error: {exc}')
        finally:
            self._session = None
            self._running = False

    def _handle_response(self, kind, body, expected_session, call) -> None:
        code = int(body[0]) if body else RESPONSE_CANCELLED + 1
        results = unwrap_vardict(body[1]) if len(body) > 1 else {}
        if kind == 'create_session':
            if code != RESPONSE_OK:
                self.failed.emit('The desktop portal refused a global shortcuts session.')
                return
            self._session = str(results.get('session_handle') or expected_session)
            print(f'[Portal] GlobalShortcuts session {self._session}')
            if self._pending_bind is not None:
                payload, self._pending_bind = self._pending_bind, None
                call('BindShortcuts', 'oa(sa{sv})sa{sv}',
                     (self._session, payload, self._parent_window, {}), 'bind')
        elif kind == 'bind':
            if code == RESPONSE_OK:
                self.bound.emit(parse_bound_shortcuts(results.get('shortcuts')))
            elif code == RESPONSE_CANCELLED:
                self.failed.emit('Shortcut setup was cancelled in the desktop dialog.')
            else:
                self.failed.emit('The desktop portal could not bind the shortcuts.')
