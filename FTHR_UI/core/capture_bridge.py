# capture_bridge.py - Python interface to the C++ capture engine.
#
# The heavy lifting (screen grab + NVENC/x264 encode + ring buffer) lives in a
# native engine process. This file is the skinny Python side that talks to it.
#
# How they talk: a single fixed-layout struct mapped into shared memory. The UI
# writes a command into the "ui_*" fields, the engine reads it, does the thing,
# and writes back into the "engine_*" fields. No sockets, no pipes, no JSON
# serialization tax on the hot path. Galaxy brain moment: just use shared memory.
#
# The catch is that Windows and Linux do shared memory completely differently:
#   - Windows: OpenFileMapping + MapViewOfFile via kernel32, wide (utf-16) strings
#   - Linux:   plain mmap of /dev/shm/<name>, plus narrow (utf-8) byte strings
# So basically every method has a "if win32 / else" fork. It's not pretty but
# the struct layout is the contract and both sides agree on it. don't touch the
# field order unless you also change the C++ side or everything reads garbage.

import sys
import ctypes
from ctypes import Structure, c_uint32, c_bool, c_float, c_uint64
from enum import IntEnum
import time
import os

if sys.platform == 'win32':
    from ctypes import c_wchar


# Command/response codes are just plain ints on the wire. Keep these in lockstep
# with the enums on the C++ side — they are not auto-generated, sadly.
class CommandType(IntEnum):
    NONE = 0
    START_RECORDING = 1
    STOP_RECORDING = 2
    SAVE_CLIP = 3
    SET_RESOLUTION = 4
    SET_QUALITY = 5
    SET_FRAMERATE = 6
    SET_HOTKEY = 7
    SET_TARGET_WINDOW = 8
    GET_STATUS = 9
    RECONFIGURE_ENCODER = 10


class ResponseType(IntEnum):
    NONE = 0
    RECORDING_STARTED = 1
    RECORDING_STOPPED = 2
    CLIP_SAVED = 3
    STATUS_UPDATE = 4
    ERROR_OCCURRED = 5
    SAVE_STARTED = 6       # Phase 3: async SaveClip queued


# THE struct. This binary layout is the entire API contract with the engine.
# Windows uses wide chars (c_wchar) because the engine was born on Win32 and
# everything there is utf-16. Linux uses plain bytes (c_char) and bigger string
# buffers because paths on Linux can get long and weird. Field ORDER and SIZES
# must match the C++ definition byte-for-byte or you'll read pure nonsense.
if sys.platform == 'win32':
    class SharedMemoryLayout(Structure):
        _fields_ = [
            ('ui_command',        c_uint32),
            ('ui_param1',         c_uint32),
            ('ui_param2',         c_uint32),
            ('ui_param3',         c_uint32),
            ('ui_string',         c_wchar * 256),
            ('engine_response',   c_uint32),
            ('engine_param1',     c_uint32),
            ('engine_param2',     c_uint32),
            ('engine_param3',     c_float),
            ('engine_string',     c_wchar * 512),
            ('is_recording',      c_bool),
            ('is_initialized',    c_bool),
            ('frames_captured',   c_uint64),
            ('bytes_written',     c_uint64),
            ('cfg_bitrate_kbps',  c_uint32),
            ('cfg_target_width',  c_uint32),
            ('cfg_target_height', c_uint32),
            ('nvenc_active',      c_bool),
            # v2 fields — must match shared_memory.h byte-for-byte
            ('cfg_codec_pref',    c_uint32),
            ('cfg_preset',        c_uint32),
            ('active_codec',      ctypes.c_char * 64),
            # v3 fields
            ('multiband_enabled',     c_bool),
            ('active_audio_mappings', ctypes.c_char * 1024),
        ]
else:
    class SharedMemoryLayout(Structure):
        _fields_ = [
            ('ui_command',        c_uint32),
            ('ui_param1',         c_uint32),
            ('ui_param2',         c_uint32),
            ('ui_param3',         c_uint32),
            ('ui_string',         ctypes.c_char * 1024),
            ('engine_response',   c_uint32),
            ('engine_param1',     c_uint32),
            ('engine_param2',     c_uint32),
            ('engine_param3',     c_float),
            ('engine_string',     ctypes.c_char * 2048),
            ('is_recording',      c_bool),
            ('is_initialized',    c_bool),
            ('frames_captured',   c_uint64),
            ('bytes_written',     c_uint64),
            ('cfg_bitrate_kbps',  c_uint32),
            ('cfg_target_width',  c_uint32),
            ('cfg_target_height', c_uint32),
            ('nvenc_active',      c_bool),
            # v2 fields — must match shared_memory.h byte-for-byte
            ('cfg_codec_pref',    c_uint32),
            ('cfg_preset',        c_uint32),
            ('active_codec',      ctypes.c_char * 64),
            # v3 fields
            ('multiband_enabled',     c_bool),
            ('active_audio_mappings', ctypes.c_char * 1024),
        ]


class CaptureBridge:
    # Bumped the _v1 suffix the day I changed the struct layout and spent two
    # hours wondering why an old engine kept reading my new fields wrong.
    # Versioned name = old + new never accidentally share the same mapping.
    SHARED_MEM_NAME = 'FTHR_SharedMemory_v3'

    # Singleton. There is exactly one engine and one mapping, so one bridge.
    # Anything else just hands you back the same object.
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._mem_handle = None
            cls._instance._layout = None
            cls._instance._linux_mmap = None
        return cls._instance


    def initialize(self) -> bool:
        if self._initialized:
            return True
        if sys.platform == 'win32':
            return self._initialize_windows()
        else:
            return self._initialize_linux()


    def _initialize_linux(self) -> bool:
        import mmap as _mmap
        shm_path = f'/dev/shm/{self.SHARED_MEM_NAME}'
        try:
            fd = os.open(shm_path, os.O_RDWR)
        except OSError:
            print('Failed to open shared memory - is Linux engine running?')
            return False

        size = ctypes.sizeof(SharedMemoryLayout)
        try:
            self._linux_mmap = _mmap.mmap(fd, size, access=_mmap.ACCESS_WRITE)
        except Exception as e:
            os.close(fd)
            print(f'mmap failed: {e}')
            return False
        finally:
            os.close(fd)  # the mmap holds its own ref, so the fd is dead weight now

        # from_buffer maps the struct directly onto the mmap — zero copy, writes
        # go straight to shared memory. This is the whole trick.
        self._layout = SharedMemoryLayout.from_buffer(self._linux_mmap)

        if not self._layout.is_initialized:
            print('Linux engine not initialized yet')
            self._linux_mmap.close()
            self._linux_mmap = None
            self._layout = None
            return False

        self._initialized = True
        print('Connected to Linux capture engine')
        return True


    def _initialize_windows(self) -> bool:
        kernel32 = ctypes.windll.kernel32

        self._mem_handle = kernel32.OpenFileMappingW(
            0xF001F, False, self.SHARED_MEM_NAME)

        if not self._mem_handle:
            print('Failed to open shared memory - is C++ engine running?')
            return False

        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        ptr = kernel32.MapViewOfFile(
            self._mem_handle, 0xF001F, 0, 0,
            ctypes.sizeof(SharedMemoryLayout))

        if not ptr:
            kernel32.CloseHandle(self._mem_handle)
            self._mem_handle = None
            return False

        self._layout = ctypes.cast(
            ptr, ctypes.POINTER(SharedMemoryLayout)).contents

        if not self._layout.is_initialized:
            print('C++ engine not initialized yet')
            self.shutdown()
            return False

        self._initialized = True
        print('Connected to C++ capture engine')
        return True


    def shutdown(self):
        if sys.platform != 'win32':
            # Must release the ctypes from_buffer reference BEFORE closing the
            # mmap — otherwise Python raises BufferError: cannot close exported
            # pointers exist. Order matters here, ask me how I know.
            self._layout = None
            if self._linux_mmap is not None:
                self._linux_mmap.close()
                self._linux_mmap = None
        else:
            if self._layout:
                ctypes.windll.kernel32.UnmapViewOfFile(
                    ctypes.byref(self._layout))
                self._layout = None
            if self._mem_handle:
                ctypes.windll.kernel32.CloseHandle(self._mem_handle)
                self._mem_handle = None
        self._initialized = False
        CaptureBridge._instance = None


    def is_connected(self) -> bool:
        if not self._initialized or self._layout is None:
            return False
        try:
            return self._layout.is_initialized
        except Exception:
            return False


    def save_clip(self, output_path: str, duration: int = 30) -> bool:
        """
        Yell at the engine: "save the last N seconds, NOW."

        Fire-and-mostly-forget. We hand the engine a path + duration, it grabs
        whatever's currently in the ring buffer and starts encoding on its own
        thread. We only block long enough to hear "yeah I got it" (SAVE_STARTED),
        not for the actual encode — that would freeze the UI on long clips.

        Returns True if the save was successfully queued, False otherwise.
        """
        if not self.is_connected():
            print('Not connected to capture engine')
            return False

        # Wipe any leftover response first — narrator: it was not, in fact, fine
        # without this. Stale CLIP_SAVED from a previous run looked like success.
        self._layout.engine_response = ResponseType.NONE
        # Send command — Linux uses c_char (bytes), Windows uses c_wchar (str)
        if sys.platform != 'win32':
            self._layout.ui_string = output_path.encode('utf-8')[:1023]
        else:
            self._layout.ui_string = output_path
        self._layout.ui_param1 = duration
        # Write the command code LAST. The engine polls ui_command, so once this
        # lands it may read every other field — they all need to be set already.
        self._layout.ui_command = CommandType.SAVE_CLIP

        # Spin until the engine acks. Should be near-instant; the 1s ceiling is
        # purely so a dead/hung engine doesn't lock the UI forever.
        start = time.time() * 1000
        while self._layout.engine_response not in (ResponseType.SAVE_STARTED,
                                                     ResponseType.ERROR_OCCURRED):
            if (time.time() * 1000 - start) > 1000:  # 1 second timeout
                print('Timeout waiting for SAVE_STARTED')
                return False
            time.sleep(0.001)
        
        # Check if successful
        response = self._layout.engine_response
        self._layout.engine_response = ResponseType.NONE
        
        if response == ResponseType.ERROR_OCCURRED:
            print('Engine returned ERROR_OCCURRED')
            return False
        
        # SAVE_STARTED received - task is queued, encoding happens in background
        print(f'Clip save queued: {output_path} ({duration}s)')
        return True


    def pause_recording(self) -> bool:
        if not self.is_connected():
            return False
        self._layout.ui_command = CommandType.STOP_RECORDING
        return True

    def resume_recording(self) -> bool:
        if not self.is_connected():
            return False
        self._layout.ui_command = CommandType.START_RECORDING
        return True

    def wait_for_clip_completion(self, timeout_ms: int = 30000) -> bool:
        """
        Optional: Wait for the queued clip to finish encoding.
        
        Use this if you need to know when the file is ready.
        Not required for normal UI flow.
        
        Args:
            timeout_ms: Maximum time to wait (default 30 seconds)
        
        Returns:
            True if CLIP_SAVED received
            False if timeout or error
        """
        if not self.is_connected():
            return False
        
        start = time.time() * 1000
        while self._layout.engine_response != ResponseType.CLIP_SAVED:
            if (time.time() * 1000 - start) > timeout_ms:
                print(f'Timeout waiting for clip completion ({timeout_ms}ms)')
                return False
            time.sleep(0.010)  # 10ms poll interval
        
        self._layout.engine_response = ResponseType.NONE
        return True


    def get_status(self) -> dict:
        if not self.is_connected():
            return {'connected': False}
        try:
            return {
                'connected': True,
                'is_recording': self._layout.is_recording,
                'frames_captured': self._layout.frames_captured,
                'nvenc_active': self._layout.nvenc_active,
            }
        except Exception:
            return {'connected': False}

    _CODEC_PREF_MAP = {'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3}

    def set_encoder_config(self, codec_pref: str, preset: int) -> bool:
        if not self.is_connected():
            return False
        pref_int = self._CODEC_PREF_MAP.get(codec_pref.lower(), 0)
        if codec_pref.lower() not in self._CODEC_PREF_MAP:
            print(f'[CaptureBridge] Unknown codec_pref "{codec_pref}", falling back to auto')
        preset   = max(1, min(7, preset))
        self._layout.cfg_codec_pref = pref_int
        self._layout.cfg_preset     = preset
        self._layout.ui_command     = CommandType.RECONFIGURE_ENCODER
        return True

    def get_active_codec(self) -> str:
        if not self.is_connected():
            return ''
        try:
            raw = self._layout.active_codec
            return raw.decode('utf-8', errors='ignore').rstrip('\x00')
        except Exception:
            return ''

    def get_active_preset(self) -> int:
        if not self.is_connected():
            return 4
        try:
            return int(self._layout.cfg_preset) or 4
        except Exception:
            return 4

    def get_audio_mappings(self) -> dict:
        """Returns {app_name: category_name} from shared memory."""
        if not self.is_connected():
            return {}
        try:
            raw  = self._layout.active_audio_mappings
            text = raw.decode('utf-8', errors='ignore').rstrip('\x00')
            if not text or text == '{}':
                return {}
            import json
            return json.loads(text)
        except Exception:
            return {}

    def is_multiband_active(self) -> bool:
        if not self.is_connected():
            return False
        try:
            return bool(self._layout.multiband_enabled)
        except Exception:
            return False
