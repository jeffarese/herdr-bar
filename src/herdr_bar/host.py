"""Host operating system differences.

Herdr runs on Linux, macOS and Windows. Everything the bar does differently
per platform lives here, so the modules above read the same everywhere:

* exclusive file locks (flock on POSIX, a byte-range lock on Windows),
* how the detached title worker is started,
* the Windows named pipe behind ``HERDR_SOCKET_PATH``,
* how old a process is.

The terminal itself is the one exception: it has a backend per platform
under ``term``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import IO, Any, Dict, Optional

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    def _declare(name, restype, *argtypes):
        function = getattr(_kernel32, name)
        function.restype = restype
        function.argtypes = list(argtypes)

    _LPOVERLAPPED = ctypes.POINTER(_Overlapped)
    _declare("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _declare("GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, *[wintypes.LPFILETIME] * 4)
    _declare("GetSystemTimeAsFileTime", None, wintypes.LPFILETIME)
    _declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _declare("CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _declare("WaitNamedPipeW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD)
    _declare("CreateEventW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
             wintypes.LPCWSTR)
    _declare("ReadFile", wintypes.BOOL, wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
             wintypes.LPDWORD, _LPOVERLAPPED)
    _declare("WriteFile", wintypes.BOOL, wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
             wintypes.LPDWORD, _LPOVERLAPPED)
    _declare("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _declare("GetOverlappedResult", wintypes.BOOL, wintypes.HANDLE, _LPOVERLAPPED,
             wintypes.LPDWORD, wintypes.BOOL)
    _declare("CancelIoEx", wintypes.BOOL, wintypes.HANDLE, _LPOVERLAPPED)

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
else:
    import fcntl

# -- locks -------------------------------------------------------------------


def lock(handle: IO, blocking: bool = False) -> None:
    """Take an exclusive lock on an open file; ``BlockingIOError`` when busy.

    The lock lasts as long as the handle: closing the file releases it, so a
    caller keeps its ``with open(...)`` block open for as long as the lock
    matters. POSIX uses flock(2). Windows locks the first byte of the file,
    which conflicts between processes and between two handles of one process
    just like flock does.
    """
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    fd = handle.fileno()
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)  # the lock covers bytes from the current offset
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not blocking:
                raise BlockingIOError(error.errno, str(error)) from error
            time.sleep(0.05)


def unlock(handle: IO) -> None:
    """Release a lock before the handle is closed."""
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    fd = handle.fileno()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# -- the title worker --------------------------------------------------------


def detached_process_options(lock_handle: IO) -> Dict[str, Any]:
    """Popen keywords for a worker that outlives the popup starting it.

    On POSIX the worker inherits the locked descriptor, so the lock follows
    the file description into the child and no startup hook can mistake the
    handoff for a stopped worker. Windows has no fork and cannot pass a locked
    handle through CreateProcess, so the worker claims the lock file by name
    as its first act instead; see ``title_watcher.watch``.
    """
    if WINDOWS:
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    return {"start_new_session": True, "pass_fds": (lock_handle.fileno(),)}


# -- the socket --------------------------------------------------------------

PIPE_PREFIX = "\\\\.\\pipe\\"
# How long a pipe may have no listening instance before the server counts as
# gone: Herdr opens the next instance right after accepting a client.
PIPE_RELISTEN_GRACE = 0.25

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
# Let the server identify this client, never act as it.
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_MORE_DATA = 234
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0


def pipe_name(socket_path: str) -> str:
    r"""The named pipe behind a Windows socket path.

    Herdr serves its Windows API endpoint in the pipe namespace under the
    socket path string itself: ``C:\Users\me\AppData\Roaming\herdr\herdr.sock``
    is reached at ``\\.\pipe\C:\Users\me\AppData\Roaming\herdr\herdr.sock``.
    The file at the path is only a marker that says which server owns it.
    """
    if socket_path.startswith(PIPE_PREFIX):
        return socket_path
    return PIPE_PREFIX + socket_path


def pipe_request(name: str, payload: bytes, timeout: float) -> bytes:
    """One request over a named pipe; the first reply line, without newline.

    Herdr answers one newline-delimited JSON line per connection, like the
    Unix socket. Every step shares one deadline, as a socket timeout bounds
    each call: the pipe is opened for overlapped I/O, because a plain read
    would wait forever on a server that stopped answering.
    """
    deadline = time.monotonic() + timeout
    handle = _open_pipe(name, deadline)
    try:
        outgoing = ctypes.create_string_buffer(payload, len(payload))
        _transfer(handle, _kernel32.WriteFile, outgoing, len(payload), deadline)
        incoming = ctypes.create_string_buffer(65536)
        chunks = []
        buffered = b""
        while b"\n" not in buffered:
            count = _transfer(handle, _kernel32.ReadFile, incoming, len(incoming), deadline)
            if not count:
                break  # the server closed its end
            chunks.append(incoming.raw[:count])
            buffered = b"".join(chunks)
    finally:
        _kernel32.CloseHandle(handle)
    return buffered.split(b"\n", 1)[0]


def _open_pipe(name: str, deadline: float):
    grace = time.monotonic() + PIPE_RELISTEN_GRACE
    flags = FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
    while True:
        handle = _kernel32.CreateFileW(
            name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, flags, None)
        if handle != INVALID_HANDLE_VALUE:
            return handle
        code = ctypes.get_last_error()
        now = time.monotonic()
        if code == ERROR_PIPE_BUSY and now < deadline:
            # Every instance is serving someone; wait for the server to offer one.
            _kernel32.WaitNamedPipeW(name, max(1, int((deadline - now) * 1000)))
        elif code == ERROR_FILE_NOT_FOUND and now < min(deadline, grace):
            # Between two instances, or gone for good: only a short wait tells.
            time.sleep(0.01)
        else:
            raise ctypes.WinError(code)


def _transfer(handle, operation, buffer, size: int, deadline: float) -> int:
    """Run one overlapped ReadFile or WriteFile; bytes moved, 0 once the pipe closed."""
    event = _kernel32.CreateEventW(None, True, False, None)
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        overlapped = _Overlapped()
        overlapped.hEvent = event
        done = wintypes.DWORD(0)
        if not operation(handle, buffer, size, None, ctypes.byref(overlapped)):
            code = ctypes.get_last_error()
            if code == ERROR_BROKEN_PIPE:
                return 0
            if code != ERROR_IO_PENDING:
                raise ctypes.WinError(code)
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if _kernel32.WaitForSingleObject(event, remaining) != WAIT_OBJECT_0:
                _kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
                # The kernel owns the buffer until the cancelled call lands.
                _kernel32.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(done), True)
                raise TimeoutError("timed out waiting for herdr")
        if not _kernel32.GetOverlappedResult(
                handle, ctypes.byref(overlapped), ctypes.byref(done), False):
            code = ctypes.get_last_error()
            if code == ERROR_BROKEN_PIPE:
                return 0
            if code != ERROR_MORE_DATA:  # a message-mode pipe hands it over in parts
                raise ctypes.WinError(code)
        return done.value
    finally:
        _kernel32.CloseHandle(event)


# -- process age -------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _filetime(value: "wintypes.FILETIME") -> int:
    return value.dwLowDateTime | (value.dwHighDateTime << 32)


def process_age(pid: int) -> Optional[float]:
    """Seconds since ``pid`` started, or None if Windows will not say.

    The kernel keeps the creation time of every process; no ``ps`` needed.
    """
    if not WINDOWS:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = _kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        )
    finally:
        _kernel32.CloseHandle(handle)
    if not ok:
        return None
    now = wintypes.FILETIME()
    _kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    # FILETIME counts 100-nanosecond intervals.
    return max(0.0, (_filetime(now) - _filetime(created)) / 1e7)
