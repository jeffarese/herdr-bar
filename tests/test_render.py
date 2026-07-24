import re
import unittest

from herdr_bar.app import Bar
from herdr_bar.config import Config
from herdr_bar.items import build_items
from herdr_bar.mru import Recents
from herdr_bar.render import (
    Row,
    compute_layout,
    render_footer,
    render_input,
    render_row,
    scrollbar_column,
)
from herdr_bar.textutil import visible_width
from herdr_bar.theme import Theme

from . import fixtures

ROW_RE = re.compile(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|\x1b\[J|$)", re.S)


class FakeClient(object):
    def snapshot(self):
        return fixtures.snapshot()

    def read_pane(self, pane_id, lines, source="visible"):
        return fixtures.PREVIEW_TEXT


class FakeTerminal(object):
    def __init__(self, width, height):
        self._size = (width, height)
        self.frames = []

    def size(self):
        return self._size

    def write(self, text):
        self.frames.append(text)

    def flush(self):
        pass


def frame_rows(frame, height):
    rows = [""] * height
    for match in ROW_RE.finditer(frame):
        index = int(match.group(1)) - 1
        if 0 <= index < height:
            rows[index] = match.group(2).replace("\x1b[K", "").replace("\x1b[0m", "")
    return rows


def draw(width, height, query="", scope="all", config=None):
    bar = Bar(
        FakeClient(),
        config or Config({"selection_background": "237"}),
        Recents(None),
        Theme(selection_background="237"),
    )
    bar.bootstrap()
    bar.scope = scope
    if query:
        bar.set_query(query)
    bar.rebuild()
    terminal = FakeTerminal(width, height)
    bar.draw(terminal)
    if bar._preview_pending:  # skip the debounce the draw just armed
        bar._preview_pending = (bar._preview_pending[0], 0.0)
    bar.pump_preview(bar._list_height)
    terminal.frames = []
    bar.draw(terminal)
    return bar, frame_rows("".join(terminal.frames), height)


SIZES = [
    (40, 6),
    (56, 8),
    (72, 10),
    (80, 24),
    (96, 12),
    (110, 20),
    (140, 30),
    (200, 40),
    (30, 5),
    (24, 4),
]


class LayoutTest(unittest.TestCase):
    def test_rows_never_exceed_the_popup_width(self):
        for width, height in SIZES:
            for query in ("", "week", "zzzz"):
                _, rows = draw(width, height, query)
                for index, row in enumerate(rows):
                    self.assertLessEqual(
                        visible_width(row),
                        width,
                        "row %d overflows at %dx%d query=%r: %r"
                        % (index, width, height, query, row),
                    )

    def test_frame_never_exceeds_the_popup_height(self):
        for width, height in SIZES:
            _, rows = draw(width, height)
            self.assertEqual(len(rows), height)

    def test_preview_only_appears_when_there_is_room(self):
        narrow = compute_layout(70, 20, preview=True)
        wide = compute_layout(140, 20, preview=True)
        self.assertEqual(narrow.preview_width, 0)
        self.assertGreater(wide.preview_width, 0)
        self.assertEqual(wide.list_width + wide.preview_width + 3, wide.inner_width)

    def test_disabled_preview_gives_the_list_everything(self):
        layout = compute_layout(140, 20, preview=False)
        self.assertEqual(layout.preview_width, 0)
        self.assertEqual(layout.list_width, layout.inner_width)

    def test_tiny_popups_still_render_a_list(self):
        layout = compute_layout(20, 3, preview=True)
        self.assertGreaterEqual(layout.list_height, 1)
        self.assertFalse(layout.rules)


class ContentTest(unittest.TestCase):
    def test_query_is_echoed_in_the_input_row(self):
        _, rows = draw(110, 20, "week")
        self.assertIn("week", rows[0])

    def test_placeholder_appears_only_when_empty(self):
        _, empty = draw(110, 20)
        _, typed = draw(110, 20, "week")
        self.assertIn("jump to a tab", empty[0])
        self.assertNotIn("jump to a tab", typed[0])

    def test_selected_row_is_marked(self):
        bar, rows = draw(110, 20)
        body = [row for row in rows if "▌" in row]
        self.assertEqual(len(body), 1)
        self.assertIn(bar.selected_item().title[:12], body[0])

    def test_preview_shows_pane_output(self):
        _, rows = draw(140, 20)
        joined = "\n".join(rows)
        self.assertIn("Ready", joined)

    def test_empty_state_when_nothing_matches(self):
        _, rows = draw(110, 20, "zzzqqq")
        self.assertIn("no matches", "\n".join(rows))

    def test_counter_shows_filtered_over_total(self):
        _, rows = draw(110, 20, "week")
        self.assertRegex(rows[-1], r"\d+/\d+")


class RowTest(unittest.TestCase):
    def setUp(self):
        self.theme = Theme()
        self.items = build_items(fixtures.snapshot())

    def test_row_is_padded_to_exactly_the_width(self):
        for width in range(20, 120, 7):
            for item in self.items:
                row = render_row(self.theme, Row(item, ()), width, False, 0, True)
                self.assertEqual(visible_width(row), width, "width %d, %r" % (width, item.title))

    def test_selected_row_is_padded_too(self):
        for width in (30, 60, 100):
            row = render_row(self.theme, Row(self.items[0], ()), width, True, 0, True)
            self.assertEqual(visible_width(row), width)

    def test_wide_characters_do_not_overflow(self):
        item = self.items[0]
        item.title = "日本語のタブ名前がとても長い場合のテスト" * 3
        for width in (24, 40, 80):
            row = render_row(self.theme, Row(item, ()), width, False, 0, True)
            self.assertEqual(visible_width(row), width)

    def test_highlights_stay_inside_the_visible_title(self):
        item = self.items[0]
        row = render_row(self.theme, Row(item, (0, 1, 2)), 60, False, 0, True)
        self.assertEqual(visible_width(row), 60)

    def test_spinner_advances_for_working_rows(self):
        working = next(item for item in self.items if item.status == "working")
        first = render_row(self.theme, Row(working, ()), 60, False, 0, True)
        second = render_row(self.theme, Row(working, ()), 60, False, 1, True)
        self.assertNotEqual(first, second)


class WidgetTest(unittest.TestCase):
    def test_input_row_fits(self):
        theme = Theme()
        for width in (20, 40, 80, 120):
            row = render_input(theme, "hello", 5, width, "placeholder text", "@ agents")
            self.assertLessEqual(visible_width(row), width)

    def test_footer_drops_hints_before_overflowing(self):
        theme = Theme()
        hints = [("↑↓", "move"), ("⏎", "jump"), ("⇥", "scope"), ("esc", "close")]
        for width in (16, 24, 40, 80):
            row = render_footer(theme, width, hints, "12/34")
            self.assertLessEqual(visible_width(row), width)

    def test_scrollbar_only_when_the_list_overflows(self):
        self.assertEqual(scrollbar_column(5, 10, 0, 10), {})
        marks = scrollbar_column(50, 10, 0, 10)
        self.assertEqual(len(marks), 10)
        self.assertEqual(marks[0], "┃")
        self.assertEqual(marks[9], "│")

    def test_scrollbar_thumb_tracks_the_offset(self):
        bottom = scrollbar_column(50, 10, 40, 10)
        self.assertEqual(bottom[9], "┃")


if __name__ == "__main__":
    unittest.main()
