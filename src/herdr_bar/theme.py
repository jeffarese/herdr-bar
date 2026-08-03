"""Colors.

Defaults are plain ANSI colors, so the bar inherits whatever theme the
terminal (and therefore Herdr) already uses. Status glyphs and colors mirror
Herdr's own sidebar so the popup reads as part of the app. Anything can be
overridden with a hex value from the plugin config.
"""

from __future__ import annotations

from typing import Dict, Optional

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
UNDERLINE = "\x1b[4m"

_NAMED = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "bright_black": 8,
    "gray": 8,
    "grey": 8,
    "bright_red": 9,
    "bright_green": 10,
    "bright_yellow": 11,
    "bright_blue": 12,
    "bright_magenta": 13,
    "bright_cyan": 14,
    "bright_white": 15,
}

# Three tiers of foreground, so a row reads in order: the title (text), the
# things you scan for once the title has landed (muted), and the punctuation
# that only exists to separate them (dim). "unknown" is an alias of dim kept
# for the status role and for configs written against the older name.
DEFAULTS = {
    "accent": "bright_blue",
    "text": "default",
    "muted": "244",
    "blocked": "bright_red",
    "working": "bright_yellow",
    "done": "bright_cyan",
    "idle": "bright_green",
    "unknown": "bright_black",
    "match": "bright_blue",
    # Agent labels stay textual in the popup, but distinct colors make mixed
    # teams scannable without depending on terminal image support.
    "agent_claude": "#D97757",
    "agent_codex": "#10A37F",
    "agent_kimi": "#9B8AFB",
    "agent_gemini": "#4285F4",
    "agent_cursor": "#A78BFA",
    "agent_opencode": "#38BDF8",
}

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Mirrors herdr's agent_icon()/state_label_color() so the bar and the
# sidebar agree at a glance.
STATUS_GLYPHS = {
    "blocked": "◉",
    "done": "●",
    "idle": "✓",
    "unknown": "○",
    "none": "·",
}


def _parse(value: Optional[str]) -> Optional[str]:
    """Return the SGR parameters for a color spec, or None for 'default'."""
    if value is None:
        return None
    spec = str(value).strip().lower()
    if not spec or spec in ("default", "none", "reset", "inherit"):
        return None
    if spec.startswith("#") and len(spec) == 7:
        try:
            red = int(spec[1:3], 16)
            green = int(spec[3:5], 16)
            blue = int(spec[5:7], 16)
        except ValueError:
            return None
        return "2;%d;%d;%d" % (red, green, blue)
    if spec in _NAMED:
        return "5;%d" % _NAMED[spec]
    if spec.isdigit() and 0 <= int(spec) <= 255:
        return "5;%d" % int(spec)
    return None


class Theme(object):
    def __init__(
        self,
        overrides: Optional[Dict[str, str]] = None,
        selection_background: Optional[str] = None,
        appearance: Optional[str] = None,
    ) -> None:
        merged = dict(DEFAULTS)
        muted = default_muted(appearance)
        if muted:
            merged["muted"] = muted
        merged.update({k: v for k, v in (overrides or {}).items() if isinstance(v, str)})
        self._fg: Dict[str, str] = {}
        for role, spec in merged.items():
            params = _parse(spec)
            self._fg[role] = "" if params is None else "\x1b[38;%sm" % params
        selection = _parse(selection_background)
        self.selection_bg = "" if selection is None else "\x1b[48;%sm" % selection

    def fg(self, role: str) -> str:
        return self._fg.get(role, "")

    def paint(self, role: str, text: str, bold: bool = False) -> str:
        color = self.fg(role)
        if not color and not bold:
            return text
        return "%s%s%s%s" % (BOLD if bold else "", color, text, RESET)

    def status_color(self, status: str) -> str:
        return self.fg(status if status in self._fg else "unknown")

    def status_glyph(self, status: str, tick: int) -> str:
        if status == "working":
            return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
        return STATUS_GLYPHS.get(status, STATUS_GLYPHS["unknown"])


def default_muted(appearance: Optional[str]) -> Optional[str]:
    """Pick the secondary foreground: legible, still clearly below the title.

    The neutral 244 survives either background, so an unknown appearance keeps
    the default rather than guessing.
    """
    if appearance == "dark":
        return "249"
    if appearance == "light":
        return "240"
    return None


def default_selection_background(appearance: Optional[str]) -> Optional[str]:
    """Pick a selection background that suits the terminal's own background."""
    if appearance == "dark":
        return "237"
    if appearance == "light":
        return "254"
    return None
