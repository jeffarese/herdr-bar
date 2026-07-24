"""User configuration.

Read from ``$HERDR_PLUGIN_CONFIG_DIR/config.json`` -- JSON rather than TOML so
the plugin keeps working on any Python 3.8+ without a parser dependency.
Unknown keys are ignored, and a broken file falls back to defaults instead of
taking the bar down.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "preview": "auto",  # true | false | "auto" (on when the popup is wide enough)
    "mouse": True,
    "spinner": True,
    "refresh_ms": 900,
    "workspaces": "auto",  # true | false | "auto" (on with more than one space)
    "colors": {},  # role -> "#rrggbb" | ansi name | 0-255
    "selection_background": "auto",  # "auto" | "none" | "#rrggbb" | 0-255
}


class Config(object):
    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(DEFAULTS)
        merged.update(values or {})
        self.preview = merged.get("preview", "auto")
        self.mouse = bool(merged.get("mouse", True))
        self.spinner = bool(merged.get("spinner", True))
        try:
            self.refresh_ms = max(200, int(merged.get("refresh_ms", 900)))
        except (TypeError, ValueError):
            self.refresh_ms = 900
        self.workspaces = merged.get("workspaces", "auto")
        colors = merged.get("colors")
        self.colors = colors if isinstance(colors, dict) else {}
        self.selection_background = merged.get("selection_background", "auto")

    @staticmethod
    def path(config_dir: Optional[str] = None) -> Optional[str]:
        directory = config_dir if config_dir is not None else os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        if not directory:
            return None
        return os.path.join(directory, "config.json")

    @classmethod
    def load(cls, config_dir: Optional[str] = None) -> "Config":
        path = cls.path(config_dir)
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(data)

    def wants_preview(self, available_width: int, threshold: int = 92) -> bool:
        if self.preview is True:
            return available_width >= 60
        if self.preview is False:
            return False
        return available_width >= threshold

    def wants_workspaces(self, workspace_count: int) -> bool:
        if self.workspaces is True:
            return True
        if self.workspaces is False:
            return False
        return workspace_count > 1
