"""A synthetic Herdr snapshot used by the tests and by scripts/demo.py."""

from __future__ import annotations

from typing import Any, Dict


def snapshot() -> Dict[str, Any]:
    workspaces = [
        {
            "workspace_id": "w1",
            "label": "pacebeats",
            "number": 1,
            "focused": True,
            "active_tab_id": "w1:t3",
            "agent_status": "working",
            "tab_count": 6,
            "pane_count": 7,
        },
        {
            "workspace_id": "w2",
            "label": "erestor",
            "number": 2,
            "focused": False,
            "active_tab_id": "w2:t1",
            "agent_status": "blocked",
            "tab_count": 2,
            "pane_count": 2,
            "worktree": {"branch": "feat/retry-queue"},
        },
    ]
    tabs = [
        {"tab_id": "w1:t1", "workspace_id": "w1", "label": "server", "number": 1,
         "agent_status": "none", "pane_count": 2, "focused": False},
        {"tab_id": "w1:t2", "workspace_id": "w1", "label": "Redesign week loading screen",
         "number": 2, "agent_status": "working", "pane_count": 1, "focused": False},
        {"tab_id": "w1:t3", "workspace_id": "w1", "label": "Check weekly explanation display",
         "number": 3, "agent_status": "working", "pane_count": 1, "focused": True},
        {"tab_id": "w1:t4", "workspace_id": "w1", "label": "cards meaningless", "number": 4,
         "agent_status": "blocked", "pane_count": 1, "focused": False},
        {"tab_id": "w1:t5", "workspace_id": "w1", "label": "library depth battery", "number": 5,
         "agent_status": "done", "pane_count": 1, "focused": False},
        {"tab_id": "w1:t6", "workspace_id": "w1", "label": "logs", "number": 6,
         "agent_status": "none", "pane_count": 1, "focused": False},
        {"tab_id": "w2:t1", "workspace_id": "w2", "label": "retry queue backoff", "number": 1,
         "agent_status": "blocked", "pane_count": 1, "focused": False},
        {"tab_id": "w2:t2", "workspace_id": "w2", "label": "migrations", "number": 2,
         "agent_status": "idle", "pane_count": 1, "focused": False},
    ]
    agents = [
        {
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1", "agent": "claude",
            "agent_status": "working", "cwd": "/Users/dev/workspace/pacebeats",
            "terminal_title_stripped": "Redesign week loading screen", "focused": False,
        },
        {
            "pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1", "agent": "claude",
            "agent_status": "working", "cwd": "/Users/dev/workspace/pacebeats",
            "terminal_title_stripped": "Check weekly explanation display", "focused": True,
        },
        {
            "pane_id": "w1:p4", "tab_id": "w1:t4", "workspace_id": "w1", "agent": "codex",
            "agent_status": "blocked", "cwd": "/Users/dev/workspace/pacebeats/web",
            "terminal_title_stripped": "cards meaningless", "focused": False,
        },
        {
            "pane_id": "w1:p5", "tab_id": "w1:t5", "workspace_id": "w1", "agent": "claude",
            "name": "battery", "agent_status": "done",
            "cwd": "/Users/dev/workspace/pacebeats/.worktrees/library",
            "terminal_title_stripped": "library depth battery", "focused": False,
        },
        {
            "pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2", "agent": "codex",
            "agent_status": "blocked", "cwd": "/Users/dev/workspace/erestor",
            "terminal_title_stripped": "retry queue backoff", "focused": False,
        },
        {
            "pane_id": "w2:p2", "tab_id": "w2:t2", "workspace_id": "w2", "agent": "gemini",
            "agent_status": "idle", "cwd": "/Users/dev/workspace/erestor",
            "terminal_title_stripped": "migrations", "focused": False,
        },
    ]
    panes = [
        {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1",
         "cwd": "/Users/dev/workspace/pacebeats", "focused": False},
        {"pane_id": "w1:p6", "tab_id": "w1:t6", "workspace_id": "w1",
         "cwd": "/Users/dev/workspace/pacebeats", "focused": False},
    ]
    for agent in agents:
        pane = dict(agent)
        pane.pop("name", None)
        panes.append(pane)

    return {
        "version": "0.7.5",
        "protocol": 18,
        "focused_workspace_id": "w1",
        "focused_tab_id": "w1:t3",
        "focused_pane_id": "w1:p3",
        "workspaces": workspaces,
        "tabs": tabs,
        "panes": panes,
        "agents": agents,
        "layouts": [],
    }


PREVIEW_TEXT = """● Ready · pacebeats
  Capacity: 0/32 · new sessions run in an isolated worktree

> the cards still read as meaningless, can you look at the copy?

⏺ Reading src/components/WeekCards.tsx …
  Found 3 call sites that render the summary string.
"""
