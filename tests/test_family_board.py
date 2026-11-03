# created-by: opus
# created: 2026-11-03
# purpose: Gal's QGenda shift titles classify correctly on the Family Board
# lifespan: helper
# project: aernhome
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import family_board_push as fb


def ev(title, start="2026-11-04T07:00:00-06:00"):
    return {"summary": title, "start": {"dateTime": start}}


def slots(title, start="2026-11-04T07:00:00-06:00"):
    by_day, notes = fb.classify_gal([ev(title, start)])
    assert not notes
    d = by_day["2026-11-04"]
    return d["am"], d["pm"]


PM = "2026-11-04T15:00:00-06:00"
NIGHT = "2026-11-04T22:00:00-06:00"


class GalShifts(unittest.TestCase):
    def test_timed_titles_map_like_bare_codes(self):
        self.assertEqual(slots("MC Yellow 7a-3p [Barak]"), ("Service", "Service"))
        self.assertEqual(slots("MC Red 7a-3p [Barak]* ML, Barak for BM out"), ("Service", "Service"))
        self.assertEqual(slots("MC Eve 3p-10p [Barak]", PM), (None, "Evening shift"))
        self.assertEqual(slots("MC Eve 2 3p-10p [Barak]", PM), (None, "Evening shift"))
        self.assertEqual(slots("MC WE Eve 3p-10p [Barak]", PM), (None, "Evening shift"))
        self.assertEqual(slots("MC Night 10p-7a [Barak]", NIGHT), (None, "Night Shift"))
        self.assertEqual(slots("BU Night 7p-7a [Barak]", PM), (None, "Backup Evening"))

    def test_codes_new_in_the_feed(self):
        self.assertEqual(slots("MC Silver 7a-7p [Barak]"), ("Service", "Service"))
        self.assertEqual(slots("MC Bronze 7a-3p [Barak]"), ("Service", "Service"))
        self.assertEqual(slots("MC WD Call 7a-3p [Barak]"), ("Service", "Service"))
        self.assertEqual(slots("MC Day 2 7a-3p [Barak]* Yellow with Tong"), ("Service", "Service"))

    def test_backup_called_in_is_service_not_home(self):
        self.assertEqual(slots("BU 1 7a-7p [Barak]"), ("Home", "Home"))
        self.assertEqual(slots("BU 1 7a-7p [Barak]* called to MC for BM out"), ("Service", "Service"))

    def test_unknown_mc_code_is_a_shift_not_a_meeting(self):
        self.assertEqual(slots("MC Purple 7a-3p [Barak]"), ("Service", "Service"))
        self.assertEqual(slots("MC Purple 3p-10p [Barak]", PM), (None, "Evening shift"))

    def test_old_bare_codes_and_meetings_still_work(self):
        self.assertEqual(slots("MC Yellow"), ("Service", "Service"))
        self.assertEqual(slots("Dentist", "2026-11-04T09:00:00-06:00"), ("Meetings", None))


if __name__ == "__main__":
    unittest.main()
