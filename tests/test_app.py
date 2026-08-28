import unittest

from herdr_bar.app import Bar, jump, score_item
from herdr_bar.client import HerdrError
from herdr_bar.config import Config
from herdr_bar.items import KIND_AGENT, KIND_PANE, KIND_SPACE, KIND_TAB
from herdr_bar.keys import Event, KeyDecoder
from herdr_bar.mru import Recents
from herdr_bar.theme import Theme

from . import fixtures


class FakeClient(object):
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or fixtures.snapshot()
        self.calls = []
        self.fail = False
        self.close_error = None
        self.rename_error = None
        self.ages = dict(fixtures.PANE_AGES)

    def snapshot(self):
        if self.fail:
            raise HerdrError("herdr is not running")
        return self._snapshot

    def read_pane(self, pane_id, lines, source="visible"):
        self.calls.append(("read", pane_id))
        return fixtures.PREVIEW_TEXT

    def pane_age(self, pane_id):
        self.calls.append(("age", pane_id))
        return self.ages.get(pane_id)

    def close_tab(self, tab_id):
        self.calls.append(("close", tab_id))
        if self.close_error:
            raise HerdrError(self.close_error)
        snapshot = dict(self._snapshot)
        for key in ("tabs", "agents", "panes"):
            snapshot[key] = [
                record for record in snapshot[key] if record.get("tab_id") != tab_id
            ]
        self._snapshot = snapshot

    def rename_tab(self, tab_id, label):
        self.calls.append(("rename", tab_id, label))
        if self.rename_error:
            raise HerdrError(self.rename_error)
        for tab in self._snapshot["tabs"]:
            if tab.get("tab_id") == tab_id:
                tab["label"] = label

    def focus_workspace(self, workspace_id):
        self.calls.append(("workspace", workspace_id))

    def focus_tab(self, tab_id):
        self.calls.append(("tab", tab_id))

    def focus_agent(self, target):
        self.calls.append(("agent", target))

    def focus_pane(self, pane_id):
        self.calls.append(("pane", pane_id))


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

    def test_positions_are_keyed_by_the_field_that_won_the_term(self):
        _, positions = score_item(["codex"], ("retry queue backoff", "codex"))
        self.assertEqual(positions, {1: (0, 1, 2, 3, 4)})

    def test_each_term_highlights_its_own_field(self):
        _, positions = score_item(["retry", "codex"], ("retry queue", "codex"))
        self.assertEqual(sorted(positions), [0, 1])

    def test_title_matches_outrank_field_matches(self):
        title_hit, _ = score_item(["retry"], ("retry queue", "other"))
        field_hit, _ = score_item(["retry"], ("something else", "retry"))
        self.assertGreater(title_hit, field_hit)


class FilterTest(unittest.TestCase):
    def test_empty_query_lists_regular_items_and_named_panes(self):
        instance = bar()
        self.assertEqual(len(instance.rows), len(instance.items) + len(instance.pane_items))
        self.assertIn("server logs", titles(instance))

    def test_typing_filters_the_list(self):
        instance = bar()
        instance.set_query("retry")
        self.assertEqual(titles(instance)[0], "retry queue backoff")

    def test_everything_search_includes_named_panes(self):
        instance = bar()
        instance.set_query("server logs")
        self.assertEqual(titles(instance)[0], "server logs")
        self.assertEqual(instance.rows[0].item.kind, KIND_PANE)

    def test_named_current_pane_does_not_replace_the_current_agent_row(self):
        snapshot = fixtures.snapshot()
        focused = next(pane for pane in snapshot["panes"] if pane["pane_id"] == "w1:p3")
        focused["label"] = "current editor"
        instance = bar(client=FakeClient(snapshot))
        keys = [row.item.key for row in instance.rows]
        self.assertIn("w1:p3", keys)
        self.assertIn("pane:w1:p3", keys)

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

    def test_pane_sigil_shows_only_named_panes(self):
        instance = bar()
        instance.insert("%")
        self.assertEqual(instance.scope, "pane")
        self.assertTrue(all(row.item.kind == KIND_PANE for row in instance.rows))
        self.assertEqual(sorted(titles(instance)), ["server logs", "web server"])

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
        instance.cycle_scope(1)
        self.assertEqual(instance.scope, "pane")
        instance.cycle_scope(1)
        self.assertEqual(instance.scope, "tab")
        instance.cycle_scope(-1)
        self.assertEqual(instance.scope, "pane")


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

    def test_preview_false_is_a_starting_point_not_a_lock(self):
        instance = bar(config=Config({"preview": False}))
        self.assertFalse(instance.preview_enabled)
        press(instance, b"\x0f")
        self.assertTrue(instance.preview_enabled)


class ConfigPreviewTest(unittest.TestCase):
    def test_false_hides_the_preview_to_begin_with(self):
        config = Config({"preview": False})
        self.assertFalse(config.preview_starts_on())
        self.assertTrue(config.fits_preview(120, chosen=True))

    def test_auto_waits_for_a_wide_popup_unless_asked(self):
        config = Config()
        self.assertTrue(config.preview_starts_on())
        self.assertFalse(config.fits_preview(88))
        self.assertTrue(config.fits_preview(88, chosen=True))
        self.assertTrue(config.fits_preview(120))

    def test_true_keeps_the_preview_in_narrower_popups(self):
        config = Config({"preview": True})
        self.assertTrue(config.preview_starts_on())
        self.assertTrue(config.fits_preview(70))
        self.assertFalse(config.fits_preview(40))


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

    def test_pane_rows_focus_the_tab_and_exact_pane(self):
        client = FakeClient()
        instance = bar(client=client)
        instance.insert("%")
        item = next(item for item in instance.rows if item.item.pane_id == "w1:p7").item
        jump(client, item)
        self.assertEqual(client.calls, [("tab", "w1:t1"), ("pane", "w1:p7")])


def escape(instance):
    decoder = KeyDecoder()
    decoder.feed(b"\x1b")
    return instance._consume(decoder.flush())


DELETE = b"\x1b[3~"


class CloseTest(unittest.TestCase):
    def closes(self, client):
        return [call for call in client.calls if call[0] == "close"]

    def test_delete_arms_a_confirmation_instead_of_closing(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, DELETE)
        self.assertIsNotNone(instance.pending_close)
        self.assertEqual(self.closes(client), [])

    def test_enter_closes_the_tab_behind_the_row(self):
        client = FakeClient()
        instance = bar(client=client)
        tab_id = instance.selected_item().tab_id
        press(instance, DELETE)
        self.assertIsNone(press(instance, b"\r"))  # confirming is not jumping
        self.assertEqual(self.closes(client), [("close", tab_id)])
        self.assertIsNone(instance.pending_close)

    def test_backspace_arms_it_too_once_it_has_nothing_to_erase(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, b"\x7f")
        self.assertIsNotNone(instance.pending_close)
        self.assertTrue(instance.backspace_closes())

    def test_backspace_erases_the_query_before_it_closes_anything(self):
        instance = bar()
        press(instance, b"week")
        press(instance, b"\x7f")
        self.assertEqual(instance.query, "wee")
        self.assertIsNone(instance.pending_close)
        self.assertFalse(instance.backspace_closes())

    def test_backspace_clears_the_filter_before_it_closes_anything(self):
        instance = bar()
        press(instance, b"@")
        press(instance, b"\x7f")
        self.assertEqual(instance.scope, "all")
        self.assertIsNone(instance.pending_close)
        press(instance, b"\x7f")
        self.assertIsNotNone(instance.pending_close)

    def test_a_held_delete_key_cannot_confirm_itself(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, DELETE, DELETE)
        self.assertIsNone(instance.pending_close)
        self.assertEqual(self.closes(client), [])
        press(instance, b"\x7f", b"\x7f")
        self.assertIsNone(instance.pending_close)
        self.assertEqual(self.closes(client), [])

    def test_y_confirms_too(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, DELETE, b"y")
        self.assertEqual(len(self.closes(client)), 1)

    def test_escape_cancels_the_close_and_leaves_the_bar_open(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, DELETE)
        self.assertIsNone(escape(instance))
        self.assertIsNone(instance.pending_close)
        self.assertEqual(self.closes(client), [])

    def test_any_other_key_cancels_and_is_swallowed(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, DELETE, b"n")
        self.assertIsNone(instance.pending_close)
        self.assertEqual(instance.query, "")
        self.assertEqual(self.closes(client), [])

    def test_ctrl_c_still_leaves(self):
        instance = bar()
        press(instance, DELETE)
        self.assertEqual(press(instance, b"\x03"), "cancel")
        self.assertIsNone(instance.pending_close)

    def test_the_closed_row_disappears(self):
        client = FakeClient()
        instance = bar(client=client)
        title = instance.selected_item().title
        press(instance, DELETE, b"\r")
        self.assertNotIn(title, titles(instance))
        self.assertLess(instance.selected, len(instance.rows))

    def test_the_confirmation_counts_the_agents_going_with_the_tab(self):
        snapshot = fixtures.snapshot()
        second = dict(snapshot["agents"][0])
        second.update({"pane_id": "w1:p7", "terminal_title_stripped": "second agent"})
        snapshot["agents"].append(second)
        snapshot["panes"].append(second)
        instance = bar(client=FakeClient(snapshot))
        instance.selected = next(
            index for index, row in enumerate(instance.rows) if row.item.pane_id == "w1:p7"
        )
        press(instance, DELETE)
        self.assertEqual(instance.pending_close[0], "w1:t2")
        self.assertEqual(instance.pending_close[2], 2)

    def test_workspace_rows_cannot_be_closed(self):
        client = FakeClient()
        instance = bar(client=client)
        instance.selected = next(
            index for index, row in enumerate(instance.rows) if row.item.kind == KIND_SPACE
        )
        press(instance, DELETE)
        self.assertIsNone(instance.pending_close)
        self.assertIsNotNone(instance.status)
        self.assertEqual(self.closes(client), [])

    def test_a_refused_close_flashes_the_reason(self):
        client = FakeClient()
        client.close_error = "tab is busy"
        instance = bar(client=client)
        press(instance, DELETE, b"\r")
        self.assertIn("busy", instance.status)

    def test_ctrl_d_forward_deletes_in_the_query(self):
        instance = bar()
        press(instance, b"week")
        press(instance, b"\x1b[D\x1b[D")  # left, left
        press(instance, b"\x04")
        self.assertEqual(instance.query, "wek")


class RenameTest(unittest.TestCase):
    def renames(self, client):
        return [call for call in client.calls if call[0] == "rename"]

    def test_ctrl_r_opens_an_editor_for_the_selected_tabs_name(self):
        instance = bar()
        selected = instance.selected_item()
        press(instance, b"\x12")
        self.assertEqual(instance.pending_rename, (selected.tab_id, selected.title))
        self.assertEqual(instance.rename_text, "")

    def test_enter_renames_the_tab_behind_an_agent_row(self):
        client = FakeClient()
        instance = bar(client=client)
        tab_id = instance.selected_item().tab_id
        press(instance, b"\x12", b"new panel name", b"\r")
        self.assertEqual(self.renames(client), [("rename", tab_id, "new panel name")])
        self.assertIsNone(instance.pending_rename)
        self.assertIn("new panel name", titles(instance))

    def test_escape_keeps_the_old_name_and_the_bar_open(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, b"\x12", b"replacement")
        self.assertIsNone(escape(instance))
        self.assertIsNone(instance.pending_rename)
        self.assertEqual(self.renames(client), [])

    def test_empty_enter_keeps_the_old_name(self):
        client = FakeClient()
        instance = bar(client=client)
        press(instance, b"\x12", b"\r")
        self.assertIsNone(instance.pending_rename)
        self.assertEqual(self.renames(client), [])
        self.assertIn("unchanged", instance.status)

    def test_rename_editor_supports_cursor_edits_and_paste(self):
        client = FakeClient()
        instance = bar(client=client)
        press(
            instance,
            b"\x12",
            b"\x1b[200~panel nme\x1b[201~",
            b"\x1b[D\x1b[D",
            b"a",
            b"\r",
        )
        self.assertEqual(self.renames(client)[0][2], "panel name")

    def test_rename_failure_stays_in_the_editor(self):
        client = FakeClient()
        client.rename_error = "name rejected"
        instance = bar(client=client)
        press(instance, b"\x12", b"bad name", b"\r")
        self.assertIsNotNone(instance.pending_rename)
        self.assertEqual(instance.rename_text, "bad name")
        self.assertIn("rejected", instance.status)

    def test_workspace_rows_cannot_be_renamed(self):
        client = FakeClient()
        instance = bar(client=client)
        instance.selected = next(
            index for index, row in enumerate(instance.rows) if row.item.kind == KIND_SPACE
        )
        press(instance, b"\x12")
        self.assertIsNone(instance.pending_rename)
        self.assertIn("tabs and agents", instance.status)

    def test_ctrl_c_during_rename_still_leaves_the_bar(self):
        instance = bar()
        press(instance, b"\x12")
        self.assertEqual(press(instance, b"\x03"), "cancel")
        self.assertIsNone(instance.pending_rename)


class AgeTest(unittest.TestCase):
    def reads(self, client):
        return [call for call in client.calls if call[0] == "age"]

    def item(self, instance, pane_id):
        return next(item for item in instance.items if item.pane_id == pane_id)

    def test_a_pane_is_read_once_and_then_ticked_locally(self):
        client = FakeClient()
        instance = bar(client=client)
        item = self.item(instance, "w1:p3")
        self.assertIsNone(instance.age_of(item))  # queued, not read yet
        instance.pump_ages()
        self.assertAlmostEqual(
            instance.age_of(item), fixtures.PANE_AGES["w1:p3"], delta=1.0
        )
        instance.pump_ages()
        self.assertEqual(self.reads(client), [("age", "w1:p3")])

    def test_only_one_pane_is_read_per_pass(self):
        client = FakeClient()
        instance = bar(client=client)
        for item in instance.items:
            instance.age_of(item)
        instance.pump_ages()
        self.assertEqual(len(self.reads(client)), 1)

    def test_an_undatable_pane_is_not_asked_twice(self):
        client = FakeClient()
        client.ages = {}
        instance = bar(client=client)
        item = self.item(instance, "w1:p3")
        instance.age_of(item)
        instance.pump_ages()
        self.assertIsNone(instance.age_of(item))
        instance.pump_ages()
        self.assertEqual(len(self.reads(client)), 1)

    def test_a_failing_read_is_not_fatal(self):
        client = FakeClient()

        def boom(pane_id):
            raise HerdrError("herdr is not running")

        client.pane_age = boom
        instance = bar(client=client)
        item = self.item(instance, "w1:p3")
        instance.age_of(item)
        instance.pump_ages()
        self.assertIsNone(instance.age_of(item))

    def test_workspace_rows_have_no_running_time(self):
        instance = bar()
        item = next(item for item in instance.items if item.kind == KIND_SPACE)
        self.assertIsNone(instance.age_of(item))
        self.assertEqual(instance._age_queue, [])


class PreviewTest(unittest.TestCase):
    def test_preview_is_debounced_then_cached(self):
        client = FakeClient()
        instance = bar(client=client)
        item = next(item for item in instance.items if item.pane_id)
        lines, loading = instance.preview_lines(item)
        self.assertTrue(loading)
        self.assertEqual(lines, [])
        instance._preview_pending = (item.pane_id, 0.0)
        instance.pump_preview(10)
        lines, loading = instance.preview_lines(item)
        self.assertFalse(loading)
        self.assertIn("Ready", "\n".join(lines))
        instance.pump_preview(10)
        self.assertEqual(len([call for call in client.calls if call[0] == "read"]), 1)

    def test_workspace_rows_have_no_preview(self):
        instance = bar()
        item = next(item for item in instance.items if item.kind == KIND_SPACE)
        self.assertEqual(instance.preview_lines(item), ([], False))


if __name__ == "__main__":
    unittest.main()
