"""Raw terminal plumbing on a Windows console: the surface of ``term_posix``.

Under Herdr on Windows the popup owns a ConPTY console rather than a pty.
Raw mode is a console mode flag, and ``ENABLE_VIRTUAL_TERMINAL_INPUT`` has
the console deliver keys, and the mouse once the bar asks for it, as the
xterm sequences the key decoder already understands. There is no SIGWINCH,
so the size is polled. ``select`` only knows sockets here, so one thread
blocks in ``ReadConsoleW`` and the main loop waits on a queue.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import queue
import shutil
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional, Tuple

from .sequences import (
    APPEARANCE_QUERY,
    appearance_from_reply,
    enter_sequence,
    exit_sequence,
    reply_complete,
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.ReadConsoleW.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID,
]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

STD_INPUT_HANDLE = 0xFFFFFFF6  # (DWORD)-10
STD_OUTPUT_HANDLE = 0xFFFFFFF5  # (DWORD)-11

ENABLE_PROCESSED_INPUT = 0x0001  # ctrl+c as a signal rather than a byte
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_QUICK_EDIT_MODE = 0x0040  # drag-select eats the mouse unless this is off
ENABLE_EXTENDED_FLAGS = 0x0080  # required to change QUICK_EDIT at all
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
DISABLE_NEWLINE_AUTO_RETURN = 0x0008

THREAD_TERMINATE = 0x0001
READ_CHARS = 4096
SIZE_POLL = 0.1


def _mode(handle) -> Optional[int]:
    mode = wintypes.DWORD(0)
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    return mode.value


def _set_mode(handle, mode: Optional[int]) -> None:
    if mode is not None:
        kernel32.SetConsoleMode(handle, mode)


def raw_input_mode(mode: int, mouse: bool) -> int:
    mode &= ~(ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT
              | ENABLE_QUICK_EDIT_MODE | ENABLE_MOUSE_INPUT)
    mode |= ENABLE_EXTENDED_FLAGS | ENABLE_VIRTUAL_TERMINAL_INPUT
    if mouse:
        mode |= ENABLE_MOUSE_INPUT
    return mode


def vt_output_mode(mode: int) -> int:
    return (mode | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | DISABLE_NEWLINE_AUTO_RETURN)


class ConsoleModes(object):
    """The console's own modes: captured once, restored once.

    The appearance query switches to raw input before the bar's Terminal
    does, and the console stays raw from then on. Going back to line input
    in between would let a key pressed in that gap start a line-mode read in
    the reader thread, and only Enter would finish it. Whoever leaves first
    -- the Terminal, or the interpreter on its way out -- puts the original
    modes back.
    """

    def __init__(self) -> None:
        self.in_handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        self.out_handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        self.saved: Optional[Tuple[Optional[int], Optional[int]]] = None

    def is_console(self) -> bool:
        return _mode(self.in_handle) is not None and _mode(self.out_handle) is not None

    def raw(self, mouse: bool) -> None:
        if self.saved is None:
            self.saved = (_mode(self.in_handle), _mode(self.out_handle))
            atexit.register(self.restore)
        saved_in, saved_out = self.saved
        if saved_in is not None:
            _set_mode(self.in_handle, raw_input_mode(saved_in, mouse))
        if saved_out is not None:
            _set_mode(self.out_handle, vt_output_mode(saved_out))

    def restore(self) -> None:
        if self.saved is None:
            return
        saved_in, saved_out = self.saved
        _set_mode(self.in_handle, saved_in)
        _set_mode(self.out_handle, saved_out)
        self.saved = None


class ConsoleReader(object):
    """One thread reading the console for the life of the process.

    ``ReadConsoleW`` blocks without a timeout, so it runs on a daemon thread
    that the appearance probe and the bar share: two readers would compete
    for the same keystrokes. Bytes typed before the bar is ready are kept in
    ``pending`` rather than dropped.
    """

    def __init__(self, handle) -> None:
        self.handle = handle
        self.queue: "queue.Queue[bytes]" = queue.Queue()
        self.pending = b""
        self.closed = False
        self._thread: Optional[threading.Thread] = None
        self._high_surrogate = ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="herdr-bar-console", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        buffer = ctypes.create_unicode_buffer(READ_CHARS)
        count = wintypes.DWORD(0)
        while True:
            ok = kernel32.ReadConsoleW(self.handle, buffer, READ_CHARS, ctypes.byref(count), None)
            if not ok:
                self.queue.put(b"")
                return
            # buffer[:n] keeps NUL (ctrl+space); buffer.value would stop there.
            text = self._high_surrogate + buffer[:count.value]
            self._high_surrogate = ""
            # A character outside the BMP is two UTF-16 units that one read
            # may split. Hold a dangling high surrogate for the next read, and
            # let a UTF-16 round trip join the halves back into one character.
            if text and "\ud800" <= text[-1] <= "\udbff":
                self._high_surrogate, text = text[-1], text[:-1]
            if text:
                text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                self.queue.put(text.encode("utf-8"))

    def wait(self, timeout: float) -> bool:
        """Whether input is waiting; blocks for up to ``timeout`` seconds."""
        if self.pending or self.closed:
            return True
        try:
            data = self.queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return False
        if not data:
            self.closed = True
        self.pending += data
        return True

    def read(self) -> bytes:
        """Everything that has arrived; empty once the console is gone."""
        data, self.pending = self.pending, b""
        while True:
            try:
                chunk = self.queue.get_nowait()
            except queue.Empty:
                return data
            if not chunk:
                self.closed = True
                return data
            data += chunk

    def cancel(self) -> None:
        """Abort the blocked read so the console can go back to its owner."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        handle = kernel32.OpenThread(THREAD_TERMINATE, False, thread.native_id)
        if handle:
            kernel32.CancelSynchronousIo(handle)
            kernel32.CloseHandle(handle)


_reader: Optional[ConsoleReader] = None
_modes: Optional[ConsoleModes] = None


def console_reader() -> ConsoleReader:
    global _reader
    if _reader is None:
        _reader = ConsoleReader(kernel32.GetStdHandle(STD_INPUT_HANDLE))
    return _reader


def console_modes() -> ConsoleModes:
    global _modes
    if _modes is None:
        _modes = ConsoleModes()
    return _modes


class Terminal(object):
    def __init__(self, mouse: bool = True) -> None:
        self.out = sys.stdout
        self.mouse = mouse
        self._reader = console_reader()
        self._modes = console_modes()
        self._known: Optional[Tuple[int, int]] = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "Terminal":
        self._modes.raw(self.mouse)
        self.write(enter_sequence(self.mouse))
        self.flush()
        self._known = self.size()
        self._reader.start()
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self.write(exit_sequence(self.mouse))
            self.flush()
        except (OSError, ValueError):
            pass
        self._reader.cancel()
        self._modes.restore()

    def take_resize(self) -> bool:
        current = self.size()
        if current == self._known:
            return False
        self._known = current
        return True

    # -- io -----------------------------------------------------------------

    def size(self) -> Tuple[int, int]:
        # The input handle has no screen buffer; measure the output's window.
        try:
            columns, rows = os.get_terminal_size(sys.stdout.fileno())
        except (OSError, ValueError):
            columns, rows = shutil.get_terminal_size((80, 24))
        return max(1, columns), max(1, rows)

    def write(self, text: str) -> None:
        self.out.write(text)

    def flush(self) -> None:
        try:
            self.out.flush()
        except (BrokenPipeError, ValueError):
            pass

    def wait(self, timeout: Optional[float]) -> bool:
        """True when input is ready; False on timeout or when the size changed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if self._reader.wait(SIZE_POLL if remaining is None else min(SIZE_POLL, remaining)):
                return True
            if self.size() != self._known:
                return False  # the caller's take_resize() sees the new size
            if remaining is not None and remaining <= SIZE_POLL:
                return False

    def read(self) -> bytes:
        return self._reader.read()


def detect_appearance(timeout: float = 0.12) -> Optional[str]:
    """Ask the terminal for its background color via OSC 11.

    Returns "dark", "light", or None when nothing answers within ``timeout``.
    Keys typed meanwhile stay queued for the bar, and the console stays in
    raw mode for it; see ``ConsoleModes``.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    modes = console_modes()
    if not modes.is_console():
        return None
    reader = console_reader()
    modes.raw(mouse=False)
    buffer = b""
    try:
        reader.start()
        sys.stdout.write(APPEARANCE_QUERY)
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not reply_complete(buffer):
            if not reader.wait(deadline - time.monotonic()) or reader.closed:
                break
            buffer += reader.read()
    except (OSError, ValueError):
        return None
    # Whatever is not the reply was typed: hand it back for the bar to read.
    reader.pending = strip_reply(buffer) + reader.pending
    return appearance_from_reply(buffer)


def strip_reply(buffer: bytes) -> bytes:
    """``buffer`` without the OSC reply in it, so only typed bytes remain."""
    start = buffer.find(b"\x1b]")
    if start < 0:
        return buffer
    ends = [(buffer.find(terminator, start), len(terminator))
            for terminator in (b"\x07", b"\x1b\\")]
    ends = [(end, width) for end, width in ends if end >= 0]
    if not ends:
        return buffer[:start]  # cut short: nothing typed after it has arrived yet
    end, width = min(ends)
    return buffer[:start] + buffer[end + width:]
