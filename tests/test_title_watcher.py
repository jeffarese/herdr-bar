import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_bar import title_watcher
from herdr_bar.auto_title import AutoTitles, title_state_dir
from herdr_bar.client import HerdrClient, HerdrError

from .test_app import FakeClient
from .test_auto_title import snapshot

ROOT = Path(title_watcher.__file__).resolve().parents[2]


class WatcherTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env = patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": temp.name, "HERDR_SOCKET_PATH": temp.name + "/custom.sock",
            "HERDR_AUTO_TITLE_TRANSCRIPT": "true",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.directory = title_state_dir()
        self.directory.mkdir(parents=True)

    def test_repeated_start_does_not_spawn_another_watcher(self):
        with (self.directory / "title-watcher.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(title_watcher.subprocess, "Popen") as spawn:
                title_watcher.start()
                spawn.assert_not_called()
        with patch.object(title_watcher.subprocess, "Popen") as spawn:
            title_watcher.start()
            self.assertTrue(spawn.call_args.kwargs["pass_fds"])
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])

    def test_unwritable_state_or_spawn_failure_does_not_break_popup(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError), \
                patch("sys.stderr"):
            title_watcher.start()
        with patch.object(title_watcher.subprocess, "Popen", side_effect=OSError), \
                patch("sys.stderr"):
            title_watcher.start()
        # Failed spawn must release the singleton lock.
        self.test_repeated_start_does_not_spawn_another_watcher()

    def test_stop_prevents_event_restarts_and_explicit_resume_clears_it(self):
        title_watcher.stop()
        with patch.object(title_watcher.subprocess, "Popen") as spawn:
            title_watcher.start()
            spawn.assert_not_called()
            title_watcher.start(resume=True)
            spawn.assert_called_once()

    def test_stop_resume_interleavings_do_not_lose_the_watcher(self):
        with (self.directory / "title-watcher.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            title_watcher.stop()
            with patch.object(title_watcher.subprocess, "Popen") as spawn:
                title_watcher.start(resume=True)
                self.assertFalse(title_watcher.finish_if_stopped(lock, self.directory))
                spawn.assert_not_called()
                title_watcher.stop()
                self.assertTrue(title_watcher.finish_if_stopped(lock, self.directory))
                title_watcher.start(resume=True)
                spawn.assert_called_once()

    def test_server_scoped_state_separates_named_sessions(self):
        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/other/session.sock"}):
            self.assertNotEqual(title_state_dir(), self.directory)

    def test_registration_uses_api_instead_of_socket_directory(self):
        client = Mock()
        client.call.return_value = {"plugins": [{"plugin_id": "herdr-bar", "enabled": True,
                                                 "plugin_root": str(ROOT)}]}
        self.assertEqual(title_watcher.registered_root(client), ROOT)
        client.call.return_value["plugins"][0]["enabled"] = False
        self.assertIsNone(title_watcher.registered_root(client))
        client.call.return_value = {"plugins": []}
        self.assertIsNone(title_watcher.registered_root(client))
        client.call.return_value = {}
        with self.assertRaises(HerdrError):
            title_watcher.registered_root(client)

    def test_socket_only_reconnects_and_never_falls_back_to_cli(self):
        client = HerdrClient(socket_only=True)
        with patch.object(client, "_socket_call", side_effect=[
            OSError("server stopped"), {"result": {"snapshot": {"tabs": []}}},
        ]) as call, patch.object(client, "_cli") as cli:
            with self.assertRaises(HerdrError):
                client.snapshot()
            self.assertEqual(client.snapshot(), {"tabs": []})
            self.assertEqual(call.call_count, 2)
            cli.assert_not_called()

    def test_exec_handoff_preserves_singleton_lock(self):
        with tempfile.TemporaryFile() as lock:
            with patch.object(os, "execv") as execute:
                title_watcher.replace(ROOT, lock.fileno())
                self.assertTrue(os.get_inheritable(lock.fileno()))
                self.assertEqual(execute.call_args.args[1][-1], str(lock.fileno()))

    def test_watcher_recovers_after_server_restart_without_opening_popup(self):
        client = FakeClient(snapshot())
        agent = client._snapshot["agents"][0]
        agent["agent"] = agent["agent_session"]["agent"] = "codex"
        agent["terminal_title_stripped"] = "First title | app"
        titles = AutoTitles()
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            client.fail = 2 <= clock[0] < 20
            if clock[0] >= 20:
                agent["terminal_title_stripped"] = "After restart | app"
            if clock[0] >= 45:
                title_watcher.stop()

        with tempfile.TemporaryFile() as lock:
            with patch.object(title_watcher, "HerdrClient", return_value=client), \
                    patch.object(title_watcher, "AutoTitles", return_value=titles), \
                    patch.object(title_watcher, "registered_root", return_value=ROOT), \
                    patch.object(title_watcher.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(title_watcher.time, "sleep", side_effect=sleep), \
                    patch("sys.stderr"):
                self.assertEqual(title_watcher.watch(os.dup(lock.fileno())), 0)
        self.assertEqual([call[-1] for call in client.calls], ["First title", "After restart"])

    def test_disabled_plugin_exits_before_any_tab_operations(self):
        with tempfile.TemporaryFile() as lock:
            with patch.object(title_watcher, "registered_root", return_value=None), \
                    patch.object(title_watcher, "HerdrClient") as client:
                title_watcher.watch(os.dup(lock.fileno()))
                client.return_value.snapshot.assert_not_called()

    def test_replacement_root_hands_off_before_any_tab_operations(self):
        with tempfile.TemporaryFile() as lock:
            with patch.object(title_watcher, "registered_root", return_value=Path("/new/root")), \
                    patch.object(title_watcher, "replace") as replace, \
                    patch.object(title_watcher, "HerdrClient") as client:
                title_watcher.watch(os.dup(lock.fileno()))
                replace.assert_called_once()
                client.return_value.snapshot.assert_not_called()


class WatcherProcessTest(unittest.TestCase):
    def test_real_process_keeps_lock_updates_closed_popup_and_stops(self):
        import json
        import socketserver
        import subprocess
        import threading
        import time

        with tempfile.TemporaryDirectory() as temporary:
            # macOS Unix-domain socket paths must stay below 104 bytes.
            socket_path = str(Path(temporary) / "s")
            data = snapshot()
            agent = data["agents"][0]
            agent["agent"] = agent["agent_session"]["agent"] = "codex"
            agent["terminal_title_stripped"] = "Delayed task | app"
            renamed = threading.Event()

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    request = json.loads(self.rfile.readline())
                    method = request["method"]
                    if method == "plugin.list":
                        result = {"plugins": [{"plugin_id": "herdr-bar", "enabled": True,
                                               "plugin_root": str(ROOT)}]}
                    elif method == "session.snapshot":
                        result = {"snapshot": data}
                    elif method == "tab.rename":
                        data["tabs"][0]["label"] = request["params"]["label"]
                        result = {}
                        renamed.set()
                    else:
                        raise AssertionError(method)
                    self.wfile.write(json.dumps({"result": result}).encode() + b"\n")

            server = socketserver.UnixStreamServer(socket_path, Handler)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            processes = []
            try:
                with patch.dict(os.environ, {"HERDR_SOCKET_PATH": socket_path,
                                             "HERDR_PLUGIN_STATE_DIR": temporary,
                                             "HERDR_AUTO_TITLE_TRANSCRIPT": "true"}):
                    original_spawn = subprocess.Popen
                    def launch(*args, **kwargs):
                        process = original_spawn(*args, **kwargs)
                        processes.append(process)
                        return process

                    with patch.object(title_watcher.subprocess, "Popen",
                                      side_effect=launch) as spawn:
                        title_watcher.start()
                        self.assertEqual(spawn.call_count, 1)
                        # A second start must see the lock inherited by the child.
                        directory = title_state_dir()
                        self.assertTrue(renamed.wait(5), "watcher failed to rename")
                        title_watcher.start()
                        self.assertEqual(spawn.call_count, 1)
                        self.assertEqual(data["tabs"][0]["label"], "Delayed task")
                        title_watcher.stop()
                    deadline = time.monotonic() + 5
                    with (directory / "title-watcher.lock").open("a") as lock:
                        while True:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    self.fail("watcher did not release its lock after stop")
                                time.sleep(0.02)
            finally:
                # Also stop on assertion failures before deleting the state directory.
                for marker in Path(temporary).glob("title-servers/*"):
                    (marker / "title-watcher.stopped").touch()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
                server.shutdown()
                thread.join()
                server.server_close()
