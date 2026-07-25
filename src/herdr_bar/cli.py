"""Command line entry point."""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

from . import __version__
from .app import Bar, jump
from .client import HerdrClient, HerdrError
from .config import Config
from .items import build_items
from .mru import Recents
from .term import Terminal, detect_appearance
from .theme import Theme, default_selection_background

USAGE = """herdr-bar - jump to any Herdr tab or agent

usage: run.py [options]

options:
  --list      print the rows the bar would show, as JSON
  --doctor    print environment diagnostics
  --version   print the plugin version
  --help      show this message

Normally Herdr launches this itself:
  herdr plugin action invoke herdr-bar.open
"""


def state_path() -> Optional[str]:
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir:
        return None
    return os.path.join(state_dir, "recent.json")


def _fail(message: str) -> int:
    sys.stderr.write("herdr-bar: %s\n" % message)
    return 2


def _doctor(client: HerdrClient, config: Config) -> int:
    lines = [
        "herdr-bar %s" % __version__,
        "python           %s" % sys.version.split()[0],
        "herdr bin        %s" % client.bin_path,
        "socket           %s" % (client.socket_path or "(unset)"),
        "socket usable    %s" % ("yes" if client.socket_ok else "no, using the CLI"),
        "config           %s" % (Config.path() or "(no HERDR_PLUGIN_CONFIG_DIR)"),
        "state            %s" % (state_path() or "(no HERDR_PLUGIN_STATE_DIR)"),
        "preview          %s" % config.preview,
        "refresh          %dms" % config.refresh_ms,
        "stdin is a tty   %s" % ("yes" if sys.stdin.isatty() else "no"),
    ]
    try:
        snapshot = client.snapshot()
    except HerdrError as error:
        lines.append("herdr            unreachable: %s" % error)
        sys.stdout.write("\n".join(lines) + "\n")
        return 1
    items = build_items(snapshot)
    lines.append("herdr            %s" % snapshot.get("version", "?"))
    lines.append(
        "rows             %d (%d workspaces, %d tabs, %d agents)"
        % (
            len(items),
            len(snapshot.get("workspaces") or []),
            len(snapshot.get("tabs") or []),
            len(snapshot.get("agents") or []),
        )
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _list(client: HerdrClient) -> int:
    snapshot = client.snapshot()
    payload = [
        {
            "kind": item.kind,
            "key": item.key,
            "title": item.title,
            "subtitle": item.subtitle,
            "workspace": item.workspace_label,
            "tab_id": item.tab_id,
            "pane_id": item.pane_id,
            "agent": item.agent,
            "status": item.status,
            "tab": item.tab_number,
            "focused": item.focused,
        }
        for item in build_items(snapshot)
    ]
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        sys.stdout.write(USAGE)
        return 0
    if "--version" in args:
        sys.stdout.write("herdr-bar %s\n" % __version__)
        return 0

    config = Config.load()
    client = HerdrClient()

    if "--doctor" in args:
        return _doctor(client, config)
    try:
        if "--list" in args:
            return _list(client)
    except HerdrError as error:
        return _fail(str(error))

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _fail("needs a terminal; run it from Herdr with `plugin pane open`")

    recents = Recents(state_path())
    selection_background = config.selection_background
    # One appearance query, and only when something still depends on it: it
    # costs a round trip to the terminal on every open.
    appearance = None
    if selection_background == "auto" or "muted" not in (config.colors or {}):
        appearance = detect_appearance()
    if selection_background == "auto":
        selection_background = default_selection_background(appearance)
    theme = Theme(config.colors, selection_background, appearance)

    bar = Bar(client, config, recents, theme)
    try:
        bar.bootstrap()
    except HerdrError as error:
        return _fail(str(error))

    with Terminal(mouse=config.mouse) as terminal:
        chosen = bar.run(terminal)

    if chosen is None:
        return 0
    try:
        jump(client, chosen)
    except HerdrError as error:
        return _fail(str(error))

    if bar.current_key and bar.current_key != chosen.key:
        recents.touch(bar.current_key, bar.current_title)
    recents.touch(chosen.key, chosen.title)
    recents.save()
    return 0
