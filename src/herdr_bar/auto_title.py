"""Follow agent task titles while preserving names supplied by the user."""

from __future__ import annotations

import fcntl
import hashlib
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


def title_state_dir() -> Optional[Path]:
    directory = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not directory:
        return None
    if not socket_path:
        return Path(directory)
    key = hashlib.sha256(os.path.abspath(socket_path).encode()).hexdigest()[:24]
    return Path(directory) / "title-servers" / key


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
            "searched": float("-inf"), "boundary": b"",
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
                # Inodes can be reused after unlink, and a transcript may be
                # rewritten in place. Check the bytes behind our cursor too.
                handle.seek(max(0, state["offset"] - 64))
                boundary = handle.read(min(state["offset"], 64))
                if (state["identity"] != identity or info.st_size < state["offset"]
                        or boundary != state["boundary"]):
                    state.update(offset=0, title="", opening="", identity=identity)
                start = max(state["offset"], info.st_size - MAX_SCAN)
                handle.seek(start)
                data = handle.read(MAX_SCAN)
                end = data.rfind(b"\n")
                if end >= 0:
                    next_offset = start + end + 1
                    handle.seek(max(0, next_offset - 64))
                    state["boundary"] = handle.read(min(next_offset, 64))
        except OSError:
            state.update(path=None, offset=0, title="", opening="")
            return ""
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


def _agent_title(agent: Dict[str, Any], pane: Dict[str, Any]) -> str:
    title = _title(agent.get("terminal_title_stripped") or pane.get("terminal_title_stripped"))
    cwd = agent.get("cwd") or pane.get("cwd") or ""
    folder = Path(cwd).name
    if folder and title.endswith(" | " + folder):
        title = title[:-(len(folder) + 3)].strip()
    generic = ("", "codex", "claude", "claude code", folder.casefold(), cwd.casefold())
    if title.casefold() in generic:
        return ""
    return title


_NAME_STRIP = re.compile(r"[^a-z0-9_-]+")


def _default_agent_name(name: Any, kind: Any, cwd: str) -> bool:
    """Whether an agent name is just the one derived from its folder.

    ``herdr agent start`` requires a name, so launchers such as
    herdr-newtab-plus fill it from the folder (``herdr-bar``, then
    ``herdr-bar-2``...). That is a default, not a name somebody chose.
    """
    if not isinstance(name, str) or not name:
        return False
    base = _NAME_STRIP.sub("-", Path(cwd.rstrip("/")).name.lower()).strip("-_")
    if not base or not base[0].isalpha():
        base = "%s-%s" % (kind, base) if base else str(kind)
    base = _NAME_STRIP.sub("-", base).strip("-_")[:32]
    if not base:
        return False
    if name == base:
        return True
    match = re.fullmatch(r"(.+)(-\d{1,2})", name)
    return bool(
        match and int(match[2][1:]) >= 2 and match[1] == base[: 32 - len(match[2])]
    )


def _candidates(
    snapshot: Dict[str, Any], owned: Optional[Dict] = None,
) -> Dict[str, Dict[str, Any]]:
    """Default or plugin-owned tabs with one unambiguously identified agent."""
    owned = owned or {}
    panes = {pane.get("pane_id"): pane for pane in snapshot.get("panes", [])}
    agents_by_tab: Dict[str, list] = {}
    for agent in snapshot.get("agents", []):
        agents_by_tab.setdefault(agent.get("tab_id"), []).append(agent)
    positions: Dict[str, int] = {}
    candidates = {}
    for tab in snapshot.get("tabs", []):
        workspace = tab.get("workspace_id", "")
        positions[workspace] = positions.get(workspace, 0) + 1
        agents = agents_by_tab.get(tab.get("tab_id"), [])
        if len(agents) != 1:
            continue
        agent = agents[0]
        pane = panes.get(agent.get("pane_id"), {})
        # A pane label or agent name is custom even if it happens to say
        # "claude". Never infer ownership from equality to a generated title.
        # The one exception is the folder-derived name a launcher had to pass.
        cwd = agent.get("cwd") or pane.get("cwd") or ""
        name = agent.get("name")
        if not pane or pane.get("label") or (
            name and not _default_agent_name(name, agent.get("agent"), cwd)
        ):
            continue
        session = agent.get("agent_session") or pane.get("agent_session") or {}
        kind = agent.get("agent")
        if (kind not in ("claude", "codex") or session.get("agent") != kind
                or session.get("kind") != "id"):
            continue
        session_id = session.get("value")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            continue
        identity = [kind, session_id, agent.get("pane_id"), pane.get("terminal_id")]
        previous = owned.get(tab["tab_id"], {})
        if not isinstance(previous, dict):
            previous = {}
        if tab.get("label") not in (None, "", str(positions[workspace])):
            if previous.get("identity") != identity or previous.get("title") != tab.get("label"):
                continue
        candidates[tab["tab_id"]] = {
            "identity": identity, "agent": kind,
            "suggested": _agent_title(agent, pane),
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
        self.owned: Dict[str, Any] = {}
        self.state_dir = title_state_dir()

    def apply(self, client: HerdrClient, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return snapshot
        if self.state_dir is None:
            return self._apply(client, snapshot)
        try:
            return self._persisted_apply(client, snapshot)
        except OSError:
            # Optional naming must not take down its caller. A later tick can
            # retry when permissions, disk space, or the state directory recover.
            return snapshot

    def _persisted_apply(self, client: HerdrClient, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "auto-titles.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return snapshot
            path = self.state_dir / "auto-titles.json"
            try:
                saved = json.loads(path.read_text())
                self.owned = saved if isinstance(saved, dict) else {}
            except FileNotFoundError:
                self.owned = {}
            except ValueError:
                self.owned = {}
            before = dict(self.owned)
            # Verify write access before renaming any tabs. The same staging
            # file is committed only if ownership changes, avoiding idle writes.
            staging = path.with_suffix(".tmp")
            result = self._apply(client, snapshot, staging)
            if self.owned != before:
                staging.write_text(json.dumps(self.owned))
                staging.replace(path)
            return result

    def _apply(
        self, client: HerdrClient, snapshot: Dict[str, Any], staging: Optional[Path] = None,
    ) -> Dict[str, Any]:
        candidates = _candidates(snapshot, self.owned)
        self.owned = {key: value for key, value in self.owned.items() if key in candidates}
        live = {candidate["session"] for candidate in candidates.values()}
        self.reader.sessions = {
            key: value for key, value in self.reader.sessions.items() if key in live
        }
        for tab_id, candidate in candidates.items():
            title = candidate["suggested"]
            if not title and candidate["agent"] == "claude":
                title = self.reader.title(candidate["session"], candidate["cwd"])
            if not title or title == candidate["label"]:
                continue
            try:
                # Disk reads can take time: check names and session identity
                # again immediately before writing, including custom pane names.
                snapshot = client.snapshot()
                if _candidates(snapshot, self.owned).get(tab_id) != candidate:
                    continue
                if staging is not None:
                    staging.write_text(json.dumps(self.owned))
                client.rename_tab(tab_id, title)
                self.owned[tab_id] = {"identity": candidate["identity"], "title": title}
            except HerdrError:
                continue  # Optional naming must never prevent opening the bar.
            for tab in snapshot.get("tabs", []):
                if tab.get("tab_id") == tab_id:
                    tab["label"] = title
        return snapshot
