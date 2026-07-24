"""Raw terminal plumbing: alt screen, raw mode, size, input reads."""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
from typing import List, Optional, Tuple

ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
WRAP_OFF = "\x1b[?7l"
WRAP_ON = "\x1b[?7h"
PASTE_ON = "\x1b[?2004h"
PASTE_OFF = "\x1b[?2004l"
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

_OSC_RGB = re.compile(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")


class Terminal(object):
    def __init__(self, mouse: bool = True) -> None:
        self.in_fd = sys.stdin.fileno()
        self.out = sys.stdout
        self.mouse = mouse
        self._saved: Optional[List] = None
        self._resized = False
        self._previous_winch = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "Terminal":
        self._saved = termios.tcgetattr(self.in_fd)
        tty.setraw(self.in_fd)
        sequence = ALT_SCREEN_ON + CURSOR_HIDE + WRAP_OFF + PASTE_ON
        if self.mouse:
            sequence += MOUSE_ON
        self.write(sequence)
        self.flush()
        try:
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            self._previous_winch = None
        return self

    def __exit__(self, *exc_info) -> None:
        sequence = PASTE_OFF + WRAP_ON + CURSOR_SHOW + ALT_SCREEN_OFF + "\x1b[0m"
        if self.mouse:
            sequence = MOUSE_OFF + sequence
        try:
            self.write(sequence)
            self.flush()
        except (OSError, ValueError):
            pass
        if self._saved is not None:
            try:
                termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self._saved)
            except termios.error:
                pass
        if self._previous_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._previous_winch)
            except (ValueError, OSError):  # pragma: no cover
                pass

    def _on_winch(self, *_args) -> None:
        self._resized = True

    def take_resize(self) -> bool:
        resized, self._resized = self._resized, False
        return resized

    # -- io -----------------------------------------------------------------

    def size(self) -> Tuple[int, int]:
        try:
            columns, rows = os.get_terminal_size(self.in_fd)
        except OSError:
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
        try:
            readable, _, _ = select.select([self.in_fd], [], [], timeout)
        except OSError:  # select.error is an alias of OSError on Python 3
            return False
        return bool(readable)

    def read(self) -> bytes:
        try:
            return os.read(self.in_fd, 4096)
        except OSError:
            return b""


def detect_appearance(timeout: float = 0.12) -> Optional[str]:
    """Ask the terminal for its background color via OSC 11.

    Returns "dark", "light", or None when the terminal stays quiet. Must run
    before raw-mode input handling starts so the reply cannot be mistaken for
    typing.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b]11;?\x07")
        sys.stdout.flush()
        buffer = b""
        deadline_parts = 0
        while deadline_parts < 4:
            readable, _, _ = select.select([fd], [], [], timeout)
            if not readable:
                break
            buffer += os.read(fd, 256)
            if b"\x07" in buffer or b"\x1b\\" in buffer:
                break
            deadline_parts += 1
    except (OSError, termios.error):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except termios.error:
            pass

    found = _OSC_RGB.search(buffer)
    if not found:
        return None
    channels = []
    for group in found.groups():
        text = group.decode("ascii", "replace")
        value = int(text, 16)
        channels.append(value / float((1 << (4 * len(text))) - 1))
    red, green, blue = channels
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "dark" if luminance < 0.5 else "light"
