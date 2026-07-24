import unittest

from herdr_bar.app import Bar, jump, score_item
from herdr_bar.client import HerdrError
from herdr_bar.config import Config
from herdr_bar.items import KIND_AGENT, KIND_SPACE, KIND_TAB
from herdr_bar.keys import Event, KeyDecoder
from herdr_bar.mru import Recents
from herdr_bar.theme import Theme

from . import fixtures


class FakeClient(object):
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or fixtures.snapshot()
        self.calls = []
        self.fail = False

    def snapshot(self):
        if self.fail:
            raise HerdrError("herdr is not running")
        return self._snapshot

    def read_pane(self, pane_id, lines, source="visible"):
        self.calls.append(("read", pane_id))
        return fixtures.PREVIEW_TEXT

    def focus_workspace(self, workspace_id):
        self.calls.append(("workspace", workspace_id))

    def focus_tab(self, tab_id):
        self.calls.append(("tab", tab_id))

    def focus_agent(self, target):
        self.calls.append(("agent", target))


def bar(recents=None, client=None, config=None):
    instance = Bar(
        client or FakeClient(), config or Config(), recents or Recents(None), Theme()
    )
    instance.bootstrap()
    return instance


def titles(instance):
    return [row.item.title for row in instance.rows]


def press(instance, *keys):
    decoder = KeyDecoder()
    outcome = None
    for key in keys:
        outcome = instance._consume(decoder.feed(key)) or outcome
    return outcome


class ScoreItemTest(unittest.TestCase):
    def test_terms_may_match_different_fields(self):
        found = score_item(["retry", "codex"], ("retry queue backoff", "erestor", "codex"))
        self.assertIsNotNone(found)

    def test_a_term_matching_nothing_rejects_the_row(self):
        self.assertIsNone(score_item(["nope"], ("retry queue", "erestor")))

    def test_only_title_matches_produce_highlights(self):
        _, positions = score_item(["codex"], ("retry queue backoff", "codex"))
        self.assertEqual(positions, ())

    def test_title_matches_outrank_field_matches(self):
        title_hit, _ = score_item(["retry"], ("retry queue", "other"))
        field_hit, _ = score_item(["retry"], ("something else", "retry"))
        self.assertGreater(title_hit, field_hit)


class FilterTest(unittest.TestCase):
    def test_empty_query_lists_everything(self):
        instance = bar()
        self.assertEqual(len(instance.rows), len(instance.items))

    def test_typing_filters_the_list(self):
        instance = bar()
        instance.set_query("retry")
        self.assertEqual(titles(instance)[0], "retry queue backoff")

    def test_search_reaches_the_working_directory(self):
        instance = bar()
        instance.set_query("worktrees")
        self.assertIn("library depth battery", titles(instance))

    def test_search_reaches_the_agent_kind(self):
        instance = bar()
        instance.set_query("gemini")
        self.assertEqual(titles(instance), ["migrations"])

    def test_terms_are_combined_with_and(self):
        instance = bar()
        instance.set_query("cards codex")
        self.assertEqual(titles(instance), ["cards meaningless"])

    def test_current_row_is_never_first_at_rest(self):
        instance = bar()
        self.assertTrue(instance.rows[0].item.key != instance.current_key)
        self.assertIn(instance.current_key, [row.item.key for row in instance.rows])

    def test_recents_float_to_the_top(self):
        recents = Recents(None)
        recents.touch("w1:p5", "library depth battery")
        instance = bar(recents=recents)
        self.assertEqual(titles(instance)[0], "library depth battery")

    def test_current_row_sits_right_after_the_recents(self):
        recents = Recents(None)
        recents.touch("w1:p5")
        instance = bar(recents=recents)
        self.assertEqual(instance.rows[1].item.key, instance.current_key)


class ScopeTest(unittest.TestCase):
    def test_sigil_sets_the_scope_without_entering_text(self):
        instance = bar()
        instance.insert("@")
        self.assertEqual(instance.scope, "agent")
        self.assertEqual(instance.query, "")
        self.assertTrue(all(row.item.kind == KIND_AGENT for row in instance.rows))

    def test_shell_sigil_shows_tabs_without_agents(self):
        instance = bar()
        instance.insert("$")
        self.assertEqual(sorted(titles(instance)), ["logs", "server"])

    def test_needs_you_sigil_shows_blocked_and_done(self):
        instance = bar()
        instance.insert("!")
        self.assertTrue(all(row.item.status in ("blocked", "done") for row in instance.rows))

    def test_sigil_is_literal_once_a_query_exists(self):
        instance = bar()
        instance.insert("a")
        instance.insert("@")
        self.assertEqual(instance.query, "a@")
        self.assertEqual(instance.scope, "all")

    def test_backspace_on_an_empty_query_clears_the_scope(self):
        instance = bar()
        instance.insert("@")
        instance.backspace()
        self.assertEqual(instance.scope, "all")

    def test_tab_cycles_scopes_both_ways(self):
        instance = bar()
        instance.cycle_scope(1)
        self.assertEqual(instance.scope, "agent")
        instance.cycle_scope(-1)
        self.assertEqual(instance.scope, "all")


class KeyTest(unittest.TestCase):
    def test_arrows_move_the_selection(self):
        instance = bar()
        press(instance, b"\x1b[B")
        self.assertEqual(instance.selected, 1)
        press(instance, b"\x1b[A")
        self.assertEqual(instance.selected, 0)

    def test_selection_wraps(self):
        instance = bar()
        press(instance, b"\x1b[A")
        self.assertEqual(instance.selected, len(instance.rows) - 1)

    def test_emacs_movement_keys(self):
        instance = bar()
        press(instance, b"\x0e\x0e")  # ctrl+n twice
        self.assertEqual(instance.selected, 2)
        press(instance, b"\x10")  # ctrl+p
        self.assertEqual(instance.selected, 1)

    def test_typing_and_editing(self):
        instance = bar()
        press(instance, b"retry")
        self.assertEqual(instance.query, "retry")
        press(instance, b"\x7f")
        self.assertEqual(instance.query, "retr")
        press(instance, b"\x15")
        self.assertEqual(instance.query, "")

    def test_delete_word(self):
        instance = bar()
        press(instance, b"week gen")
        press(instance, b"\x17")
        self.assertEqual(instance.query, "week ")

    def test_cursor_editing_inserts_in_place(self):
        instance = bar()
        press(instance, b"week")
        press(instance, b"\x1b[D\x1b[D")  # left, left
        press(instance, b"X")
        self.assertEqual(instance.query, "weXek")

    def test_enter_confirms_and_escape_cancels(self):
        instance = bar()
        self.assertEqual(press(instance, b"\r"), "confirm")
        decoder = KeyDecoder()
        decoder.feed(b"\x1b")
        self.assertEqual(instance._consume(decoder.flush()), "cancel")

    def test_paste_becomes_query_text(self):
        instance = bar()
        press(instance, b"\x1b[200~retry queue\x1b[201~")
        self.assertEqual(instance.query, "retry queue")

    def test_enter_does_nothing_when_the_list_is_empty(self):
        instance = bar()
        instance.set_query("zzzqqq")
        self.assertIsNone(press(instance, b"\r"))

    def test_preview_toggle(self):
        instance = bar()
        before = instance.preview_enabled
        press(instance, b"\x0f")
        self.assertNotEqual(before, instance.preview_enabled)


class MouseTest(unittest.TestCase):
    def test_wheel_scrolls_the_selection(self):
        instance = bar()
        instance._list_top = 2
        instance._list_height = 8
        instance.handle_mouse(Event("mouse", "wheel_down", 5, 5))
        self.assertEqual(instance.selected, 3)

    def test_click_selects_then_confirms(self):
        instance = bar()
        instance._list_top = 2
        instance._list_height = 8
        first = instance.handle_mouse(Event("mouse", "press", 5, 5))
        self.assertIsNone(first)
        self.assertEqual(instance.selected, 2)
        second = instance.handle_mouse(Event("mouse", "press", 5, 5))
        self.assertEqual(second, "confirm")

    def test_clicks_outside_the_list_are_ignored(self):
        instance = bar()
        instance._list_top = 2
        instance._list_height = 8
        self.assertIsNone(instance.handle_mouse(Event("mouse", "press", 5, 1)))
        self.assertEqual(instance.selected, 0)


class RefreshTest(unittest.TestCase):
    def test_selection_survives_a_refresh(self):
        client = FakeClient()
        instance = bar(client=client)
        instance.selected = 3
        keep = instance.selected_item().key
        instance.refresh()
        self.assertEqual(instance.selected_item().key, keep)

    def test_a_dead_server_flashes_instead_of_raising(self):
        client = FakeClient()
        instance = bar(client=client)
        client.fail = True
        instance.refresh()
        self.assertIsNotNone(instance.status)
        self.assertTrue(instance.rows)

    def test_closed_rows_disappear(self):
        client = FakeClient()
        instance = bar(client=client)
        snapshot = fixtures.snapshot()
        snapshot["agents"] = snapshot["agents"][:1]
        client._snapshot = snapshot
        instance.refresh()
        self.assertEqual(len([r for r in instance.rows if r.item.kind == KIND_AGENT]), 1)


class JumpTest(unittest.TestCase):
    def test_agent_rows_focus_the_tab_and_the_pane(self):
        client = FakeClient()
        instance = bar(client=client)
        item = next(item for item in instance.items if item.title == "cards meaningless")
        jump(client, item)
        self.assertEqual(client.calls, [("tab", "w1:t4"), ("agent", "w1:p4")])

    def test_tab_rows_only_focus_the_tab(self):
        client = FakeClient()
        instance = bar(client=client)
        item = next(item for item in instance.items if item.kind == KIND_TAB)
        jump(client, item)
        self.assertEqual(client.calls, [("tab", item.tab_id)])

    def test_workspace_rows_focus_the_workspace(self):
        client = FakeClient()
        instance = bar(client=client)
        item = next(item for item in instance.items if item.kind == KIND_SPACE)
        jump(client, item)
        self.assertEqual(client.calls, [("workspace", item.workspace_id)])


class PreviewTest(unittest.TestCase):
    def test_preview_is_debounced_then_cached(self):
        client = FakeClient()
        instance = bar(client=client)
        item = next(item for item in instance.items if item.pane_id)
        lines, loading = instance.preview_lines(item, 10)
        self.assertTrue(loading)
        self.assertEqual(lines, [])
        instance._preview_pending = (item.pane_id, 0.0)
        instance.pump_preview(10)
        lines, loading = instance.preview_lines(item, 10)
        self.assertFalse(loading)
        self.assertIn("Ready", "\n".join(lines))
        instance.pump_preview(10)
        self.assertEqual(len([call for call in client.calls if call[0] == "read"]), 1)

    def test_workspace_rows_have_no_preview(self):
        instance = bar()
        item = next(item for item in instance.items if item.kind == KIND_SPACE)
        self.assertEqual(instance.preview_lines(item, 10), ([], False))


if __name__ == "__main__":
    unittest.main()
