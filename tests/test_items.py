import unittest

from herdr_bar.items import KIND_AGENT, KIND_SPACE, KIND_TAB, build_items, shorten_path

from . import fixtures


class BuildItemsTest(unittest.TestCase):
    def setUp(self):
        self.items = build_items(fixtures.snapshot())

    def by_kind(self, kind):
        return [item for item in self.items if item.kind == kind]

    def test_one_row_per_agent(self):
        self.assertEqual(len(self.by_kind(KIND_AGENT)), 6)

    def test_only_tabs_without_agents_become_tab_rows(self):
        titles = sorted(item.title for item in self.by_kind(KIND_TAB))
        self.assertEqual(titles, ["logs", "server"])

    def test_workspaces_appear_when_there_is_more_than_one(self):
        self.assertEqual(len(self.by_kind(KIND_SPACE)), 2)

    def test_single_workspace_sessions_have_no_space_rows(self):
        snapshot = fixtures.snapshot()
        snapshot["workspaces"] = snapshot["workspaces"][:1]
        self.assertEqual([i for i in build_items(snapshot) if i.kind == KIND_SPACE], [])

    def test_agent_rows_carry_pane_and_tab_ids(self):
        agent = next(item for item in self.items if item.title == "cards meaningless")
        self.assertEqual(agent.pane_id, "w1:p4")
        self.assertEqual(agent.tab_id, "w1:t4")
        self.assertEqual(agent.agent, "codex")
        self.assertEqual(agent.status, "blocked")
        self.assertEqual(agent.tab_number, 4)

    def test_agent_rows_lead_with_the_tab_name_and_trail_the_summary(self):
        agent = next(item for item in self.items if item.pane_id == "w1:p2")
        self.assertEqual(agent.title, "Redesign week loading screen")
        self.assertEqual(agent.detail, "Start server for viewing")
        self.assertIn("Start server for viewing", agent.fields)

    def test_summary_stands_alone_when_the_tab_has_no_name(self):
        snapshot = fixtures.snapshot()
        snapshot["tabs"][1]["label"] = ""
        agent = next(i for i in build_items(snapshot) if i.pane_id == "w1:p2")
        self.assertEqual(agent.title, "Start server for viewing")
        self.assertEqual(agent.detail, "")

    def test_summary_is_dropped_when_it_repeats_the_tab_name(self):
        agent = next(item for item in self.items if item.pane_id == "w1:p4")
        self.assertEqual(agent.title, "cards meaningless")
        self.assertEqual(agent.detail, "")

    def test_focused_pane_marks_exactly_one_row(self):
        focused = [item for item in self.items if item.focused and item.kind != KIND_SPACE]
        self.assertEqual(len(focused), 1)
        self.assertEqual(focused[0].pane_id, "w1:p3")

    def test_named_agents_keep_their_name(self):
        named = next(item for item in self.items if item.agent_name)
        self.assertEqual(named.agent_name, "battery")
        self.assertIn("battery", named.fields)

    def test_fields_start_with_the_title_and_have_no_blanks(self):
        for item in self.items:
            self.assertEqual(item.fields[0], item.title)
            self.assertTrue(all(field for field in item.fields))

    def test_status_ranking_puts_blocked_first(self):
        ranks = {item.status: item.status_rank for item in self.items}
        self.assertLess(ranks["blocked"], ranks["done"])
        self.assertLess(ranks["done"], ranks["working"])
        self.assertLess(ranks["working"], ranks["idle"])

    def test_missing_records_do_not_raise(self):
        self.assertEqual(build_items({}), [])
        self.assertEqual(build_items({"tabs": [{}], "agents": [{}]}), [])

    def test_control_characters_are_stripped_from_titles(self):
        snapshot = fixtures.snapshot()
        snapshot["tabs"][0]["label"] = "ser\x1b[31mver\x07"
        title = next(
            item.title for item in build_items(snapshot) if item.tab_id == "w1:t1"
        )
        self.assertNotIn("\x1b", title)
        self.assertNotIn("\x07", title)


class ShortenPathTest(unittest.TestCase):
    def test_replaces_home_with_tilde(self):
        import os

        home = os.path.expanduser("~")
        self.assertEqual(shorten_path(home + "/code"), "~/code")

    def test_leaves_other_paths_alone(self):
        self.assertEqual(shorten_path("/etc/hosts"), "/etc/hosts")

    def test_handles_missing_values(self):
        self.assertEqual(shorten_path(None), "")


if __name__ == "__main__":
    unittest.main()
