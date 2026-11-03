# created-by: opus
# created: 2026-11-03
# purpose: the gal_sync board check goes red on a failed or stale sync and warns on a short horizon
# lifespan: helper
# project: aernhome
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet


class GalSyncCheck(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "stamp.json")
        p = mock.patch.object(fleet, "GAL_SYNC_STAMP", self.path)
        p.start()
        self.addCleanup(p.stop)

    def stamp(self, age_h=0, **fields):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(fields, f)
        when = time.time() - age_h * 3600
        os.utime(self.path, (when, when))

    def through(self, days):
        return (dt.datetime.now(fleet.CENTRAL_TZ).date() + dt.timedelta(days=days)).isoformat()

    def test_no_stamp_is_unknown(self):
        self.assertEqual(fleet.check_gal_sync()[0], "unknown")

    def test_healthy_is_up(self):
        self.stamp(ok=True, through=self.through(90), at="2026-11-03T20:00:00+00:00")
        self.assertEqual(fleet.check_gal_sync()[0], "up")

    def test_failed_run_is_red_and_says_why(self):
        self.stamp(ok=False, error="feed fetch failed: HTTPError; nothing written", through=self.through(90))
        status, detail = fleet.check_gal_sync()
        self.assertEqual(status, "down")
        self.assertIn("HTTPError", detail)

    def test_dead_task_is_red(self):
        self.stamp(age_h=10, ok=True, through=self.through(90))
        self.assertEqual(fleet.check_gal_sync()[0], "down")

    def test_short_horizon_warns(self):
        self.stamp(ok=True, through=self.through(5))
        status, detail = fleet.check_gal_sync()
        self.assertEqual(status, "warn")
        self.assertIn("5d", detail)


if __name__ == "__main__":
    unittest.main()
