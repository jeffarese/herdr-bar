"""The bar itself: state, key handling, and the draw loop."""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import render
from .client import HerdrClient, HerdrError
from .config import Config
from .fuzzy import match, split_query
from .items import KIND_AGENT, KIND_SPACE, KIND_TAB, Item, build_items
from .keys import KEY, MOUSE, PASTE, PRESS, TEXT, WHEEL_DOWN, WHEEL_UP, Event, KeyDecoder
from .mru import Recents
from .render import Row, compute_layout, scrollbar_column
from .term import Terminal
from .textutil import pad, sanitize
from .theme import Theme

SCOPES = ("all", "agent", "tab", "blocked")
SCOPE_SIGILS = {"@": "agent", "$": "tab", "!": "blocked"}
SCOPE_CHIPS = {
    "all": "",
    "agent": "@ agents",
    "tab": "$ shells",
    "blocked": "! needs you",
}

TITLE_BONUS = 40
SPINNER_INTERVAL = 0.11
PREVIEW_DEBOUNCE = 0.07
PREVIEW_TTL = 1.5
ESC_TIMEOUT = 0.04
STATUS_TTL = 2.5

PLACEHOLDER = "jump to a tab or agent…    @ agents   $ shells   ! needs you"
PLACEHOLDER_SHORT = "jump to a tab or agent…"


def score_item(
    terms: Sequence[str], fields: Sequence[str]
) -> Optional[Tuple[int, Tuple[int, ...]]]:
    """Score one row: every term must match one field, best field wins.

    Only title matches (field 0) produce highlight positions, so the
    highlighting always lines up with the text actually on screen.
    """
    total = 0
    positions: List[int] = []
    for term in terms:
        best: Optional[int] = None
        best_positions: Tuple[int, ...] = ()
        for index, field in enumerate(fields):
            found = match(term, field)
            if found is None:
                continue
            value = found.score + (TITLE_BONUS if index == 0 else 0)
            if best is None or value > best:
                best = value
                best_positions = found.positions if index == 0 else ()
        if best is None:
            return None
        total += best
        positions.extend(best_positions)
    return total, tuple(sorted(set(positions)))


class Bar(object):
    def __init__(self, client: HerdrClient, config: Config, recents: Recents, theme: Theme) -> None:
        self.client = client
        self.config = config
        self.recents = recents
        self.theme = theme

        self.items: List[Item] = []
        self.rows: List[Row] = []
        self.query = ""
        self.cursor = 0
        self.scope = "all"
        self.selected = 0
        self.offset = 0
        self.preview_enabled = config.preview is not False
        self.workspace_count = 0
        self.current_key: Optional[str] = None
        self.current_title = ""

        self.tick = 0
        self.status: Optional[str] = None
        self.status_until = 0.0
        self._preview_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._preview_pending: Optional[Tuple[str, float]] = None
        self._last_refresh = 0.0
        self._last_spin = 0.0
        self._dirty = True
        self._list_top = 2
        self._list_height = 10

    # -- data ---------------------------------------------------------------

    def bootstrap(self) -> None:
        """First load, outside the alt screen, so failures can be reported."""
        self._apply(self.client.snapshot())

    def refresh(self, force: bool = False) -> None:
        try:
            snapshot = self.client.snapshot()
        except HerdrError as error:
            self.flash(str(error))
            self._last_refresh = time.monotonic()
            return
        self._apply(snapshot, force=force)

    def _apply(self, snapshot: Dict[str, object], force: bool = False) -> None:
        keep = self.selected_item().key if self.rows else None
        self.workspace_count = len(snapshot.get("workspaces") or [])
        items = build_items(snapshot)
        if not self.config.wants_workspaces(self.workspace_count):
            items = [item for item in items if item.kind != KIND_SPACE]
        self.items = items
        for item in items:
            if item.focused and item.kind != KIND_SPACE:
                self.current_key = item.key
                self.current_title = item.title
                break
        self._last_refresh = time.monotonic()
        self.rebuild(keep_key=None if force else keep)

    def rebuild(self, keep_key: Optional[str] = None) -> None:
        self.rows = self._filter()
        if keep_key:
            for index, row in enumerate(self.rows):
                if row.item.key == keep_key:
                    self.selected = index
                    break
            else:
                self.selected = 0
        self.selected = max(0, min(self.selected, len(self.rows) - 1)) if self.rows else 0
        self._dirty = True

    def _in_scope(self, item: Item) -> bool:
        if self.scope == "all":
            return True
        if self.scope == "agent":
            return item.kind == KIND_AGENT
        if self.scope == "tab":
            return item.kind == KIND_TAB
        if self.scope == "blocked":
            return item.status in ("blocked", "done")
        return True

    def _filter(self) -> List[Row]:
        candidates = [item for item in self.items if self._in_scope(item)]
        terms = split_query(self.query)
        if not terms:
            return [Row(item, ()) for item in self._resting_order(candidates)]

        scored: List[Tuple[int, int, int, int, Row]] = []
        for item in candidates:
            found = score_item(terms, item.fields)
            if found is None:
                continue
            score, positions = found
            rank = self.recents.rank(item.key, item.title)
            scored.append(
                (
                    -score,
                    rank if rank is not None else 999,
                    item.status_rank,
                    len(item.title),
                    Row(item, positions),
                )
            )
        scored.sort(key=lambda entry: entry[:4])
        return [entry[4] for entry in scored]

    def _resting_order(self, items: Sequence[Item]) -> List[Item]:
        recent: List[Tuple[int, Item]] = []
        rest: List[Item] = []
        current: Optional[Item] = None
        for item in items:
            if item.key == self.current_key:
                current = item
                continue
            rank = self.recents.rank(item.key, item.title)
            if rank is None:
                rest.append(item)
            else:
                recent.append((rank, item))
        recent.sort(key=lambda entry: entry[0])
        rest.sort(
            key=lambda item: (
                item.kind == KIND_SPACE,
                item.status_rank,
                item.workspace_label,
                item.tab_number if item.tab_number is not None else 1 << 30,
                item.title,
            )
        )
        ordered = [item for _, item in recent]
        ordered.extend(rest)
        if current is not None:
            # Never first: open-then-Enter should take you somewhere else, the
            # way alt-tab does. Right after the recents block is close enough
            # to stay findable.
            ordered.insert(min(len(ordered), max(1, len(recent))), current)
        return ordered

    def selected_item(self) -> Item:
        return self.rows[self.selected].item

    # -- interaction --------------------------------------------------------

    def flash(self, message: str) -> None:
        self.status = message
        self.status_until = time.monotonic() + STATUS_TTL
        self._dirty = True

    def move(self, delta: int, wrap: bool = True) -> None:
        if not self.rows:
            return
        target = self.selected + delta
        if wrap and abs(delta) == 1:
            target %= len(self.rows)
        self.selected = max(0, min(target, len(self.rows) - 1))
        self._dirty = True

    def set_query(self, query: str, cursor: Optional[int] = None) -> None:
        self.query = query
        self.cursor = len(query) if cursor is None else max(0, min(cursor, len(query)))
        self.selected = 0
        self.offset = 0
        self.rebuild()

    def cycle_scope(self, step: int) -> None:
        index = (SCOPES.index(self.scope) + step) % len(SCOPES)
        self.scope = SCOPES[index]
        self.selected = 0
        self.rebuild()

    def insert(self, text: str, pasted: bool = False) -> None:
        text = sanitize(text, " ")
        if pasted:
            text = " ".join(text.split())
        if not text or (not self.query and not text.strip()):
            return
        if not self.query and len(text) == 1 and text in SCOPE_SIGILS:
            # A leading sigil is a filter, not a character: "@" scopes the list
            # to agents instead of searching for a literal at sign.
            self.scope = SCOPE_SIGILS[text]
            self.selected = 0
            self.rebuild()
            return
        self.set_query(
            self.query[: self.cursor] + text + self.query[self.cursor :],
            self.cursor + len(text),
        )

    def backspace(self) -> None:
        if not self.query:
            if self.scope != "all":
                self.scope = "all"
                self.rebuild()
            return
        if self.cursor <= 0:
            return
        self.set_query(
            self.query[: self.cursor - 1] + self.query[self.cursor :],
            self.cursor - 1,
        )

    def delete_word(self) -> None:
        if self.cursor <= 0:
            return
        head = self.query[: self.cursor].rstrip()
        space = head.rfind(" ")
        head = head[: space + 1] if space >= 0 else ""
        self.set_query(head + self.query[self.cursor :], len(head))

    def handle_key(self, name: str) -> Optional[str]:
        if name in ("esc", "ctrl+c", "ctrl+g", "ctrl+q"):
            return "cancel"
        if name == "enter":
            return "confirm" if self.rows else None
        if name in ("up", "ctrl+p", "ctrl+k"):
            self.move(-1)
            return None
        if name in ("down", "ctrl+n", "ctrl+j"):
            self.move(1)
            return None
        if name == "tab":
            self.cycle_scope(1)
            return None
        if name == "shift+tab":
            self.cycle_scope(-1)
            return None
        if name in ("pgup", "alt+v"):
            self.move(-self._page(), wrap=False)
            return None
        if name in ("pgdn", "ctrl+v"):
            self.move(self._page(), wrap=False)
            return None
        if name in ("home", "ctrl+home"):
            self.cursor = 0
            self._dirty = True
            return None
        if name in ("end", "ctrl+end"):
            self.cursor = len(self.query)
            self._dirty = True
            return None
        if name == "ctrl+a":
            self.cursor = 0
            self._dirty = True
            return None
        if name == "ctrl+e":
            self.cursor = len(self.query)
            self._dirty = True
            return None
        if name == "left":
            self.cursor = max(0, self.cursor - 1)
            self._dirty = True
            return None
        if name == "right":
            self.cursor = min(len(self.query), self.cursor + 1)
            self._dirty = True
            return None
        if name == "backspace":
            self.backspace()
            return None
        if name == "delete":
            if self.cursor < len(self.query):
                self.set_query(
                    self.query[: self.cursor] + self.query[self.cursor + 1 :], self.cursor
                )
            return None
        if name == "ctrl+u":
            self.scope = "all"
            self.set_query("")
            return None
        if name == "ctrl+w":
            self.delete_word()
            return None
        if name == "ctrl+o":
            self.preview_enabled = not self.preview_enabled
            self._dirty = True
            return None
        if name == "ctrl+r":
            self.refresh(force=False)
            self.flash("refreshed")
            return None
        return None

    def _page(self) -> int:
        return max(1, self._list_height - 1)

    def handle_mouse(self, event: Event) -> Optional[str]:
        if event.value == WHEEL_UP:
            self.move(-3, wrap=False)
            return None
        if event.value == WHEEL_DOWN:
            self.move(3, wrap=False)
            return None
        if event.value != PRESS:
            return None
        row_index = event.y - 1 - self._list_top
        if row_index < 0 or row_index >= self._list_height:
            return None
        target = self.offset + row_index
        if target >= len(self.rows):
            return None
        if target == self.selected:
            return "confirm"
        self.selected = target
        self._dirty = True
        return None

    # -- preview ------------------------------------------------------------

    def preview_lines(self, item: Optional[Item], height: int) -> Tuple[List[str], bool]:
        if item is None or not item.pane_id or item.kind == KIND_SPACE:
            return [], False
        cached = self._preview_cache.get(item.pane_id)
        now = time.monotonic()
        if cached and now - cached[0] < PREVIEW_TTL:
            return cached[1], False
        if self._preview_pending is None or self._preview_pending[0] != item.pane_id:
            self._preview_pending = (item.pane_id, now + PREVIEW_DEBOUNCE)
        return (cached[1] if cached else []), True

    def pump_preview(self, height: int) -> None:
        if not self._preview_pending:
            return
        pane_id, due = self._preview_pending
        if time.monotonic() < due:
            return
        self._preview_pending = None
        try:
            text = self.client.read_pane(pane_id, max(4, height))
        except HerdrError:
            text = ""
        lines = [sanitize(line) for line in text.splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        self._preview_cache[pane_id] = (time.monotonic(), lines)
        self._dirty = True

    # -- drawing ------------------------------------------------------------

    def draw(self, terminal: Terminal) -> None:
        width, height = terminal.size()
        preview_on = self.preview_enabled and self.config.wants_preview(max(0, width - 2))
        layout = compute_layout(width, height, preview_on)
        self._list_top = layout.list_top
        self._list_height = layout.list_height

        self._scroll_into_view(layout.list_height)

        rows: List[str] = []
        chip = SCOPE_CHIPS.get(self.scope, "")
        placeholder = PLACEHOLDER if layout.inner_width >= 62 else PLACEHOLDER_SHORT
        rows.append(
            " "
            + render.render_input(
                self.theme, self.query, self.cursor, layout.inner_width, placeholder, chip
            )
        )
        if layout.rules:
            rows.append(" " + render.render_rule(self.theme, layout.inner_width))

        selected_item = self.selected_item() if self.rows else None
        preview_body: List[str] = []
        if layout.preview_width:
            lines, loading = self.preview_lines(selected_item, layout.list_height)
            preview_body = render.render_preview(
                self.theme,
                selected_item,
                lines,
                layout.preview_width,
                layout.list_height,
                loading,
            )

        list_body = self._list_rows(layout)
        for index in range(layout.list_height):
            left = list_body[index] if index < len(list_body) else ""
            if layout.preview_width:
                right = preview_body[index] if index < len(preview_body) else ""
                left = pad(left, layout.list_width)
                separator = self.theme.paint("unknown", render.SEPARATOR)
                rows.append(" " + left + separator + right)
            else:
                rows.append(" " + left)

        if layout.footer:
            if layout.rules:
                rows.append(" " + render.render_rule(self.theme, layout.inner_width))
            rows.append(" " + self._footer(layout.inner_width))

        terminal.write(render.compose(rows[:height]))
        terminal.flush()
        self._dirty = False

    def _scroll_into_view(self, height: int) -> None:
        if not self.rows:
            self.offset = 0
            return
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + height:
            self.offset = self.selected - height + 1
        self.offset = max(0, min(self.offset, max(0, len(self.rows) - height)))

    def _list_rows(self, layout: render.Layout) -> List[str]:
        if not self.rows:
            empty = render.render_empty_state(
                self.theme,
                layout.list_width,
                self.query,
                bool(self.items),
                self.scope != "all",
            )
            top = max(0, (layout.list_height - len(empty)) // 2)
            return [""] * top + empty

        marks = scrollbar_column(
            len(self.rows), layout.list_height, self.offset, layout.list_height
        )
        content_width = layout.list_width - (1 if marks else 0)
        show_workspace = self.workspace_count > 1

        out: List[str] = []
        for index in range(layout.list_height):
            position = self.offset + index
            if position >= len(self.rows):
                body = ""
            else:
                body = render.render_row(
                    self.theme,
                    self.rows[position],
                    content_width,
                    position == self.selected,
                    self.tick,
                    show_workspace,
                )
            if marks:
                body = pad(body, content_width) + self.theme.paint("unknown", marks.get(index, " "))
            out.append(body)
        return out

    def _footer(self, width: int) -> str:
        if self.status and time.monotonic() < self.status_until:
            message = self.theme.paint("accent", self.status)
            return message
        hints = [
            ("↑↓", "move"),
            ("⏎", "jump"),
            ("⇥", "scope"),
            ("^o", "preview"),
            ("esc", "close"),
        ]
        if width < 52:
            hints = [("↑↓", ""), ("⏎", ""), ("esc", "")]
        total = len(self.items)
        shown = len(self.rows)
        counter = "%d/%d" % (shown, total) if shown != total else "%d" % total
        return render.render_footer(self.theme, width, hints, counter)

    # -- main loop ----------------------------------------------------------

    def run(self, terminal: Terminal) -> Optional[Item]:
        decoder = KeyDecoder()
        self.draw(terminal)
        escape_deadline: Optional[float] = None

        while True:
            timeout = self._next_timeout(escape_deadline)
            if terminal.wait(timeout):
                data = terminal.read()
                if not data:
                    return None
                outcome = self._consume(decoder.feed(data))
                escape_deadline = (
                    time.monotonic() + ESC_TIMEOUT if decoder.pending_escape() else None
                )
                if outcome == "cancel":
                    return None
                if outcome == "confirm":
                    return self.selected_item() if self.rows else None
            elif escape_deadline is not None and time.monotonic() >= escape_deadline:
                escape_deadline = None
                if self._consume(decoder.flush()) == "cancel":
                    return None

            if terminal.take_resize():
                self._dirty = True

            now = time.monotonic()
            if now - self._last_refresh >= self.config.refresh_ms / 1000.0:
                self.refresh()
            if self.config.spinner and now - self._last_spin >= SPINNER_INTERVAL:
                self._last_spin = now
                if any(row.item.status == "working" for row in self.rows):
                    self.tick += 1
                    self._dirty = True
            self.pump_preview(self._list_height)
            if self.status and now >= self.status_until:
                self.status = None
                self._dirty = True
            if self._dirty:
                self.draw(terminal)

    def _consume(self, events: Sequence[Event]) -> Optional[str]:
        outcome: Optional[str] = None
        for event in events:
            if event.kind == KEY:
                outcome = self.handle_key(event.value) or outcome
            elif event.kind == TEXT:
                self.insert(event.value)
            elif event.kind == PASTE:
                self.insert(event.value, pasted=True)
            elif event.kind == MOUSE:
                outcome = self.handle_mouse(event) or outcome
            if outcome in ("cancel", "confirm"):
                return outcome
        return outcome

    def _next_timeout(self, escape_deadline: Optional[float]) -> float:
        now = time.monotonic()
        deadlines = [self._last_refresh + self.config.refresh_ms / 1000.0]
        if self.config.spinner and any(row.item.status == "working" for row in self.rows):
            deadlines.append(self._last_spin + SPINNER_INTERVAL)
        if self._preview_pending:
            deadlines.append(self._preview_pending[1])
        if escape_deadline is not None:
            deadlines.append(escape_deadline)
        if self.status:
            deadlines.append(self.status_until)
        return max(0.01, min(deadlines) - now)


def jump(client: HerdrClient, item: Item) -> None:
    """Focus whatever the row points at."""
    if item.kind == KIND_SPACE:
        client.focus_workspace(item.workspace_id)
        return
    if item.tab_id:
        client.focus_tab(item.tab_id)
    if item.kind == KIND_AGENT and item.pane_id:
        client.focus_agent(item.pane_id)
