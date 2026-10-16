"""One reconnecting background watcher per Herdr server, for tab titles and status."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Callable, Optional, Union

from . import host
from .auto_title import AutoTitles, title_state_dir
from .client import EventStream, HerdrClient, HerdrError
from .config import Config
from .renaming_guard import (
    AutomaticTabClient,
    RenamingPluginDetected,
    check_plugins,
    installed_plugins,
)
from .tab_status import TabStatus

INTERVAL = 2
REGISTRATION_INTERVAL = 10
MAX_BACKOFF = 30
# In place of an inherited descriptor: the worker claims the lock file by name.
LOCK_BY_NAME = "-"


def start(resume: bool = False) -> None:
    """Best-effort launch; unavailable state storage must never break the popup."""
    titles = AutoTitles()
    directory = titles.state_dir
    if directory is None or not os.environ.get("HERDR_SOCKET_PATH"):
        return
    if not titles.enabled and not Config.load().tab_status:
        records = TabStatus(HerdrClient(socket_only=True), directory).records
        if not any(not record.get("yielded") for record in records.values()):
            return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "title-launch.lock").open("a") as gate:
            try:
                host.lock(gate, blocking=resume)
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
            host.lock(lock)
        except BlockingIOError:
            return
        # Keep the singleton lock across spawn and any later exec replacement.
        with _log(directory) as log:
            _spawn(Path(__file__).resolve().parents[2] / "run.py", directory, log, lock)


def _log(directory: Path) -> IO:
    return (directory / "title-watcher.log").open("a", encoding="utf-8", errors="replace")


def _spawn(run_py: Path, directory: Path, log: IO, lock: Optional[IO]) -> None:
    """Start a detached worker that holds, or is about to claim, the lock.

    The worker runs in its state directory, not in the plugin's: Windows does
    not let Herdr replace a directory that a running process works in, so a
    long-lived worker there would make every plugin update fail.
    """
    if host.WINDOWS:
        # CreateProcess cannot hand over a locked handle. Let go so the worker
        # can claim the file by name; a launcher racing into that gap starts a
        # second worker, which loses the claim and exits.
        if lock is not None:
            host.unlock(lock)
        token = LOCK_BY_NAME
    else:
        token = str(lock.fileno())
    subprocess.Popen(
        [sys.executable, str(run_py), "--watch-titles", token],
        cwd=str(directory), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        **host.detached_process_options(lock),
    )


def stop() -> None:
    directory = title_state_dir()
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "title-watcher.stopped").touch()


def finish_if_stopped(lock, directory: Path,
                      cleanup: Optional[Callable[[], None]] = None) -> bool:
    """Close the worker lock under the launch gate to make stop/resume atomic."""
    stopped = directory / "title-watcher.stopped"
    if not stopped.exists():
        return False
    try:
        with (directory / "title-launch.lock").open("a") as gate:
            host.lock(gate, blocking=True)
            if stopped.exists():
                # Still holding the lock, so no successor can decorate meanwhile.
                if cleanup is not None:
                    cleanup()
                lock.close()
                return True
    except OSError:
        pass
    return False


def registered_root(client: HerdrClient) -> Optional[Path]:
    """Ask the server, including in named sessions and custom socket locations."""
    plugins = installed_plugins(client)
    check_plugins(plugins, Config.load().tab_renaming_plugins)
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


def replace(root: Path, lock: Union[int, IO]) -> None:
    """Hand this server's watcher over to the plugin files now registered."""
    if host.WINDOWS and not isinstance(lock, int):
        # No exec on Windows. Release the lock, then start a worker that
        # claims it; a startup hook racing into the gap loses and exits.
        directory = Path(lock.name).parent
        lock.close()
        with _log(directory) as log:
            _spawn(root / "run.py", directory, log, None)
        return
    # exec keeps the lock held, so no startup hook can mistake this handoff for
    # a stopped worker, and the replacement cannot race an old writer.
    lock_fd = lock if isinstance(lock, int) else lock.fileno()
    os.set_inheritable(lock_fd, True)
    os.execv(sys.executable, [sys.executable, str(root / "run.py"),
                             "--watch-titles", str(lock_fd)])


def claim(directory: Path) -> Optional[IO]:
    """Take the worker lock by name; None when another worker holds it."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "title-watcher.lock").open("a")
    try:
        host.lock(lock)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def watch(lock_fd: Optional[int]) -> int:
    client = HerdrClient(timeout=2, socket_only=True)
    titles = AutoTitles()
    if titles.state_dir is None:
        if lock_fd is not None:
            os.close(lock_fd)
        return 0
    lock = os.fdopen(lock_fd, "a") if lock_fd is not None else claim(titles.state_dir)
    if lock is None:
        return 0
    writer = AutomaticTabClient(client)
    tabs = TabStatus(writer, titles.state_dir)
    # Status changes arrive as events, so idle tabs cost no polling beyond titles.
    events = EventStream(getattr(client, "socket_path", None))
    current_root = Path(__file__).resolve().parents[2]
    current_revision = revision(current_root)
    next_registration = 0.0
    next_poll = 0.0
    delay = INTERVAL
    last_error = ""
    with lock:
        while True:
            if finish_if_stopped(lock, titles.state_dir, tabs.clear):
                return 0
            now = time.monotonic()
            deadline = now + delay
            try:
                if now >= next_registration:
                    root = registered_root(client)
                    if root is None:
                        tabs.clear()
                        return 0
                    if root != current_root or revision(root) != current_revision:
                        replace(root, lock)
                        return 0
                    next_registration = now + REGISTRATION_INTERVAL
                if now >= next_poll:
                    config = Config.load()
                    titles.configure(config)
                    tabs.configure(config)
                    if not (titles.enabled or tabs.enabled):
                        tabs.clear()
                        events.close()
                        return 0
                    snapshot = titles.apply(tabs, tabs.snapshot())
                    next_poll = now + INTERVAL
                else:
                    snapshot = tabs.snapshot()
                deadline = next_poll
                tabs.apply(snapshot)
                if writer.conflict:
                    raise RenamingPluginDetected(writer.conflict)
                try:
                    events.follow(tabs.subscriptions())
                except HerdrError:
                    pass  # e.g. a pane closed since the snapshot; polling covers it
                delay = INTERVAL
                last_error = ""
            except RenamingPluginDetected as error:
                # Do not even restore our prefixes: another writer owns names now.
                print("herdr-bar: %s" % error, file=sys.stderr, flush=True)
                events.close()
                return 0
            except (HerdrError, OSError) as error:
                # Stay available across server shutdown/restart. Never fall back
                # to a CLI that might address a different server or spawn one.
                message = str(error)
                if message != last_error:
                    print("herdr-bar: title watcher retrying: %s" % message,
                          file=sys.stderr, flush=True)
                    last_error = message
                events.close()
                next_registration = next_poll = 0
                delay = min(MAX_BACKOFF, delay * 2)
                deadline = time.monotonic() + delay
            # An explicit stop is noticed even while reconnecting with backoff.
            while time.monotonic() < deadline:
                if finish_if_stopped(lock, titles.state_dir, tabs.clear):
                    return 0
                if events.wait(min(1, max(0, deadline - time.monotonic()))):
                    next_poll = 0
                    break
