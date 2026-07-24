"""Recently visited rows, persisted under the plugin state directory.

Ordering an empty bar by "where you have been" is what makes the first
Enter useful: the top row is the tab you came from, so open-and-Enter behaves
like alt-tab.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, List, Optional

MAX_ENTRIES = 120


class Recents(object):
    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self.entries: List[Dict[str, object]] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return
        cleaned: List[Dict[str, object]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            cleaned.append(
                {
                    "key": key,
                    "title": entry.get("title") if isinstance(entry.get("title"), str) else "",
                    "ts": entry.get("ts") if isinstance(entry.get("ts"), (int, float)) else 0,
                }
            )
        self.entries = cleaned[:MAX_ENTRIES]

    def rank(self, key: str, title: str = "") -> Optional[int]:
        """Position in the recent list, 0 being most recent."""
        for index, entry in enumerate(self.entries):
            if entry["key"] == key:
                return index
        if title:
            for index, entry in enumerate(self.entries):
                if entry.get("title") == title:
                    return index
        return None

    def touch(self, key: str, title: str = "") -> None:
        self.entries = [entry for entry in self.entries if entry["key"] != key]
        self.entries.insert(0, {"key": key, "title": title, "ts": int(time.time())})
        del self.entries[MAX_ENTRIES:]

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(self.path)
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", dir=directory or ".", delete=False, prefix=".recent-", suffix=".json"
            )
            try:
                json.dump({"version": 1, "entries": self.entries}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)
        except OSError:
            # Losing the recent list is not worth failing a jump over.
            pass
