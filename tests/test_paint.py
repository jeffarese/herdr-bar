"""The painter is only allowed to be clever if it is also right.

Two things are being asserted here: that the model of a row agrees with what a
terminal would put on screen, and that the update it emits is both minimal and
safe -- a diff that saves bytes by leaving the wrong thing on screen is worse
than the full repaint it replaced.
"""

import re
import unittest

from herdr_bar.paint import (
    DEFAULT_STYLE,
    JUMP_COST,
    Painter,
    _sgr,
    cells_of,
    compose,
    encode_style,
)


class CellsTest(unittest.TestCase):
    def test_plain_text_fills_from_the_left(self):
        cells, ink = cells_of("hi", 5)
        self.assertEqual([cell[0] for cell in cells], ["h", "i", " ", " ", " "])
        self.assertEqual(ink, 1)

    def test_an_empty_row_has_no_ink(self):
        cells, ink = cells_of("", 4)
        self.assertEqual(ink, -1)
        self.assertEqual([cell[0] for cell in cells], [" "] * 4)

    def test_trailing_spaces_are_not_ink(self):
        self.assertEqual(cells_of("hi     ", 12)[1], 1)

    def test_a_coloured_space_is_ink(self):
        """A selection background makes a space something worth painting."""
        self.assertEqual(cells_of("\x1b[48;5;237m  \x1b[0m", 8)[1], 1)

    def test_a_wide_glyph_covers_two_cells(self):
        cells, ink = cells_of("漢x", 6)
        self.assertEqual([cell[0] for cell in cells[:3]], ["漢", "", "x"])
        self.assertEqual(ink, 2)

    def test_a_wide_glyph_carries_its_rendition_into_both_cells(self):
        cells, _ = cells_of("\x1b[1m漢", 4)
        self.assertEqual(cells[0][1], cells[1][1])
        self.assertTrue(cells[1][1][2])

    def test_text_wider_than_the_row_is_cut(self):
        cells, ink = cells_of("abcdef", 3)
        self.assertEqual([cell[0] for cell in cells], ["a", "b", "c"])
        self.assertEqual(ink, 2)

    def test_a_wide_glyph_in_the_last_column_keeps_its_cell(self):
        cells, ink = cells_of("ab漢", 3)
        self.assertEqual([cell[0] for cell in cells], ["a", "b", "漢"])
        self.assertEqual(ink, 2)

    def test_renditions_apply_to_what_follows_them(self):
        cells, _ = cells_of("a\x1b[1mb\x1b[0mc", 4)
        self.assertFalse(cells[0][1][2])
        self.assertTrue(cells[1][1][2])
        self.assertFalse(cells[2][1][2])

    def test_zero_width_characters_take_no_cell(self):
        cells, ink = cells_of("áb", 4)
        self.assertEqual([cell[0] for cell in cells[:2]], ["a", "b"])
        self.assertEqual(ink, 1)

    def test_a_truncated_escape_ends_the_row(self):
        cells, _ = cells_of("ab\x1b[38;5", 5)
        self.assertEqual([cell[0] for cell in cells[:2]], ["a", "b"])

    def test_non_csi_escapes_are_skipped_without_eating_the_text(self):
        cells, _ = cells_of("a\x1bMb", 4)
        self.assertEqual([cell[0] for cell in cells[:2]], ["a", "b"])


class StyleTest(unittest.TestCase):
    def test_encoded_renditions_parse_back_to_themselves(self):
        styles = [
            DEFAULT_STYLE,
            ("38;5;244", None, False, False, False),
            ("38;2;10;20;30", "48;5;237", True, True, False),
            (None, "48;5;237", False, False, True),
            ("38;5;12", None, True, False, False),
            (None, None, False, True, False),
        ]
        for start in styles:
            for target in styles:
                sequence = encode_style(start, target)
                landed = start
                for params in re.findall(r"\x1b\[([0-9;]*)m", sequence):
                    landed = _sgr(params, landed)
                self.assertEqual(landed, target, "%r -> %r via %r" % (start, target, sequence))

    def test_no_change_costs_nothing(self):
        self.assertEqual(encode_style(DEFAULT_STYLE, DEFAULT_STYLE), "")

    def test_going_back_to_plain_is_a_single_reset(self):
        bold = (None, None, True, False, False)
        self.assertEqual(encode_style(bold, DEFAULT_STYLE), "\x1b[0m")

    def test_adding_an_attribute_does_not_rebuild_the_rest(self):
        colored = ("38;5;244", None, False, False, False)
        both = ("38;5;244", None, True, False, False)
        self.assertEqual(encode_style(colored, both), "\x1b[1m")


class PainterTest(unittest.TestCase):
    def test_the_first_frame_paints_everything(self):
        painter = Painter()
        out = painter.frame(["hello"], 20, 3)
        self.assertIn("hello", out)
        self.assertIn("\x1b[J", out)

    def test_a_frame_that_changes_nothing_emits_nothing(self):
        painter = Painter()
        painter.frame(["hello", "there"], 20, 3)
        self.assertEqual(painter.frame(["hello", "there"], 20, 3), "")

    def test_one_character_costs_a_handful_of_bytes(self):
        painter = Painter()
        painter.frame(["x" * 40], 40, 2)
        out = painter.frame(["x" * 20 + "y" + "x" * 19], 40, 2)
        self.assertIn("y", out)
        self.assertLess(len(out), 12)

    def test_an_untouched_row_is_not_resent(self):
        painter = Painter()
        painter.frame(["one", "two", "three"], 20, 3)
        out = painter.frame(["one", "TWO", "three"], 20, 3)
        self.assertNotIn("one", out)
        self.assertNotIn("three", out)
        self.assertIn("TWO", out)

    def test_a_tail_that_goes_blank_is_erased_not_overwritten(self):
        painter = Painter()
        painter.frame(["abcdefghij" * 4], 40, 2)
        out = painter.frame(["abc"], 40, 2)
        self.assertIn("\x1b[K", out)
        self.assertLess(len(out), 16)

    def test_a_background_is_cleared_before_erasing(self):
        """Erasing paints with the current background, so it must be default.

        Otherwise a selected row's colour smears across everything the erase
        touches -- the classic background-colour-erase bug.
        """
        painter = Painter()
        painter.frame(["\x1b[48;5;237m" + "x" * 30 + "\x1b[0m"], 40, 2)
        out = painter.frame(["\x1b[48;5;237m" + "y" * 5 + "\x1b[0m"], 40, 2)
        self.assertIn("\x1b[K", out)
        self.assertTrue(out[: out.index("\x1b[K")].endswith("\x1b[0m"))

    def test_two_distant_edits_jump_rather_than_rewrite_the_middle(self):
        painter = Painter()
        painter.frame(["x" * 60], 60, 2)
        out = painter.frame(["y" + "x" * 58 + "z"], 60, 2)
        self.assertIn("y", out)
        self.assertIn("z", out)
        self.assertNotIn("xxxxx", out)

    def test_a_gap_shorter_than_a_jump_is_overwritten(self):
        painter = Painter()
        painter.frame(["x" * 20], 20, 2)
        out = painter.frame(["y" + "x" * (JUMP_COST - 2) + "y" + "x" * (21 - JUMP_COST)], 20, 2)
        self.assertEqual(out.count("\x1b["), 1)

    def test_a_rendition_carries_between_frames(self):
        painter = Painter()
        painter.frame(["\x1b[1mabcd\x1b[0m"], 20, 2)
        painter.frame(["\x1b[1mabcX\x1b[0m"], 20, 2)  # leaves the terminal bold
        out = painter.frame(["\x1b[1mabYX\x1b[0m"], 20, 2)
        self.assertIn("Y", out)
        self.assertNotIn("\x1b[1m", out)

    def test_a_run_that_ends_on_a_wide_glyph_covers_both_of_its_cells(self):
        painter = Painter()
        painter.frame(["a漢b"], 10, 2)
        out = painter.frame(["a字b"], 10, 2)
        self.assertIn("字", out)
        self.assertNotIn("b", out)

    def test_a_resize_repaints_everything(self):
        painter = Painter()
        painter.frame(["hello"], 20, 3)
        out = painter.frame(["hello"], 30, 3)
        self.assertIn("hello", out)
        self.assertIn("\x1b[J", out)

    def test_reset_repaints_everything(self):
        painter = Painter()
        painter.frame(["hello"], 20, 3)
        self.assertEqual(painter.frame(["hello"], 20, 3), "")
        painter.reset()
        self.assertIn("hello", painter.frame(["hello"], 20, 3))

    def test_rows_beyond_the_screen_are_dropped(self):
        painter = Painter()
        out = painter.frame(["one", "two", "three"], 20, 2)
        self.assertNotIn("three", out)

    def test_rows_that_disappear_are_cleared(self):
        painter = Painter()
        painter.frame(["one", "two"], 20, 3)
        out = painter.frame(["one"], 20, 3)
        self.assertIn("\x1b[K", out)
        self.assertEqual(painter.screen()[1], cells_of("", 20)[0])

    def test_the_model_tracks_what_was_drawn(self):
        painter = Painter()
        painter.frame(["hello", "world"], 20, 3)
        painter.frame(["hello", "WORLD"], 20, 3)
        self.assertEqual(painter.screen()[1], cells_of("WORLD", 20)[0])


class ComposeTest(unittest.TestCase):
    def test_every_row_is_positioned_and_cleared(self):
        out = compose(["a", "b"])
        self.assertIn("\x1b[1;1H", out)
        self.assertIn("\x1b[2;1H", out)
        self.assertEqual(out.count("\x1b[K"), 2)
        self.assertTrue(out.endswith("\x1b[J"))

    def test_it_does_not_inherit_a_rendition(self):
        self.assertTrue(compose(["a"]).startswith("\x1b[0m"))


if __name__ == "__main__":
    unittest.main()
