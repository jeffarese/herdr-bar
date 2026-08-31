import re
import unittest

from herdr_bar.app import Bar, score_item
from herdr_bar.config import Config
from herdr_bar.fuzzy import split_query
from herdr_bar.items import build_items
from herdr_bar.keys import KeyDecoder
from herdr_bar.mru import Recents
from herdr_bar.render import (
    Row,
    compute_layout,
    render_confirm,
    render_empty_state,
    render_footer,
    render_input,
    render_row,
    scrollbar_column,
)
from herdr_bar.textutil import strip_ansi, visible_width
from herdr_bar.theme import Theme

from . import fixtures

ROW_RE = re.compile(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|\x1b\[J|$)", re.S)


class FakeClient(object):
    def snapshot(self):
        return fixtures.snapshot()

    def read_pane(self, pane_id, lines, source="visible"):
        return fixtures.PREVIEW_TEXT

    def pane_age(self, pane_id):
        return fixtures.PANE_AGES.get(pane_id)

    def close_tab(self, tab_id):
        pass


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


def settle(bar, terminal):
    bar.draw(terminal)
    if bar._preview_pending:  # skip the debounce the draw just armed
        bar._preview_pending = (bar._preview_pending[0], 0.0)
    bar.pump_preview(bar._list_height)
    while bar._age_queue:  # the loop would spread these over a few frames
        bar.pump_ages()


def draw(width, height, query="", scope="all", config=None, confirm=False, keys=b""):
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
    settle(bar, terminal)
    if keys:  # the real loop always paints a frame before it reads a key
        bar._consume(KeyDecoder().feed(keys))
        settle(bar, terminal)
    if confirm:
        bar.request_close()
    terminal.frames = []
    bar.invalidate()  # these tests read whole rows, so ask for a whole frame
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

    def test_an_armed_close_never_overflows_either(self):
        for width, height in SIZES:
            _, rows = draw(width, height, confirm=True)
            for index, row in enumerate(rows):
                self.assertLessEqual(
                    visible_width(row),
                    width,
                    "row %d overflows at %dx%d: %r" % (index, width, height, row),
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

    def test_top_bar_highlights_the_active_filter(self):
        _, all_rows = draw(110, 20)
        _, agent_rows = draw(110, 20, scope="agent")
        accent = Theme().fg("accent")
        self.assertIn(accent + " everything ", all_rows[0])
        self.assertIn("everything", strip_ansi(all_rows[0]))
        self.assertIn(accent + " @ agents ", agent_rows[0])
        self.assertIn("@ agents", strip_ansi(agent_rows[0]))

    def test_scoped_empty_state_names_the_active_filter(self):
        for filter_name in ("@ agents", "$ shells", "! needs you"):
            theme = Theme()
            lines = render_empty_state(theme, 60, "", True, filter_name)
            rendered = "\n".join(lines)
            message = strip_ansi(rendered)
            self.assertIn("nothing in %s filter" % filter_name, message)
            self.assertNotIn("this filter", message)
            self.assertNotIn("\x1b[7m", rendered)
            highlight_start = rendered.index(theme.fg("accent"))
            highlight_end = rendered.index("\x1b[0m", highlight_start)
            self.assertIn(filter_name, rendered[highlight_start:highlight_end])
            self.assertIn(" filter", rendered[highlight_end:])

    def test_selected_row_is_marked(self):
        bar, rows = draw(110, 20)
        body = [row for row in rows if "▌" in row]
        self.assertEqual(len(body), 1)
        self.assertIn(bar.selected_item().title[:12], body[0])

    def test_preview_shows_pane_output(self):
        _, rows = draw(140, 20)
        joined = "\n".join(rows)
        self.assertIn("Ready", joined)

    def test_preview_false_hides_the_preview_but_ctrl_o_brings_it_back(self):
        hidden = Config({"preview": False})
        _, off = draw(140, 20, config=hidden)
        self.assertNotIn("Ready", "\n".join(off))
        _, on = draw(140, 20, config=hidden, keys=b"\x0f")
        self.assertIn("Ready", "\n".join(on))

    def test_ctrl_o_opens_the_preview_in_a_popup_auto_thinks_is_too_narrow(self):
        _, off = draw(90, 20)
        self.assertNotIn("Ready", "\n".join(off))
        _, on = draw(90, 20, keys=b"\x0f")
        self.assertIn("Ready", "\n".join(on))

    def test_ctrl_o_hides_a_preview_that_is_showing(self):
        _, rows = draw(140, 20, keys=b"\x0f")
        self.assertNotIn("Ready", "\n".join(rows))

    def test_empty_state_when_nothing_matches(self):
        _, rows = draw(110, 20, "zzzqqq")
        self.assertIn("no matches", "\n".join(rows))

    def test_pane_filter_shows_its_name_and_named_panes(self):
        _, rows = draw(110, 20, scope="pane")
        plain = strip_ansi("\n".join(rows))
        self.assertIn("% panes", plain)
        self.assertIn("server logs", plain)

    def test_counter_shows_filtered_over_total(self):
        _, rows = draw(110, 20, "week")
        self.assertRegex(rows[-1], r"\d+/\d+")

    def test_rows_carry_their_running_time(self):
        _, rows = draw(140, 20)
        self.assertRegex(strip_ansi("\n".join(rows)), r"\d+h\d\dm")

    def test_the_footer_names_the_delete_key_that_works(self):
        _, empty = draw(110, 20)
        _, typed = draw(110, 20, "week")
        self.assertIn("⌫ close", strip_ansi(empty[-1]))
        self.assertIn("⌦ close", strip_ansi(typed[-1]))

    def test_an_armed_close_replaces_the_hints(self):
        bar, rows = draw(110, 20, confirm=True)
        footer = strip_ansi(rows[-1])
        self.assertIn("close", footer)
        self.assertIn(bar.pending_close[1][:12], footer)
        self.assertNotIn("scope", footer)


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

    def test_a_summary_match_is_highlighted_where_the_summary_is_drawn(self):
        item = next(i for i in self.items if i.detail)
        terms = split_query(item.detail.split()[0])
        found = score_item(terms, item.fields)
        self.assertIsNotNone(found)
        row = Row(item, found[1].get(0, ()), found[1])
        self.assertIn("\x1b[4m", render_row(self.theme, row, 120, False, 0, True))

    def test_a_meta_match_is_highlighted_and_the_row_keeps_its_width(self):
        item = next(i for i in self.items if i.agent_name)
        found = score_item(split_query(item.agent_name), item.fields)
        self.assertIsNotNone(found)
        row = Row(item, found[1].get(0, ()), found[1])
        rendered = render_row(self.theme, row, 120, False, 0, True)
        self.assertIn("\x1b[4m" + self.theme.fg("match") + item.agent_name, rendered)
        for width in range(20, 130, 3):
            self.assertEqual(
                visible_width(render_row(self.theme, row, width, False, 0, True, 90.0)),
                width,
                "width %d" % width,
            )

    def test_matched_characters_are_underlined_and_the_rest_is_not(self):
        item = self.items[0]
        item.title = "week loading"
        row = render_row(self.theme, Row(item, (0, 1, 2, 3)), 60, False, 0, True)
        self.assertIn("\x1b[4m" + self.theme.fg("match") + "week", row)
        self.assertNotIn("\x1b[4m" + self.theme.fg("match") + " loading", row)
        self.assertEqual(visible_width(row), 60)

    def test_the_summary_follows_the_tab_name_when_there_is_room(self):
        item = next(i for i in self.items if i.detail)
        plain = strip_ansi(render_row(self.theme, Row(item, ()), 120, False, 0, True))
        self.assertIn(item.title + " — " + item.detail, plain)

    def test_a_narrow_row_drops_the_summary_and_keeps_its_width(self):
        item = next(i for i in self.items if i.detail)
        for width in range(20, 120, 3):
            row = render_row(self.theme, Row(item, ()), width, False, 0, True)
            self.assertEqual(visible_width(row), width, "width %d" % width)
        narrow = strip_ansi(render_row(self.theme, Row(item, ()), 34, False, 0, True))
        self.assertNotIn(item.detail, narrow)

    def test_running_time_joins_the_meta_without_overflowing(self):
        item = self.items[0]
        for width in range(20, 120, 7):
            row = render_row(self.theme, Row(item, ()), width, False, 0, True, age=7440)
            self.assertEqual(visible_width(row), width)
        self.assertIn("2h04m", render_row(self.theme, Row(item, ()), 90, False, 0, True, 7440))

    def test_context_is_not_repeated_when_the_summary_already_shows_it(self):
        item = next(item for item in self.items if item.detail)
        item.detail = "pacebeats"
        item.subtitle = "/Users/dev/workspace/pacebeats"
        item.workspace_label = "pacebeats"
        plain = strip_ansi(render_row(self.theme, Row(item, ()), 120, False, 0, True))
        self.assertEqual(plain.count("pacebeats"), 1)

    def test_context_is_not_repeated_when_it_is_the_title(self):
        item = next(item for item in self.items if item.agent == "codex")
        item.title = "erestor"
        item.detail = ""
        item.subtitle = "/Users/dev/workspace/erestor"
        item.workspace_label = "erestor"
        plain = strip_ansi(render_row(self.theme, Row(item, ()), 120, False, 0, True))
        self.assertEqual(plain.count("erestor"), 1)

    def test_agent_labels_have_distinct_colors(self):
        expected = {
            "claude": "agent_claude",
            "codex": "agent_codex",
            "gemini": "agent_gemini",
            "kimi": "agent_kimi",
        }
        item = next(item for item in self.items if item.agent == "claude" and not item.agent_name)
        for agent, role in expected.items():
            item.agent = agent
            rendered = render_row(self.theme, Row(item, ()), 120, False, 0, True)
            self.assertIn(self.theme.fg(role) + agent, rendered)
        self.assertEqual(len({self.theme.fg(role) for role in expected.values()}), len(expected))

    def test_named_agents_use_their_underlying_agent_color(self):
        item = next(item for item in self.items if item.agent_name)
        rendered = render_row(self.theme, Row(item, ()), 120, False, 0, True)
        self.assertIn(self.theme.fg("agent_claude") + "@battery", rendered)

    def test_a_pane_with_no_reading_shows_no_time(self):
        item = self.items[0]
        plain = strip_ansi(render_row(self.theme, Row(item, ()), 90, False, 0, True))
        self.assertNotRegex(plain, r"\d+[hms]\b")

    def test_spinner_advances_for_working_rows(self):
        working = next(item for item in self.items if item.status == "working")
        first = render_row(self.theme, Row(working, ()), 60, False, 0, True)
        second = render_row(self.theme, Row(working, ()), 60, False, 1, True)
        self.assertNotEqual(first, second)


class ThemeTest(unittest.TestCase):
    def test_secondary_text_is_brighter_than_the_separators(self):
        for appearance in (None, "dark", "light"):
            theme = Theme(appearance=appearance)
            self.assertNotEqual(theme.fg("muted"), theme.fg("unknown"), repr(appearance))

    def test_a_configured_muted_wins_over_the_appearance(self):
        theme = Theme({"muted": "#ff00ff"}, appearance="dark")
        self.assertEqual(theme.fg("muted"), "\x1b[38;2;255;0;255m")


class WidgetTest(unittest.TestCase):
    def test_input_row_fits(self):
        theme = Theme()
        for width in (20, 40, 80, 120):
            row = render_input(theme, "hello", 5, width, "placeholder text", "@ agents")
            self.assertLessEqual(visible_width(row), width)

    def test_rename_prompt_and_hint_fit(self):
        theme = Theme()
        for width in (20, 40, 80, 120):
            row = render_input(
                theme,
                "new name",
                8,
                width,
                "old name",
                "⏎ save · esc keep",
                "rename ❯",
            )
            self.assertLessEqual(visible_width(row), width)
            self.assertIn("rename", strip_ansi(row))

    def test_footer_drops_hints_before_overflowing(self):
        theme = Theme()
        hints = [("↑↓", "move"), ("⏎", "jump"), ("⇥", "scope"), ("esc", "close")]
        for width in (16, 24, 40, 80):
            row = render_footer(theme, width, hints, "12/34")
            self.assertLessEqual(visible_width(row), width)

    def test_confirmation_fits_every_width(self):
        theme = Theme()
        for width in list(range(1, 20)) + [24, 40, 60, 80, 120]:
            for agents in (1, 3):
                row = render_confirm(theme, width, "library depth battery", agents)
                self.assertLessEqual(visible_width(row), width, "width %d" % width)

    def test_confirmation_warns_when_more_than_one_agent_goes(self):
        theme = Theme()
        self.assertIn("3 agents", render_confirm(theme, 80, "server", 3))
        self.assertNotIn("agents", render_confirm(theme, 80, "server", 1))

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
