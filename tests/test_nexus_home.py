# created-by: opus
# created: 2026-10-10
# purpose: unit tests for nexus_home (date, SCW lead, day strip, next up, waiting asks, tasks, binder pockets, fleet line)
# lifespan: helper
# project: nexus-panels
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nexus_home as nh  # noqa: E402

CT = nh.CT
NOW = dt.datetime(2026, 10, 10, 12, 15, tzinfo=CT)  # Saturday
TODAY = NOW.date()


def ev(summary, start, end=None, allday=False):
    if allday:
        return {"summary": summary, "when": "all day", "allday": True, "start": start, "end": end or start}
    s = dt.datetime.fromisoformat(start).replace(tzinfo=CT)
    e = dt.datetime.fromisoformat(end).replace(tzinfo=CT) if end else s
    return {"summary": summary, "when": "", "allday": False, "start": s.isoformat(), "end": e.isoformat()}


SCHED = {
    "today": {"matt": [], "gal": [ev("MC Eve", "2026-10-10T15:00", "2026-10-10T22:00")]},
    "tomorrow": {"matt": [], "gal": []},
    "later": {"matt": [ev("L&D", "2026-10-12T07:00", "2026-10-12T19:00")], "gal": []},
}


class DateAndWeather(unittest.TestCase):
    def test_date_label(self):
        self.assertEqual(nh.date_label(NOW), "Saturday, October 10")

    def feed(self, pub, body):
        return (f'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
                f'<title>t</title><pubDate>{pub}</pubDate><content:encoded><![CDATA[{body}]]></content:encoded>'
                f'</item></channel></rss>').encode()

    def test_scw_in_brief_first_sentence(self):
        x = self.feed("Sat, 10 Oct 2026 11:00:00 +0000",
                      "<p>Good morning.</p><p><strong>In brief:</strong> Hot today, low 90s. Humidity returns next week.</p>")
        self.assertEqual(nh.scw_lead(x, NOW), "Hot today, low 90s.")

    def test_scw_old_post_is_none(self):
        x = self.feed("Fri, 09 Oct 2026 11:00:00 +0000", "<p>In brief: Yesterday.</p>")
        self.assertIsNone(nh.scw_lead(x, NOW))

    def test_open_meteo_line(self):
        w = {"daily": {"temperature_2m_max": [92.6], "temperature_2m_min": [75.2]},
             "hourly": {"time": ["2026-10-10T13:00", "2026-10-11T13:00"], "precipitation_probability": [20, 90]}}
        self.assertEqual(nh.open_meteo_line(w, NOW), "High 93, low 75, 20% chance of rain.")


class Strip(unittest.TestCase):
    def test_gal_block_position_and_now(self):
        s = nh.day_strip(SCHED, NOW, "Jaina home all day")
        gal = [r for r in s["rows"] if r["who"] == "gal"][0]["blocks"][0]
        self.assertEqual((gal["left"], gal["width"]), (56.25, 43.75))
        self.assertEqual(s["now"], 39.06)
        self.assertEqual(s["rows"][0]["who"], "jaina")
        self.assertIn("Gal: MC Eve, 3 pm to 10 pm", s["aria"])

    def test_overnight_block_clips_to_strip(self):
        sched = {"today": {"matt": [ev("L&D Night", "2026-10-09T19:00", "2026-10-10T07:00")], "gal": []}}
        b = nh.day_strip(sched, NOW)["rows"][0]["blocks"][0]
        self.assertEqual(b["left"], 0.0)
        self.assertAlmostEqual(b["width"], 6.25, places=2)

    def test_no_now_tick_at_night(self):
        self.assertIsNone(nh.day_strip({}, NOW.replace(hour=23))["now"])

    def test_next_up_looks_past_tomorrow(self):
        self.assertEqual(nh.next_up(SCHED, NOW.replace(hour=23)), "Monday 7 am, L&D")

    def test_next_up_today_gal(self):
        self.assertEqual(nh.next_up(SCHED, NOW), "Today 3 pm, Gal: MC Eve")


class Waiting(unittest.TestCase):
    ITEMS = [
        {"source_kind": "queue", "id": "a", "priority": 2, "todoist_id": "t1"},
        {"source_kind": "queue", "id": "b", "priority": 3, "todoist_id": "t2"},
        {"source_kind": "fleet", "id": None, "priority": 2},
        {"source_kind": "tcg_held", "priority": 1},
        {"source_kind": "seat", "priority": 1},
        {"source_kind": "todoist", "priority": 1},
    ]

    def test_only_asks_and_dated_twins_wait(self):
        out = nh.waiting(self.ITEMS, {"t2": "2026-10-24"}, TODAY)
        self.assertEqual([i["id"] for i in out], ["a"])

    def test_twin_due_today_shows(self):
        out = nh.waiting(self.ITEMS, {"t2": "2026-10-10"}, TODAY)
        self.assertEqual([i["id"] for i in out], ["a", "b"])


class Tasks(unittest.TestCase):
    def test_when_labels(self):
        rows = nh.tasks([{"id": "1", "content": "late", "due": "2026-10-07", "overdue_days": 3},
                         {"id": "2", "content": "now", "due": "2026-10-10", "overdue_days": 0}],
                        [{"id": "3", "content": "soon", "due": "2026-10-13"}], TODAY)
        self.assertEqual([(r["text"], r["when"], r["late"]) for r in rows],
                         [("late", "3 days late", True), ("now", "today", False), ("soon", "Tue", False)])


class Pockets(unittest.TestCase):
    def test_nine_pockets_three_rows(self):
        p = nh.pockets({})
        self.assertEqual(len(p), 9)
        self.assertEqual([x["row"] for x in p], ["sell"] * 3 + ["buy"] * 3 + ["home"] * 3)

    def test_missing_inputs_stay_static_not_invented(self):
        notes = {x["name"]: x["note"] for x in nh.pockets({})}
        self.assertEqual(notes["Want board"], "Open the want board")
        self.assertEqual(notes["Restock"], "Household list")
        self.assertFalse(any(x["alert"] for x in nh.pockets({})))

    def test_movers_ignore_cards_he_does_not_hold(self):
        p = {x["name"]: x for x in nh.pockets({"movers": {
            "drops": [], "gainers": [{"card": "Promo", "pct": "+3726%", "flags": ["PLAY"]}]}})}
        self.assertEqual(p["Movers"]["note"], "No moves on your cards")

    def test_live_notes_and_amber(self):
        p = {x["name"]: x for x in nh.pockets({
            "tcg_business": {"tsn": 0, "sr": "$41"}, "tcg_alerts": {"reprice_due": 2, "sales_today": 1},
            "held": 1, "wants": {"hits": 3},
            "movers": {"drops": [{"card": "Doublefinger", "pct": "-11%", "flags": ["HELD"]}], "gainers": []},
            "restock": {"count": 4, "categories": [{"label": "Grocery", "count": 3}, {"label": "House", "count": 1}]},
            "gal_today": "Gal 3 pm to 10 pm", "notebook": "The bags that worked"})}
        self.assertEqual(p["Orders to ship"]["note"], "1 held order. Needs you.")
        self.assertTrue(p["Orders to ship"]["alert"])
        self.assertEqual(p["Sales"]["note"], "1 today, $41 this week")
        self.assertEqual((p["Want board"]["note"], p["Want board"]["alert"]), ("3 hits on targets", True))
        self.assertEqual((p["Movers"]["note"], p["Movers"]["alert"]), ("Doublefinger -11%", True))
        self.assertEqual(p["Restock"]["note"], "4 items: grocery and house")
        self.assertEqual(p["Reprice"]["note"], "2 cards due")


class ScheduleSource(unittest.TestCase):
    def test_quiet_weekend_keeps_next_week(self):
        # Live 10/10: today + tomorrow empty threw the whole week away, so Next up vanished.
        from unittest import mock
        import nexus_sources as ns
        later = [ev("L&D", "2026-10-12T07:00", "2026-10-12T19:00")]
        fake = {"today": [], "tomorrow": [], "later": later}
        with mock.patch.object(ns, "_schedule_for", return_value=fake), \
                mock.patch.dict(os.environ, {"CALENDAR_ID_GAL": "x"}):
            out = ns._schedule_today_compute()
        self.assertEqual(out["later"]["matt"], later)
        self.assertEqual(nh.next_up(out, NOW), "Monday 7 am, L&D")


ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT", "SUMMARY:GEN - PFW L&D Day", "DTSTART:20261012T120000Z", "DTEND:20261013T000000Z", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:GEN - PFW Clinic AM", "DTSTART;TZID=America/Chicago:20261010T080000",
    "DTEND;TZID=America/Chicago:20261010T120000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:1:1 with Dr. Resident Name", "DTSTART:20261010T200000Z", "DTEND:20261010T210000Z", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:PTO", "DTSTART;VALUE=DATE:20261011", "DTEND;VALUE=DATE:20261012", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:GEN - PFW Education P", " M", "DTSTART:20261014T180000Z", "DTEND:20261014T220000Z", "END:VEVENT",
    "END:VCALENDAR", ""])


class WorkShifts(unittest.TestCase):
    def setUp(self):
        import nexus_sources as ns
        self.ns = ns
        self.b = ns._work_shift_buckets(ns._ics_events(ICS), NOW)

    def test_only_shifts_and_pto_never_meetings(self):
        every = [e["summary"] for bucket in self.b.values() for e in bucket]
        self.assertEqual(sorted(every), ["Clinic AM", "Education PM", "L&D Day", "PTO"])
        self.assertFalse(any("Resident" in s for s in every))

    def test_buckets_and_times(self):
        self.assertEqual([e["summary"] for e in self.b["today"]], ["Clinic AM"])
        self.assertEqual(self.b["today"][0]["start"], "2026-10-10T08:00:00-05:00")
        self.assertEqual([(e["summary"], e["allday"]) for e in self.b["tomorrow"]], [("PTO", True)])
        ld = [e for e in self.b["later"] if e["summary"] == "L&D Day"][0]
        self.assertEqual((ld["start"], ld["end"]), ("2026-10-12T07:00:00-05:00", "2026-10-12T19:00:00-05:00"))
        self.assertEqual(nh.next_up({"later": {"matt": self.b["later"]}}, NOW.replace(hour=23)), "Monday 7 am, L&D Day")

    def test_no_link_means_no_fetch_and_no_error(self):
        from unittest import mock
        errs = []
        with mock.patch.object(self.ns, "_work_ics_url", return_value=""), \
                mock.patch.object(self.ns.requests, "get", side_effect=AssertionError("fetched")):
            self.assertEqual(self.ns._work_shifts(errs), {"today": [], "tomorrow": [], "later": []})
        self.assertEqual(errs, [])

    def test_failed_fetch_never_puts_the_link_in_the_error(self):
        from unittest import mock
        errs = []
        secret = "https://example.invalid/private/SECRET-TOKEN/basic.ics"
        with mock.patch.object(self.ns, "_work_ics_url", return_value=secret), \
                mock.patch.object(self.ns.requests, "get", side_effect=ConnectionError(f"failed {secret}")):
            self.ns._work_shifts(errs)
        self.assertEqual(errs, ["error: work shifts ConnectionError"])
        self.assertNotIn("SECRET", " ".join(errs))


class Fleet(unittest.TestCase):
    def test_quiet_when_ok(self):
        self.assertIsNone(nh.fleet_line(("ok", "Fleet: all 30 checks up")))
        self.assertEqual(nh.fleet_line(("warn", "Fleet: 1 warning(s) (x)")), "Fleet: 1 warning(s) (x)")


if __name__ == "__main__":
    unittest.main()
