import unittest

from herdr_bar.textutil import (
    display_width,
    pad,
    sanitize,
    strip_ansi,
    truncate,
    truncate_middle,
    visible_width,
    window_positions,
)


class WidthTest(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(display_width("hello"), 5)

    def test_wide_characters_count_double(self):
        self.assertEqual(display_width("日本"), 4)

    def test_combining_marks_are_free(self):
        self.assertEqual(display_width("é"), 1)

    def test_control_characters_are_free(self):
        self.assertEqual(display_width("a\x07b"), 2)

    def test_visible_width_ignores_styling(self):
        self.assertEqual(visible_width("\x1b[38;5;12mhi\x1b[0m"), 2)


class SanitizeTest(unittest.TestCase):
    def test_strips_escape_sequences(self):
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_strips_osc_sequences(self):
        self.assertEqual(strip_ansi("\x1b]0;title\x07text"), "text")

    def test_replaces_control_characters(self):
        self.assertEqual(sanitize("a\x00b"), "a b")

    def test_expands_tabs(self):
        self.assertEqual(sanitize("a\tb"), "a    b")


class TruncateTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(truncate("hi", 10), "hi")

    def test_adds_an_ellipsis(self):
        self.assertEqual(truncate("hello world", 8), "hello w…")

    def test_result_never_exceeds_the_width(self):
        for width in range(1, 12):
            self.assertLessEqual(display_width(truncate("hello world", width)), width)

    def test_wide_characters_never_overflow(self):
        for width in range(1, 12):
            self.assertLessEqual(display_width(truncate("日本語のテスト", width)), width)

    def test_middle_truncation_keeps_both_ends(self):
        result = truncate_middle("/home/dev/workspace/project", 16)
        self.assertTrue(result.startswith("/home"))
        self.assertTrue(result.endswith("ject"))
        self.assertLessEqual(display_width(result), 16)


class PadTest(unittest.TestCase):
    def test_pads_to_the_width(self):
        self.assertEqual(visible_width(pad("hi", 6)), 6)

    def test_pads_styled_text_by_visible_width(self):
        self.assertEqual(visible_width(pad("\x1b[31mhi\x1b[0m", 6)), 6)

    def test_never_shrinks(self):
        self.assertEqual(pad("hello", 2), "hello")


class WindowPositionsTest(unittest.TestCase):
    def test_short_text_keeps_its_positions(self):
        text, positions, offset = window_positions("week gen", 20, [0, 1])
        self.assertEqual((text, positions, offset), ("week gen", [0, 1], 0))

    def test_positions_stay_on_the_same_characters(self):
        source = "Redesign the week generation loading screen"
        marks = [15, 16, 17, 18]
        text, positions, _ = window_positions(source, 20, marks)
        self.assertEqual(
            "".join(text[index] for index in positions),
            "".join(source[index] for index in marks),
        )

    def test_window_slides_to_keep_late_matches_visible(self):
        source = "a very long tab title that ends with needle"
        needle = list(range(len(source) - 6, len(source)))
        text, positions, offset = window_positions(source, 20, needle)
        self.assertGreater(offset, 0)
        self.assertTrue(text.startswith("…"))
        self.assertEqual("".join(text[index] for index in positions), "needle")

    def test_result_fits_the_width(self):
        source = "a very long tab title that ends with needle"
        for width in range(4, 30):
            text, _, _ = window_positions(source, width, [len(source) - 1])
            self.assertLessEqual(display_width(text), width)


if __name__ == "__main__":
    unittest.main()
