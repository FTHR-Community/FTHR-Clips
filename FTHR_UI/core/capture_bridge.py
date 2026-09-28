# Shared-memory command/status bridge to the native capture engine.
# Windows uses kernel32 mappings and UTF-16 strings; Linux uses /dev/shm and
# UTF-8 buffers. Each layout must match its native shared_memory.h byte for byte.
# Frames remain in the engine; only commands and status cross this boundary.

import sys
import ctypes
from ctypes import Structure, c_uint32, c_bool, c_float, c_uint64
from enum import IntEnum
import os

if sys.platform == 'win32':
    from ctypes import c_wchar


# Keep wire command/response values aligned with the native enums.
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
    SHUTDOWN = 11


class ResponseType(IntEnum):
    NONE = 0
    RECORDING_STARTED = 1
    RECORDING_STOPPED = 2
    CLIP_SAVED = 3
    STATUS_UPDATE = 4
    ERROR_OCCURRED = 5
    SAVE_STARTED = 6       # Save request queued; completion arrives separately
    MANUAL_RECORDING_ERROR = 7


# Windows strings use c_wchar; Linux uses c_char with larger UTF-8 buffers.
# Field order, sizes, and alignment must match the platform C++ layout.
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
            # v4 capture/content health fields
            ('capture_health_flags',       c_uint32),
            ('capture_generation',         c_uint32),
            ('content_sample_sequence',    c_uint32),
            ('content_suspicious_streak',  c_uint32),
            ('content_luma_mean',           c_float),
            ('content_luma_variance',       c_float),
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
            # v4 capture/content health fields
            ('capture_health_flags',       c_uint32),
            ('capture_generation',         c_uint32),
            ('content_sample_sequence',    c_uint32),
            ('content_suspicious_streak',  c_uint32),
            ('content_luma_mean',           c_float),
            ('content_luma_variance',       c_float),
        ]


class CaptureBridge:
    # The mapping name is versioned so processes with different structure
    # layouts cannot attach to one another.
    SHARED_MEM_NAME = 'FTHR_SharedMemory_v4'

    # Singleton. There is exactly one engine and one mapping, so one bridge.
    # Anything else just hands you back the same object.
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._mem_handle = None
            cls._instance._win_map_ptr = None
            cls._instance._layout = None
            cls._instance._linux_mmap = None
            cls._instance._logged_read_errors = set()
        return cls._instance

    def _log_read_error_once(self, where: str, e: Exception):
        """Shared-memory read errors usually mean layout mismatch / dead engine.
        Log each site once so IPC corruption doesn't masquerade as 'feature off'."""
        if where not in self._logged_read_errors:
            self._logged_read_errors.add(where)
            print(f'[CaptureBridge] {where} read failed: {type(e).__name__}: {e}')


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

        # Reject a size mismatch before mapping. This catches layout changes made
        # without updating the versioned mapping name.
        try:
            actual = os.fstat(fd).st_size
        except OSError:
            actual = -1
        if actual >= 0 and actual != size:
            os.close(fd)
            print(
                f'[Bridge] Shared-memory layout mismatch on {shm_path}: the '
                f'engine created {actual} bytes, this UI expects {size} bytes '
                f'for {self.SHARED_MEM_NAME}.\n'
                f'[Bridge] The running engine was built from a different '
                f'shared_memory.h. Rebuild the Linux engine from this source '
                f'tree (see CONTRIBUTING.md, "The shared-memory contract"). '
                f'Refusing to map it — reading it would return garbage.'
            )
            return False

        try:
            self._linux_mmap = _mmap.mmap(fd, size, access=_mmap.ACCESS_WRITE)
        except Exception as e:
            # No close() here: the finally below owns it. Closing in both places
            # made the second close raise EBADF *out of the finally*, replacing
            # the intended `return False` with an exception.
            print(f'mmap failed: {e}')
            return False
        finally:
            os.close(fd)  # The mmap retains the mapping after the descriptor closes.

        # from_buffer maps the structure directly onto shared memory without a copy.
        self._layout = SharedMemoryLayout.from_buffer(self._linux_mmap)

        if not self._layout.is_initialized:
            print('Linux engine not initialized yet')
            del self._layout
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

        # Keep the raw mapping pointer — UnmapViewOfFile needs exactly this
        # address, not byref() of the casted struct.
        self._win_map_ptr = ptr
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
        # Release the acknowledgement's layout reference before unmapping.
        self._save_acknowledged_layout = None
        if sys.platform != 'win32':
            # Release the ctypes from_buffer reference before closing the mmap;
            # otherwise Python raises BufferError for the exported pointer.
            self._layout = None
            if self._linux_mmap is not None:
                self._linux_mmap.close()
                self._linux_mmap = None
        else:
            if self._layout:
                ctypes.windll.kernel32.UnmapViewOfFile(
                    ctypes.c_void_p(getattr(self, '_win_map_ptr', None) or 0))
                self._win_map_ptr = None
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
        """Submit SaveClip without blocking the Qt thread.

        True means submission succeeded; it doesn't confirm a saved file.
        False means no connection or an unrepresentable path. The poller in main.py
        owns responses and must process any pending result before submission.
        Never clear engine_response here: it may hold the previous save result.
        """
        if not self.is_connected():
            print('Not connected to capture engine')
            return False

        # Send command — Linux uses c_char (bytes), Windows uses c_wchar (str)
        if sys.platform != 'win32':
            # Encode and truncate at a char boundary so we never split a multi-byte
            # UTF-8 sequence. The buffer is 1024 bytes; leave 1 for the null terminator.
            encoded = output_path.encode('utf-8')
            if len(encoded) > 1023:
                encoded = output_path.encode('utf-8')[:1023].decode('utf-8', errors='ignore').encode('utf-8')
            self._layout.ui_string = encoded
        else:
            # Buffer is c_wchar * 256 — a longer path raises ValueError mid-save.
            if len(output_path) > 255:
                print(f'save_clip: path too long ({len(output_path)} chars), refusing')
                return False
            self._layout.ui_string = output_path
        self._layout.ui_param1 = duration
        self._save_acknowledged_layout = None
        # Write the command code LAST. The engine polls ui_command, so once this
        # lands it may read every other field — they all need to be set already.
        self._layout.ui_command = CommandType.SAVE_CLIP
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

    def start_manual_recording(self, output_path: str) -> bool:
        """Start the native Windows continuous recorder asynchronously."""
        if sys.platform != 'win32' or not self.is_connected():
            return False
        if not output_path or len(output_path) > 255:
            print('[CaptureBridge] Manual-recording path is empty or too long')
            return False
        self._layout.ui_string = output_path
        self._layout.ui_command = CommandType.START_RECORDING
        return True

    def stop_manual_recording(self) -> bool:
        if sys.platform != 'win32' or not self.is_connected():
            return False
        self._layout.ui_command = CommandType.STOP_RECORDING
        return True

    def request_engine_shutdown(self) -> bool:
        """Ask the native engine to leave its command loop cleanly.

        This uses the existing command channel only; the v4 shared-memory
        layout remains byte-for-byte unchanged.  The caller still owns the
        bounded process wait and escalation policy.
        """
        if not self.is_connected():
            return False
        try:
            self._layout.ui_command = CommandType.SHUTDOWN
            return True
        except Exception as e:
            self._log_read_error_once('request_engine_shutdown', e)
            return False

    # The save poller in main.py owns this response channel. It peeks the code
    # and detail together, interprets them, then consumes the result. The engine
    # writes the payload before publishing the response; error details may be
    # empty if the mapping is unavailable or an older engine is running.

    #: Responses that belong to a save. Everything else (STATUS_UPDATE,
    #: RECORDING_STARTED, …) is not ours and must be left in the field.
    _SAVE_RESPONSES = {
        ResponseType.CLIP_SAVED: 'saved',
        ResponseType.ERROR_OCCURRED: 'error',
        ResponseType.SAVE_STARTED: 'started',
    }

    def peek_save_response(self) -> tuple[str, str] | None:
        """Read a pending save response without consuming it.

        Returns ('started' | 'saved' | 'error', detail) or None. Non-save
        responses return None and are left untouched.
        """
        if not self.is_connected():
            return None
        try:
            resp = self._layout.engine_response
            kind = self._SAVE_RESPONSES.get(resp)
            if kind is None:
                return None
            if (resp == ResponseType.SAVE_STARTED
                    and getattr(self, '_save_acknowledged_layout', None)
                    is self._layout):
                # Leave the acknowledgement in shared memory. The engine can
                # replace it with a terminal result at any instant; clearing
                # it here could erase that result. Hide our already-read ack
                # locally so the poller can still advance its deadlines.
                return None
            return (kind, self._read_engine_string())
        except Exception as e:
            self._log_read_error_once('peek_save_response', e)
            return None

    def consume_save_response(self, expected_kind: str | None = None) -> bool:
        """Consume the result that the poller actually read.

        Acknowledge SAVE_STARTED locally: completion can replace it between peek
        and consume. Only terminal results are cleared in shared memory; one
        outstanding save prevents a subsequent result from being erased.
        """
        if not self.is_connected():
            return False
        try:
            current_kind = self._SAVE_RESPONSES.get(self._layout.engine_response)
            kind = expected_kind if expected_kind is not None else current_kind
            if kind == 'started':
                if getattr(self, '_save_acknowledged_layout', None) is self._layout:
                    return False
                self._save_acknowledged_layout = self._layout
                return True
            if kind not in ('saved', 'error') or current_kind != kind:
                return False
            self._layout.engine_response = ResponseType.NONE
            return True
        except Exception as e:
            self._log_read_error_once('consume_save_response', e)
            return False

    _MANUAL_RECORDING_RESPONSES = {
        ResponseType.RECORDING_STARTED: 'started',
        ResponseType.RECORDING_STOPPED: 'stopped',
        ResponseType.MANUAL_RECORDING_ERROR: 'error',
    }

    def peek_manual_recording_response(self) -> tuple[str, str] | None:
        """Read a continuous-recording response without stealing save events."""
        if not self.is_connected():
            return None
        try:
            response = self._layout.engine_response
            kind = self._MANUAL_RECORDING_RESPONSES.get(response)
            if kind is None:
                return None
            return kind, self._read_engine_string()
        except Exception as exc:
            self._log_read_error_once('peek_manual_recording_response', exc)
            return None

    def consume_manual_recording_response(self) -> bool:
        if not self.is_connected():
            return False
        try:
            if self._layout.engine_response not in self._MANUAL_RECORDING_RESPONSES:
                return False
            self._layout.engine_response = ResponseType.NONE
            return True
        except Exception as exc:
            self._log_read_error_once('consume_manual_recording_response', exc)
            return False

    def _read_engine_string(self) -> str:
        """Read engine_string across both layouts (wchar on Windows, bytes on
        Linux) without letting a garbled buffer raise into the caller."""
        try:
            raw = self._layout.engine_string
            if isinstance(raw, bytes):
                return raw.decode('utf-8', errors='replace').rstrip('\x00').strip()
            return str(raw).rstrip('\x00').strip()
        except Exception:
            return ''

    def get_status(self) -> dict:
        if not self.is_connected():
            return {'connected': False}
        try:
            return {
                'connected': True,
                'is_recording': bool(self._layout.is_recording),
                'frames_captured': int(self._layout.frames_captured),
                'nvenc_active': bool(self._layout.nvenc_active),
                'capture_health_flags': int(self._layout.capture_health_flags),
                'capture_generation': int(self._layout.capture_generation),
                'content_sample_sequence': int(self._layout.content_sample_sequence),
                'content_suspicious_streak': int(self._layout.content_suspicious_streak),
                'content_luma_mean': float(self._layout.content_luma_mean),
                'content_luma_variance': float(self._layout.content_luma_variance),
            }
        except Exception as e:
            self._log_read_error_once('get_status', e)
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
        except Exception as e:
            self._log_read_error_once('get_active_codec', e)
            return ''

    def get_active_preset(self) -> int:
        if not self.is_connected():
            return 4
        try:
            return int(self._layout.cfg_preset) or 4
        except Exception as e:
            self._log_read_error_once('get_active_preset', e)
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
        except Exception as e:
            self._log_read_error_once('get_audio_mappings', e)
            return {}

    def is_multiband_active(self) -> bool:
        if not self.is_connected():
            return False
        try:
            return bool(self._layout.multiband_enabled)
        except Exception as e:
            self._log_read_error_once('is_multiband_active', e)
            return False
