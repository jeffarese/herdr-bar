"""Escape sequences both terminal backends send, and the OSC 11 reply parser."""

from __future__ import annotations

import re
from typing import Optional

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

# "What is your background color?" The reply names it as rgb:RRRR/GGGG/BBBB.
APPEARANCE_QUERY = "\x1b]11;?\x07"

_OSC_RGB = re.compile(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")


def enter_sequence(mouse: bool) -> str:
    sequence = ALT_SCREEN_ON + CURSOR_HIDE + WRAP_OFF + PASTE_ON
    if mouse:
        sequence += MOUSE_ON
    return sequence


def exit_sequence(mouse: bool) -> str:
    sequence = PASTE_OFF + WRAP_ON + CURSOR_SHOW + ALT_SCREEN_OFF + "\x1b[0m"
    if mouse:
        sequence = MOUSE_OFF + sequence
    return sequence


def reply_complete(buffer: bytes) -> bool:
    """Whether an OSC reply has been terminated, by BEL or by ST."""
    return b"\x07" in buffer or b"\x1b\\" in buffer


def appearance_from_reply(buffer: bytes) -> Optional[str]:
    """Read "dark" or "light" out of an OSC 11 reply; None when there is none."""
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
