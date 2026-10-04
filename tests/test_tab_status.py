import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_bar import icons
from herdr_bar.auto_title import AutoTitles
from herdr_bar.client import EventStream, HerdrError
from herdr_bar.config import Config
from herdr_bar.items import build_items
from herdr_bar.tab_status import TabStatus, compose, strip

from .test_auto_title import SESSION

LOGO = icons.LOGOS["claude"]


def session(status="working", label="2"):
    return {
        "focused_workspace_id": "w1", "focused_tab_id": "w1:t1",
        "tabs": [
            {"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"},
            {"tab_id": "w1:t2", "workspace_id": "w1", "label": label},
        ],
        "panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "terminal_id": "term2"}],
        "agents": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "agent": "claude",
                    "agent_status": status, "cwd": "/work/app"}],
    }


class Server:
    """Returns fresh copies, as the socket does, and applies renames."""

    def __init__(self, data):
        self.data = data
        self.renames = []
        self.fail_rename = False

    def snapshot(self):
        return copy.deepcopy(self.data)

    def rename_tab(self, tab_id, label):
        if self.fail_rename:
            raise HerdrError("rename refused")
        self.renames.append((tab_id, label))
        for tab in self.data["tabs"]:
            if tab["tab_id"] == tab_id:
                tab["label"] = label

    def label(self, tab_id="w1:t2"):
        return next(tab["label"] for tab in self.data["tabs"] if tab["tab_id"] == tab_id)


class LabelTest(unittest.TestCase):
    def test_every_label_strips_back_to_the_name(self):
        for logo in ("", LOGO):
            for status in ("blocked", "working", "done", "idle", "unknown", "odd"):
                label = compose("Fix login", status, logo)
                self.assertEqual(strip(label), "Fix login", label)

    def test_glyphs_are_static(self):
        self.assertEqual(compose("x", "blocked", LOGO), LOGO + "  ◉ x")
        self.assertEqual(compose("x", "working", ""), "◐ x")
        self.assertEqual(compose("x", "idle", ""), "✓ x")

    def test_labels_from_the_animated_release_still_strip(self):
        for label in ("⠋ Fix login", "⠏ Fix login", "  Fix login",
                      LOGO + "  ⠹ Fix login", LOGO + "    Fix login"):
            self.assertEqual(strip(label), "Fix login", label)

    def test_plain_names_are_left_alone(self):
        for label in ("Fix login", "●", "◉notes", " lead", ""):
            self.assertEqual(strip(label), label)


class TabStatusTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def make(self, data, **config):
        server = Server(data)
        tabs = TabStatus(server, self.directory)
        tabs.configure(Config({"agent_icons": "none", **config}))
        return server, tabs

    def tick(self, tabs):
        tabs.apply(tabs.snapshot())

    def test_agent_tab_gets_status_and_logo_others_are_untouched(self):
        server, tabs = self.make(session(), agent_icons="font")
        self.tick(tabs)
        self.assertEqual(server.renames, [("w1:t2", LOGO + "  ◐ 2")])
        self.tick(tabs)
        self.assertEqual(len(server.renames), 1, "an unchanged tab is not renamed again")

    def test_status_changes_rewrite_only_the_prefix(self):
        server, tabs = self.make(session("working", "Fix login"))
        self.tick(tabs)
        server.data["agents"][0]["agent_status"] = "done"
        self.tick(tabs)
        self.assertEqual(server.label(), "● Fix login")

    def test_record_is_written_before_the_decorated_label(self):
        server, tabs = self.make(session())
        seen = []
        original = server.rename_tab
        server.rename_tab = lambda tab_id, label: (
            seen.append(json.loads((self.directory / "tab-status.json").read_text())),
            original(tab_id, label))
        self.tick(tabs)
        self.assertIn("w1:t2", seen[0])

    def test_unwritable_state_never_leaves_an_unremovable_prefix(self):
        server, tabs = self.make(session())
        with patch.object(Path, "write_text", side_effect=PermissionError):
            self.tick(tabs)
        self.assertEqual(server.renames, [])
        self.assertEqual(server.label(), "2")

    def test_unnamed_tab_keeps_following_its_position(self):
        server, tabs = self.make(session())
        self.tick(tabs)
        del server.data["tabs"][0]  # the first tab closes; herdr would say "1" now
        self.tick(tabs)
        self.assertEqual(server.label(), "◐ 1")
        server.data["agents"].clear()
        self.tick(tabs)
        self.assertEqual(server.label(), "1")
        server.data["tabs"].insert(0, {"tab_id": "w1:t0", "workspace_id": "w1", "label": "1"})
        self.tick(tabs)
        self.assertEqual(server.label(), "2", "still numbered like an unnamed tab")

    def test_user_names_are_kept_and_decorated(self):
        server, tabs = self.make(session())
        self.tick(tabs)
        server.data["tabs"][1]["label"] = "Release notes"
        self.tick(tabs)
        self.assertEqual(server.label(), "◐ Release notes")
        # Editing a shown name in place keeps the edit, not the old prefix.
        server.data["tabs"][1]["label"] = "◐ Release notes v2"
        self.tick(tabs)
        self.assertEqual(server.label(), "◐ Release notes v2")
        del server.data["tabs"][0]
        self.tick(tabs)
        self.assertEqual(server.label(), "◐ Release notes v2", "a chosen name never renumbers")
        server.data["agents"].clear()
        self.tick(tabs)
        self.assertEqual(server.label(), "Release notes v2")
        self.assertEqual(tabs.records, {})

    def test_glyph_led_names_on_unrecorded_tabs_are_not_stripped(self):
        data = session("idle", "● pinned")
        data["tabs"][0]["label"] = "✓ notes"
        server, tabs = self.make(data)
        self.tick(tabs)
        self.assertEqual(server.label("w1:t1"), "✓ notes")
        self.assertEqual(server.label(), "✓ ● pinned")

    def test_most_urgent_agent_leads_a_shared_tab(self):
        data = session("working", "Pair")
        data["agents"].append({"pane_id": "w1:p3", "tab_id": "w1:t2", "agent": "codex",
                               "agent_status": "blocked"})
        server, tabs = self.make(data, agent_icons="font")
        self.tick(tabs)
        self.assertEqual(server.label(), icons.LOGOS["codex"] + "  ◉ Pair")

    def test_labels_change_only_with_status(self):
        server, tabs = self.make(session("blocked", "Fix"))
        self.tick(tabs)
        self.assertEqual(server.label(), "◉ Fix")
        for focus in ("w1:t2", "w1:t1", "w2:t1"):
            server.data["focused_tab_id"] = focus
            server.data["focused_workspace_id"] = focus.split(":")[0]
            self.tick(tabs)
        self.assertEqual(len(server.renames), 1, "focus and time never rename a tab")
        server.data["agents"][0]["agent_status"] = "working"
        self.tick(tabs)
        self.tick(tabs)
        self.assertEqual(server.renames[1:], [("w1:t2", "◐ Fix")])

    def test_an_animated_label_settles_in_one_rename(self):
        server, tabs = self.make(session("working", "Fix"))
        self.tick(tabs)
        server.data["tabs"][1]["label"] = "⠸ Fix"  # left behind by the animated release
        self.tick(tabs)
        self.tick(tabs)
        self.assertEqual(server.renames[1:], [("w1:t2", "◐ Fix")])

    def test_disabling_and_clearing_restore_bare_names(self):
        server, tabs = self.make(session("working", "Fix"))
        self.tick(tabs)
        tabs.configure(Config({"tab_status": False}))
        self.tick(tabs)
        self.assertEqual(server.label(), "Fix")
        tabs.configure(Config({}))
        self.tick(tabs)
        tabs.clear()
        self.assertEqual(server.label(), "Fix")
        self.assertEqual(TabStatus(server, self.directory).records, {})

    def test_records_survive_a_restart(self):
        server, tabs = self.make(session("working", "Fix"))
        self.tick(tabs)
        server.data["agents"][0]["agent_status"] = "idle"
        _, restarted = self.make(server.data)
        restarted.client = server
        self.tick(restarted)
        self.assertEqual(server.label(), "✓ Fix", "no second prefix after a restart")

    def test_failed_rename_is_retried_later(self):
        server, tabs = self.make(session("working", "Fix"))
        server.fail_rename = True
        self.tick(tabs)
        server.fail_rename = False
        self.tick(tabs)
        self.assertEqual(server.label(), "◐ Fix")

    def test_failed_rename_preserves_glyph_led_name_after_restart(self):
        server, tabs = self.make(session("working", "● pinned"))
        server.fail_rename = True
        self.tick(tabs)
        server.fail_rename = False
        restarted = TabStatus(server, self.directory)
        restarted.configure(Config({"agent_icons": "none"}))
        self.tick(restarted)
        self.assertEqual(server.label(), "◐ ● pinned")
        restarted.clear()
        self.assertEqual(server.label(), "● pinned")

    def test_auto_title_arrives_decorated_in_one_rename(self):
        data = session("working", "2")
        data["tabs"][1]["number"] = 2
        agent = data["agents"][0]
        agent.update(agent="codex", terminal_title_stripped="Repair OAuth | app",
                     agent_session={"agent": "codex", "kind": "id", "value": SESSION})
        server, tabs = self.make(data)
        with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.directory),
                                     "HERDR_AUTO_TITLE_TRANSCRIPT": "true"}):
            titles = AutoTitles()
        tabs.apply(titles.apply(tabs, tabs.snapshot()))
        self.assertEqual(server.renames, [("w1:t2", "◐ Repair OAuth")])
        # Ownership sees the bare title, so later task titles keep flowing.
        agent["terminal_title_stripped"] = "Repair OAuth callbacks | app"
        tabs.apply(titles.apply(tabs, tabs.snapshot()))
        self.assertEqual(server.label(), "◐ Repair OAuth callbacks")

    def test_subscriptions_follow_agent_panes_only(self):
        _, tabs = self.make(session())
        tabs.snapshot()
        self.assertEqual(tabs.subscriptions(), [
            {"type": "pane.agent_detected"},
            {"type": "pane.agent_status_changed", "pane_id": "w1:p2"},
        ])
        tabs.configure(Config({"tab_status": False}))
        self.assertEqual(tabs.subscriptions(), [])


class BarStripTest(unittest.TestCase):
    def test_bar_rows_show_the_bare_tab_name(self):
        data = session("working", "◐ Fix login")
        data["tabs"][0]["label"] = "◐ my plain tab"
        titles = {item.key: item.title for item in build_items(data)}
        self.assertEqual(titles["w1:p2"], "Fix login")
        self.assertEqual(titles["w1:t1"], "◐ my plain tab")


class ConfigTest(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        self.assertTrue(Config().tab_status)
        self.assertFalse(Config({"tab_status": False}).tab_status)


class EventStreamTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "s")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.requests = []

    def serve(self, reply):
        def run():
            conn, _ = self.listener.accept()
            self.requests.append(json.loads(conn.makefile().readline()))
            reply(conn)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_events_wake_and_silence_times_out(self):
        held = []

        def reply(conn):
            conn.sendall(b'{"id":"bar-events","result":{"type":"subscription_started"}}\n')
            held.append(conn)

        thread = self.serve(reply)
        stream = EventStream(self.path)
        wanted = [{"type": "pane.agent_status_changed", "pane_id": "w1:p2"}]
        stream.follow(wanted)
        thread.join(2)
        self.assertEqual(self.requests[0]["params"]["subscriptions"], wanted)
        started = time.monotonic()
        self.assertFalse(stream.wait(0.05))
        self.assertLess(time.monotonic() - started, 1)
        held[0].sendall(b'{"event":"pane.agent_status_changed","data":{}}\n')
        self.assertTrue(stream.wait(2))
        stream.follow(wanted)  # unchanged: no reconnect
        self.assertEqual(len(self.requests), 1)
        held[0].close()
        self.assertTrue(stream.wait(2), "a dropped stream wakes the caller")
        self.assertIsNone(stream.conn)
        stream.follow(wanted)
        self.assertIsNone(stream.conn, "reconnects wait for the retry delay")
        stream.close()

    def test_refused_subscription_raises(self):
        def reply(conn):
            conn.sendall(b'{"id":"bar-events","error":{"code":"pane_not_found","message":"gone"}}\n')
            conn.close()

        thread = self.serve(reply)
        stream = EventStream(self.path)
        with self.assertRaises(HerdrError):
            stream.follow([{"type": "pane.agent_status_changed", "pane_id": "w1:p9"}])
        thread.join(2)
        self.assertIsNone(stream.conn)

    def test_event_coalesced_with_acknowledgement_is_not_lost(self):
        conn = Mock()
        conn.recv.return_value = (
            b'{"result":{"type":"subscription_started"}}\n'
            b'{"event":"pane.agent_status_changed"}\n'
        )
        stream = EventStream(self.path)
        self.addCleanup(stream.close)
        with patch("herdr_bar.client.socket.socket", return_value=conn):
            stream.follow([{"type": "pane.agent_detected"}])
        with patch("herdr_bar.client.select.select", return_value=([], [], [])) as select:
            self.assertTrue(stream.wait(1))
            select.assert_not_called()
            self.assertFalse(stream.wait(0))

    def test_continuous_event_traffic_does_not_trap_the_waiter(self):
        conn = Mock()
        conn.recv.return_value = b"x" * 65536
        stream = EventStream(self.path)
        stream.conn = conn
        self.addCleanup(stream.close)
        with patch("herdr_bar.client.select.select", return_value=([conn], [], [])):
            self.assertTrue(stream.wait(1))
        conn.recv.assert_called_once_with(65536)

    def test_invalid_acknowledgement_closes_stream_and_backs_off(self):
        for response in (b'[]\n', b'x' * 65536):
            with self.subTest(response_length=len(response)):
                conn = Mock()
                conn.recv.return_value = response
                stream = EventStream(self.path)
                with patch("herdr_bar.client.socket.socket", return_value=conn):
                    with self.assertRaises(HerdrError):
                        stream.follow([{"type": "pane.agent_detected"}])
                conn.close.assert_called_once()
                self.assertIsNone(stream.conn)
                self.assertGreater(stream.retry_at, time.monotonic())

    def test_without_a_stream_wait_just_sleeps(self):
        stream = EventStream(None)
        stream.follow([{"type": "tab.focused"}])
        with patch("herdr_bar.client.time.sleep") as sleep:
            self.assertFalse(stream.wait(0.5))
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
