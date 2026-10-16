"""The terminal the bar draws on: one backend per platform, same surface.

``Terminal`` is a context manager that owns raw mode and the alternate
screen, reports its size, says when input is waiting and hands over the raw
bytes. ``detect_appearance`` asks for the background color before any of
that starts. POSIX has a pty; Windows has a ConPTY console.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .term_windows import Terminal, detect_appearance
else:
    from .term_posix import Terminal, detect_appearance

__all__ = ["Terminal", "detect_appearance"]
