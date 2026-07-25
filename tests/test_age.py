import unittest

from herdr_bar.age import elapsed_seconds, format_age, parse_etime, pid_for


class PidForTest(unittest.TestCase):
    def test_the_group_leader_wins_over_its_children(self):
        info = {
            "foreground_process_group_id": 200,
            "foreground_processes": [{"pid": 311}, {"pid": 200}, {"pid": 480}],
            "shell_pid": 100,
        }
        self.assertEqual(pid_for(info), 200)

    def test_the_first_process_stands_in_for_an_unlisted_leader(self):
        info = {
            "foreground_process_group_id": 999,
            "foreground_processes": [{"pid": 311}],
            "shell_pid": 100,
        }
        self.assertEqual(pid_for(info), 311)

    def test_an_idle_pane_falls_back_to_its_shell(self):
        self.assertEqual(pid_for({"foreground_processes": [], "shell_pid": 100}), 100)

    def test_nothing_usable_reads_as_no_pid(self):
        self.assertIsNone(pid_for({}))
        self.assertIsNone(pid_for({"shell_pid": 0, "foreground_processes": [{"pid": "x"}]}))


class ParseEtimeTest(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(parse_etime("07:23"), 443)

    def test_hours(self):
        self.assertEqual(parse_etime("02:04:00"), 7440)

    def test_days(self):
        self.assertEqual(parse_etime("3-04:05:06"), 3 * 86400 + 4 * 3600 + 5 * 60 + 6)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(parse_etime("      01:00\n"), 60)

    def test_junk_reads_as_unknown(self):
        for text in ("", "   ", "not-a-time", "1:2:3:4", "x-01:00"):
            self.assertIsNone(parse_etime(text), text)


class ElapsedSecondsTest(unittest.TestCase):
    def test_this_process_has_an_age(self):
        import os

        seconds = elapsed_seconds(os.getpid())
        self.assertIsNotNone(seconds)
        self.assertGreaterEqual(seconds, 0)

    def test_a_dead_pid_reads_as_unknown(self):
        self.assertIsNone(elapsed_seconds(2 ** 30))


class FormatAgeTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(format_age(0), "0s")
        self.assertEqual(format_age(59.9), "59s")
        self.assertEqual(format_age(60), "1m")
        self.assertEqual(format_age(3599), "59m")
        self.assertEqual(format_age(3600), "1h00m")
        self.assertEqual(format_age(7440), "2h04m")
        self.assertEqual(format_age(86399), "23h59m")
        self.assertEqual(format_age(86400), "1d0h")
        self.assertEqual(format_age(4 * 86400 + 3 * 3600), "4d3h")

    def test_it_stays_short_enough_for_a_row(self):
        for seconds in (0, 61, 3601, 86401, 400 * 86400):
            self.assertLessEqual(len(format_age(seconds)), 6)

    def test_a_negative_age_shows_nothing(self):
        self.assertEqual(format_age(-1), "")


if __name__ == "__main__":
    unittest.main()
