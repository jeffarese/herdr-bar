"""Open the bar's popup through the Herdr CLI: what ``open-windows`` runs.

Herdr starts manifest commands without a shell, and Windows has no ``sh`` for
the one-liner behind ``open``. Herdr refuses a second action with the same id
even when the two list different ``platforms``, so Windows has its own action
and the CLI one-liner lives here. This module imports nothing the one-liner
does not need, which keeps the cost to the interpreter's own startup.
"""

from __future__ import annotations

import os
import sys
from typing import List

POPUP_ARGS = [
    "plugin", "pane", "open", "--plugin", "herdr-bar", "--entrypoint", "bar",
    "--placement", "popup",
]


def herdr_bin() -> str:
    """The Herdr binary to shell out to.

    HERDR_BIN_PATH can outlive the binary it names -- upgrading Herdr under a
    running server leaves the old path dangling -- so an unusable value falls
    back to whatever `herdr` resolves to on PATH.
    """
    candidate = os.environ.get("HERDR_BIN_PATH")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return "herdr"


def popup_command() -> List[str]:
    return [herdr_bin()] + POPUP_ARGS


def open_bar() -> int:
    command = popup_command()
    try:
        if sys.platform == "win32":
            # No exec on Windows: wait, so the CLI's exit code reaches Herdr.
            import subprocess
            return subprocess.run(command, check=False).returncode
        # Replace this interpreter, so nothing lingers behind the popup.
        os.execvp(command[0], command)
    except OSError as error:
        sys.stderr.write("herdr-bar: could not run herdr: %s\n" % error)
        return 2
    return 0  # pragma: no cover - execvp does not return
