"""Give enabled naming plugins priority over all automatic tab writes."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from .client import HerdrClient, HerdrError
from .config import Config

# IDs from the plugins' published manifests. Metadata matching also covers
# forks; custom IDs can be listed in tab_renaming_plugins.
KNOWN_RENAMERS = frozenset({
    "herdr.auto-title",
    "aarsh21.tab-title",
    "herdr-tab-title",
    "zhangzujian.auto-session-title",
    "herdr-automatic-rename",
})
_NAMING = re.compile(
    r"\bauto(?:matic)? (?:session )?(?:title|rename|name)\b"
    r"|\btabs? (?:titles?|names?|renam\w*|naming)\b"
    r"|\brenam\w*\b.{0,40}\btabs?\b"
)


class RenamingPluginDetected(HerdrError):
    """Another enabled plugin owns automatic tab naming."""


def installed_plugins(client: HerdrClient) -> List[Dict[str, Any]]:
    result = client.call("plugin.list", {}, ["plugin", "list"])
    plugins = result.get("plugins")
    if not isinstance(plugins, list) or any(not isinstance(item, dict) for item in plugins):
        raise HerdrError("plugin.list did not return a plugin list")
    return plugins


def check_plugins(plugins: List[Dict[str, Any]], extra: Sequence[str] = ()) -> None:
    conflicts = []
    for plugin in plugins:
        plugin_id = plugin.get("plugin_id")
        if not plugin.get("enabled") or plugin_id == "herdr-bar":
            continue
        source = plugin.get("source")
        source = source if isinstance(source, dict) else {}
        values = (plugin_id, plugin.get("name"), plugin.get("description"), source.get("repo"))
        metadata = " ".join(value for value in values if isinstance(value, str)).casefold()
        metadata = re.sub(r"[._-]+", " ", metadata)
        if plugin_id in KNOWN_RENAMERS or plugin_id in extra or _NAMING.search(metadata):
            conflicts.append(str(plugin_id or plugin.get("name") or "unknown plugin"))
    if conflicts:
        raise RenamingPluginDetected(
            "automatic tab updates disabled: enabled naming plugin(s): " + ", ".join(conflicts))


class AutomaticTabClient:
    """Guard background writes, leaving explicit user renames on the raw client."""

    def __init__(self, client: HerdrClient) -> None:
        self.client = client
        self.conflict = ""

    def snapshot(self) -> Dict[str, Any]:
        return self.client.snapshot()

    def rename_tab(self, tab_id: str, label: str) -> None:
        if self.conflict:
            raise RenamingPluginDetected(self.conflict)
        try:
            check_plugins(installed_plugins(self.client), Config.load().tab_renaming_plugins)
        except RenamingPluginDetected as error:
            self.conflict = str(error)
            raise
        self.client.rename_tab(tab_id, label)
