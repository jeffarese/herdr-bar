"""The bench has to be right before its score means anything.

The load-bearing claim is that `needed_bytes` is a number a real renderer
could have hit. So the tests check the two things that would make it a
fiction: that the screen model agrees with an independent reading of the same
frame, and that the diff it prices actually reproduces the frame it claims to.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

import bench  # noqa: E402

from herdr_bar import paint  # noqa: E402
from scripts.demo import _flatten  # noqa: E402

ROW_RE = re.compile(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|\x1b\[J|$)", re.S)


def frames_of(scenario):
    """Run a scenario's shape by hand and keep the raw frames."""
    client = bench.CountingClient()
    terminal = bench.Recorder(104, 22)
    bar = bench.build_bar(client)
    bench.settle(bar, terminal)
    scenario(bar, terminal)
    return terminal.frames


def screen_text(screen):
    return "\n".join(
        "".join(char for char, _ in row).rstrip() for row in screen.cells
    )


class ScreenModelTest(unittest.TestCase):
    def test_matches_an_independent_reading_of_the_same_frame(self):
        """The model and the demo's flattener must see the same characters."""
        client = bench.CountingClient()
        terminal = bench.Recorder(104, 22)
        bar = bench.build_bar(client)
        bench.settle(bar, terminal)
        bar.insert("w")
        # The flattener reads whole rows out of one frame, which is what the
        # demo asks the bar for; the partial updates have their own test.
        bar.invalidate()
        terminal.frames = []
        bar.draw(terminal)

        screen = bench.Screen(104, 22)
        for frame in terminal.frames:
            screen.feed(frame)

        expected = _flatten("".join(terminal.frames), 22)
        expected = re.sub(r"\x1b\[[0-9;]*m", "", expected)
        expected = "\n".join(line.rstrip() for line in expected.split("\n")).rstrip("\n")
        self.assertEqual(screen_text(screen).rstrip("\n"), expected)

    def test_understands_everything_the_renderer_emits(self):
        """A new escape sequence in the renderer must not be silently ignored."""
        for scenario in bench.SCENARIOS:
            result = bench.run(scenario)
            self.assertEqual(
                result["unhandled"], 0, "%s emitted sequences the model skips" % result["name"]
            )

    def test_erase_to_end_of_line(self):
        screen = bench.Screen(10, 2)
        screen.feed("\x1b[1;1Habcdefghij")
        screen.feed("\x1b[1;4H\x1b[K")
        self.assertEqual(screen_text(screen).split("\n")[0], "abc")

    def test_erase_below(self):
        screen = bench.Screen(6, 3)
        screen.feed("\x1b[1;1Haaa\x1b[2;1Hbbb\x1b[3;1Hccc")
        screen.feed("\x1b[2;2H\x1b[J")
        self.assertEqual(screen_text(screen).split("\n"), ["aaa", "b", ""])

    def test_wide_characters_cover_two_cells(self):
        screen = bench.Screen(6, 1)
        screen.feed("\x1b[1;1H漢x")
        self.assertEqual(screen.cells[0][0][0], "漢")
        self.assertEqual(screen.cells[0][1][0], "")
        self.assertEqual(screen.cells[0][2][0], "x")

    def test_style_is_part_of_a_cell(self):
        a = bench.Screen(4, 1)
        a.feed("\x1b[1;1H\x1b[38;5;244mab")
        b = bench.Screen(4, 1)
        b.feed("\x1b[1;1Hab")
        self.assertNotEqual(a.snapshot_rows(), b.snapshot_rows())

    def test_reset_clears_every_attribute(self):
        screen = bench.Screen(4, 1)
        screen.feed("\x1b[1m\x1b[4m\x1b[38;5;9m\x1b[0ma")
        self.assertEqual(screen.cells[0][0][1], bench.DEFAULT_STYLE)


class RowModelTest(unittest.TestCase):
    """The painter only sends a diff if it knows what is already on screen.

    Its idea of a row and the bench's idea of the same row are separate pieces
    of code; where they disagree, one of them is lying about the terminal.
    """

    ROWS = [
        "",
        " ",
        "plain text",
        "  ".join(["padded"] * 8),
        "\x1b[1mbold\x1b[0m tail",
        "\x1b[38;5;244mmuted\x1b[0m\x1b[48;5;237m selected \x1b[0m",
        "\x1b[7mreverse\x1b[27m after",
        "\x1b[38;2;10;20;30mtruecolor\x1b[0m",
        "漢字 wide 字漢",
        "\x1b[1m漢\x1b[0mx",
        "combining á mark",
        "trailing spaces   ",
        "\x1b[48;5;237m   \x1b[0m",
        "x" * 200,
        "漢" * 80,
        "\x1b[0m\x1b[1m\x1b[4m\x1b[38;5;9mall at once\x1b[0m",
        "❯ ⏎ ⇥ ⌫ ⌦ ↑↓ ─ │ ▌ ┃ ⠋",
        "mixed 漢 \x1b[1mbold\x1b[0m 字 tail",
    ]

    def test_agrees_with_the_screen_model_on_every_shape_of_row(self):
        for width in (1, 2, 3, 7, 40, 104):
            for text in self.ROWS:
                screen = bench.Screen(width, 1)
                screen.feed("\x1b[1;1H" + text)
                modelled, _ = paint.cells_of(text, width)
                self.assertEqual(
                    list(modelled),
                    list(screen.snapshot_rows()[0]),
                    "row %r at width %d" % (text, width),
                )

    def test_agrees_on_every_row_the_bar_actually_draws(self):
        client = bench.CountingClient()
        terminal = bench.Recorder(104, 22)
        bar = bench.build_bar(client)
        bench.settle(bar, terminal)
        seen = 0
        for query in ("", "week", "cl", "zzz"):
            bar.set_query(query)
            bar.invalidate()
            whole = bench.Recorder(104, 22)
            bar.draw(whole)
            for row in ROW_RE.finditer("".join(whole.frames)):
                text = row.group(2).replace("\x1b[K", "")
                screen = bench.Screen(104, 1)
                screen.feed("\x1b[1;1H" + text)
                self.assertEqual(
                    list(paint.cells_of(text, 104)[0]),
                    list(screen.snapshot_rows()[0]),
                )
                seen += 1
        self.assertGreater(seen, 40)


class PartialUpdateTest(unittest.TestCase):
    """The bar sends diffs. The screen still has to end up exactly right.

    Checked against the bench's screen model, which was written against the
    old whole-screen renderer and knows nothing about the painter -- so a
    painter that miscounts a column, forgets a rendition or erases a cell it
    should have kept has nowhere to hide.
    """

    def _agrees_with_a_full_repaint(self, steps):
        """Drive two identical bars, one sending diffs and one repainting.

        The mirror is a separate bar because a full repaint tells the painter
        the terminal has been reset -- true of the terminal it was sent to,
        and not of the one collecting the diffs.
        """
        with bench.virtual_clock():  # both bars must read the same running times
            terminal = bench.Recorder(104, 22)
            bar = bench.build_bar(bench.CountingClient())
            bench.settle(bar, terminal)
            live = bench.Screen(104, 22)
            for frame in terminal.frames:
                live.feed(frame)

            mirror = bench.build_bar(bench.CountingClient())
            bench.settle(mirror, bench.Recorder(104, 22))

            for index, (step, echo) in enumerate(zip(steps(bar), steps(mirror))):
                step()
                echo()

                terminal.frames = []
                bar.draw(terminal)
                for frame in terminal.frames:
                    live.feed(frame)

                whole = bench.Recorder(104, 22)
                mirror.invalidate()
                mirror.draw(whole)
                reference = bench.Screen(104, 22)
                for frame in whole.frames:
                    reference.feed(frame)

                self.assertEqual(
                    live.snapshot_rows(),
                    reference.snapshot_rows(),
                    "step %d: the diff left a different screen than a full repaint" % index,
                )

    def test_typing(self):
        def steps(bar):
            for char in "weekly":
                yield lambda char=char: bar.insert(char)
            for _ in range(6):
                yield bar.backspace

        self._agrees_with_a_full_repaint(steps)

    def test_navigation(self):
        def steps(bar):
            for _ in range(12):
                yield lambda: bar.handle_key("down")
            for _ in range(4):
                yield lambda: bar.handle_key("up")

        self._agrees_with_a_full_repaint(steps)

    def test_scope_cycling_and_the_empty_state(self):
        def steps(bar):
            for _ in range(6):
                yield lambda: bar.handle_key("tab")
            yield lambda: bar.insert("z")  # no matches: the empty state
            yield lambda: bar.insert("q")
            yield bar.backspace
            yield bar.backspace

        self._agrees_with_a_full_repaint(steps)

    def test_the_spinner(self):
        def steps(bar):
            for _ in range(12):
                yield lambda: setattr(bar, "tick", bar.tick + 1)

        self._agrees_with_a_full_repaint(steps)

    def test_the_close_confirmation_and_the_status_line(self):
        def steps(bar):
            yield bar.request_close
            yield lambda: setattr(bar, "pending_close", None)
            yield lambda: bar.flash("closed something")
            yield lambda: setattr(bar, "status", None)

        self._agrees_with_a_full_repaint(steps)

    def test_a_frame_that_changes_nothing_sends_nothing(self):
        client = bench.CountingClient()
        terminal = bench.Recorder(104, 22)
        bar = bench.build_bar(client)
        bench.settle(bar, terminal)
        terminal.frames = []
        bar.draw(terminal)
        bar.draw(terminal)
        self.assertEqual(terminal.frames, [])


class RepaintTest(unittest.TestCase):
    """`needed_bytes` is only honest if the diff it measures actually works."""

    def _round_trip(self, frames):
        replay = bench.Screen(104, 22)
        diffed = bench.Screen(104, 22)
        for frame in frames:
            before = replay.snapshot_rows()
            replay.feed(frame)
            after = replay.snapshot_rows()
            patch = bench.repaint(before, after, 104)
            diffed.feed(patch)
            self.assertEqual(
                diffed.snapshot_rows(),
                after,
                "the priced diff did not reproduce the frame",
            )

    def test_typing_diffs_reproduce_every_frame(self):
        def typing(bar, terminal):
            for char in "weekly":
                bar.insert(char)
                bar.draw(terminal)

        self._round_trip(frames_of(typing))

    def test_navigation_diffs_reproduce_every_frame(self):
        def navigate(bar, terminal):
            for _ in range(10):
                bar.handle_key("down")
                bar.draw(terminal)

        self._round_trip(frames_of(navigate))

    def test_spinner_diffs_reproduce_every_frame(self):
        def spin(bar, terminal):
            for _ in range(12):
                bar.tick += 1
                bar.draw(terminal)

        self._round_trip(frames_of(spin))

    def test_scope_and_empty_state_diffs_reproduce_every_frame(self):
        def scope(bar, terminal):
            for _ in range(6):
                bar.handle_key("tab")
                bar.draw(terminal)
            bar.insert("z")  # no matches: the empty state replaces the list
            bar.draw(terminal)

        self._round_trip(frames_of(scope))

    def test_nothing_to_say_costs_nothing(self):
        screen = bench.Screen(20, 3)
        screen.feed("\x1b[1;1Hhello")
        rows = screen.snapshot_rows()
        self.assertEqual(bench.repaint(rows, rows, 20), "")

    def test_a_one_glyph_change_is_priced_in_bytes_not_kilobytes(self):
        before = bench.Screen(80, 5)
        before.feed("\x1b[1;1H" + "x" * 40)
        rows_before = before.snapshot_rows()
        after = bench.Screen(80, 5)
        after.feed("\x1b[1;1H" + "x" * 40)
        after.feed("\x1b[1;20Hy")
        self.assertLess(len(bench.repaint(rows_before, after.snapshot_rows(), 80)), 20)


class StyleTest(unittest.TestCase):
    def test_encoded_styles_parse_back_to_themselves(self):
        styles = [
            bench.DEFAULT_STYLE,
            bench.Style(("38;5;244", None, False, False, False)),
            bench.Style(("38;2;10;20;30", "48;5;237", True, True, False)),
            bench.Style((None, "48;5;237", False, False, True)),
            bench.Style(("38;5;12", None, True, False, False)),
        ]
        for start in styles:
            for target in styles:
                sequence = bench.encode_style(start, target)
                landed = start
                for params in re.findall(r"\x1b\[([0-9;]*)m", sequence):
                    landed = bench._sgr(params, landed)
                self.assertEqual(landed, target, "%r -> %r via %r" % (start, target, sequence))

    def test_no_change_emits_nothing(self):
        self.assertEqual(bench.encode_style(bench.DEFAULT_STYLE, bench.DEFAULT_STYLE), "")


class ScoreTest(unittest.TestCase):
    def test_curve_endpoints(self):
        self.assertEqual(bench._curve(1.0, 4.0, 16.0), 100.0)
        self.assertEqual(bench._curve(4.0, 4.0, 16.0), 100.0)
        self.assertEqual(bench._curve(16.0, 4.0, 16.0), 0.0)
        self.assertEqual(bench._curve(99.0, 4.0, 16.0), 0.0)
        self.assertAlmostEqual(bench._curve(10.0, 4.0, 16.0), 50.0)

    def test_percentile(self):
        values = [1.0, 2.0, 3.0, 4.0, 100.0]
        self.assertEqual(bench.percentile(values, 0.5), 3.0)
        self.assertEqual(bench.percentile(values, 0.99), 100.0)
        self.assertEqual(bench.percentile([], 0.5), 0.0)

    def test_weights_sum_to_one_hundred(self):
        self.assertEqual(sum(weight for _, weight, _ in bench.WEIGHTS), 100)

    def test_a_perfect_run_scores_one_hundred(self):
        results = [
            bench.Result(
                name=name, frames=4, changed=4, emitted=100, needed=100, calls=0,
                seconds=0.0, unhandled=0, samples=[0.1], interactive=True,
            )
            for name in ("typing", "scale")
        ]
        results.append(
            bench.Result(
                name="idle", frames=1, changed=1, emitted=10, needed=10, calls=1,
                seconds=1.0, unhandled=0, samples=[0.1], interactive=False,
            )
        )
        self.assertEqual(bench.score(results)["score"], 100.0)

    def test_wasted_bytes_cost_the_paint_score(self):
        results = [
            bench.Result(
                name=name, frames=4, changed=4, emitted=1000, needed=100, calls=0,
                seconds=0.0, unhandled=0, samples=[0.1], interactive=True,
            )
            for name in ("typing", "scale")
        ]
        results.append(
            bench.Result(
                name="idle", frames=1, changed=1, emitted=1000, needed=100, calls=1,
                seconds=1.0, unhandled=0, samples=[0.1], interactive=False,
            )
        )
        data = bench.score(results)
        self.assertEqual(data["parts"]["paint"], 10.0)
        self.assertLess(data["score"], 70.0)

    def test_frames_that_change_nothing_cost_the_frame_score(self):
        results = [
            bench.Result(
                name=name, frames=4, changed=2, emitted=100, needed=100, calls=0,
                seconds=0.0, unhandled=0, samples=[0.1], interactive=True,
            )
            for name in ("typing", "scale")
        ]
        results.append(
            bench.Result(
                name="idle", frames=2, changed=1, emitted=10, needed=10, calls=1,
                seconds=1.0, unhandled=0, samples=[0.1], interactive=False,
            )
        )
        self.assertEqual(bench.score(results)["parts"]["frames"], 50.0)


class RegressionGateTest(unittest.TestCase):
    def _data(self, **metrics):
        base = {
            "emitted_bytes": 1000, "needed_bytes": 100, "frames": 10,
            "null_frames": 1, "paint_ratio": 0.100,
        }
        base.update(metrics)
        return {"score": 50.0, "metrics": base}

    def test_clean_run_passes(self):
        ok, problems = bench.check(self._data(), self._data())
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_more_bytes_fails(self):
        ok, problems = bench.check(self._data(emitted_bytes=1500), self._data())
        self.assertFalse(ok)
        self.assertIn("emitted_bytes", problems[0])

    def test_fewer_bytes_passes(self):
        ok, _ = bench.check(self._data(emitted_bytes=400), self._data())
        self.assertTrue(ok)

    def test_a_new_do_nothing_repaint_fails(self):
        ok, problems = bench.check(self._data(null_frames=4), self._data())
        self.assertFalse(ok)
        self.assertIn("null_frames", problems[0])

    def test_a_less_efficient_payload_fails(self):
        ok, problems = bench.check(self._data(paint_ratio=0.05), self._data())
        self.assertFalse(ok)
        self.assertIn("paint_ratio", problems[0])

    def test_a_more_efficient_payload_passes(self):
        ok, _ = bench.check(self._data(paint_ratio=0.80), self._data())
        self.assertTrue(ok)

    def test_extra_visual_change_is_not_a_regression(self):
        """A new field on the row raises needed_bytes; that is a feature."""
        ok, _ = bench.check(self._data(needed_bytes=400), self._data())
        self.assertTrue(ok)

    def test_noise_inside_the_tolerance_passes(self):
        ok, _ = bench.check(self._data(emitted_bytes=1010), self._data())
        self.assertTrue(ok)

    def test_timings_never_gate(self):
        """A slow runner must not fail a tree whose bytes are unchanged."""
        slow = self._data()
        slow["score"] = 20.0
        slow["metrics"]["latency_p99_ms"] = 999.0
        ok, _ = bench.check(slow, self._data())
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
