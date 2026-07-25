"""Send only what changed.

The bar composes whole rows. That is the simple thing to do and it keeps the
layout code honest, but a terminal already showing most of those rows does not
need to be told about them again -- and over ssh, or through a multiplexer,
the repeated bytes are the whole latency budget. So this module sits between
the composed rows and the terminal: it models the screen it has painted, and
emits the shortest sequence that turns that screen into the next one.

The model is deliberately small. The bar draws with absolute cursor
addressing, SGR renditions and erase-to-end-of-line, and never scrolls or
wraps, so nothing here has to understand more than that.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .textutil import char_width

# One cell's rendition: (fg, bg, bold, underline, reverse), where the colors
# are the SGR parameters that select them ("38;5;244") or None for default.
Style = Tuple[Optional[str], Optional[str], bool, bool, bool]
Cell = Tuple[str, Style]

DEFAULT_STYLE: Style = (None, None, False, False, False)
BLANK: Cell = (" ", DEFAULT_STYLE)

# Roughly what "\x1b[12;34H" costs. A stretch of unchanged cells shorter than
# a cursor jump is cheaper to overwrite than to skip.
JUMP_COST = 9


def _apply(params: str, style: Style) -> Style:
    """Apply one SGR sequence's parameters to a rendition."""
    fg, bg, bold, underline, reverse = style
    parts = params.split(";") if params else ["0"]
    index = 0
    while index < len(parts):
        part = parts[index] or "0"
        if part in ("38", "48"):
            # Extended color: 5;n for the 256 palette, 2;r;g;b for truecolor.
            mode = parts[index + 1] if index + 1 < len(parts) else ""
            length = 3 if mode == "2" else 1
            value = ";".join(parts[index : index + 2 + length])
            if part == "38":
                fg = value
            else:
                bg = value
            index += 2 + length
            continue
        if part == "0":
            fg = bg = None
            bold = underline = reverse = False
        elif part == "1":
            bold = True
        elif part == "4":
            underline = True
        elif part == "7":
            reverse = True
        elif part == "22":
            bold = False
        elif part == "24":
            underline = False
        elif part == "27":
            reverse = False
        elif part == "39":
            fg = None
        elif part == "49":
            bg = None
        index += 1
    return (fg, bg, bold, underline, reverse)


# A row carries a couple of dozen SGR sequences and the bar only ever writes a
# handful of distinct ones, so the same few transitions are re-parsed on every
# frame. The result depends on nothing but the parameters and the rendition
# they start from.
_TRANSITIONS: Dict[Tuple[str, Style], Style] = {}
_TRANSITIONS_LIMIT = 4096


def _sgr(params: str, style: Style) -> Style:
    if not params or params == "0":
        return DEFAULT_STYLE  # by far the most common, and it has no history
    key = (params, style)
    found = _TRANSITIONS.get(key)
    if found is None:
        found = _apply(params, style)
        if len(_TRANSITIONS) >= _TRANSITIONS_LIMIT:
            _TRANSITIONS.clear()
        _TRANSITIONS[key] = found
    return found


def encode_style(current: Style, target: Style) -> str:
    """The shortest SGR that moves a terminal from one rendition to another.

    Turning an attribute off costs a reset and a rebuild, so the additive
    case -- which is most of them, within a row -- stays cheap.
    """
    if current == target:
        return ""
    turning_off = (
        (current[2] and not target[2])
        or (current[3] and not target[3])
        or (current[4] and not target[4])
        or (current[0] is not None and target[0] != current[0])
        or (current[1] is not None and target[1] != current[1])
    )
    out: List[str] = []
    if turning_off:
        if target == DEFAULT_STYLE:
            return "\x1b[0m"
        out.append("0")
        base = DEFAULT_STYLE
    else:
        base = current
    if target[2] and not base[2]:
        out.append("1")
    if target[3] and not base[3]:
        out.append("4")
    if target[4] and not base[4]:
        out.append("7")
    if target[0] and target[0] != base[0]:
        out.append(target[0])
    if target[1] and target[1] != base[1]:
        out.append(target[1])
    return "\x1b[" + ";".join(out) + "m" if out else ""


def cells_of(text: str, width: int) -> Tuple[Tuple[Cell, ...], int]:
    """Lay one composed row out into cells, and say where its ink ends.

    The second value is the index of the last cell holding anything, or -1 for
    an empty row: that is what tells the painter it can erase a tail instead of
    spending a space on every column of it.
    """
    row: List[Cell] = [BLANK] * width
    style = DEFAULT_STYLE
    x = 0
    # Escape sequences are the only thing that is not literal text, so split on
    # them once and walk the runs between: a row is mostly one long run, and
    # laying a run of plain ASCII down is a slice assignment rather than a
    # width lookup per character.
    leading = True
    for part in text.split("\x1b"):
        if leading:
            leading = False
            literal = part
        elif part[:1] == "[":
            end = 1
            length = len(part)
            while end < length and not ("\x40" <= part[end] <= "\x7e"):
                end += 1
            if end >= length:
                break  # truncated: nothing after it can be placed either
            if part[end] == "m":
                style = _sgr(part[1:end], style)
            literal = part[end + 1 :]
        else:
            literal = part[1:]  # a two-character escape

        if not literal or x >= width:
            continue
        if literal.isascii() and literal.isprintable():
            take = width - x
            if take > len(literal):
                take = len(literal)
            row[x : x + take] = [(char, style) for char in literal[:take]]
            x += take
            continue
        for char in literal:
            if x >= width:
                break
            step = char_width(char)
            if step == 0:
                continue
            row[x] = (char, style)
            if step == 2 and x + 1 < width:
                # The trailing half of a wide glyph holds no character of its
                # own, but it is covered -- and still has to compare equal.
                row[x + 1] = ("", style)
            x += step
    ink = min(x, width) - 1
    while ink >= 0 and row[ink] == BLANK:
        # A space in the default rendition is indistinguishable from an empty
        # cell, and rows pad themselves with plenty of both.
        ink -= 1
    return tuple(row), ink


def compose(rows: Sequence[str]) -> str:
    """Position and paint every row, clearing the rest of each line.

    The whole screen, unconditionally. Used for the first frame of a session
    and after anything that invalidates the painter's model of the screen.
    """
    # Self-contained: whatever rendition the terminal was left in, this frame
    # does not inherit it -- \x1b[J would otherwise erase in a stale colour.
    out: List[str] = ["\x1b[0m\x1b[H"]
    for index, row in enumerate(rows):
        out.append("\x1b[%d;1H" % (index + 1))
        out.append(row)
        out.append("\x1b[0m\x1b[K")
    out.append("\x1b[J")
    return "".join(out)


def _runs(old: Sequence[Cell], new: Sequence[Cell], first: int, stop: int) -> List[Tuple[int, int]]:
    """Split a changed span into the stretches worth rewriting.

    Two edits at opposite ends of a row are two small writes with a jump
    between them, not one write across everything in between -- but only once
    the gap is wider than the jump that skips it.
    """
    runs: List[Tuple[int, int]] = []
    start = first
    gap = 0
    for x in range(first, stop + 1):
        if old[x] == new[x]:
            gap += 1
            continue
        if gap > JUMP_COST:
            runs.append((start, x - gap - 1))
            start = x
        gap = 0
    runs.append((start, stop))
    return runs


class Painter(object):
    """Holds the screen the bar last painted, and prices every frame against it."""

    def __init__(self) -> None:
        self._width = 0
        self._height = 0
        self._grid: List[Tuple[Cell, ...]] = []
        self._style: Style = DEFAULT_STYLE
        self._parsed: Dict[str, Tuple[Tuple[Cell, ...], int]] = {}
        self._fresh = True

    def reset(self) -> None:
        """Forget the screen: the next frame repaints all of it."""
        self._fresh = True

    def screen(self) -> List[Tuple[Cell, ...]]:
        """The screen as the painter believes it now stands."""
        return list(self._grid)

    def frame(self, rows: Sequence[str], width: int, height: int) -> str:
        """The bytes that turn the painted screen into these rows.

        Empty when nothing moved -- a frame with nothing to say costs nothing,
        which is the point.
        """
        rows = list(rows)[:height]
        if self._fresh or width != self._width or height != self._height:
            return self._repaint(rows, width, height)

        previous, self._parsed = self._parsed, {}
        grid: List[Tuple[Cell, ...]] = []
        out: List[str] = []
        style = self._style
        for y in range(height):
            source = rows[y] if y < len(rows) else ""
            entry = self._parsed.get(source)
            if entry is None:
                # Rows the bar recomposed byte-for-byte need no second look:
                # most of the screen, most of the time.
                entry = previous.get(source) or cells_of(source, width)
                self._parsed[source] = entry
            cells, ink = entry
            grid.append(cells)
            painted = self._grid[y]
            if cells is painted or cells == painted:
                continue
            patch, style = self._row(y, painted, cells, ink, width, style)
            out.append(patch)
        self._grid = grid
        self._style = style
        return "".join(out)

    def _repaint(self, rows: List[str], width: int, height: int) -> str:
        self._width = width
        self._height = height
        self._parsed = {}
        grid: List[Tuple[Cell, ...]] = []
        for y in range(height):
            source = rows[y] if y < len(rows) else ""
            entry = self._parsed.get(source)
            if entry is None:
                entry = cells_of(source, width)
                self._parsed[source] = entry
            grid.append(entry[0])
        self._grid = grid
        # compose() ends every row with a reset, and erases what is below.
        self._style = DEFAULT_STYLE
        self._fresh = False
        return compose(rows)

    def _row(
        self,
        y: int,
        old: Tuple[Cell, ...],
        new: Tuple[Cell, ...],
        ink: int,
        width: int,
        style: Style,
    ) -> Tuple[str, Style]:
        first = 0
        while old[first] == new[first]:
            first += 1  # the rows differ, so this always stops inside the row
        stop = width - 1
        while stop > first and old[stop] == new[stop]:
            stop -= 1
        while first > 0 and new[first][0] == "":
            first -= 1  # never start mid-glyph

        # Everything past the row's ink is blank, and erasing a tail is three
        # bytes however long it is.
        erase = ink < stop
        write_stop = min(stop, ink)

        out: List[str] = []
        cursor = -1
        if write_stop >= first:
            for start, end in _runs(old, new, first, write_stop):
                while start > 0 and new[start][0] == "":
                    start -= 1
                while end + 1 < width and new[end + 1][0] == "":
                    end += 1  # never stop mid-glyph either
                if start != cursor:
                    out.append("\x1b[%d;%dH" % (y + 1, start + 1))
                for x in range(start, end + 1):
                    char, cell = new[x]
                    if char == "":
                        continue
                    move = encode_style(style, cell)
                    if move:
                        out.append(move)
                        style = cell
                    out.append(char)
                cursor = end + 1
        if erase:
            if cursor != ink + 1:
                out.append("\x1b[%d;%dH" % (y + 1, ink + 2))
            if style[1] is not None or style[4]:
                # Erasing paints with the current background, so a selection
                # colour left in the rendition would smear down the row.
                out.append("\x1b[0m")
                style = DEFAULT_STYLE
            out.append("\x1b[K")
        return "".join(out), style
