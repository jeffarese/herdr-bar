import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_bar import cli, host, opener
from herdr_bar.client import EventStream

SRC = str(Path(opener.__file__).resolve().parents[1])


class LockTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, "singleton.lock")

    def test_second_handle_is_refused_until_the_first_lets_go(self):
        with self.path.open("a") as first, self.path.open("a") as second:
            host.lock(first)
            with self.assertRaises(BlockingIOError):
                host.lock(second)
            host.unlock(first)
            host.lock(second)
        with self.path.open("a") as third:
            host.lock(third)  # closing released it

    def test_blocking_lock_waits_for_the_holder(self):
        held = threading.Event()

        def hold():
            with self.path.open("a") as holder:
                host.lock(holder)
                held.set()
                time.sleep(0.2)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        with self.path.open("a") as waiter:
            host.lock(waiter, blocking=True)
            self.assertGreater(time.monotonic() - started, 0.1)
        thread.join()

    def test_worker_options_detach_from_the_popup(self):
        with self.path.open("a") as lock:
            options = host.detached_process_options(lock)
            descriptor = lock.fileno()
        if host.WINDOWS:
            self.assertTrue(options["creationflags"] & subprocess.DETACHED_PROCESS)
            self.assertNotIn("pass_fds", options)
        else:
            self.assertTrue(options["start_new_session"])
            self.assertEqual(options["pass_fds"], (descriptor,))


class PipeTest(unittest.TestCase):
    def test_pipe_name_is_the_socket_path_under_the_pipe_namespace(self):
        path = r"C:\Users\me\AppData\Roaming\herdr\herdr.sock"
        self.assertEqual(host.pipe_name(path), r"\\.\pipe" + "\\" + path)
        self.assertEqual(host.pipe_name(host.pipe_name(path)), host.pipe_name(path))

    @unittest.skipUnless(host.WINDOWS, "named pipes are a Windows transport")
    def test_windows_event_stream_leaves_the_watcher_polling(self):
        stream = EventStream(r"C:\nowhere\herdr.sock")
        stream.follow([{"event": "pane.status_changed"}])
        self.assertIsNone(stream.conn)
        started = time.monotonic()
        self.assertFalse(stream.wait(0.05))
        self.assertGreaterEqual(time.monotonic() - started, 0.04)


class ProcessAgeTest(unittest.TestCase):
    @unittest.skipUnless(host.WINDOWS, "the kernel clock is read on Windows only")
    def test_own_process_has_an_age_and_a_missing_pid_has_none(self):
        age = host.process_age(os.getpid())
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, 24 * 3600)
        self.assertIsNone(host.process_age(0x7FFFFFF1))

    @unittest.skipIf(host.WINDOWS, "elsewhere ps answers")
    def test_process_age_is_windows_only(self):
        self.assertIsNone(host.process_age(os.getpid()))


class OpenActionTest(unittest.TestCase):
    def test_open_hands_the_popup_to_the_herdr_cli(self):
        expected = ["/opt/herdr", "plugin", "pane", "open", "--plugin", "herdr-bar",
                    "--entrypoint", "bar", "--placement", "popup"]
        with patch.object(opener, "herdr_bin", return_value="/opt/herdr"), \
                patch("subprocess.run") as run, \
                patch.object(opener.os, "execvp") as execvp:
            run.return_value.returncode = 0
            self.assertEqual(cli.main(["--open"]), 0)
        if host.WINDOWS:
            run.assert_called_once_with(expected, check=False)
            execvp.assert_not_called()
        else:
            execvp.assert_called_once_with("/opt/herdr", expected)
            run.assert_not_called()

    def test_missing_herdr_is_reported_not_raised(self):
        with patch.object(opener, "herdr_bin", return_value="/nonexistent/herdr"), \
                patch("sys.stderr"):
            self.assertEqual(opener.open_bar(), 2)

    def test_stale_bin_path_falls_back_to_path(self):
        with patch.dict(os.environ, {"HERDR_BIN_PATH": "/nonexistent/herdr"}):
            self.assertEqual(opener.herdr_bin(), "herdr")

    def test_open_path_skips_the_bar_imports(self):
        # The action pays for an interpreter; it should not pay for the bar too.
        loaded = subprocess.run(
            [sys.executable, "-c", "import sys, herdr_bar.opener; "
             "print(' '.join(sorted(m for m in sys.modules if m.startswith('herdr_bar'))))"],
            env=dict(os.environ, PYTHONPATH=SRC), capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(loaded, ["herdr_bar", "herdr_bar.opener"])


@unittest.skipUnless(host.WINDOWS, "console backend")
class WindowsTerminalTest(unittest.TestCase):
    def test_typed_bytes_survive_the_appearance_reply(self):
        from herdr_bar.term_windows import strip_reply
        self.assertEqual(strip_reply(b"ab\x1b]11;rgb:0000/0000/0000\x07cd"), b"abcd")
        self.assertEqual(strip_reply(b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\"), b"")
        self.assertEqual(strip_reply(b"x\x1b]11;rgb:00"), b"x")
        self.assertEqual(strip_reply(b"plain typing"), b"plain typing")

    def test_raw_mode_turns_on_virtual_terminal_input_and_off_echo(self):
        from herdr_bar import term_windows as win
        mode = win.raw_input_mode(win.ENABLE_LINE_INPUT | win.ENABLE_ECHO_INPUT
                                  | win.ENABLE_QUICK_EDIT_MODE, mouse=True)
        self.assertTrue(mode & win.ENABLE_VIRTUAL_TERMINAL_INPUT)
        self.assertTrue(mode & win.ENABLE_MOUSE_INPUT)
        self.assertTrue(mode & win.ENABLE_EXTENDED_FLAGS)
        for flag in (win.ENABLE_LINE_INPUT, win.ENABLE_ECHO_INPUT,
                     win.ENABLE_QUICK_EDIT_MODE, win.ENABLE_PROCESSED_INPUT):
            self.assertFalse(mode & flag)
        self.assertFalse(win.raw_input_mode(0, mouse=False) & win.ENABLE_MOUSE_INPUT)

    def test_console_stays_raw_from_the_query_until_the_bar_leaves(self):
        from herdr_bar import term_windows as win
        cooked = 0x1F7  # line input, echo and ctrl+c processing among them
        modes = win.ConsoleModes()
        calls = []

        def record(handle, mode):
            calls.append(mode)

        with patch.object(win, "_mode", return_value=cooked), \
                patch.object(win, "_set_mode", side_effect=record), \
                patch.object(win.atexit, "register") as register:
            modes.raw(mouse=False)  # the appearance query
            modes.raw(mouse=True)  # the Terminal, still restoring the originals later
            self.assertEqual(modes.saved, (cooked, cooked))
            register.assert_called_once_with(modes.restore)
            modes.restore()
            modes.restore()  # a second exit path finds nothing left to do
        self.assertEqual(len(calls), 6)
        self.assertTrue(calls[0] & win.ENABLE_VIRTUAL_TERMINAL_INPUT)
        self.assertFalse(calls[0] & win.ENABLE_LINE_INPUT)
        self.assertTrue(calls[2] & win.ENABLE_MOUSE_INPUT)
        self.assertEqual(calls[-2:], [cooked, cooked])

    def test_reader_holds_a_split_surrogate_pair_for_the_next_read(self):
        from herdr_bar import term_windows as win
        reader = win.ConsoleReader(handle=None)
        reads = iter(["a\ud83d", "\ude00b", None])

        def fake_read(handle, buffer, size, count, reserved):
            text = next(reads)
            if text is None:
                return 0
            buffer.value = text
            count._obj.value = len(text)
            return 1

        with patch.object(win.kernel32, "ReadConsoleW", side_effect=fake_read):
            reader._run()
        self.assertEqual(reader.read(), "a\U0001f600b".encode("utf-8"))
        self.assertTrue(reader.closed)
