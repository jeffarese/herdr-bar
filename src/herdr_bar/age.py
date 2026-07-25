"""How long the process in a pane has been running.

A Herdr snapshot carries no clocks -- no start time, no timestamp on a status
change -- so the only honest answer comes from the operating system.
``pane.process_info`` names the process living in a pane and ``ps`` says how
old it is. A start time never moves, so one reading per pane is enough for as
long as the bar is open; everything after that is arithmetic.

For an agent pane the answer is the age of the agent session. For a plain tab
it is the age of whatever the shell is running, or of the shell itself when it
is sitting at a prompt -- which reads as the age of the tab.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def pid_for(info: Dict[str, Any]) -> Optional[int]:
    """The process whose age stands for the pane's."""
    leader = info.get("foreground_process_group_id")
    pids = []
    processes = info.get("foreground_processes")
    if isinstance(processes, list):
        for process in processes:
            pid = process.get("pid") if isinstance(process, dict) else None
            if isinstance(pid, int) and pid > 0:
                pids.append(pid)
    if isinstance(leader, int) and leader in pids:
        # The group leader is the command you actually started; its children
        # (an MCP server, a `caffeinate`) came later and would read as younger.
        return leader
    if pids:
        return pids[0]
    shell = info.get("shell_pid")
    return shell if isinstance(shell, int) and shell > 0 else None


def parse_etime(text: str) -> Optional[float]:
    """Read ps(1) elapsed time -- ``[[dd-]hh:]mm:ss`` -- as seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        try:
            days = int(head)
        except ValueError:
            return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    seconds = 0
    for number in numbers:
        if number < 0:
            return None
        seconds = seconds * 60 + number
    total = days * DAY + seconds
    return float(total) if total >= 0 else None


def elapsed_seconds(pid: int, timeout: float = 1.0) -> Optional[float]:
    """Seconds since ``pid`` started, or None if it cannot be read."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,  # a dead pid exits non-zero with no output
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_etime(proc.stdout.decode("ascii", "replace"))


def format_age(seconds: float) -> str:
    """A running time short enough to sit in a row: 42s, 7m, 2h04m, 3d4h."""
    total = int(seconds)
    if total < 0:
        return ""
    if total < MINUTE:
        return "%ds" % total
    minutes = total // MINUTE
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%dh" % (days, hours)
