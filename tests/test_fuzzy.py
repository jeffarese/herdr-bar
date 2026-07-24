import unittest

from herdr_bar.fuzzy import match, split_query


class SplitQueryTest(unittest.TestCase):
    def test_splits_on_whitespace(self):
        self.assertEqual(split_query("tri  week"), ["tri", "week"])

    def test_keeps_quoted_phrases_together(self):
        self.assertEqual(split_query('"week gen" tri'), ["week gen", "tri"])

    def test_empty_query_has_no_terms(self):
        self.assertEqual(split_query("   "), [])


class MatchTest(unittest.TestCase):
    def test_returns_none_when_characters_are_missing(self):
        self.assertIsNone(match("xyz", "week generation"))

    def test_returns_none_when_order_does_not_hold(self):
        self.assertIsNone(match("kew", "week"))

    def test_positions_point_at_the_matched_characters(self):
        found = match("week", "Redesign week loading")
        assert found is not None
        self.assertEqual("".join("Redesign week loading"[i] for i in found.positions), "week")

    def test_prefix_beats_a_later_match(self):
        prefix = match("log", "logs")
        buried = match("log", "the catalog")
        assert prefix is not None and buried is not None
        self.assertGreater(prefix.score, buried.score)

    def test_word_boundary_beats_mid_word(self):
        boundary = match("wg", "week gen")
        inside = match("wg", "weekgen")
        assert boundary is not None and inside is not None
        self.assertGreater(boundary.score, inside.score)

    def test_consecutive_beats_scattered(self):
        together = match("week", "week")
        scattered = match("week", "w e e k")
        assert together is not None and scattered is not None
        self.assertGreater(together.score, scattered.score)

    def test_smart_case_is_insensitive_for_lowercase_queries(self):
        self.assertIsNotNone(match("rc", "RC"))

    def test_smart_case_is_sensitive_once_the_query_has_uppercase(self):
        self.assertIsNone(match("RC", "rc"))
        self.assertIsNotNone(match("RC", "RC"))

    def test_empty_query_matches_with_no_positions(self):
        found = match("", "anything")
        assert found is not None
        self.assertEqual(found.positions, ())

    def test_single_character_query(self):
        found = match("k", "week")
        assert found is not None
        self.assertEqual(found.positions, (3,))


if __name__ == "__main__":
    unittest.main()
