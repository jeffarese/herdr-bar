import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest

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


def ok(request):
    method = request["method"]
    if method == "session.snapshot":
        return {"id": request["id"], "result": {"type": "session_snapshot", "snapshot": fixtures.snapshot()}}
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
        server = SocketServer(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        snapshot = client.snapshot()
        self.assertEqual(len(snapshot["tabs"]), 8)
        self.assertEqual(server.requests[0]["method"], "session.snapshot")

    def test_pane_read_sends_the_documented_params(self):
        server = SocketServer(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        self.assertEqual(client.read_pane("w1:p1", 12), "hello\nworld")
        params = server.requests[0]["params"]
        self.assertEqual(params, {"pane_id": "w1:p1", "source": "visible", "lines": 12})

    def test_focus_calls_use_the_documented_targets(self):
        server = SocketServer(self.path, ok)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        client.focus_tab("w1:t1")
        client.focus_workspace("w1")
        client.focus_agent("w1:p1")
        self.assertEqual(
            [(r["method"], r["params"]) for r in server.requests],
            [
                ("tab.focus", {"tab_id": "w1:t1"}),
                ("workspace.focus", {"workspace_id": "w1"}),
                ("agent.focus", {"target": "w1:p1"}),
            ],
        )

    def test_server_errors_become_herdr_errors(self):
        def failing(request):
            return {"id": request["id"], "error": {"code": "not_found", "message": "no such tab"}}

        server = SocketServer(self.path, failing)
        self.addCleanup(server.close)
        client = HerdrClient(socket_path=self.path, bin_path="/nonexistent")
        with self.assertRaises(HerdrError) as caught:
            client.focus_tab("w1:t9")
        self.assertIn("no such tab", str(caught.exception))

    def test_missing_socket_falls_back_to_the_cli(self):
        client = HerdrClient(socket_path=os.path.join(self.dir, "gone.sock"), bin_path="/nonexistent")
        self.assertFalse(client.socket_ok)
        with self.assertRaises(HerdrError):
            client.snapshot()


class CliTransportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # A path that does not exist forces the CLI transport, whatever
        # HERDR_SOCKET_PATH says in the environment running the tests.
        self.absent_socket = os.path.join(self.dir, "absent.sock")
        self.bin = os.path.join(self.dir, "fake-herdr")
        with open(self.bin, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                'echo "{\\"id\\":\\"cli\\",\\"result\\":{\\"type\\":\\"session_snapshot\\",'
                '\\"snapshot\\":{\\"tabs\\":[],\\"agents\\":[],\\"workspaces\\":[]}}}"\n'
            )
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

    def test_unparseable_output_reports_clearly(self):
        with open(self.bin, "w") as handle:
            handle.write("#!/bin/sh\necho 'not json'\n")
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        client = HerdrClient(socket_path=self.absent_socket, bin_path=self.bin)
        with self.assertRaises(HerdrError):
            client.snapshot()


if __name__ == "__main__":
    unittest.main()
