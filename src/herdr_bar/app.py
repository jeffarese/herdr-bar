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
from .paint import Painter
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


_UNSCORED = object()


def score_item(
    terms: Sequence[str],
    fields: Sequence[str],
    memo: Optional[Dict[Tuple[str, str], object]] = None,
) -> Optional[Tuple[int, Dict[int, Tuple[int, ...]]]]:
    """Score one row: every term must match one field, best field wins.

    Positions come back keyed by the field that won the term, so a caller can
    light up whichever of those fields it actually draws -- and terms that were
    answered by a field nobody can see simply have nowhere to land.

    ``memo`` is an optional scratch dict shared across the rows of one pass.
    Whole fields repeat across a session -- every agent is "claude", every
    other one is "working" -- and scoring the same term against the same string
    twice cannot give a different answer.
    """
    total = 0
    found_positions: Dict[int, List[int]] = {}
    for term in terms:
        best: Optional[int] = None
        best_index = -1
        best_positions: Tuple[int, ...] = ()
        for index, field in enumerate(fields):
            if memo is None:
                found = match(term, field)
            else:
                key = (term, field)
                found = memo.get(key, _UNSCORED)
                if found is _UNSCORED:
                    found = match(term, field)
                    memo[key] = found
            if found is None:
                continue
            value = found.score + (TITLE_BONUS if index == 0 else 0)
            if best is None or value > best:
                best = value
                best_index = index
                best_positions = found.positions
        if best is None:
            return None
        total += best
        if best_positions:
            found_positions.setdefault(best_index, []).extend(best_positions)
    return total, {index: tuple(sorted(set(marks))) for index, marks in found_positions.items()}


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
        self.preview_enabled = config.preview_starts_on()
        self.preview_chosen = False
        self._preview_showing = self.preview_enabled
        self.workspace_count = 0
        self.current_key: Optional[str] = None
        self.current_title = ""

        self.tick = 0
        self.status: Optional[str] = None
        self.status_until = 0.0
        self.pending_close: Optional[Tuple[str, str, int]] = None
        self.pending_rename: Optional[Tuple[str, str]] = None
        self.rename_text = ""
        self.rename_cursor = 0
        self._preview_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._preview_pending: Optional[Tuple[str, float]] = None
        self._ages: Dict[str, Optional[Tuple[float, float]]] = {}
        self._age_queue: List[str] = []
        self._last_refresh = 0.0
        self._last_spin = 0.0
        self._dirty = True
        self._list_top = 2
        self._list_height = 10
        self._painter = Painter()

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
        memo: Dict[Tuple[str, str], object] = {}
        for item in candidates:
            found = score_item(terms, item.fields, memo)
            if found is None:
                continue
            score, by_field = found
            rank = self.recents.rank(item.key, item.title)
            scored.append(
                (
                    -score,
                    rank if rank is not None else 999,
                    item.status_rank,
                    len(item.title),
                    Row(item, by_field.get(0, ()), by_field),
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
            # Nothing left to unwind, and this is the key labelled "delete" on
            # a Mac keyboard: offer to close the row instead of doing nothing.
            self.request_close()
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
            self.request_close()
            return None
        if name == "ctrl+d":
            # Forward delete lives here now that ⌦ closes tabs.
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
            # Flip what is on screen, not a hidden wish: in a popup too narrow
            # for "auto" the preview is off even though it is enabled, and one
            # press should bring it up rather than agree it is already gone.
            self.preview_enabled = not self._preview_showing
            self.preview_chosen = True
            self._dirty = True
            return None
        if name == "ctrl+r":
            self.request_rename()
            return None
        return None

    def _page(self) -> int:
        return max(1, self._list_height - 1)

    # -- renaming -----------------------------------------------------------

    def request_rename(self) -> None:
        """Open an inline editor for the tab behind the selected row."""
        if not self.rows:
            return
        item = self.selected_item()
        if item.kind == KIND_SPACE or not item.tab_id:
            self.flash("only tabs and agents can be renamed")
            return
        self.pending_rename = (item.tab_id, item.title)
        self.rename_text = ""
        self.rename_cursor = 0
        self._dirty = True

    def cancel_rename(self) -> None:
        self.pending_rename = None
        self.rename_text = ""
        self.rename_cursor = 0
        self._dirty = True

    def _set_rename_text(self, text: str, cursor: Optional[int] = None) -> None:
        self.rename_text = text
        self.rename_cursor = (
            len(text) if cursor is None else max(0, min(cursor, len(text)))
        )
        self._dirty = True

    def _insert_rename(self, text: str, pasted: bool = False) -> None:
        text = sanitize(text, " ")
        if pasted:
            text = " ".join(text.split())
        if not text:
            return
        self._set_rename_text(
            self.rename_text[: self.rename_cursor]
            + text
            + self.rename_text[self.rename_cursor :],
            self.rename_cursor + len(text),
        )

    def _rename_delete_word(self) -> None:
        if self.rename_cursor <= 0:
            return
        head = self.rename_text[: self.rename_cursor].rstrip()
        space = head.rfind(" ")
        head = head[: space + 1] if space >= 0 else ""
        self._set_rename_text(head + self.rename_text[self.rename_cursor :], len(head))

    def confirm_rename(self) -> None:
        if self.pending_rename is None:
            return
        tab_id, old_title = self.pending_rename
        label = self.rename_text.strip()
        if not label or label == old_title:
            self.cancel_rename()
            self.flash("name unchanged")
            return
        try:
            self.client.rename_tab(tab_id, label)
        except HerdrError as error:
            self.flash(str(error))
            return
        self.cancel_rename()
        self.refresh(force=False)
        self.flash("renamed to %s" % label)

    def handle_rename(self, event: Event) -> Optional[str]:
        """The rename editor owns input until it is saved or cancelled."""
        if event.kind == TEXT:
            self._insert_rename(event.value)
            return None
        if event.kind == PASTE:
            self._insert_rename(event.value, pasted=True)
            return None
        if event.kind != KEY:
            return None

        name = event.value
        if name in ("esc", "ctrl+g"):
            self.cancel_rename()
            return None
        if name in ("ctrl+c", "ctrl+q"):
            self.cancel_rename()
            return "cancel"
        if name == "enter":
            self.confirm_rename()
            return None
        if name in ("home", "ctrl+home", "ctrl+a"):
            self.rename_cursor = 0
            self._dirty = True
            return None
        if name in ("end", "ctrl+end", "ctrl+e"):
            self.rename_cursor = len(self.rename_text)
            self._dirty = True
            return None
        if name == "left":
            self.rename_cursor = max(0, self.rename_cursor - 1)
            self._dirty = True
            return None
        if name == "right":
            self.rename_cursor = min(len(self.rename_text), self.rename_cursor + 1)
            self._dirty = True
            return None
        if name in ("backspace", "ctrl+h"):
            if self.rename_cursor > 0:
                self._set_rename_text(
                    self.rename_text[: self.rename_cursor - 1]
                    + self.rename_text[self.rename_cursor :],
                    self.rename_cursor - 1,
                )
            return None
        if name in ("delete", "ctrl+d"):
            if self.rename_cursor < len(self.rename_text):
                self._set_rename_text(
                    self.rename_text[: self.rename_cursor]
                    + self.rename_text[self.rename_cursor + 1 :],
                    self.rename_cursor,
                )
            return None
        if name == "ctrl+u":
            self._set_rename_text("")
            return None
        if name == "ctrl+w":
            self._rename_delete_word()
            return None
        return None

    # -- closing ------------------------------------------------------------

    def backspace_closes(self) -> bool:
        """Whether backspace currently arms a close rather than erasing."""
        return not self.query and self.scope == "all"

    def request_close(self) -> None:
        """Arm the confirmation for the selected row's tab.

        Rows are per agent, so a tab running two of them shows up twice; the
        confirmation says how many are going, because the tab takes them all.
        """
        if not self.rows:
            return
        item = self.selected_item()
        if item.kind == KIND_SPACE or not item.tab_id:
            self.flash("only tabs and agents can be closed")
            return
        agents = sum(
            1
            for other in self.items
            if other.kind == KIND_AGENT and other.tab_id == item.tab_id
        )
        self.pending_close = (item.tab_id, item.title, agents)
        self._dirty = True

    def confirm_close(self) -> None:
        if self.pending_close is None:
            return
        tab_id, title, _ = self.pending_close
        self.pending_close = None
        try:
            self.client.close_tab(tab_id)
        except HerdrError as error:
            self.flash(str(error))
            return
        self.refresh(force=True)
        self.flash("closed %s" % title)

    def handle_confirm(self, event: Event) -> Optional[str]:
        """An armed close owns the keyboard: every key answers it, once.

        Deliberately not the delete keys themselves -- they repeat when held,
        and a repeat should never be read as a yes.
        """
        if event.kind == KEY:
            if event.value == "enter":
                self.confirm_close()
                return None
            if event.value in ("ctrl+c", "ctrl+q"):
                # The panic exit stays the panic exit.
                self.pending_close = None
                return "cancel"
        elif event.kind == TEXT and event.value in ("y", "Y"):
            self.confirm_close()
            return None
        self.pending_close = None
        self._dirty = True
        return None

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

    def preview_lines(self, item: Optional[Item]) -> Tuple[List[str], bool]:
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

    # -- running time -------------------------------------------------------

    def age_of(self, item: Item) -> Optional[float]:
        """How long the row's process has been up, queueing a first reading.

        A start time never moves, so each pane is read once and then ticked
        locally -- the list stays live without paying for it every refresh.
        """
        pane_id = item.pane_id
        if not pane_id or item.kind == KIND_SPACE:
            return None
        if pane_id not in self._ages:
            if pane_id not in self._age_queue:
                self._age_queue.append(pane_id)
            return None
        reading = self._ages[pane_id]
        if reading is None:
            return None
        taken, seconds = reading
        return seconds + (time.monotonic() - taken)

    def pump_ages(self) -> None:
        """Take at most one reading per pass, so input never waits on ps."""
        while self._age_queue:
            pane_id = self._age_queue.pop(0)
            if pane_id in self._ages:
                continue
            try:
                seconds = self.client.pane_age(pane_id)
            except HerdrError:
                seconds = None
            # A pane that cannot be dated is remembered as such: one failed
            # reading is enough, and the row simply shows no time.
            self._ages[pane_id] = None if seconds is None else (time.monotonic(), seconds)
            self._dirty = True
            return

    # -- drawing ------------------------------------------------------------

    def draw(self, terminal: Terminal) -> None:
        width, height = terminal.size()
        preview_on = self.preview_enabled and self.config.fits_preview(
            max(0, width - 2), self.preview_chosen
        )
        self._preview_showing = preview_on
        layout = compute_layout(width, height, preview_on)
        self._list_top = layout.list_top
        self._list_height = layout.list_height

        self._scroll_into_view(layout.list_height)

        rows: List[str] = []
        chip = SCOPE_CHIPS.get(self.scope, "")
        placeholder = PLACEHOLDER if layout.inner_width >= 62 else PLACEHOLDER_SHORT
        if self.pending_rename is not None:
            _, old_title = self.pending_rename
            rows.append(
                " "
                + render.render_input(
                    self.theme,
                    self.rename_text,
                    self.rename_cursor,
                    layout.inner_width,
                    "new name for “%s”" % old_title,
                    "⏎ save · esc keep",
                    "rename ❯",
                )
            )
        else:
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
            lines, loading = self.preview_lines(selected_item)
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

        payload = self._painter.frame(rows, width, height)
        if payload:
            # A frame with nothing to say says nothing: no bytes, no syscall.
            terminal.write(payload)
            terminal.flush()
        self._dirty = False

    def invalidate(self) -> None:
        """Forget what is on screen, so the next draw repaints all of it.

        The bar owns the alt screen while it is up, which is what lets the
        painter trust its own model of it. A resize -- or anything else that
        writes over the top -- is where that stops being true.
        """
        self._painter.reset()
        self._dirty = True

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
                    self.age_of(self.rows[position].item),
                )
            if marks:
                body = pad(body, content_width) + self.theme.paint("unknown", marks.get(index, " "))
            out.append(body)
        return out

    def _footer(self, width: int) -> str:
        if self.pending_close is not None:
            _, title, agents = self.pending_close
            return render.render_confirm(self.theme, width, title, agents)
        if self.status and time.monotonic() < self.status_until:
            message = self.theme.paint("accent", self.status)
            return message
        if self.pending_rename is not None:
            return render.render_footer(
                self.theme,
                width,
                [("⏎", "save"), ("esc", "keep"), ("^u", "clear")],
                "renaming",
            )
        hints = [
            ("↑↓", "move"),
            ("⏎", "jump"),
            ("⇥", "scope"),
            # Backspace only reaches the close once there is nothing left for
            # it to erase, so the footer names whichever key works right now.
            ("⌫" if self.backspace_closes() else "⌦", "close"),
            ("^r", "rename"),
            ("^o", "preview"),
            ("esc", "quit"),
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
                self.invalidate()

            now = time.monotonic()
            if now - self._last_refresh >= self.config.refresh_ms / 1000.0:
                self.refresh()
            if self.config.spinner and now - self._last_spin >= SPINNER_INTERVAL:
                self._last_spin = now
                if any(row.item.status == "working" for row in self.rows):
                    self.tick += 1
                    self._dirty = True
            self.pump_preview(self._list_height)
            self.pump_ages()
            if self.status and now >= self.status_until:
                self.status = None
                self._dirty = True
            if self._dirty:
                self.draw(terminal)

    def _consume(self, events: Sequence[Event]) -> Optional[str]:
        outcome: Optional[str] = None
        for event in events:
            if self.pending_close is not None:
                outcome = self.handle_confirm(event) or outcome
                if outcome == "cancel":
                    return outcome
                continue
            if self.pending_rename is not None:
                outcome = self.handle_rename(event) or outcome
                if outcome == "cancel":
                    return outcome
                continue
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
        if self._age_queue:
            # Readings outstanding: come straight back for the next one, so a
            # freshly drawn list fills in its times inside a few frames.
            return 0.01
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
