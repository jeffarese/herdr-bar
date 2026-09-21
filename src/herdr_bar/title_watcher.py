"""One reconnecting background title watcher per Herdr server."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .auto_title import AutoTitles, title_state_dir
from .client import HerdrClient, HerdrError

INTERVAL = 2
REGISTRATION_INTERVAL = 10
MAX_BACKOFF = 30


def start(resume: bool = False) -> None:
    """Best-effort launch; unavailable state storage must never break the popup."""
    titles = AutoTitles()
    directory = titles.state_dir
    if not titles.enabled or directory is None or not os.environ.get("HERDR_SOCKET_PATH"):
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "title-launch.lock").open("a") as gate:
            try:
                flags = fcntl.LOCK_EX | (0 if resume else fcntl.LOCK_NB)
                fcntl.flock(gate, flags)
            except BlockingIOError:
                return
            _launch(directory, resume)
    except OSError as error:
        print("herdr-bar: title watcher could not start: %s" % error, file=sys.stderr)


def _launch(directory: Path, resume: bool) -> None:
    stopped = directory / "title-watcher.stopped"
    if resume:
        stopped.unlink(missing_ok=True)
    elif stopped.exists():
        return
    with (directory / "title-watcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Keep the singleton lock across spawn and any later exec replacement.
        with (directory / "title-watcher.log").open("a") as log:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parents[2] / "run.py"),
                 "--watch-titles", str(lock.fileno())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, pass_fds=(lock.fileno(),),
            )


def stop() -> None:
    directory = title_state_dir()
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "title-watcher.stopped").touch()


def finish_if_stopped(lock, directory: Path) -> bool:
    """Close the worker lock under the launch gate to make stop/resume atomic."""
    stopped = directory / "title-watcher.stopped"
    if not stopped.exists():
        return False
    try:
        with (directory / "title-launch.lock").open("a") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            if stopped.exists():
                lock.close()
                return True
    except OSError:
        pass
    return False


def registered_root(client: HerdrClient) -> Optional[Path]:
    """Ask the server, including in named sessions and custom socket locations."""
    result = client.call("plugin.list", {}, ["plugin", "list"])
    plugins = result.get("plugins")
    if not isinstance(plugins, list):
        raise HerdrError("plugin.list did not return a plugin list")
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("plugin_id") == "herdr-bar":
            if not plugin.get("enabled"):
                return None
            root = plugin.get("plugin_root")
            if not isinstance(root, str) or not root:
                raise HerdrError("plugin root missing from plugin.list")
            return Path(root).resolve()
    return None


def revision(root: Path) -> tuple:
    """Detect in-place upgrades as well as installs that move the plugin root."""
    files = [root / "run.py", *sorted((root / "src" / "herdr_bar").glob("*.py"))]
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in files)


def replace(root: Path, lock_fd: int) -> None:
    # exec keeps the lock held, so no startup hook can mistake this handoff for
    # a stopped worker, and the replacement cannot race an old writer.
    os.set_inheritable(lock_fd, True)
    os.execv(sys.executable, [sys.executable, str(root / "run.py"),
                             "--watch-titles", str(lock_fd)])


def watch(lock_fd: int) -> int:
    client = HerdrClient(timeout=2, socket_only=True)
    titles = AutoTitles()
    if titles.state_dir is None:
        os.close(lock_fd)
        return 0
    current_root = Path(__file__).resolve().parents[2]
    current_revision = revision(current_root)
    next_registration = 0.0
    delay = INTERVAL
    last_error = ""
    with os.fdopen(lock_fd, "a") as lock:
        while titles.enabled:
            if finish_if_stopped(lock, titles.state_dir):
                return 0
            try:
                now = time.monotonic()
                if now >= next_registration:
                    root = registered_root(client)
                    if root is None:
                        return 0
                    if root != current_root or revision(root) != current_revision:
                        replace(root, lock_fd)
                        return 0
                    next_registration = now + REGISTRATION_INTERVAL
                titles.apply(client, client.snapshot())
                delay = INTERVAL
                last_error = ""
            except (HerdrError, OSError) as error:
                # Stay available across server shutdown/restart. Never fall back
                # to a CLI that might address a different server or spawn one.
                message = str(error)
                if message != last_error:
                    print("herdr-bar: title watcher retrying: %s" % message,
                          file=sys.stderr, flush=True)
                    last_error = message
                next_registration = 0
                delay = min(MAX_BACKOFF, delay * 2)
            # An explicit stop is noticed even while reconnecting with backoff.
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if finish_if_stopped(lock, titles.state_dir):
                    return 0
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    return 0
