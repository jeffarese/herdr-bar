"""Turn a Herdr session snapshot into the rows the bar shows.

The rule is one row per agent, plus one row per tab that has no agent, plus one
row per workspace once a session has more than one. That mirrors how people
think about a Herdr session: you jump to an agent, or to a plain terminal tab,
or to a whole project.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

KIND_AGENT = "agent"
KIND_TAB = "tab"
KIND_SPACE = "space"

STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_NONE = "none"

# Blocked agents want you now; finished ones want you next; everything else can
# wait. This drives the resting order and breaks ties between equal scores.
STATUS_ORDER = {
    STATUS_BLOCKED: 0,
    STATUS_DONE: 1,
    STATUS_WORKING: 2,
    STATUS_IDLE: 3,
    "unknown": 4,
    STATUS_NONE: 5,
}


class Item(object):
    __slots__ = (
        "kind",
        "key",
        "title",
        "subtitle",
        "workspace_id",
        "workspace_label",
        "tab_id",
        "pane_id",
        "agent",
        "agent_name",
        "status",
        "tab_number",
        "focused",
        "fields",
    )

    def __init__(
        self,
        kind: str,
        key: str,
        title: str,
        subtitle: str = "",
        workspace_id: str = "",
        workspace_label: str = "",
        tab_id: Optional[str] = None,
        pane_id: Optional[str] = None,
        agent: Optional[str] = None,
        agent_name: Optional[str] = None,
        status: str = STATUS_NONE,
        tab_number: Optional[int] = None,
        focused: bool = False,
        extra_terms: Sequence[str] = (),
    ) -> None:
        self.kind = kind
        self.key = key
        self.title = title
        self.subtitle = subtitle
        self.workspace_id = workspace_id
        self.workspace_label = workspace_label
        self.tab_id = tab_id
        self.pane_id = pane_id
        self.agent = agent
        self.agent_name = agent_name
        self.status = status
        self.tab_number = tab_number
        self.focused = focused

        # Searchable fields, title first. Each query term has to match one
        # field on its own -- matching across a flattened blob of everything
        # turns short queries into noise.
        candidates: List[str] = [title, subtitle, workspace_label]
        candidates.extend(str(part) for part in extra_terms if part)
        candidates.extend([agent or "", agent_name or "", status])
        if tab_number is not None:
            candidates.append("#%d" % tab_number)
        seen = set()
        fields: List[str] = []
        for candidate in candidates:
            key_text = candidate.lower()
            if candidate and key_text not in seen:
                seen.add(key_text)
                fields.append(candidate)
        self.fields = tuple(fields)

    @property
    def status_rank(self) -> int:
        return STATUS_ORDER.get(self.status, STATUS_ORDER["unknown"])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Item(%s, %r, %s)" % (self.kind, self.title, self.status)


def shorten_path(path: Optional[str]) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    return "".join(char for char in text if char >= " " or char == "\t").strip()


def _first(*values: Any) -> str:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _status_of(record: Dict[str, Any]) -> str:
    status = _clean(record.get("agent_status")) or STATUS_NONE
    return status


def build_items(snapshot: Dict[str, Any]) -> List[Item]:
    workspaces: List[Dict[str, Any]] = list(snapshot.get("workspaces") or [])
    tabs: List[Dict[str, Any]] = list(snapshot.get("tabs") or [])
    panes: List[Dict[str, Any]] = list(snapshot.get("panes") or [])
    agents: List[Dict[str, Any]] = list(snapshot.get("agents") or [])

    focused_tab_id = snapshot.get("focused_tab_id")
    focused_pane_id = snapshot.get("focused_pane_id")

    workspace_labels = {
        ws.get("workspace_id"): _first(ws.get("label"), "workspace %s" % ws.get("number", ""))
        for ws in workspaces
    }
    panes_by_id = {pane.get("pane_id"): pane for pane in panes if pane.get("pane_id")}
    tabs_by_id = {tab.get("tab_id"): tab for tab in tabs if tab.get("tab_id")}

    agents_by_tab: Dict[str, List[Dict[str, Any]]] = {}
    for agent in agents:
        tab_id = agent.get("tab_id")
        if tab_id:
            agents_by_tab.setdefault(tab_id, []).append(agent)

    items: List[Item] = []

    for tab in tabs:
        tab_id = tab.get("tab_id")
        if not tab_id:
            continue
        workspace_id = tab.get("workspace_id") or ""
        workspace_label = workspace_labels.get(workspace_id, "")
        tab_label = _first(tab.get("label"))
        tab_number = tab.get("number") if isinstance(tab.get("number"), int) else None
        tab_agents = agents_by_tab.get(tab_id, [])

        if not tab_agents:
            pane = None
            for candidate in panes:
                if candidate.get("tab_id") == tab_id:
                    pane = candidate
                    break
            cwd = shorten_path(_first((pane or {}).get("foreground_cwd"), (pane or {}).get("cwd")))
            items.append(
                Item(
                    kind=KIND_TAB,
                    key=tab_id,
                    title=_first(tab_label, os.path.basename(cwd), "tab"),
                    subtitle=cwd,
                    workspace_id=workspace_id,
                    workspace_label=workspace_label,
                    tab_id=tab_id,
                    pane_id=(pane or {}).get("pane_id"),
                    status=_status_of(tab),
                    tab_number=tab_number,
                    focused=tab_id == focused_tab_id,
                )
            )
            continue

        for agent in tab_agents:
            pane_id = agent.get("pane_id")
            pane = panes_by_id.get(pane_id, {})
            cwd = shorten_path(
                _first(
                    agent.get("foreground_cwd"),
                    agent.get("cwd"),
                    pane.get("foreground_cwd"),
                    pane.get("cwd"),
                )
            )
            title = _first(
                pane.get("title"),
                agent.get("title"),
                pane.get("label"),
                agent.get("terminal_title_stripped"),
                pane.get("terminal_title_stripped"),
                tab_label,
                os.path.basename(cwd),
                "agent",
            )
            agent_kind = _first(agent.get("display_agent"), agent.get("agent"))
            agent_name = _first(agent.get("name"))
            items.append(
                Item(
                    kind=KIND_AGENT,
                    key=pane_id or tab_id,
                    title=title,
                    subtitle=cwd,
                    workspace_id=workspace_id,
                    workspace_label=workspace_label,
                    tab_id=tab_id,
                    pane_id=pane_id,
                    agent=agent_kind,
                    agent_name=agent_name,
                    status=_status_of(agent),
                    tab_number=tab_number,
                    focused=(pane_id == focused_pane_id)
                    or (len(tab_agents) == 1 and tab_id == focused_tab_id),
                    extra_terms=(tab_label,),
                )
            )

    if len(workspaces) > 1:
        for workspace in workspaces:
            workspace_id = workspace.get("workspace_id")
            if not workspace_id:
                continue
            worktree = workspace.get("worktree") or {}
            branch = _clean(worktree.get("branch"))
            active_tab = tabs_by_id.get(workspace.get("active_tab_id"), {})
            items.append(
                Item(
                    kind=KIND_SPACE,
                    key=workspace_id,
                    title=workspace_labels.get(workspace_id, workspace_id),
                    subtitle=branch or _clean(active_tab.get("label")),
                    workspace_id=workspace_id,
                    workspace_label=workspace_labels.get(workspace_id, ""),
                    status=_status_of(workspace),
                    focused=bool(workspace.get("focused")),
                    extra_terms=("space", "workspace", branch),
                )
            )

    return items
