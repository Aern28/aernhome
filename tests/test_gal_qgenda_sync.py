# created-by: opus
# created: 2026-11-03
# purpose: the Gal QGenda sync cleans titles and only ever deletes what it should
# lifespan: helper
# project: aernhome
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import family_board_push as fb
import gal_qgenda_sync as sync

UTC = timezone.utc
SINCE = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
A = ("MC Yellow 7a-3p", datetime(2026, 11, 4, 13, 0, tzinfo=UTC), datetime(2026, 11, 4, 21, 0, tzinfo=UTC))
B = ("MC Night 10p-7a", datetime(2026, 11, 5, 4, 0, tzinfo=UTC), datetime(2026, 11, 5, 13, 0, tzinfo=UTC))
PAST = ("MC Red 7a-3p", datetime(2026, 11, 1, 13, 0, tzinfo=UTC), datetime(2026, 11, 1, 21, 0, tzinfo=UTC))


def gcal(event_id, key):
    return {"id": event_id, "summary": key[0],
            "start": {"dateTime": key[1].isoformat()}, "end": {"dateTime": key[2].isoformat()}}


class Titles(unittest.TestCase):
    def test_colleague_names_and_comments_are_dropped(self):
        self.assertEqual(sync.clean_title("MC Yellow 7a-3p [Barak]"), "MC Yellow 7a-3p")
        self.assertEqual(sync.clean_title("MC Red 7a-3p [Barak]* ML\\, Barak for BM out"), "MC Red 7a-3p")
        self.assertEqual(sync.clean_title("MC Day 2 7a-3p [Barak]* Yellow with Tong"), "MC Day 2 7a-3p")

    def test_called_in_survives_and_board_still_reads_it(self):
        title = sync.clean_title("BU 1 7a-7p [Barak]* called to MC for BM out")
        self.assertEqual(title, "BU 1 7a-7p (called to MC)")
        self.assertEqual(fb.gal_shift(title.lower(), 7), ("both", "Service"))


class Feed(unittest.TestCase):
    def test_parse_utc_events_and_skip_the_rest(self):
        text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261104T130000Z\r\nDTEND:20261104T210000Z\r\n"
                "SUMMARY:MC Yellow 7a-3p\r\n  [Barak]\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261105\r\nSUMMARY:All day\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        wanted, skipped = sync.parse_feed(text)
        self.assertEqual(wanted, {A})
        self.assertEqual(skipped, 1)


class Plan(unittest.TestCase):
    def test_in_sync_does_nothing(self):
        self.assertEqual(sync.plan({A, B}, [gcal("a", A), gcal("b", B)], SINCE), ([], []))

    def test_new_shift_is_created(self):
        self.assertEqual(sync.plan({A, B}, [gcal("a", A)], SINCE), ([B], []))

    def test_dropped_shift_is_deleted(self):
        self.assertEqual(sync.plan({A}, [gcal("a", A), gcal("b", B)], SINCE), ([], ["b"]))

    def test_changed_shift_is_delete_plus_create(self):
        moved = ("MC Red 7a-3p", A[1], A[2])
        self.assertEqual(sync.plan({moved}, [gcal("a", A)], SINCE), ([moved], ["a"]))

    def test_past_feed_events_are_not_created(self):
        self.assertEqual(sync.plan({PAST, A}, [gcal("a", A)], SINCE), ([], []))

    def test_duplicate_tagged_event_is_removed(self):
        self.assertEqual(sync.plan({A}, [gcal("a", A), gcal("a2", A)], SINCE), ([], ["a2"]))


if __name__ == "__main__":
    unittest.main()
