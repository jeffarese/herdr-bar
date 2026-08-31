"""Decode terminal input bytes into key, text, paste and mouse events.

Herdr writes ordinary xterm-style sequences into the popup's pty, so this is a
standard CSI parser plus incremental UTF-8 decoding. A lone ESC is ambiguous
until the next byte arrives; ``flush()`` resolves it after the caller's timeout.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

KEY = "key"
TEXT = "text"
PASTE = "paste"
MOUSE = "mouse"

PRESS = "press"
RELEASE = "release"
WHEEL_UP = "wheel_up"
WHEEL_DOWN = "wheel_down"


class Event(NamedTuple):
    kind: str
    value: str
    x: int = 0
    y: int = 0


_CSI_FINAL = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "shift+tab",
}

_CSI_TILDE = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pgup",
    "6": "pgdn",
    "7": "home",
    "8": "end",
}

_CTRL_NAMES = {
    0x00: "ctrl+space",
    0x08: "backspace",
    0x09: "tab",
    0x0A: "ctrl+j",
    0x0D: "enter",
    0x1B: "esc",
    0x7F: "backspace",
}

_MODIFIER_PREFIX = {
    2: "shift+",
    3: "alt+",
    4: "alt+shift+",
    5: "ctrl+",
    6: "ctrl+shift+",
    7: "ctrl+alt+",
    8: "ctrl+alt+shift+",
}


class KeyDecoder(object):
    def __init__(self) -> None:
        self._buffer = b""
        self._paste: Optional[bytearray] = None

    def feed(self, data: bytes) -> List[Event]:
        self._buffer += data
        events: List[Event] = []
        while self._buffer:
            consumed, event = self._step()
            if consumed == 0:
                break
            self._buffer = self._buffer[consumed:]
            if event is not None:
                events.append(event)
        return events

    def pending_escape(self) -> bool:
        return self._buffer == b"\x1b"

    def flush(self) -> List[Event]:
        """Resolve a dangling ESC once the caller decides it stands alone."""
        if self._buffer == b"\x1b":
            self._buffer = b""
            return [Event(KEY, "esc")]
        return []

    # -- parsing ------------------------------------------------------------

    def _step(self) -> Tuple[int, Optional[Event]]:
        buffer = self._buffer
        if self._paste is not None:
            return self._step_paste()

        first = buffer[0]
        if first == 0x1B:
            if len(buffer) == 1:
                return 0, None  # ambiguous: wait for more, or flush()
            second = buffer[1]
            if second == 0x5B:  # CSI
                return self._step_csi()
            if second == 0x4F and len(buffer) >= 3:  # SS3, application cursor keys
                name = _CSI_FINAL.get(chr(buffer[2]))
                if name:
                    return 3, Event(KEY, name)
                return 3, None
            if second == 0x5D:  # OSC: a late terminal reply, never user input
                return self._step_osc()
            if second == 0x1B:
                return 1, Event(KEY, "esc")
            # ESC followed by a printable byte is alt+<key>.
            text, size = self._decode_text(buffer[1:])
            if size == 0:
                return 0, None
            if text:
                return 1 + size, Event(KEY, "alt+" + text)
            return 1 + size, None

        if first < 0x20 or first == 0x7F:
            name = _CTRL_NAMES.get(first)
            if name is None:
                name = "ctrl+" + chr(first + 0x60)
            return 1, Event(KEY, name)

        text, size = self._decode_text(buffer)
        if size == 0:
            return 0, None
        return size, Event(TEXT, text) if text else None

    def _step_csi(self) -> Tuple[int, Optional[Event]]:
        buffer = self._buffer
        index = 2
        while index < len(buffer) and 0x30 <= buffer[index] <= 0x3F:
            index += 1
        while index < len(buffer) and 0x20 <= buffer[index] <= 0x2F:
            index += 1
        if index >= len(buffer):
            return 0, None  # incomplete sequence
        final = chr(buffer[index])
        params = buffer[2:index].decode("ascii", "replace")
        length = index + 1

        if params.startswith("<") and final in ("M", "m"):
            return length, _mouse_event(params[1:], final)

        if final == "~":
            if params == "200":
                self._paste = bytearray()
                return length, None
            base, modifier = _split_params(params)
            name = _CSI_TILDE.get(base)
            if not name:
                return length, None
            return length, Event(KEY, _MODIFIER_PREFIX.get(modifier, "") + name)

        name = _CSI_FINAL.get(final)
        if not name:
            return length, None
        _, modifier = _split_params(params)
        prefix = _MODIFIER_PREFIX.get(modifier, "")
        return length, Event(KEY, prefix + name if name != "shift+tab" else name)

    def _step_osc(self) -> Tuple[int, Optional[Event]]:
        buffer = self._buffer
        bel = buffer.find(b"\x07", 2)
        st = buffer.find(b"\x1b\\", 2)
        if bel >= 0 and (st < 0 or bel < st):
            return bel + 1, None
        if st >= 0:
            return st + 2, None
        if len(buffer) > 512:
            return 2, None  # unterminated: drop the introducer and resync
        return 0, None

    def _step_paste(self) -> Tuple[int, Optional[Event]]:
        assert self._paste is not None
        buffer = self._buffer
        end = buffer.find(b"\x1b[201~")
        if end < 0:
            keep = max(0, len(buffer) - 5)  # a split terminator may still arrive
            self._paste.extend(buffer[:keep])
            return keep, None
        self._paste.extend(buffer[:end])
        text = bytes(self._paste).decode("utf-8", "replace")
        self._paste = None
        return end + 6, Event(PASTE, text)

    @staticmethod
    def _decode_text(data: bytes) -> Tuple[str, int]:
        first = data[0]
        if first < 0x80:
            size = 1
        elif first >= 0xF0:
            size = 4
        elif first >= 0xE0:
            size = 3
        elif first >= 0xC0:
            size = 2
        else:
            return "", 1  # stray continuation byte
        if len(data) < size:
            return "", 0
        try:
            return data[:size].decode("utf-8"), size
        except UnicodeDecodeError:
            return "", 1


def _split_params(params: str) -> Tuple[str, int]:
    parts = params.split(";")
    base = parts[0] or "1"
    modifier = 0
    if len(parts) > 1 and parts[1].isdigit():
        modifier = int(parts[1])
    return base, modifier


def _mouse_event(params: str, final: str) -> Optional[Event]:
    fields = params.split(";")
    if len(fields) != 3:
        return None
    try:
        button = int(fields[0])
        column = int(fields[1])
        row = int(fields[2])
    except ValueError:
        return None
    if button & 64:
        return Event(MOUSE, WHEEL_UP if button & 1 == 0 else WHEEL_DOWN, column, row)
    if button & 32:
        return None  # drag/motion
    if final == "m":
        return Event(MOUSE, RELEASE, column, row)
    if button & 3 == 0:
        return Event(MOUSE, PRESS, column, row)
    return None
