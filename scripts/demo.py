#!/usr/bin/env python3
"""Run the bar against fixture data, with no Herdr server involved.

    python3 scripts/demo.py            # interactive, prints the chosen row
    python3 scripts/demo.py --frame    # print one static frame and exit
    python3 scripts/demo.py --frame --size 76x14 --query "week"
    python3 scripts/demo.py --frame --plain    # no colors, for the README

Useful while iterating on the UI, and for capturing screenshots.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from herdr_bar.app import Bar  # noqa: E402
from herdr_bar.config import Config  # noqa: E402
from herdr_bar.mru import Recents  # noqa: E402
from herdr_bar.term import Terminal  # noqa: E402
from herdr_bar.theme import Theme  # noqa: E402
from tests.fixtures import PREVIEW_TEXT, snapshot  # noqa: E402


class FakeClient(object):
    socket_path = None
    bin_path = "herdr"
    socket_ok = False

    def snapshot(self):
        return snapshot()

    def read_pane(self, pane_id, lines, source="visible"):
        return PREVIEW_TEXT

    def focus_workspace(self, workspace_id):
        pass

    def focus_tab(self, tab_id):
        pass

    def focus_agent(self, target):
        pass


class FakeTerminal(object):
    def __init__(self, width, height):
        self._size = (width, height)
        self.buffer = []

    def size(self):
        return self._size

    def write(self, text):
        self.buffer.append(text)

    def flush(self):
        pass


def build(config=None):
    bar = Bar(
        FakeClient(),
        config or Config({"selection_background": "237"}),
        Recents(None),
        Theme(selection_background="237"),
    )
    bar.bootstrap()
    return bar


def main(argv):
    width, height = 104, 22
    query = ""
    for index, arg in enumerate(argv):
        if arg == "--size" and index + 1 < len(argv):
            raw = argv[index + 1].lower().split("x")
            width, height = int(raw[0]), int(raw[1])
        if arg == "--query" and index + 1 < len(argv):
            query = argv[index + 1]

    bar = build()
    if query:
        bar.set_query(query)

    if "--frame" in argv:
        terminal = FakeTerminal(width, height)
        bar.draw(terminal)
        if bar._preview_pending:  # skip the debounce the draw just armed
            bar._preview_pending = (bar._preview_pending[0], 0.0)
        bar.pump_preview(bar._list_height)
        terminal.buffer = []
        bar.draw(terminal)
        frame = _flatten("".join(terminal.buffer), height)
        if "--plain" in argv:
            frame = re.sub(r"\x1b\[[0-9;]*m", "", frame)
        sys.stdout.write(frame)
        return 0

    with Terminal(mouse=True) as terminal:
        chosen = bar.run(terminal)
    sys.stdout.write("chosen: %s\n" % (chosen.title if chosen else "(cancelled)"))
    return 0


def _flatten(frame: str, height: int) -> str:
    """Turn the renderer's absolute-positioned output into plain lines."""
    rows = [""] * height
    for match in re.finditer(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|\x1b\[J|$)", frame, re.S):
        index = int(match.group(1)) - 1
        if 0 <= index < height:
            rows[index] = match.group(2).replace("\x1b[K", "")
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
