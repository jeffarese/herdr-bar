"""Fill unnamed Claude tabs from local transcripts, leaving named work alone.

Only runs while the popup is open. No model calls, transcript uploads, or
terminal/branch-derived names. A successful name is left alone on later opens,
just like a name supplied by a person or another plugin.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .client import HerdrClient, HerdrError
from .textutil import sanitize, truncate

MAX_SCAN = 2 * 1024 * 1024
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _title(value: Any) -> str:
    return truncate(" ".join(sanitize(value).split()), 80) if isinstance(value, str) else ""


def _opening(content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if not isinstance(content, str):
        return ""
    command = re.search(r"<command-name>\s*/?([^<\s]+)\s*</command-name>", content)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        return _title(command[1] + " " + (args[1].strip().split("\n")[0] if args else ""))
    content = re.sub(r"<[a-z][a-z-]*>.*?</[a-z][a-z-]*>", " ", content, flags=re.S)
    return _title(content.strip().split("\n")[0])


class TranscriptReader:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def title(self, session_id: str, cwd: str) -> str:
        if not SESSION_ID.fullmatch(session_id):
            return ""
        state = self.sessions.setdefault(session_id, {
            "path": None, "offset": 0, "identity": None, "title": "", "opening": "",
            "searched": float("-inf"),
        })
        if state["path"] is None:
            if time.monotonic() - state["searched"] < 10:
                return ""
            state["searched"] = time.monotonic()
            projects = self.root / "projects"
            candidate = projects / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / (session_id + ".jsonl")
            try:
                if not candidate.is_file():
                    candidate = next(projects.glob("*/" + session_id + ".jsonl"), None)
                if candidate is None:
                    return ""
                state["path"] = candidate
            except OSError:
                return ""
        try:
            with state["path"].open("rb") as handle:
                info = os.fstat(handle.fileno())
                identity = (info.st_dev, info.st_ino)
                if state["identity"] != identity or info.st_size < state["offset"]:
                    state.update(offset=0, title="", opening="", identity=identity)
                start = max(state["offset"], info.st_size - MAX_SCAN)
                handle.seek(start)
                data = handle.read(MAX_SCAN)
        except OSError:
            state.update(path=None, offset=0, title="", opening="")
            return ""
        end = data.rfind(b"\n")
        if end >= 0:
            lines = data[:end].split(b"\n")
            if start > state["offset"]:
                lines = lines[1:]  # tail starts inside a record
            state["offset"] = start + end + 1
            for line in lines:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "ai-title":
                    state["title"] = _title(record.get("aiTitle")) or state["title"]
                elif record.get("type") == "user" and not state["opening"]:
                    origin, message = record.get("origin"), record.get("message")
                    if (isinstance(origin, dict) and origin.get("kind") == "human"
                            and isinstance(message, dict)):
                        state["opening"] = _opening(message.get("content"))
        return state["title"] or state["opening"]


def _candidates(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Only default tabs with one unambiguously identified, unnamed agent."""
    panes = {pane.get("pane_id"): pane for pane in snapshot.get("panes", [])}
    positions: Dict[str, int] = {}
    candidates = {}
    for tab in snapshot.get("tabs", []):
        workspace = tab.get("workspace_id", "")
        positions[workspace] = positions.get(workspace, 0) + 1
        if tab.get("label") not in (None, "", str(positions[workspace])):
            continue
        agents = [agent for agent in snapshot.get("agents", [])
                  if agent.get("tab_id") == tab.get("tab_id")]
        if len(agents) != 1:
            continue
        agent = agents[0]
        pane = panes.get(agent.get("pane_id"), {})
        # A pane label or agent name is custom even if it happens to say
        # "claude". Never infer ownership from equality to a generated title.
        if not pane or pane.get("label") or agent.get("name"):
            continue
        session = agent.get("agent_session") or pane.get("agent_session") or {}
        if (agent.get("agent") != "claude" or session.get("agent") != "claude"
                or session.get("kind") != "id"):
            continue
        session_id = session.get("value")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            continue
        candidates[tab["tab_id"]] = {
            "session": session_id, "pane": agent.get("pane_id"),
            "terminal": pane.get("terminal_id"), "label": tab.get("label"),
            "cwd": agent.get("cwd") or pane.get("cwd") or "",
        }
    return candidates


class AutoTitles:
    def __init__(self) -> None:
        self.enabled = os.environ.get("HERDR_AUTO_TITLE_TRANSCRIPT", "true").lower() not in (
            "0", "false", "no", "off",
        )
        self.reader = TranscriptReader()

    def apply(self, client: HerdrClient, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return snapshot
        candidates = _candidates(snapshot)
        live = {candidate["session"] for candidate in candidates.values()}
        self.reader.sessions = {
            key: value for key, value in self.reader.sessions.items() if key in live
        }
        for tab_id, candidate in candidates.items():
            title = self.reader.title(candidate["session"], candidate["cwd"])
            if not title:
                continue
            try:
                # Disk reads can take time: check names and session identity
                # again immediately before writing, including custom pane names.
                snapshot = client.snapshot()
                if _candidates(snapshot).get(tab_id) != candidate:
                    continue
                client.rename_tab(tab_id, title)
            except HerdrError:
                continue  # Optional naming must never prevent opening the bar.
            for tab in snapshot.get("tabs", []):
                if tab.get("tab_id") == tab_id:
                    tab["label"] = title
        return snapshot
