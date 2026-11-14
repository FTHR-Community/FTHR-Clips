# Shared-memory command/status bridge to the native capture engine.
# Windows uses kernel32 mappings and UTF-16 strings; Linux uses /dev/shm and
# UTF-8 buffers. Each layout must match its native shared_memory.h byte for byte.
# Frames remain in the engine; only commands and status cross this boundary.

import sys
import ctypes
from ctypes import Structure, c_uint32, c_bool, c_float, c_uint64
from enum import IntEnum
import os
import threading
from dataclasses import dataclass
from uuid import UUID
import weakref

from core.capture_health import CaptureHealthFlag

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
    MARK_HIGHLIGHT = 12


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


@dataclass(frozen=True, slots=True)
class TerminalResponseSnapshot:
    """Detached terminal response data from one retained native mapping."""

    capture_generation: int
    response_code: int
    detail: str
    save_response: tuple[str, str] | None
    manual_response: tuple[str, str] | None
    is_recording: bool

    __hash__ = None

    def __post_init__(self):
        if type(self.capture_generation) is not int:
            raise TypeError('capture_generation must be an integer')
        if not 0 <= self.capture_generation <= 0xFFFFFFFF:
            raise ValueError('capture_generation must be a uint32')
        if type(self.response_code) is not int:
            raise TypeError('response_code must be an integer')
        if not isinstance(self.detail, str):
            raise TypeError('detail must be a string')
        if type(self.is_recording) is not bool:
            raise TypeError('is_recording must be a boolean')
        for channel in (self.save_response, self.manual_response):
            if channel is not None:
                if (not isinstance(channel, tuple) or len(channel) != 2
                        or any(not isinstance(item, str) for item in channel)):
                    raise TypeError('response channels must be string tuples')
        if self.save_response is not None and self.manual_response is not None:
            raise ValueError('a response cannot belong to two channels')


@dataclass(frozen=True, slots=True)
class _TerminalResponseOwner:
    """Opaque owner for one exact bridge/layout/native-generation tuple."""

    bridge: object
    _layout_ref: object
    capture_generation: int


class CaptureBridge:
    # The mapping name is versioned so processes with different structure
    # layouts cannot attach to one another.
    SHARED_MEM_NAME = 'FTHR_SharedMemory_v4'
    HIGHLIGHT_CAPABILITY = 1 << 16

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
            cls._instance._mapping_lock = threading.RLock()
        return cls._instance

    def _log_read_error_once(self, where: str, e: Exception):
        """Shared-memory read errors usually mean layout mismatch / dead engine.
        Log each site once so IPC corruption doesn't masquerade as 'feature off'."""
        if where not in self._logged_read_errors:
            self._logged_read_errors.add(where)
            print(f'[CaptureBridge] {where} read failed: {type(e).__name__}: {e}')

    def _submit_command(self, command: CommandType, payload_writer=None,
                        *, allow_unhealthy=False) -> bool:
        """Publish one payload/command pair under mapping ownership.

        The ABI has one command slot and no correlation token.  Keeping the
        existing mapping lock across the NONE check, payload writes, and final
        command store prevents concurrent save/mark/stop/config callers from
        interleaving their payloads or overwriting a pending command.
        """
        with self._mapping_lock:
            if not self._initialized or self._layout is None:
                return False
            try:
                if not self._layout.is_initialized:
                    return False
                if int(self._layout.ui_command) != int(CommandType.NONE):
                    return False
                flags = CaptureHealthFlag(int(self._layout.capture_health_flags))
                if (not allow_unhealthy and flags & (
                        CaptureHealthFlag.RECOVERING
                        | CaptureHealthFlag.BACKEND_FAILED
                        | CaptureHealthFlag.RECONFIGURE_REQUIRED)):
                    return False
                # A focus pause preserves replay history. Let the native save
                # boundary validate that history (and explain an empty ring)
                # instead of treating every Alt-Tab as a broken engine.
                if (not allow_unhealthy and flags & CaptureHealthFlag.PAUSED
                        and command != CommandType.SAVE_CLIP):
                    return False
                if payload_writer is not None and payload_writer() is False:
                    return False
                self._layout.ui_command = command
                return True
            except (AttributeError, OSError, TypeError, ValueError):
                # A stale or invalid ABI slot is a failed submission; do not
                # publish a command after partial shared-memory access.
                return False


    def initialize(self) -> bool:
        # Stop/restart may arrive while the mapping is being opened. Serialize
        # mapping ownership on both platforms; process waits remain outside.
        with self._mapping_lock:
            if sys.platform == 'win32':
                if self._initialized:
                    return True
                return self._initialize_windows()
            if self._initialized:
                return True
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
            self._release_linux_mapping()
            return False

        self._initialized = True
        print('Connected to Linux capture engine')
        return True


    def _release_linux_mapping(self) -> None:
        """Release ctypes' exported view before closing the mmap.

        ``SharedMemoryLayout.from_buffer`` keeps an exported pointer on the
        mapping.  Closing first raises ``BufferError`` and masks the intended
        failed-initialization result.
        """
        self._layout = None
        mapping = self._linux_mmap
        self._linux_mmap = None
        if mapping is not None:
            mapping.close()


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
        with self._mapping_lock:
            # Release the acknowledgement's layout reference before unmapping.
            self._save_acknowledged_layout = None
            if sys.platform != 'win32':
                # Release the ctypes from_buffer reference before closing the
                # mmap; otherwise Python raises BufferError for the exported
                # pointer. Keep this under the same mapping lock as reads.
                self._release_linux_mapping()
                self._initialized = False
                if CaptureBridge._instance is self:
                    CaptureBridge._instance = None
                return
            if self._layout:
                ctypes.windll.kernel32.UnmapViewOfFile(
                    ctypes.c_void_p(getattr(self, '_win_map_ptr', None) or 0))
                self._win_map_ptr = None
                self._layout = None
            if self._mem_handle:
                ctypes.windll.kernel32.CloseHandle(self._mem_handle)
                self._mem_handle = None
            self._initialized = False
            if CaptureBridge._instance is self:
                CaptureBridge._instance = None


    @staticmethod
    def _valid_uint32(value) -> bool:
        return type(value) is int and 0 <= value <= 0xFFFFFFFF

    def retain_terminal_response_owner(
            self, *, expected_capture_generation: int):
        """Retain one exact mapping for a post-exit response read.

        Native shutdown sets ``layout.is_initialized`` false before queued
        save work drains. The wrapper and mapping still remain owned here;
        therefore this method deliberately does not use ``is_connected`` or
        inspect that native field.
        """
        if not self._valid_uint32(expected_capture_generation):
            return None
        with self._mapping_lock:
            if not self._initialized or self._layout is None:
                return None
            layout = self._layout
            try:
                if int(layout.capture_generation) != expected_capture_generation:
                    return None
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                self._log_read_error_once(
                    'retain_terminal_response_owner', exc)
                return None
            return _TerminalResponseOwner(
                self, weakref.ref(layout), expected_capture_generation)

    def release_terminal_response_owner(self, owner=None) -> bool:
        """Release exactly the mapping retained by ``owner``.

        Terminal-response owners are a capability for one particular native
        mapping, so releasing one must never fall through to ``shutdown`` and
        close a replacement mapping.  All identity and generation checks, as
        well as the platform close sequence, stay under the mapping lock.
        """
        with self._mapping_lock:
            if owner is None:
                # A missing owner is only a read-only proof that no physical
                # bridge resources remain.  It must never trigger teardown.
                return (
                    self._initialized is False
                    and self._layout is None
                    and self._win_map_ptr in (None, 0)
                    and self._mem_handle in (None, 0)
                    and self._linux_mmap is None
                )

            if type(owner) is not _TerminalResponseOwner:
                return False
            try:
                if owner.bridge is not self:
                    return False
                if (not self._initialized or self._layout is None
                        or self._linux_mmap is not None
                        and sys.platform == 'win32'):
                    return False
                if not self._valid_uint32(owner.capture_generation):
                    return False
                # Do not retain this weakly referenced layout in a local
                # variable: on Linux mmap.close() must see no exported view.
                if owner._layout_ref() is not self._layout:
                    return False
                if (int(self._layout.capture_generation)
                        != owner.capture_generation):
                    return False
            except Exception:
                # Invalid ownership cannot authorize releasing any resource.
                return False

            if sys.platform == 'win32':
                # A real Windows owner always has both native resources.  A
                # partial wrapper cannot truthfully claim that it was freed.
                original_ptr = self._win_map_ptr
                original_handle = self._mem_handle
                if (original_ptr in (None, 0)
                        or original_handle in (None, 0)
                        or self._linux_mmap is not None):
                    return False

                kernel32 = ctypes.windll.kernel32
                try:
                    if not kernel32.UnmapViewOfFile(
                            ctypes.c_void_p(original_ptr)):
                        return False
                except Exception:
                    # Retain the original view and handle when unmap is uncertain.
                    return False

                # The view is gone permanently after a successful unmap.  Do
                # not restore its pointer if the handle close is unsuccessful.
                self._layout = None
                self._win_map_ptr = None
                self._initialized = False
                self._save_acknowledged_layout = None
                try:
                    if not kernel32.CloseHandle(original_handle):
                        return False
                except Exception:
                    # Keep the handle visible until its close can be proved.
                    return False
                self._mem_handle = None
            else:
                mapping = self._linux_mmap
                layout_ref = owner._layout_ref
                if (mapping is None or self._win_map_ptr not in (None, 0)
                        or self._mem_handle not in (None, 0)):
                    return False

                # Drop the ctypes exported view before close().  Keep the mmap
                # object in a local until close succeeds so a failure leaves
                # the original resource visible on the bridge.
                self._save_acknowledged_layout = None
                self._layout = None
                try:
                    mapping.close()
                except Exception:
                    retained_layout = layout_ref()
                    if retained_layout is not None:
                        self._layout = retained_layout
                    return False
                self._linux_mmap = None
                self._initialized = False

            if (self._layout is not None
                    or self._win_map_ptr not in (None, 0)
                    or self._mem_handle not in (None, 0)
                    or self._linux_mmap is not None
                    or self._initialized):
                return False
            if CaptureBridge._instance is self:
                CaptureBridge._instance = None
            return True

    def read_terminal_response_snapshot(
            self, owner, *, process_exited: bool):
        """Read retained terminal response data without consuming or writing."""
        if type(process_exited) is not bool or not process_exited:
            return None
        with self._mapping_lock:
            if type(owner) is not _TerminalResponseOwner:
                return None
            if owner.bridge is not self:
                return None
            if not self._initialized or self._layout is None:
                return None
            layout = owner._layout_ref()
            if layout is None or self._layout is not layout:
                return None
            try:
                generation = int(layout.capture_generation)
                if (not self._valid_uint32(generation)
                        or generation != owner.capture_generation):
                    return None
                response_code = int(layout.engine_response)
                detail = self._read_engine_string_from_layout(layout)
                save_kind = self._SAVE_RESPONSES.get(response_code)
                manual_kind = self._MANUAL_RECORDING_RESPONSES.get(response_code)
                save_response = ((save_kind, detail)
                                 if save_kind is not None else None)
                manual_response = ((manual_kind, detail)
                                   if manual_kind is not None else None)
                return TerminalResponseSnapshot(
                    capture_generation=generation,
                    response_code=response_code,
                    detail=detail,
                    save_response=save_response,
                    manual_response=manual_response,
                    is_recording=bool(layout.is_recording),
                )
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                self._log_read_error_once(
                    'read_terminal_response_snapshot', exc)
                return None


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
        def write_payload():
            if sys.platform != 'win32':
                encoded = output_path.encode('utf-8')
                if len(encoded) > 1023:
                    encoded = (encoded[:1023].decode('utf-8', errors='ignore')
                               .encode('utf-8'))
                self._layout.ui_string = encoded
            else:
                if len(output_path) > 255:
                    print(f'save_clip: path too long ({len(output_path)} chars), refusing')
                    return False
                self._layout.ui_string = output_path
            self._layout.ui_param1 = duration
            self._save_acknowledged_layout = None
            return True
        return self._submit_command(CommandType.SAVE_CLIP, write_payload)

    def mark_highlight(self, marker_id: str) -> bool:
        """Submit one bounded highlight request through the existing ABI.

        This is submission-only: native capture owns the ring mark and the
        shared response slot remains exclusively owned by the poller.
        """

        try:
            parsed = UUID(marker_id)
        except (AttributeError, TypeError, ValueError):
            # Invalid caller marker IDs are rejected without touching the
            # shared-memory command or response slots.
            return False
        canonical = str(parsed)
        if marker_id != canonical:
            return False
        def write_payload():
            flags = int(self._layout.capture_health_flags)
            if not flags & self.HIGHLIGHT_CAPABILITY:
                return False
            health = CaptureHealthFlag(flags)
            if not health & CaptureHealthFlag.ACTIVE:
                return False
            self._layout.ui_string = (canonical if sys.platform == 'win32'
                                      else canonical.encode('utf-8'))
            return True
        return self._submit_command(CommandType.MARK_HIGHLIGHT, write_payload)


    def pause_recording(self) -> bool:
        return self._submit_command(CommandType.STOP_RECORDING,
                                    allow_unhealthy=True)

    def resume_recording(self) -> bool:
        return self._submit_command(CommandType.START_RECORDING,
                                    allow_unhealthy=True)

    def start_manual_recording(self, output_path: str) -> bool:
        """Start the native Windows continuous recorder asynchronously."""
        if sys.platform != 'win32':
            return False
        if not output_path or len(output_path) > 255:
            print('[CaptureBridge] Manual-recording path is empty or too long')
            return False
        return self._submit_command(
            CommandType.START_RECORDING,
            lambda: setattr(self._layout, 'ui_string', output_path) or True,
            allow_unhealthy=True)

    def stop_manual_recording(self) -> bool:
        if sys.platform != 'win32':
            return False
        return self._submit_command(CommandType.STOP_RECORDING,
                                    allow_unhealthy=True)

    def request_engine_shutdown(self) -> bool:
        """Ask the native engine to leave its command loop cleanly.

        This uses the existing command channel only; the v4 shared-memory
        layout remains byte-for-byte unchanged.  The caller still owns the
        bounded process wait and escalation policy.
        """
        return self._submit_command(CommandType.SHUTDOWN,
                                    allow_unhealthy=True)

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
        return self._read_engine_string_from_layout(self._layout)

    @staticmethod
    def _read_engine_string_from_layout(layout) -> str:
        try:
            raw = layout.engine_string
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
                # Linux publishes a bounded terminal backend reason through
                # engine_string. On Windows that field can still contain a
                # save error, so never expose it as capture evidence there.
                'capture_failure_detail': (
                    self._read_engine_string()
                    if sys.platform != 'win32'
                    and int(self._layout.capture_health_flags)
                    & int(CaptureHealthFlag.BACKEND_FAILED)
                    else ''),
            }
        except Exception as e:
            self._log_read_error_once('get_status', e)
            return {'connected': False}

    _CODEC_PREF_MAP = {'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3}

    def set_encoder_config(self, codec_pref: str, preset: int) -> bool:
        pref_int = self._CODEC_PREF_MAP.get(codec_pref.lower(), 0)
        if codec_pref.lower() not in self._CODEC_PREF_MAP:
            print(f'[CaptureBridge] Unknown codec_pref "{codec_pref}", falling back to auto')
        preset   = max(1, min(7, preset))
        def write_payload():
            self._layout.cfg_codec_pref = pref_int
            self._layout.cfg_preset = preset
            return True
        return self._submit_command(CommandType.RECONFIGURE_ENCODER, write_payload)

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
