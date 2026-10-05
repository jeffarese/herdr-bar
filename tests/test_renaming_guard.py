import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_bar.client import HerdrError
from herdr_bar.config import Config
from herdr_bar.renaming_guard import (
    KNOWN_RENAMERS,
    AutomaticTabClient,
    RenamingPluginDetected,
    check_plugins,
)
from herdr_bar.tab_status import TabStatus

from .test_tab_status import Server, session


class RenamingGuardTest(unittest.TestCase):
    def test_known_enabled_renamers_block_and_disabled_ones_do_not(self):
        for plugin_id in KNOWN_RENAMERS:
            with self.subTest(plugin_id=plugin_id):
                with self.assertRaises(RenamingPluginDetected):
                    check_plugins([{"plugin_id": plugin_id, "enabled": True}])
                check_plugins([{"plugin_id": plugin_id, "enabled": False}])

    def test_forks_and_new_plugins_are_detected_from_metadata(self):
        for fields in ({"name": "Smart tab renaming"},
                       {"description": "Automatically rename tabs from the current task"},
                       {"source": {"repo": "herdr-auto-title"}}):
            with self.subTest(fields=fields):
                with self.assertRaises(RenamingPluginDetected):
                    check_plugins([{"plugin_id": "custom.plugin", "enabled": True, **fields}])

    def test_self_and_unrelated_plugins_do_not_block(self):
        check_plugins([
            {"plugin_id": "herdr-bar", "enabled": True, "description": "Automatic tab titles"},
            {"plugin_id": "herdr-newtab-plus", "enabled": True,
             "description": "Pick a folder and open a tab"},
            {"plugin_id": "herdr-grid", "enabled": True, "name": "Agent Grid"},
        ])

    def test_custom_ids_can_be_declared(self):
        with self.assertRaises(RenamingPluginDetected):
            check_plugins([{"plugin_id": "private.tools", "enabled": True}], ["private.tools"])

    def test_plugin_enabled_between_writes_blocks_the_next_write(self):
        client = Mock()
        client.call.side_effect = [
            {"plugins": []},
            {"plugins": [{"plugin_id": "herdr.auto-title", "enabled": True}]},
        ]
        writer = AutomaticTabClient(client)
        writer.rename_tab("t1", "First")
        with self.assertRaises(RenamingPluginDetected):
            writer.rename_tab("t1", "Second")
        with self.assertRaises(RenamingPluginDetected):
            writer.rename_tab("t2", "Third")
        client.rename_tab.assert_called_once_with("t1", "First")

    def test_unavailable_or_invalid_registry_never_allows_a_write(self):
        for result in ({}, {"plugins": [None]}, HerdrError("offline")):
            with self.subTest(result=result):
                client = Mock()
                if isinstance(result, Exception):
                    client.call.side_effect = result
                else:
                    client.call.return_value = result
                with self.assertRaises(HerdrError):
                    AutomaticTabClient(client).rename_tab("t1", "Changed")
                client.rename_tab.assert_not_called()

    def test_custom_config_blocks_writes(self):
        client = Mock()
        client.call.return_value = {
            "plugins": [{"plugin_id": "private.tools", "enabled": True}]}
        with patch.object(Config, "load", return_value=Config({
                "tab_renaming_plugins": ["private.tools"]})):
            with self.assertRaises(RenamingPluginDetected):
                AutomaticTabClient(client).rename_tab("t1", "Changed")
        client.rename_tab.assert_not_called()

    def test_competing_plugin_prevents_cleanup_writes_too(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Server(session("working", "Fix login"))
            server.call = Mock(return_value={"plugins": []})
            writer = AutomaticTabClient(server)
            tabs = TabStatus(writer, Path(directory))
            tabs.configure(Config({"tab_status": True, "agent_icons": "none"}))
            tabs.apply(tabs.snapshot())
            server.call.return_value = {
                "plugins": [{"plugin_id": "herdr.auto-title", "enabled": True}]}
            tabs.clear()
            self.assertEqual(server.renames, [("w1:t2", "◐ Fix login")])
            self.assertEqual(server.label(), "◐ Fix login")
