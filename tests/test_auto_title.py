import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_bar.app import Bar
from herdr_bar.auto_title import MAX_SCAN, AutoTitles, TranscriptReader
from herdr_bar.config import Config
from herdr_bar.mru import Recents
from herdr_bar.theme import Theme

from .test_app import FakeClient

SESSION = "12345678-1234-1234-1234-123456789abc"


def snapshot():
    return {
        "tabs": [{"tab_id": "w1:t9", "workspace_id": "w1", "number": 9, "label": "1"}],
        "panes": [{"pane_id": "w1:p9", "tab_id": "w1:t9", "terminal_id": "term9"}],
        "agents": [{
            "pane_id": "w1:p9", "tab_id": "w1:t9", "agent": "claude", "cwd": "/work/app",
            "agent_session": {"agent": "claude", "kind": "id", "value": SESSION},
        }],
    }


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "projects" / "-work-app" / (SESSION + ".jsonl")
        self.path.parent.mkdir(parents=True)
        self.reader = TranscriptReader(self.root)

    def write(self, *records):
        with self.path.open("a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def title(self):
        return self.reader.title(SESSION, "/work/app")

    def test_latest_ai_title_wins_over_opening(self):
        self.write({"type": "user", "origin": {"kind": "human"},
                    "message": {"content": "Fix login\nHere is context"}},
                   {"type": "ai-title", "aiTitle": "Login repair"})
        self.assertEqual(self.title(), "Login repair")
        self.write({"type": "ai-title", "aiTitle": "Repair OAuth callbacks"})
        self.assertEqual(self.title(), "Repair OAuth callbacks")

    def test_opening_ignores_tool_output_and_uses_human_text_blocks(self):
        self.write({"type": "user", "message": {"content": "tool output"}},
                   {"type": "user", "origin": {"kind": "human"}, "message": {"content": [
                       {"type": "tool_result", "content": "secret output"},
                       {"type": "text", "text": "<system-reminder>context</system-reminder>"},
                       {"type": "text", "text": "Fix login\nMore detail"},
                   ]}})
        self.assertEqual(self.title(), "Fix login")

    def test_slash_command_and_arguments(self):
        self.write({"type": "user", "origin": {"kind": "human"}, "message": {"content":
                    "<command-name>/review</command-name><command-args>spec.md</command-args>"}})
        self.assertEqual(self.title(), "review spec.md")

    def test_partial_line_waits_until_complete_and_bad_records_are_ignored(self):
        self.path.write_bytes(b'not json\n[]\n{"type":"ai-title","aiTitle":"OAuth')
        self.assertEqual(self.title(), "")
        with self.path.open("ab") as handle:
            handle.write(b' repair"}\n')
        self.assertEqual(self.title(), "OAuth repair")

    def test_truncated_or_replaced_file_does_not_reuse_old_title(self):
        self.write({"type": "ai-title", "aiTitle": "Old title"})
        self.assertEqual(self.title(), "Old title")
        self.path.write_text('{}\n')
        self.assertEqual(self.title(), "")
        self.path.unlink()
        self.write({"type": "ai-title", "aiTitle": "New title"})
        self.assertEqual(self.title(), "New title")

    def test_missing_transcript_retried_and_can_be_found_under_old_cwd(self):
        with patch("herdr_bar.auto_title.time.monotonic", return_value=1):
            self.assertEqual(self.reader.title(SESSION, "/work/new"), "")
        self.write({"type": "ai-title", "aiTitle": "Moved project"})
        with patch("herdr_bar.auto_title.time.monotonic", return_value=12):
            self.assertEqual(self.reader.title(SESSION, "/work/new"), "Moved project")

    def test_scan_is_bounded_and_terminal_controls_removed(self):
        self.path.write_bytes(b'x' * (MAX_SCAN + 100) + b'\n')
        self.write({"type": "ai-title", "aiTitle": "\x1b[31mFix\x1b[0m\nlogin\x07"})
        self.assertEqual(self.title(), "Fix login")

    def test_invalid_session_id_cannot_escape_projects(self):
        self.assertEqual(self.reader.title("../../private", "/work/app"), "")
        self.assertEqual(self.reader.sessions, {})


class AutoTitlesTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"HERDR_AUTO_TITLE_TRANSCRIPT": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.titles = AutoTitles()
        self.title = patch.object(self.titles.reader, "title", return_value="Repair login").start()
        self.addCleanup(patch.stopall)
        self.client = FakeClient(snapshot())

    def apply(self):
        return self.titles.apply(self.client, copy.deepcopy(self.client.snapshot()))

    def test_default_is_display_position_not_monotonic_tab_number(self):
        result = self.apply()
        self.assertEqual(result["tabs"][0]["label"], "Repair login")
        self.assertEqual(self.client.calls, [("rename", "w1:t9", "Repair login")])

    def test_names_survive_refresh_reopen_and_transcript_changes(self):
        self.apply()
        self.title.return_value = "New task"
        self.apply()
        with patch.object(TranscriptReader, "title", return_value="New task"):
            AutoTitles().apply(self.client, self.client.snapshot())
        self.assertEqual(len(self.client.calls), 1)

    def test_existing_custom_names_are_preserved_on_first_open(self):
        for label in ("My work", "claude", "9", "1 · app › claude › Repair login", "Repair login"):
            with self.subTest(label=label):
                self.client._snapshot["tabs"][0]["label"] = label
                self.apply()
        self.assertEqual(self.client.calls, [])
        self.title.assert_not_called()

    def test_custom_pane_and_agent_names_are_preserved(self):
        for group, key in (("panes", "label"), ("agents", "name")):
            for label in ("My work", "claude"):
                self.client._snapshot = snapshot()
                self.client._snapshot[group][0][key] = label
                self.apply()
        self.assertEqual(self.client.calls, [])

    def test_empty_name_opts_back_in(self):
        self.apply()
        self.client._snapshot["tabs"][0]["label"] = ""
        self.title.return_value = "New task"
        self.apply()
        self.assertEqual(self.client.calls[-1], ("rename", "w1:t9", "New task"))

    def test_workspace_positions_count_shell_tabs_and_reset_per_workspace(self):
        self.client._snapshot["tabs"].insert(0, {
            "tab_id": "w2:t50", "workspace_id": "w2", "label": "shell", "number": 50,
        })
        self.client._snapshot["tabs"].insert(1, {
            "tab_id": "w1:t8", "workspace_id": "w1", "label": "shell", "number": 8,
        })
        self.client._snapshot["tabs"][-1]["label"] = "2"
        self.apply()
        self.assertEqual(len(self.client.calls), 1)

    def test_multiple_agents_other_agents_and_missing_sessions_are_skipped(self):
        cases = []
        data = snapshot()
        data["agents"].append(dict(data["agents"][0], pane_id="w1:p10"))
        cases.append(data)
        data = snapshot()
        data["agents"][0]["agent"] = "codex"
        cases.append(data)
        data = snapshot()
        data["agents"][0].pop("agent_session")
        cases.append(data)
        for data in cases:
            self.client._snapshot = data
            self.apply()
        self.assertEqual(self.client.calls, [])

    def test_rename_or_session_switch_during_read_is_respected(self):
        for group, key, value in (("tabs", "label", "Manual title"),
                                  ("panes", "label", "Plugin name"),
                                  ("panes", "terminal_id", "replacement")):
            self.client._snapshot = snapshot()

            def read(*args):
                self.client._snapshot[group][0][key] = value
                return "Repair login"

            self.title.side_effect = read
            self.apply()
        self.assertEqual(self.client.calls, [])

    def test_failure_does_not_pretend_tab_was_renamed(self):
        self.client.rename_error = "connection failed"
        self.assertEqual(self.apply()["tabs"][0]["label"], "1")

    def test_opt_out_does_not_read_transcripts_or_mutate_session(self):
        with patch.dict(os.environ, {"HERDR_AUTO_TITLE_TRANSCRIPT": "false"}):
            titles = AutoTitles()
        with patch.object(titles.reader, "title") as read:
            titles.apply(self.client, snapshot())
            read.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_bootstrap_and_refresh_use_real_transcript(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root) / "projects" / "-work-app"
            projects.mkdir(parents=True)
            path = projects / (SESSION + ".jsonl")
            path.write_text(json.dumps({"type": "ai-title", "aiTitle": "Repair login"}) + "\n")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root}):
                bar = Bar(self.client, Config(), Recents(None), Theme())
                bar.bootstrap()
                self.assertEqual(bar.items[0].title, "Repair login")
                self.client._snapshot["tabs"][0]["label"] = "My choice"
                bar.refresh()
                self.assertEqual(bar.items[0].title, "My choice")
