"""Paint the bar.

Everything is drawn as full rows into the popup's inner area: Herdr already
draws the popup border, so the bar stays borderless and full-bleed. Rows
are composed as (role, text) segments, measured in display columns, and only
then turned into escape sequences -- which keeps truncation honest for CJK and
emoji, and makes the layout testable by stripping the colors back out.
"""

from __future__ import annotations

import os
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from .age import format_age
from .items import KIND_SPACE, Item
from .textutil import display_width, pad, truncate, truncate_middle, window_positions
from .theme import BOLD, RESET, UNDERLINE, Theme

PROMPT = "❯"
SELECTED_BAR = "▌"
SCROLL_TRACK = "│"
SCROLL_THUMB = "┃"
DIVIDER = "│"

STATUS_WORDS = {
    "blocked": "needs you",
    "working": "working",
    "done": "done",
    "idle": "idle",
    "unknown": "",
    "none": "",
}

DETAIL_SEP = " — "
MIN_DETAIL = 6
MIN_TITLE = 16
META_SHARE = 0.45
MIN_LIST = 46
PREVIEW_MIN = 28
PREVIEW_MAX = 56


class Row(NamedTuple):
    """An item plus where the query landed in it.

    ``positions`` is the title, kept separate because that is the one field
    every row draws. ``field_positions`` maps a searchable field index (see
    Item.fields) to its matched characters, so the summary and the meta
    chunks can light up the same way wherever they are drawn.
    """

    item: Item
    positions: Tuple[int, ...]
    field_positions: Mapping[int, Tuple[int, ...]] = {}


class Layout(NamedTuple):
    list_top: int
    list_height: int
    rules: bool
    footer: bool
    inner_width: int
    list_width: int
    preview_width: int


SEPARATOR = " " + DIVIDER + " "


def compute_layout(width: int, height: int, preview: bool) -> Layout:
    rules = height >= 8
    footer = height >= 6
    list_top = 2 if rules else 1
    chrome = list_top + (2 if rules and footer else (1 if footer else 0))
    list_height = max(1, height - chrome)

    inner = max(1, width - 2)  # one column of padding on each side
    preview_width = 0
    list_width = inner
    if preview and inner >= PREVIEW_MIN + MIN_LIST + len(SEPARATOR):
        preview_width = min(PREVIEW_MAX, max(PREVIEW_MIN, inner // 3))
        list_width = inner - preview_width - len(SEPARATOR)
    return Layout(list_top, list_height, rules, footer, inner, list_width, preview_width)


class Segment(NamedTuple):
    role: str
    text: str
    bold: bool = False
    underline: bool = False


def _segments_to_text(theme: Theme, segments: Sequence[Segment], background: str = "") -> str:
    out: List[str] = []
    for segment in segments:
        if not segment.text:
            continue
        color = theme.fg(segment.role) if segment.role else ""
        if not color and not segment.bold and not segment.underline:
            out.append(segment.text)
            continue
        prefix = (BOLD if segment.bold else "") + (UNDERLINE if segment.underline else "") + color
        out.append(prefix + segment.text + RESET + background)
    return "".join(out)


def _plain_width(segments: Sequence[Segment]) -> int:
    return sum(display_width(segment.text) for segment in segments)


def _highlight(
    theme: Theme,
    text: str,
    positions: Sequence[int],
    base_role: str,
    bold: bool,
) -> List[Segment]:
    if not positions:
        return [Segment(base_role, text, bold)]
    marks = set(positions)
    segments: List[Segment] = []
    run: List[str] = []
    run_marked = False

    def flush() -> None:
        if not run:
            return
        # Matched characters are usually one or two letters adrift in a
        # sentence, so color alone reads as noise: underline them too and the
        # subsequence is legible at a glance.
        segments.append(
            Segment(
                "match" if run_marked else base_role,
                "".join(run),
                bold or run_marked,
                run_marked,
            )
        )

    for index, char in enumerate(text):
        marked = index in marks
        if marked != run_marked and run:
            flush()
            run = []
        run_marked = marked
        run.append(char)
    flush()
    return segments


def _context_label(item: Item) -> Tuple[str, int]:
    """The row's place: a workspace name, or the last segment of its path.

    Returns the text and its offset inside item.subtitle, so a match in the
    full path can be shifted onto the part actually drawn.
    """
    if item.kind == KIND_SPACE:
        return item.subtitle, 0
    if item.subtitle:
        stripped = item.subtitle.rstrip("/")
        base = os.path.basename(stripped)
        if base:
            return base, len(stripped) - len(base)
    return "", 0


def _field_marks(row: Row, text: str, offset: int = 0) -> Tuple[int, ...]:
    """Matched positions for a searchable field, moved onto what is drawn.

    ``offset`` is where the drawn text starts inside the field it came from
    (a path's last segment, a name behind its "@").
    """
    if not text or not row.field_positions:
        return ()
    try:
        index = row.item.fields.index(text)
    except ValueError:
        return ()
    marks = row.field_positions.get(index, ())
    if not marks:
        return ()
    if not offset:
        return marks
    return tuple(mark + offset for mark in marks if mark + offset >= 0)


def _chunk(theme: Theme, role: str, text: str, marks: Sequence[int]) -> List[Segment]:
    if not marks:
        return [Segment(role, text)]
    return _highlight(theme, text, [m for m in marks if 0 <= m < len(text)], role, False)


def render_row(
    theme: Theme,
    row: Row,
    width: int,
    selected: bool,
    tick: int,
    show_workspace: bool,
    age: Optional[float] = None,
) -> str:
    item = row.item
    background = theme.selection_bg if selected else ""

    marker = Segment("accent", SELECTED_BAR + " ") if selected else Segment("", "  ")
    glyph_role = item.status if item.status in ("blocked", "working", "done", "idle") else "unknown"
    glyph = Segment(glyph_role, theme.status_glyph(item.status, tick) + " ")

    meta: List[List[Segment]] = []
    context, context_start = _context_label(item)
    if (
        show_workspace
        and item.workspace_label
        and item.kind != KIND_SPACE
        and item.workspace_label.lower() != context.lower()
    ):
        meta.append(
            _chunk(theme, "muted", item.workspace_label, _field_marks(row, item.workspace_label))
        )
    if context:
        marks = _field_marks(row, item.subtitle, -context_start)
        meta.append(_chunk(theme, "muted", context, marks))
    if item.agent:
        if item.agent_name:
            label = "@" + item.agent_name
            marks = _field_marks(row, item.agent_name, 1)
        else:
            label = item.agent
            marks = _field_marks(row, item.agent)
        meta.append(_chunk(theme, "muted", label, marks))
    if item.focused:
        meta.append([Segment("accent", "current")])
    if age is not None:
        # Late in, so running time and the status word -- the two things that
        # tell you whether to look now -- survive the responsive trimming.
        meta.append([Segment("muted", format_age(age))])
    word = STATUS_WORDS.get(item.status, "")
    if word:
        meta.append(_chunk(theme, glyph_role, word, _field_marks(row, word)))
    if item.kind == KIND_SPACE:
        meta.append(_chunk(theme, "muted", "space", _field_marks(row, "space")))

    fixed = _plain_width([marker, glyph])
    available = max(0, width - fixed)

    # Titles win: drop meta chunks from the front (least specific first) until
    # the meta block fits its share of the row and the title has room.
    meta_budget = max(MIN_TITLE, int(available * META_SHARE))
    while meta and (
        _meta_width(meta) > meta_budget or available - _meta_width(meta) - 2 < MIN_TITLE
    ):
        meta.pop(0)
    meta_width = _meta_width(meta) if meta else 0
    title_width = max(1, available - (meta_width + 2 if meta_width else 0))

    title_text, positions, _ = window_positions(item.title, title_width, row.positions)
    title_segments = _highlight(theme, title_text, positions, "text", selected)

    used = _plain_width(title_segments)
    if item.detail:
        # The tab name has already had its say; the summary takes whatever the
        # row has left, and steps aside entirely when that is not worth reading.
        room = title_width - used - display_width(DETAIL_SEP)
        if room >= MIN_DETAIL:
            detail_text, detail_marks, _ = window_positions(
                item.detail, room, row.field_positions.get(item.detail_field, ())
            )
            title_segments.append(Segment("unknown", DETAIL_SEP))
            title_segments.extend(_highlight(theme, detail_text, detail_marks, "muted", False))
            used = _plain_width(title_segments)
    gap = max(1, available - used - meta_width) if meta_width else available - used
    segments = [marker, glyph] + title_segments
    if meta_width:
        segments.append(Segment("", " " * gap))
        segments.extend(_join_meta(meta))
    else:
        segments.append(Segment("", " " * max(0, gap)))

    body = _segments_to_text(theme, segments, background)
    trailing = max(0, width - _plain_width(segments))
    if background:
        return background + body + " " * trailing + RESET
    return body + " " * trailing


def _join_meta(meta: Sequence[Sequence[Segment]]) -> List[Segment]:
    out: List[Segment] = []
    for index, chunk in enumerate(meta):
        if index:
            out.append(Segment("unknown", " · "))
        out.extend(chunk)
    return out


def _meta_width(meta: Sequence[Sequence[Segment]]) -> int:
    if not meta:
        return 0
    return sum(_plain_width(chunk) for chunk in meta) + 3 * (len(meta) - 1)


def render_input(
    theme: Theme,
    query: str,
    cursor: int,
    width: int,
    placeholder: str,
    chip: str,
) -> str:
    chip_text = ""
    chip_width = 0
    if chip and width > len(chip) + 24:
        chip_text = chip
        chip_width = display_width(chip) + 2

    field_width = max(4, width - 2 - chip_width)
    prompt = _segments_to_text(theme, [Segment("accent", PROMPT + " ", True)])

    if query:
        visible = truncate(query, field_width)
        cursor_index = min(cursor, len(visible))
        before = visible[:cursor_index]
        under = visible[cursor_index] if cursor_index < len(visible) else " "
        after = visible[cursor_index + 1 :] if cursor_index < len(visible) else ""
        rendered = (
            prompt
            + _segments_to_text(theme, [Segment("text", before)])
            + "\x1b[7m"
            + under
            + "\x1b[27m"
            + _segments_to_text(theme, [Segment("text", after)])
        )
        used = 2 + display_width(before) + display_width(under) + display_width(after)
    else:
        hint = truncate(placeholder, max(0, field_width - 2))
        rendered = (
            prompt + "\x1b[7m \x1b[27m" + _segments_to_text(theme, [Segment("unknown", hint)])
        )
        used = 3 + display_width(hint)

    if chip_text:
        padding = max(1, width - used - display_width(chip_text))
        rendered += " " * padding + _segments_to_text(theme, [Segment("accent", chip_text)])
    return rendered


def render_rule(theme: Theme, width: int) -> str:
    return _segments_to_text(theme, [Segment("unknown", "─" * max(0, width))])


def render_footer(theme: Theme, width: int, hints: Sequence[Tuple[str, str]], counter: str) -> str:
    segments: List[Segment] = []
    for index, (key, label) in enumerate(hints):
        if index:
            segments.append(Segment("unknown", "  "))
        segments.append(Segment("muted", key))
        if label:
            segments.append(Segment("unknown", " " + label))
    used = _plain_width(segments)
    counter_width = display_width(counter)
    while segments and used + counter_width + 2 > width:
        # Drop hints in pairs from the end until the counter fits.
        segments.pop()
        while segments and segments[-1].role == "unknown" and segments[-1].text.strip() == "":
            segments.pop()
        used = _plain_width(segments)
    padding = max(1, width - used - counter_width)
    segments.append(Segment("", " " * padding))
    segments.append(Segment("unknown", counter))
    return _segments_to_text(theme, segments)


def render_confirm(theme: Theme, width: int, title: str, agents: int) -> str:
    """The armed close: what is about to go, and the keys that answer."""
    keys = "⏎ close · esc keep"
    detail = " and %d agents" % agents if agents > 1 else ""
    room = max(6, width - display_width(keys) - display_width(detail) - 12)
    segments = [
        Segment("blocked", "close ", True),
        Segment("text", "“%s”" % truncate(title, room), True),
        Segment("blocked", detail + "?"),
    ]
    used = _plain_width(segments)
    if used + display_width(keys) + 1 > width:
        keys = "⏎/esc"
    if used + display_width(keys) + 1 > width:
        # Nothing fits twice over: keep the question, lose the styling.
        text = truncate("close “%s”?" % title, max(0, width - display_width(keys) - 1))
        segments = [Segment("blocked", text, True)]
        used = _plain_width(segments)
    padding = max(1, width - used - display_width(keys))
    if used + padding + display_width(keys) > width:  # a popup too narrow for both
        return _segments_to_text(theme, [Segment("blocked", truncate("close?", width), True)])
    segments.append(Segment("", " " * padding))
    segments.append(Segment("muted", keys))
    return _segments_to_text(theme, segments)


def render_empty_state(
    theme: Theme, width: int, query: str, has_items: bool, scoped: bool
) -> List[str]:
    if not has_items:
        headline = "nothing to jump to"
        detail = "open a tab or start an agent"
        hint = ""
    elif query:
        headline = "no matches"
        detail = "for “%s”" % truncate(query, max(6, width - 12))
        hint = "^u clears the query"
    elif scoped:
        headline = "nothing in this filter"
        detail = ""
        hint = "⇥ next filter · ⌫ back to everything"
    else:
        headline = "no matches"
        detail = ""
        hint = ""
    lines = []
    for text, role, bold in (
        (headline, "text", True),
        (detail, "muted", False),
        (hint, "unknown", False),
    ):
        if not text:
            continue
        text = truncate(text, width)
        left = max(0, (width - display_width(text)) // 2)
        lines.append(
            pad(" " * left + _segments_to_text(theme, [Segment(role, text, bold)]), width)
        )
    return lines


def render_preview(
    theme: Theme,
    item: Optional[Item],
    lines: Sequence[str],
    width: int,
    height: int,
    loading: bool,
) -> List[str]:
    if height <= 0 or width <= 0:
        return []
    out: List[str] = []
    if item is None:
        return [""] * height

    header = [Segment("text", truncate(item.title, width), True)]
    room = width - _plain_width(header) - display_width(DETAIL_SEP)
    if item.detail and room >= MIN_DETAIL:
        header.append(Segment("unknown", DETAIL_SEP))
        header.append(Segment("muted", truncate(item.detail, room)))
    out.append(_segments_to_text(theme, header))
    subtitle = item.subtitle or item.workspace_label
    out.append(
        _segments_to_text(theme, [Segment("muted", truncate_middle(subtitle, width))])
        if subtitle
        else ""
    )
    out.append(_segments_to_text(theme, [Segment("unknown", "─" * width)]))

    body_height = height - len(out)
    if body_height <= 0:
        return out[:height]

    if loading and not lines:
        body = ["…"]
    elif not lines:
        body = ["no output yet"]
    else:
        body = list(lines)[-body_height:]
    out.extend([""] * max(0, body_height - len(body)))
    for line in body[-body_height:]:
        out.append(_segments_to_text(theme, [Segment("muted", truncate(line, width))]))
    return out[:height]


def scrollbar_column(total: int, visible: int, offset: int, height: int) -> Dict[int, str]:
    """Map row index -> scrollbar glyph for the visible window."""
    if total <= visible or height <= 0:
        return {}
    thumb = max(1, int(round(height * visible / float(total))))
    span = max(1, total - visible)
    start = int(round((height - thumb) * (offset / float(span))))
    marks: Dict[int, str] = {}
    for row in range(height):
        marks[row] = SCROLL_THUMB if start <= row < start + thumb else SCROLL_TRACK
    return marks


def compose(rows: Sequence[str]) -> str:
    """Position and paint every row, clearing the rest of each line."""
    out: List[str] = ["\x1b[H"]
    for index, row in enumerate(rows):
        out.append("\x1b[%d;1H" % (index + 1))
        out.append(row)
        out.append("\x1b[0m\x1b[K")
    out.append("\x1b[J")
    return "".join(out)
