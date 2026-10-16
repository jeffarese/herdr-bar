"""Raw terminal plumbing on a pty: alt screen, raw mode, size, input reads."""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import termios
import tty
from typing import List, Optional, Tuple

from .sequences import (
    APPEARANCE_QUERY,
    appearance_from_reply,
    enter_sequence,
    exit_sequence,
    reply_complete,
)


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
        self.write(enter_sequence(self.mouse))
        self.flush()
        try:
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            self._previous_winch = None
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self.write(exit_sequence(self.mouse))
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
        sys.stdout.write(APPEARANCE_QUERY)
        sys.stdout.flush()
        buffer = b""
        deadline_parts = 0
        while deadline_parts < 4:
            readable, _, _ = select.select([fd], [], [], timeout)
            if not readable:
                break
            buffer += os.read(fd, 256)
            if reply_complete(buffer):
                break
            deadline_parts += 1
    except (OSError, termios.error):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except termios.error:
            pass
    return appearance_from_reply(buffer)
