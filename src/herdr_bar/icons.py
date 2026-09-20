"""Optional Herdr Agent Icons Max vendor marks, compatible with herdr-radar.

Codepoints: https://github.com/hhdebb/herdr-radar/blob/main/tools/codepoints.toml
The popup never installs fonts or changes terminal configuration.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

FAMILY = "Herdr Agent Icons Max"
VENDORS = (
    "claude", "codex", "opencode", "omp", "cline", "mastracode", "kimi", "kilo",
    "maki", "pi", "hermes", "cursor", "copilot", "deepseek", "gemini", "gpt",
    "qwen", "grok", "agy", "kiro", "amp", "devin", "qodercli", "glm",
)
LOGOS = {name: chr(0xE1A0 + index) for index, name in enumerate(VENDORS)}


@lru_cache(maxsize=1)
def font_available() -> bool:
    """Best-effort local detection, once per popup; remote fonts need opt-in."""
    if sys.platform == "darwin":
        directories = [Path.home() / "Library/Fonts", Path("/Library/Fonts")]
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        directories = [data / "fonts", Path.home() / ".fonts"]
    for directory in directories:
        if any(directory.glob("HerdrAgentIconsMax*.ttf")):
            return True
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["fc-match", "--format", "%{family}", FAMILY],
                capture_output=True, text=True, timeout=1, check=False,
            )
            families = [name.strip() for name in result.stdout.split(",")]
            return result.returncode == 0 and FAMILY in families
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def enabled(mode: object) -> bool:
    if mode == "font":
        return True
    return mode == "auto" and font_available()


def logo_for(agent: Optional[str]) -> str:
    kind = (agent or "").strip().lower().split(":", 1)[0]
    if kind.endswith(" code"):
        kind = kind[:-5]
    kind = {"cursor-agent": "cursor", "open-code": "opencode"}.get(kind, kind)
    return LOGOS.get(kind, "")
