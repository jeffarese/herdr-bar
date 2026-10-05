"""Lead agent tab names with the vendor logo and a live status glyph.

Herdr tab labels are plain text, so the status lives in the label itself:
``<logo>  <glyph> <name>``. Glyphs are static: a tab is renamed only when its
agent's status or its name changes, never on a timer. Only tabs recorded here
are ever stripped, and externally changed labels are yielded for the rest
of the tab's lifetime, including across watcher restarts. A record is written
before its tab's first decorated label and dropped only after the bare name
is back or the tab closes.

Herdr numbers unnamed tabs by position, and a rename cannot hand that back.
Decorated tabs that started unnamed therefore keep following their position
for as long as the watcher runs.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import icons
from .client import HerdrClient, HerdrError
from .config import Config

GLYPHS = {"blocked": "◉", "working": "◐", "done": "●", "idle": "✓", "unknown": "○"}
RANK = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4}
FONT_RECHECK = 60
# Spinner frames and the blink's blank cell from earlier local builds.
_RETIRED_GLYPHS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ "

_PREFIX = re.compile("^(?:[%s]  )?[%s] " % (
    re.escape("".join(icons.LOGOS.values())),
    re.escape("".join(GLYPHS.values()) + _RETIRED_GLYPHS),
))

Record = Dict[str, Any]


def strip(label: str) -> str:
    return _PREFIX.sub("", label, count=1)


def compose(base: str, status: str, logo: str) -> str:
    glyph = GLYPHS.get(status, GLYPHS["unknown"])
    # The icon font draws into the next cell, as in the bar's rows.
    return "%s  %s %s" % (logo, glyph, base) if logo else "%s %s" % (glyph, base)


def tab_states(snapshot: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """tab id -> (most urgent agent status, that agent's kind)."""
    states: Dict[str, Tuple[str, str]] = {}
    for agent in snapshot.get("agents", []):
        tab_id = agent.get("tab_id")
        if not isinstance(tab_id, str):
            continue
        status = agent.get("agent_status")
        status = status if status in RANK else "unknown"
        current = states.get(tab_id)
        if current is None or RANK[status] < RANK[current[0]]:
            states[tab_id] = (status, str(agent.get("agent") or ""))
    return states


class TabStatus:
    """Present tabs to auto titles by their bare names; write them decorated.

    Stands in for the client in ``AutoTitles.apply``: ``snapshot`` hands out
    names without the prefix and ``rename_tab`` adds it, so a new title and
    its status reach Herdr in one rename.
    """

    def __init__(self, client: HerdrClient, state_dir: Optional[Path]) -> None:
        self.client = client
        self.path = state_dir / "tab-status.json" if state_dir is not None else None
        self.enabled = self.path is not None
        self.logos = False
        self.font_checked = float("-inf")
        self.records: Dict[str, Record] = self._load()
        self.states: Dict[str, Tuple[str, str]] = {}
        self.latest: Dict[str, Any] = {}

    def configure(self, config: Config) -> None:
        self.enabled = config.tab_status and self.path is not None
        # The watcher outlives font installs; the popup's once-per-run cache does not.
        if time.monotonic() >= self.font_checked + FONT_RECHECK:
            icons.font_available.cache_clear()
            self.font_checked = time.monotonic()
        self.logos = icons.enabled(config.agent_icons)

    # -- records ---------------------------------------------------------------

    def _load(self) -> Dict[str, Record]:
        if self.path is None:
            return {}
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {
            tab_id: dict(record, default=record.get("default") is True)
            for tab_id, record in saved.items()
            if isinstance(record, dict) and isinstance(record.get("base"), str)
        }

    def _save(self, records: Dict[str, Record]) -> None:
        if records == self.records:
            return
        if self.path is None:
            raise OSError("no plugin state directory")
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(records, sort_keys=True))
        staging.replace(self.path)
        self.records = records

    # -- client stand-in -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        snapshot = dict(self.client.snapshot())
        # Copies: bare names must never leak back into the client's own records.
        snapshot["tabs"] = [dict(tab) for tab in snapshot.get("tabs", [])]
        positions: Dict[str, int] = {}
        for tab in snapshot["tabs"]:
            workspace = tab.get("workspace_id", "")
            positions[workspace] = positions.get(workspace, 0) + 1
            label = tab.get("label")
            if not isinstance(label, str):
                continue
            tab["shown"] = label
            tab["position"] = str(positions[workspace])
            record = self.records.get(tab.get("tab_id"))
            if record is not None:
                if record.get("yielded"):
                    tab["title_conflict"] = True
                    continue
                written = record.get("written")
                if written is None:
                    # Older releases recorded only the base. Adopt a prefix
                    # only when removing it gives that exact recorded base.
                    if label == record["base"] or strip(label) == record["base"]:
                        record = dict(record, written=label)
                        self._save({**self.records, tab["tab_id"]: record})
                    else:
                        self._yield(tab["tab_id"], label)
                        tab["title_conflict"] = True
                        continue
                elif label not in (written, record.get("previous")):
                    self._yield(tab["tab_id"], label)
                    tab["title_conflict"] = True
                    continue
                if label == record.get("previous"):
                    # A refused or interrupted write still shows the old label.
                    base = record["previous_base"]
                    default = record.get("previous_default", False)
                else:
                    base, default = record["base"], record["default"]
                tab["default"] = default
                tab["label"] = tab["position"] if default else base
        self.states = tab_states(snapshot)
        self.latest = snapshot
        return snapshot

    def rename_tab(self, tab_id: str, label: str) -> None:
        tab = next((tab for tab in self.latest.get("tabs", [])
                    if tab.get("tab_id") == tab_id), {"tab_id": tab_id})
        self._sync(tab, label, self._desired(tab_id, label))

    # -- decoration ------------------------------------------------------------

    def _desired(self, tab_id: str, base: str) -> str:
        state = self.states.get(tab_id)
        if not self.enabled or state is None or not base:
            return base
        status, kind = state
        return compose(base, status, icons.logo_for(kind) if self.logos else "")

    def _yield(self, tab_id: str, label: str) -> None:
        """Remember another writer took over, including across watcher restarts."""
        self._save({**self.records, tab_id: {
            "base": label, "default": False, "yielded": True,
        }})

    def _sync(self, tab: Dict[str, Any], base: str, label: str) -> None:
        """Rename only a label we still own, recording pending writes first."""
        tab_id = tab["tab_id"]
        if tab.get("title_conflict") or self.records.get(tab_id, {}).get("yielded"):
            raise HerdrError("tab name changed by another writer")
        changing = label != tab.get("shown")
        if changing:
            current = next((item for item in self.client.snapshot().get("tabs", [])
                            if item.get("tab_id") == tab_id), None)
            if current is None:
                raise HerdrError("tab closed before rename")
            if current.get("label") != tab.get("shown"):
                self._yield(tab_id, current.get("label") or "")
                raise HerdrError("tab name changed before rename")
        records = dict(self.records)
        default = base == tab.get("position") and (
            tab.get("default", False) or tab_id not in self.records)
        if label != base or (tab_id in self.records and default):
            record = {"base": base, "default": default, "written": label}
            if changing:
                record.update(previous=tab.get("shown"), previous_base=tab.get("label", base),
                              previous_default=tab.get("default", default))
            records[tab_id] = record
            # If ownership cannot be saved, do not risk an untracked rename.
            self._save(records)
        else:
            records.pop(tab_id, None)
        if changing:
            self.client.rename_tab(tab_id, label)
            tab["shown"] = label
        if tab_id in records:
            # Successful writes must not accept a later reversion to the old
            # name as a failed write; that is another writer taking over.
            self._save({**records, tab_id: {
                "base": base, "default": default, "written": label,
            }})
        else:
            self._forget(tab_id)

    def apply(self, snapshot: Dict[str, Any]) -> None:
        """Bring every label up to date; a label already right costs no rename."""
        live = set()
        for tab in snapshot.get("tabs", []):
            tab_id, base = tab.get("tab_id"), tab.get("label")
            if not isinstance(tab_id, str) or not isinstance(base, str):
                continue
            live.add(tab_id)
            if tab.get("title_conflict"):
                continue
            label = self._desired(tab_id, base)
            if label == tab.get("shown", base) and tab_id not in self.records:
                continue
            try:
                self._sync(tab, base, label)
            except (HerdrError, OSError):
                continue
        self._forget(*(set(self.records) - live))

    def _forget(self, *tab_ids: str) -> None:
        if any(tab_id in self.records for tab_id in tab_ids):
            try:
                self._save({key: value for key, value in self.records.items()
                            if key not in tab_ids})
            except OSError:
                pass  # a stale record only costs one extra strip later

    def clear(self) -> None:
        """Restore every recorded tab to its bare name before the watcher exits."""
        if not self.records:
            return
        try:
            snapshot = self.snapshot()
        except (HerdrError, OSError):
            return
        for tab in snapshot.get("tabs", []):
            tab_id = tab.get("tab_id")
            if tab_id not in self.records or tab.get("title_conflict"):
                continue
            try:
                self._sync(tab, tab["label"], tab["label"])
            except (HerdrError, OSError):
                continue
            self._forget(tab_id)
        self._forget(*(set(self.records) - {tab.get("tab_id") for tab in snapshot.get("tabs", [])}))

    def subscriptions(self) -> List[Dict[str, str]]:
        """Events that change what a tab should show, for an instant refresh."""
        if not self.enabled:
            return []
        panes = sorted({agent["pane_id"] for agent in self.latest.get("agents", [])
                        if isinstance(agent.get("pane_id"), str)})
        return [{"type": "pane.agent_detected"}] + [
            {"type": "pane.agent_status_changed", "pane_id": pane} for pane in panes]
