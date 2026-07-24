import json
import os
import shutil
import tempfile
import unittest

from herdr_bar.mru import MAX_ENTRIES, Recents


class RecentsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state", "recent.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_touch_orders_most_recent_first(self):
        recents = Recents(None)
        recents.touch("a")
        recents.touch("b")
        self.assertEqual(recents.rank("b"), 0)
        self.assertEqual(recents.rank("a"), 1)

    def test_touching_again_moves_to_the_front_without_duplicating(self):
        recents = Recents(None)
        recents.touch("a")
        recents.touch("b")
        recents.touch("a")
        self.assertEqual(recents.rank("a"), 0)
        self.assertEqual(len(recents.entries), 2)

    def test_unknown_keys_have_no_rank(self):
        self.assertIsNone(Recents(None).rank("nope"))

    def test_titles_match_when_ids_change_between_sessions(self):
        recents = Recents(None)
        recents.touch("w1:p4", "cards meaningless")
        self.assertEqual(recents.rank("w9:p9", "cards meaningless"), 0)

    def test_list_is_capped(self):
        recents = Recents(None)
        for index in range(MAX_ENTRIES + 25):
            recents.touch("key-%d" % index)
        self.assertEqual(len(recents.entries), MAX_ENTRIES)

    def test_round_trip_through_disk(self):
        recents = Recents(self.path)
        recents.touch("a", "Agent A")
        recents.save()
        self.assertEqual(Recents(self.path).rank("a"), 0)

    def test_save_is_atomic_and_creates_the_directory(self):
        recents = Recents(self.path)
        recents.touch("a")
        recents.save()
        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["version"], 1)

    def test_corrupt_state_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(Recents(self.path).entries, [])

    def test_junk_entries_are_dropped(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            json.dump({"entries": [{"key": "ok"}, {"nope": 1}, "string", 5]}, handle)
        recents = Recents(self.path)
        self.assertEqual([entry["key"] for entry in recents.entries], ["ok"])

    def test_saving_without_a_path_is_a_no_op(self):
        recents = Recents(None)
        recents.touch("a")
        recents.save()

    def test_unwritable_location_does_not_raise(self):
        recents = Recents("/proc/definitely/not/writable/recent.json")
        recents.touch("a")
        recents.save()


if __name__ == "__main__":
    unittest.main()
