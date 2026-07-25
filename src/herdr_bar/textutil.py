"""Terminal-safe text helpers: display width, truncation, sanitising."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Sequence, Tuple

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_WIDE = ("W", "F")

# Measuring a character means two unicodedata lookups, and a frame measures
# tens of thousands of them -- nearly all drawn from the same small alphabet.
# The answer never changes, so it is only ever worked out once.
_WIDTHS: Dict[str, int] = {}


def _measure(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    code = ord(char)
    if code < 32 or code == 127:
        return 0
    if unicodedata.east_asian_width(char) in _WIDE:
        return 2
    return 1


def char_width(char: str) -> int:
    width = _WIDTHS.get(char)
    if width is None:
        width = _measure(char)
        _WIDTHS[char] = width
    return width


def display_width(text: str) -> int:
    if text.isascii() and text.isprintable():
        return len(text)  # the overwhelmingly common case, answered in C
    widths = _WIDTHS
    total = 0
    for char in text:
        width = widths.get(char)
        if width is None:
            width = _measure(char)
            widths[char] = width
        total += width
    return total


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def sanitize(text: str, replacement: str = " ") -> str:
    """Drop control characters so foreign text can never break the layout."""
    out: List[str] = []
    for char in strip_ansi(text):
        if char == "\t":
            out.append("    ")
        elif char < " " or char == "\x7f":
            out.append(replacement)
        else:
            out.append(char)
    return "".join(out)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Cut text to a display width, appending an ellipsis when it was cut."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width:
        return ellipsis[:width]
    budget = width - ellipsis_width
    out: List[str] = []
    used = 0
    for char in text:
        step = char_width(char)
        if used + step > budget:
            break
        out.append(char)
        used += step
    return "".join(out) + ellipsis


def truncate_middle(text: str, width: int, ellipsis: str = "…") -> str:
    """Keep both ends of a path-like string when it does not fit."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width + 2:
        return truncate(text, width, ellipsis)
    tail_budget = (width - ellipsis_width) // 2
    head_budget = width - ellipsis_width - tail_budget

    head: List[str] = []
    used = 0
    for char in text:
        step = char_width(char)
        if used + step > head_budget:
            break
        head.append(char)
        used += step

    tail: List[str] = []
    used = 0
    for char in reversed(text):
        step = char_width(char)
        if used + step > tail_budget:
            break
        tail.append(char)
        used += step
    tail.reverse()
    return "".join(head) + ellipsis + "".join(tail)


def visible_width(text: str) -> int:
    """Display width of already-styled text, ignoring escape sequences."""
    return display_width(strip_ansi(text))


def pad(text: str, width: int) -> str:
    missing = width - visible_width(text)
    return text + " " * missing if missing > 0 else text


def window_positions(
    text: str, width: int, positions: Sequence[int]
) -> Tuple[str, List[int], int]:
    """Truncate text to width while keeping highlighted characters visible.

    Returns the visible text, the highlight offsets rebased onto it, and the
    number of characters trimmed from the start.
    """
    if display_width(text) <= width or width <= 1:
        return truncate(text, width), list(positions), 0
    if not positions or max(positions) < width - 1:
        visible = truncate(text, width)
        limit = len(visible)
        return visible, [p for p in positions if p < limit], 0

    # Slide the window so the last highlighted character stays in view.
    last = max(positions)
    start = 0
    while display_width(text[start : last + 1]) > width - 2 and start < last:
        start += 1
    head = "…" if start else ""
    body = truncate(text[start:], width - display_width(head))
    shifted = [p - start + len(head) for p in positions if p >= start]
    limit = len(head) + len(body)
    return head + body, [p for p in shifted if p < limit], start
