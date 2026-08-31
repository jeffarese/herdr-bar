import unittest

from herdr_bar.keys import KEY, MOUSE, PASTE, PRESS, TEXT, WHEEL_DOWN, WHEEL_UP, KeyDecoder


def names(events):
    return [(event.kind, event.value) for event in events]


class DecoderTest(unittest.TestCase):
    def setUp(self):
        self.decoder = KeyDecoder()

    def test_plain_text(self):
        self.assertEqual(names(self.decoder.feed(b"hi")), [(TEXT, "h"), (TEXT, "i")])

    def test_control_keys(self):
        self.assertEqual(
            names(self.decoder.feed(b"\r\t\x7f\x15\x0f")),
            [(KEY, "enter"), (KEY, "tab"), (KEY, "backspace"), (KEY, "ctrl+u"), (KEY, "ctrl+o")],
        )

    def test_ctrl_j_and_ctrl_k(self):
        self.assertEqual(
            names(self.decoder.feed(b"\x0a\x0b")),
            [(KEY, "ctrl+j"), (KEY, "ctrl+k")],
        )

    def test_arrows_and_navigation(self):
        data = b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[5~\x1b[6~\x1b[H\x1b[F\x1b[3~\x1b[Z"
        self.assertEqual(
            [value for _, value in names(self.decoder.feed(data))],
            ["up", "down", "right", "left", "pgup", "pgdn", "home", "end", "delete", "shift+tab"],
        )

    def test_application_cursor_keys(self):
        self.assertEqual(names(self.decoder.feed(b"\x1bOA")), [(KEY, "up")])

    def test_modified_arrows(self):
        self.assertEqual(names(self.decoder.feed(b"\x1b[1;5A")), [(KEY, "ctrl+up")])

    def test_escape_needs_a_flush(self):
        self.assertEqual(self.decoder.feed(b"\x1b"), [])
        self.assertTrue(self.decoder.pending_escape())
        self.assertEqual(names(self.decoder.flush()), [(KEY, "esc")])

    def test_escape_followed_by_a_letter_is_alt(self):
        self.assertEqual(names(self.decoder.feed(b"\x1bx")), [(KEY, "alt+x")])

    def test_sequence_split_across_reads(self):
        self.assertEqual(self.decoder.feed(b"\x1b["), [])
        self.assertEqual(names(self.decoder.feed(b"B")), [(KEY, "down")])

    def test_utf8_split_across_reads(self):
        self.assertEqual(self.decoder.feed(b"\xc3"), [])
        self.assertEqual(names(self.decoder.feed(b"\xa9")), [(TEXT, "é")])

    def test_wide_characters(self):
        self.assertEqual(names(self.decoder.feed("日".encode("utf-8"))), [(TEXT, "日")])

    def test_bracketed_paste(self):
        events = self.decoder.feed(b"\x1b[200~one two\x1b[201~")
        self.assertEqual(names(events), [(PASTE, "one two")])

    def test_paste_split_across_reads(self):
        self.assertEqual(self.decoder.feed(b"\x1b[200~par"), [])
        self.assertEqual(names(self.decoder.feed(b"tial\x1b[201~")), [(PASTE, "partial")])

    def test_mouse_press_and_wheel(self):
        events = self.decoder.feed(b"\x1b[<0;12;5M\x1b[<64;1;1M\x1b[<65;1;1M")
        self.assertEqual(
            names(events), [(MOUSE, PRESS), (MOUSE, WHEEL_UP), (MOUSE, WHEEL_DOWN)]
        )
        self.assertEqual((events[0].x, events[0].y), (12, 5))

    def test_osc_replies_are_swallowed(self):
        events = self.decoder.feed(b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07k")
        self.assertEqual(names(events), [(TEXT, "k")])

    def test_osc_reply_with_string_terminator(self):
        events = self.decoder.feed(b"\x1b]11;rgb:0/0/0\x1b\\z")
        self.assertEqual(names(events), [(TEXT, "z")])

    def test_unknown_sequences_are_ignored(self):
        self.assertEqual(names(self.decoder.feed(b"\x1b[?25h")), [])


if __name__ == "__main__":
    unittest.main()
