# created-by: opus
# created: 2026-10-09
# purpose: unit tests for nexus_panels view-models (card freshness, Needs-you split, evening schedule, status line, tabs)
# lifespan: helper
# project: nexus-panels
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nexus_panels as npl  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 9, 22, 30, tzinfo=CT).astimezone(timezone.utc)


def env(as_of=None, reason=None, warning=None, source="Todoist API (today + overdue)"):
    return {"as_of": as_of, "empty_reason": reason, "warning": warning, "source": source}


class Freshness(unittest.TestCase):
    def test_fresh_label(self):
        f = npl.freshness(env((NOW - timedelta(minutes=2)).isoformat()), 1, NOW)
        self.assertEqual((f["state"], f["label"]), ("fresh", "2m ago · Todoist API"))

    def test_stale_over_bar(self):
        f = npl.freshness(env((NOW - timedelta(hours=50)).isoformat()), 48, NOW)
        self.assertEqual((f["state"], f["label"]), ("stale", "2d ago · Todoist API"))

    def test_down_beats_everything(self):
        f = npl.freshness(env(NOW.isoformat(), reason="error: ConnectionError"), 1, NOW)
        self.assertEqual(f["state"], "down")
        self.assertEqual(f["why"], "error: ConnectionError")

    def test_none_due_is_not_down(self):
        self.assertEqual(npl.freshness(env(NOW.isoformat(), reason="none_due"), 1, NOW)["state"], "fresh")

    def test_warning_is_stale(self):
        self.assertEqual(npl.freshness(env(NOW.isoformat(), warning="partial: stopped at 10 pages"), 1, NOW)["state"], "stale")


class Needs(unittest.TestCase):
    ITEMS = [
        {"source_kind": "seat", "title": "Photo backup", "priority": 1},
        {"source_kind": "queue", "id": "q1", "title": "Audit", "priority": "2"},
        {"source_kind": "fleet", "title": "Nexus Pages", "priority": 2, "ref": "/nexus/fleet"},
        {"source_kind": "todoist", "todoist_id": "t1", "title": "Overdue thing", "priority": 1, "overdue_days": 3},
        {"source_kind": "todoist", "todoist_id": "t2", "title": "Due today", "priority": 3, "overdue_days": 0},
    ]

    def test_split(self):
        v = npl.needs_view(self.ITEMS)
        self.assertEqual([i["title"] for i in v["items"]], ["Overdue thing", "Audit", "Nexus Pages"])
        self.assertEqual([s["title"] for s in v["sessions"]], ["Photo backup"])
        self.assertEqual([t["title"] for t in v["today"]], ["Due today"])

    def test_actions(self):
        acts = {i["title"]: i["action"] for i in npl.needs_view(self.ITEMS)["items"]}
        self.assertEqual(acts["Audit"]["kind"], "queue-resolve")
        self.assertEqual(acts["Overdue thing"]["kind"], "todoist-close")
        self.assertEqual(acts["Nexus Pages"], {"kind": "link", "href": "/nexus/fleet", "label": "Open"})

    def test_cap_and_more(self):
        many = [{"source_kind": "queue", "id": str(n), "title": str(n), "priority": 2} for n in range(8)]
        v = npl.needs_view(many)
        self.assertEqual((len(v["items"]), v["more"]), (5, 3))

    def test_non_nexus_ref_gets_no_link(self):
        v = npl.needs_view([{"source_kind": "fleet", "title": "x", "ref": "C:/somewhere"}])
        self.assertIsNone(v["items"][0]["action"])


class Schedule(unittest.TestCase):
    SCHED = {"today": {"matt": [{"when": "all day", "summary": "PD day"}, {"when": "8:00 PM", "summary": "Late call"}],
                       "gal": [{"when": "1:00 PM", "summary": "Clinic"}, {"when": "11:00 PM", "summary": "Night"}]},
             "tomorrow": {"matt": [], "gal": []}}

    def test_daytime_shows_today_only(self):
        noon = datetime(2026, 10, 9, 12, 0, tzinfo=CT)
        v = npl.schedule_view(self.SCHED, noon)
        self.assertEqual([d["label"] for d in v], ["Today"])

    def test_evening_tomorrow_first_then_unstarted(self):
        v = npl.schedule_view(self.SCHED, NOW)
        self.assertEqual([d["label"] for d in v], ["Tomorrow", "Rest of today"])
        self.assertEqual(v[0]["groups"], [])
        rest = [e["summary"] for g in v[1]["groups"] for e in g["events"]]
        self.assertEqual(rest, ["Night"])  # all-day and already-started are gone

    def test_evening_nothing_left(self):
        late = datetime(2026, 10, 9, 23, 30, tzinfo=CT)
        self.assertEqual([d["label"] for d in npl.schedule_view(self.SCHED, late)], ["Tomorrow"])


class StatusAndTabs(unittest.TestCase):
    def test_status(self):
        self.assertEqual(npl.status_line({"a": {"status": "up"}})[0], "ok")
        self.assertEqual(npl.status_line({"a": {"status": "warn", "label": "Nexus Pages"}}),
                         ("warn", "Fleet: 1 warning(s) (Nexus Pages)"))
        self.assertEqual(npl.status_line({"a": {"status": "down", "label": "Relay"}})[0], "down")
        self.assertEqual(npl.status_line({})[0], "warn")

    def test_tabs(self):
        cases = {"/nexus": "home", "/nexus/": "home", "/nexus/aern": "aern", "/nexus/signals": "tcg",
                 "/nexus/books": "media", "/nexus/house": "more", "/nexus/feed/x-feed": "more",
                 "/nexus/tcg": "tcg", "/nexus/tcgx": "more"}
        for path, tab in cases.items():
            self.assertEqual(npl.tab_for(path), tab, path)


if __name__ == "__main__":
    unittest.main()
