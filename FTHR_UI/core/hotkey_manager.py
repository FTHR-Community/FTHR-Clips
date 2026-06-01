"""
Hotkey Manager - global keyboard shortcuts for FTHR Clips.

On Linux/Wayland the preferred trigger path is via Hyprland binds that send
commands to a Unix socket at /tmp/fthr_hotkey.sock.  This file starts that
socket server automatically so the Hyprland config just needs:

    bind = , F9,  exec, echo -n "save_clip"          | nc -U /tmp/fthr_hotkey.sock
    bind = , F10, exec, echo -n "save_extended_clip" | nc -U /tmp/fthr_hotkey.sock
    bind = , F11, exec, echo -n "save_screenshot"    | nc -U /tmp/fthr_hotkey.sock

The `keyboard` library fallback is kept for non-Wayland / Windows use, but
it needs the user in the 'input' group on Linux and may not work on Wayland.
"""
import keyboard
import socket
import os
import sys
import threading
from PyQt6.QtCore import QObject, pyqtSignal
import json
from pathlib import Path

HOTKEY_SOCKET_PATH = '/tmp/fthr_hotkey.sock'


class HotkeyManager(QObject):
    """Manages global hotkeys for the application"""
    
    # Signals
    save_clip_triggered = pyqtSignal()
    save_extended_clip_triggered = pyqtSignal()
    save_screenshot_triggered = pyqtSignal()
    confirm_game_detection_triggered  = pyqtSignal()
    dismiss_game_detection_triggered  = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        # Lives next to all the other user state in ~/.fthr. Survives reinstalls.
        self.config_file = Path.home() / '.fthr' / 'hotkeys.json'

        # Sensible defaults nobody's ever bound to anything else. F9/F10/F11 it is.
        self.hotkeys = {
            'save_clip': 'F9',
            'save_extended_clip': 'F10',
            'save_screenshot': 'F11',
            'confirm_game_detection':  'F8',
            'dismiss_game_detection':  'F7',
        }

        # We track what we've actually registered so we can cleanly unhook later.
        # The `keyboard` lib gets cranky if you remove a hotkey you never added.
        self._registered_hotkeys = []

        self._socket_running = False
        self._socket_thread: threading.Thread | None = None

        # Load saved hotkeys
        self._load_hotkeys()
    
    def _load_hotkeys(self):
        """Load hotkeys from config file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    saved_hotkeys = json.load(f)
                    self.hotkeys.update(saved_hotkeys)
            except Exception as e:
                print(f"Failed to load hotkeys: {e}")
    
    def _save_hotkeys(self):
        """Save hotkeys to config file"""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.hotkeys, f, indent=2)
        except Exception as e:
            print(f"Failed to save hotkeys: {e}")
    
    def set_hotkey(self, action: str, key: str):
        """
        Set a hotkey for an action
        
        Args:
            action: One of 'save_clip', 'save_extended_clip', 'save_screenshot'
            key: The key combination (e.g., 'F9', 'ctrl+shift+s')
        """
        if action not in self.hotkeys:
            print(f"Unknown action: {action}")
            return False
        
        # Tear down the old binding first, otherwise both keys fire the action
        # and you get two clips. (ask me how I know)
        old_key = self.hotkeys.get(action)
        if old_key and old_key in self._registered_hotkeys:
            try:
                keyboard.remove_hotkey(old_key)
                self._registered_hotkeys.remove(old_key)
            except Exception:
                pass
        
        # Update hotkey
        self.hotkeys[action] = key
        self._save_hotkeys()
        
        # Register new hotkey
        if action == 'save_clip':
            self._register_save_clip()
        elif action == 'save_extended_clip':
            self._register_save_extended_clip()
        elif action == 'save_screenshot':
            self._register_save_screenshot()
        elif action == 'confirm_game_detection':
            self._register_confirm_game_detection()
        elif action == 'dismiss_game_detection':
            self._register_dismiss_game_detection()
        
        return True
    
    def get_hotkey(self, action: str) -> str:
        """Get the current hotkey for an action"""
        return self.hotkeys.get(action, '')
    
    def _register_keyboard_hotkey(self, key: str, signal):
        """Register one hotkey via the keyboard library. Silent on Linux if it
        fails — the socket server is the primary hotkey path on Linux/Wayland."""
        try:
            keyboard.add_hotkey(key, lambda: signal.emit())
            if key not in self._registered_hotkeys:
                self._registered_hotkeys.append(key)
        except Exception as e:
            if sys.platform != 'linux':
                print(f"Failed to register hotkey {key}: {e}")

    def _register_save_clip(self):
        self._register_keyboard_hotkey(
            self.hotkeys['save_clip'], self.save_clip_triggered)

    def _register_save_extended_clip(self):
        self._register_keyboard_hotkey(
            self.hotkeys['save_extended_clip'], self.save_extended_clip_triggered)

    def _register_save_screenshot(self):
        self._register_keyboard_hotkey(
            self.hotkeys['save_screenshot'], self.save_screenshot_triggered)

    def _register_confirm_game_detection(self):
        self._register_keyboard_hotkey(
            self.hotkeys['confirm_game_detection'],
            self.confirm_game_detection_triggered)

    def _register_dismiss_game_detection(self):
        self._register_keyboard_hotkey(
            self.hotkeys['dismiss_game_detection'],
            self.dismiss_game_detection_triggered)

    def register_all(self):
        """Register all hotkeys"""
        self._register_save_clip()
        self._register_save_extended_clip()
        self._register_save_screenshot()
        self._register_confirm_game_detection()
        self._register_dismiss_game_detection()
        self._start_socket_server()

    def _start_socket_server(self):
        """Listen on HOTKEY_SOCKET_PATH for Hyprland bind commands."""
        try:
            os.unlink(HOTKEY_SOCKET_PATH)
        except FileNotFoundError:
            pass

        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(HOTKEY_SOCKET_PATH)
            srv.listen(8)
            srv.settimeout(1.0)
        except Exception as e:
            print(f"[Hotkey] Socket server failed to start: {e}")
            return

        self._socket_running = True
        print(f"[Hotkey] Socket server listening on {HOTKEY_SOCKET_PATH}")

        _dispatch = {
            'save_clip':               self.save_clip_triggered,
            'save_extended_clip':      self.save_extended_clip_triggered,
            'save_screenshot':         self.save_screenshot_triggered,
            'confirm_game_detection':  self.confirm_game_detection_triggered,
            'dismiss_game_detection':  self.dismiss_game_detection_triggered,
        }

        def _serve():
            while self._socket_running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                with conn:
                    try:
                        data = conn.recv(64).decode().strip()
                        if data in _dispatch:
                            # Emit on the Qt main thread via QTimer.singleShot —
                            # emitting PyQt signals directly from a non-QThread
                            # thread is not thread-safe with direct connections.
                            sig = _dispatch[data]
                            from PyQt6.QtCore import QTimer
                            QTimer.singleShot(0, sig.emit)
                        else:
                            print(f"[Hotkey] Unknown command: {data!r}")
                    except Exception:
                        pass
            srv.close()
            try:
                os.unlink(HOTKEY_SOCKET_PATH)
            except FileNotFoundError:
                pass

        self._socket_thread = threading.Thread(target=_serve, daemon=True, name='fthr-hotkey-socket')
        self._socket_thread.start()

    def unregister_all(self):
        """Unregister all hotkeys"""
        for key in self._registered_hotkeys:
            try:
                keyboard.remove_hotkey(key)
            except Exception:
                pass
        self._registered_hotkeys.clear()

    def cleanup(self):
        """Clean up hotkeys on exit"""
        self.unregister_all()
        self._socket_running = False
        if self._socket_thread:
            self._socket_thread.join(timeout=2.0)


# The dropdown options in settings. Not exhaustive on purpose — these are the
# combos that (a) don't collide with common game binds and (b) actually work
# cross-platform with the `keyboard` lib. Add more at your own peril.
AVAILABLE_KEYS = [
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'ctrl+shift+s', 'ctrl+shift+c', 'ctrl+shift+x',
    'alt+s', 'alt+c', 'alt+x',
    'ctrl+alt+s', 'ctrl+alt+c', 'ctrl+alt+x'
]