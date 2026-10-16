import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest

from herdr_bar import host
from herdr_bar.client import HerdrClient, HerdrError

from . import fixtures


class SocketServer(object):
    """A stand-in for the Herdr socket: one request per connection."""

    def __init__(self, path, responder):
        self.path = path
        self.responder = responder
        self.requests = []
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(path)
        self._socket.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.split(b"\n")[0].decode("utf-8"))
                self.requests.append(request)
                reply = self.responder(request)
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                conn.close()

    def close(self):
        self._stop = True
        try:
            self._socket.close()
        except OSError:
            pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID,
    ]
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID,
    ]
    for _name in ("FlushFileBuffers", "DisconnectNamedPipe", "CloseHandle"):
        getattr(_kernel32, _name).argtypes = [wintypes.HANDLE]

    PIPE_ACCESS_DUPLEX = 0x0003
    PIPE_BYTE_STREAM = 0x0000  # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class PipeServer(object):
        """A stand-in for the Herdr named pipe: one request per connection.

        Herdr serves the pipe under the socket path string and leaves a
        marker file at the path itself, and so does this.
        """

        def __init__(self, path, responder):
            self.path = path
            self.name = host.pipe_name(path)
            open(path, "a").close()
            self.responder = responder
            self.requests = []
            self._stop = False
            self._thread = threading.Thread(target=self._serve)
            self._thread.daemon = True
            self._thread.start()

        def _instance(self):
            return _kernel32.CreateNamedPipeW(
                self.name, PIPE_ACCESS_DUPLEX, PIPE_BYTE_STREAM,
                PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, None,
            )

        def _serve(self):
            handle = self._instance()
            while handle != INVALID_HANDLE_VALUE:
                connected = (_kernel32.ConnectNamedPipe(handle, None)
                             or ctypes.get_last_error() == ERROR_PIPE_CONNECTED)
                if self._stop or not connected:
                    _kernel32.CloseHandle(handle)
                    return
                # Listen for the next client before answering this one, as
                # Herdr does, so back-to-back requests find an instance.
                following = self._instance()
                try:
                    self._answer(handle)
                except ValueError:
                    pass
                finally:
                    _kernel32.DisconnectNamedPipe(handle)
                    _kernel32.CloseHandle(handle)
                handle = following

        def _answer(self, handle):
            buffer = ctypes.create_string_buffer(4096)
            count = wintypes.DWORD(0)
            data = b""
            while b"\n" not in data:
                ok = _kernel32.ReadFile(handle, buffer, 4096, ctypes.byref(count), None)
                if not ok or not count.value:
                    return
                data += buffer.raw[:count.value]
            request = json.loads(data.split(b"\n")[0].decode("utf-8"))
            self.requests.append(request)
            reply = (json.dumps(self.responder(request)) + "\n").encode("utf-8")
            _kernel32.WriteFile(handle, reply, len(reply), ctypes.byref(count), None)
            _kernel32.FlushFileBuffers(handle)

        def close(self):
            self._stop = True
            try:
                open(self.name, "r+b", buffering=0).close()  # wake ConnectNamedPipe
            except OSError:
                pass
            self._thread.join(timeout=2)


def make_server(path, responder):
    """The socket transport's test double for this platform."""
    if sys.platform == "win32":
        return PipeServer(path, responder)
    return SocketServer(path, responder)


def ok(request):
    method = request["method"]
    if method == "session.snapshot":
        return {
            "id": request["id"],
            "result": {"type": "session_snapshot", "snapshot": fixtures.snapshot()},
        }
    if method == "pane.read":
        return {
            "id": request["id"],
            "result": {"type": "pane_read", "read": {"text": "hello\nworld"}},
        }
    return {"id": request["id"], "result": {"type": "ok"}}


class SocketTransportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "herdr.sock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_snapshot_round_trip(self):
        server = make_server(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        self.assertTrue(client.socket_ok)
        snapshot = client.snapshot()
        self.assertEqual(len(snapshot["tabs"]), 8)
        self.assertEqual(server.requests[0]["method"], "session.snapshot")

    def test_pane_read_sends_the_documented_params(self):
        server = make_server(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        self.assertEqual(client.read_pane("w1:p1", 12), "hello\nworld")
        params = server.requests[0]["params"]
        self.assertEqual(params, {"pane_id": "w1:p1", "source": "visible", "lines": 12})

    def test_focus_calls_use_the_documented_targets(self):
        server = make_server(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        client.focus_tab("w1:t1")
        client.focus_workspace("w1")
        client.focus_agent("w1:p1")
        client.focus_pane("w1:p2")
        self.assertEqual(
            [(r["method"], r["params"]) for r in server.requests],
            [
                ("tab.focus", {"tab_id": "w1:t1"}),
                ("workspace.focus", {"workspace_id": "w1"}),
                ("agent.focus", {"target": "w1:p1"}),
                ("pane.focus", {"pane_id": "w1:p2"}),
            ],
        )

    def test_tab_rename_sends_the_documented_params(self):
        server = make_server(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        client.rename_tab("w1:t1", "new panel name")
        self.assertEqual(server.requests[0]["method"], "tab.rename")
        self.assertEqual(
            server.requests[0]["params"],
            {"tab_id": "w1:t1", "label": "new panel name"},
        )

    def test_server_errors_become_herdr_errors(self):
        def failing(request):
            return {"id": request["id"], "error": {"code": "not_found", "message": "no such tab"}}

        server = make_server(self.path, failing)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        with self.assertRaises(HerdrError) as caught:
            client.focus_tab("w1:t9")
        self.assertIn("no such tab", str(caught.exception))

    def test_missing_socket_falls_back_to_the_cli(self):
        client = HerdrClient(
            socket_path=os.path.join(self.dir, "gone.sock"), bin_path="/nonexistent"
        )
        self.assertFalse(client.socket_ok)
        with self.assertRaises(HerdrError):
            client.snapshot()

    def test_dead_socket_falls_back_to_the_cli(self):
        # The marker is there but nothing answers: a server that went away.
        open(self.path, "a").close()
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        self.assertTrue(client.socket_ok)
        with self.assertRaises(HerdrError) as caught:
            client.snapshot()
        self.assertIn("not found", str(caught.exception))
        self.assertFalse(client.socket_ok)

    def test_silent_server_times_out_instead_of_hanging(self):
        release = threading.Event()

        def stall(request):
            release.wait(10)
            return {"id": request["id"], "result": {"type": "ok"}}

        server = make_server(self.path, stall)
        self.addCleanup(server.close)
        self.addCleanup(release.set)  # runs first: unblock the server thread
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent", timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(HerdrError):
            client.snapshot()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(client.socket_ok)

    def test_socket_only_client_reports_the_timeout(self):
        release = threading.Event()
        server = make_server(self.path, lambda request: release.wait(10))
        self.addCleanup(server.close)
        self.addCleanup(release.set)
        client = HerdrClient(socket_path=self.path, timeout=0.3, socket_only=True)
        with self.assertRaises(HerdrError) as caught:
            client.snapshot()
        self.assertIn("timed out", str(caught.exception))

    @unittest.skipUnless(sys.platform == "win32", "named pipe transport")
    def test_busy_pipe_waits_for_a_free_instance(self):
        server = make_server(self.path, ok)
        self.addCleanup(server.close)
        clients = [HerdrClient(socket_path=self.path, bin_path="/nonexistent")
                   for _ in range(8)]
        results = []
        threads = [threading.Thread(target=lambda c=c: results.append(c.snapshot()))
                   for c in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 8)


SNAPSHOT_JSON = (
    '{"id":"cli","result":{"type":"session_snapshot",'
    '"snapshot":{"tabs":[],"agents":[],"workspaces":[]}}}'
)


class CliTransportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # A path that does not exist forces the CLI transport, whatever
        # HERDR_SOCKET_PATH says in the environment running the tests.
        self.absent_socket = os.path.join(self.dir, "absent.sock")
        self.bin = os.path.join(self.dir, "fake-herdr.cmd" if host.WINDOWS else "fake-herdr")
        self.fake_herdr(SNAPSHOT_JSON)

    def fake_herdr(self, output):
        """A herdr stand-in that prints ``output`` whatever it is asked."""
        with open(self.bin, "w") as handle:
            if host.WINDOWS:
                handle.write("@echo %s\r\n" % output)
            else:
                handle.write("#!/bin/sh\necho '%s'\n" % output)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_snapshot_over_the_cli(self):
        client = HerdrClient(socket_path=self.absent_socket, bin_path=self.bin)
        self.assertEqual(client.snapshot()["tabs"], [])

    def test_missing_binary_reports_clearly(self):
        client = HerdrClient(socket_path=self.absent_socket, bin_path="/nonexistent/herdr")
        with self.assertRaises(HerdrError) as caught:
            client.snapshot()
        self.assertIn("not found", str(caught.exception))

    def test_focus_pane_degrades_to_a_no_op_over_the_cli(self):
        # The CLI cannot focus a pane by id, so without the socket the call
        # must quietly leave the user on the already-focused tab.
        client = HerdrClient(socket_path=self.absent_socket, bin_path="/nonexistent/herdr")
        client.focus_pane("w1:p1")

    def test_unparseable_output_reports_clearly(self):
        self.fake_herdr("not json")
        client = HerdrClient(socket_path=self.absent_socket, bin_path=self.bin)
        with self.assertRaises(HerdrError):
            client.snapshot()


if __name__ == "__main__":
    unittest.main()
