"""
Hotkey Manager - global keyboard shortcuts for FTHR Clips.

This is what lets you smash F9 mid-game and clip the play without alt-tabbing.
We lean on the `keyboard` library, which hooks input at the OS level.

LINUX FOOTGUN, READ THIS: if you're at 2am wondering why nothing happens when
you press the hotkey — the `keyboard` lib needs raw access to /dev/input on
Linux, which means you either run as root (don't) or your user is in the
'input' group. `sudo usermod -aG input $USER`, log out, log back in. See README.
You're welcome.
"""
import keyboard
from PyQt6.QtCore import QObject, pyqtSignal
import json
from pathlib import Path


class HotkeyManager(QObject):
    """Manages global hotkeys for the application"""
    
    # Signals
    save_clip_triggered = pyqtSignal()
    save_extended_clip_triggered = pyqtSignal()
    save_screenshot_triggered = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        # Lives next to all the other user state in ~/.fthr. Survives reinstalls.
        self.config_file = Path.home() / '.fthr' / 'hotkeys.json'

        # Sensible defaults nobody's ever bound to anything else. F9/F10/F11 it is.
        self.hotkeys = {
            'save_clip': 'F9',
            'save_extended_clip': 'F10',
            'save_screenshot': 'F11'
        }

        # We track what we've actually registered so we can cleanly unhook later.
        # The `keyboard` lib gets cranky if you remove a hotkey you never added.
        self._registered_hotkeys = []
        
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
        
        return True
    
    def get_hotkey(self, action: str) -> str:
        """Get the current hotkey for an action"""
        return self.hotkeys.get(action, '')
    
    def _register_save_clip(self):
        """Register the save clip hotkey"""
        key = self.hotkeys['save_clip']
        if not key:
            return
        
        try:
            keyboard.add_hotkey(key, lambda: self.save_clip_triggered.emit())
            if key not in self._registered_hotkeys:
                self._registered_hotkeys.append(key)
            print(f"Registered hotkey: {key} -> Save Clip")
        except Exception as e:
            print(f"Failed to register hotkey {key}: {e}")
    
    def _register_save_extended_clip(self):
        """Register the save extended clip hotkey"""
        key = self.hotkeys['save_extended_clip']
        if not key:
            return
        
        try:
            keyboard.add_hotkey(key, lambda: self.save_extended_clip_triggered.emit())
            if key not in self._registered_hotkeys:
                self._registered_hotkeys.append(key)
            print(f"Registered hotkey: {key} -> Save Extended Clip")
        except Exception as e:
            print(f"Failed to register hotkey {key}: {e}")
    
    def _register_save_screenshot(self):
        """Register the save screenshot hotkey"""
        key = self.hotkeys['save_screenshot']
        if not key:
            return
        
        try:
            keyboard.add_hotkey(key, lambda: self.save_screenshot_triggered.emit())
            if key not in self._registered_hotkeys:
                self._registered_hotkeys.append(key)
            print(f"Registered hotkey: {key} -> Save Screenshot")
        except Exception as e:
            print(f"Failed to register hotkey {key}: {e}")
    
    def register_all(self):
        """Register all hotkeys"""
        self._register_save_clip()
        self._register_save_extended_clip()
        self._register_save_screenshot()
    
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


# The dropdown options in settings. Not exhaustive on purpose — these are the
# combos that (a) don't collide with common game binds and (b) actually work
# cross-platform with the `keyboard` lib. Add more at your own peril.
AVAILABLE_KEYS = [
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'ctrl+shift+s', 'ctrl+shift+c', 'ctrl+shift+x',
    'alt+s', 'alt+c', 'alt+x',
    'ctrl+alt+s', 'ctrl+alt+c', 'ctrl+alt+x'
]