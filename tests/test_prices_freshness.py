# created-by: opus
# created: 2026-10-10
# purpose: prices.date is date-only; freshness must read it as a local day, not midnight UTC
# lifespan: helper
# project: nexus-panels
import datetime as dt
import sqlite3

import tcg_plugin_data as t


def _con(date):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE prices (date TEXT)")
    con.execute("INSERT INTO prices VALUES (?)", (date,))
    return con


def test_date_only_today_evening():
    now = dt.datetime(2026, 10, 10, 17, 9, tzinfo=t.TZ)
    assert t.query_prices_freshness(_con("2026-10-10"), now) == {"pf": "Oct 10", "pfa": "today"}


def test_date_only_yesterday_and_older():
    now = dt.datetime(2026, 10, 10, 8, 0, tzinfo=t.TZ)
    assert t.query_prices_freshness(_con("2026-10-09"), now)["pfa"] == "yesterday"
    assert t.query_prices_freshness(_con("2026-10-06"), now)["pfa"] == "4 days ago"


def test_timestamp_still_uses_age():
    now = dt.datetime(2026, 10, 10, 17, 0, tzinfo=dt.timezone.utc)
    assert t.query_prices_freshness(_con("2026-10-10T15:00:00Z"), now)["pfa"] == "2h ago"
