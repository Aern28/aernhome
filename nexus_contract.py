# created-by: opus
# created: 2026-10-09
# purpose: connector contract - every Nexus connector as {data, as_of, source, empty_reason}, so empty can never read as all-clear
# lifespan: infrastructure
# project: nexus-verify
"""nexus_contract.py - the connector contract for Nexus panels.

The connectors in nexus_sources.py degrade to {} / [] on ANY failure (never
raise, by design for Flask routes). That made "Todoist is down" and "nothing
due today" the same empty list. envelope(name) wraps a connector without
changing its return shape (app.py and templates keep calling them directly):

    {"name", "data", "as_of", "source", "freshness", "empty_reason", "warning"}

  as_of        ISO UTC of the newest underlying data, or of the fetch for live APIs
  freshness    live (fetched from an API) | data (as_of comes from the data) | user (Aern-entered, no bar)
  empty_reason None when data is non-empty; else none_due | source_missing: ... | error: ...
               An empty envelope WITHOUT a reason is a contract violation (tests enforce it).

Every public function in nexus_sources is either in CONNECTORS or in
NOT_CONNECTORS - tests/test_nexus_contract.py fails on a new unregistered one,
so a connector cannot ship without saying how old its data is.
"""
import os
import sqlite3
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import nexus_sources as ns

CENTRAL = ZoneInfo("America/Chicago")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TCG_DB = os.environ.get("TCG_DB_PATH", "/tcg/inventory.db")


def _iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def _epoch(t):
    return datetime.fromtimestamp(t, tz=timezone.utc) if t else None


def _sql_max(db, sql):
    """One timestamp/date from a read-only query; naive = UTC, bare date = Central midnight."""
    if not os.path.exists(db):
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        v = con.execute(sql).fetchone()[0]
    finally:
        con.close()
    return _parse(v)


def _parse(v):
    if not v:
        return None
    s = str(v).strip()
    try:
        if len(s) == 10:  # YYYY-MM-DD: a local calendar day
            return datetime.combine(datetime.fromisoformat(s).date(), dtime.min, tzinfo=CENTRAL)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _mtime(path):
    return _epoch(os.path.getmtime(path)) if os.path.exists(path) else None


def _oura_as_of(data):
    days = [v.get("day") for v in (data or {}).values() if isinstance(v, dict) and v.get("day")]
    return _parse(max(days)) if days else None


# name -> spec. call: the connector; as_of(data) -> datetime|None; health: the
# nexus_sources._mark key for live connectors; retired: why it can only be empty.
CONNECTORS = {
    "todoist_today": {
        "call": ns.todoist_today, "freshness": "live", "source": "Todoist API (today + overdue)",
        "health": "todoist_today", "as_of": lambda d: _epoch(ns.cache_time("todoist_today"))},
    "schedule_today": {
        "call": ns.schedule_today, "freshness": "live", "source": "Google Calendar (matt + gal)",
        "health": "schedule_today", "as_of": lambda d: _epoch(ns.cache_time("schedule_today"))},
    "oura_summary": {
        "call": ns.oura_summary, "freshness": "data", "source": "Oura API (newest scored day)",
        "health": "oura_summary", "as_of": _oura_as_of},
    "tcg_alerts": {
        "call": ns.tcg_alerts, "freshness": "data", "source": "inventory.db prices (newest price date)",
        "as_of": lambda d: _sql_max(TCG_DB, "SELECT MAX(date) FROM prices")},
    "tcg_business": {
        "call": ns.tcg_business, "freshness": "data", "source": "inventory.db (file write time)",
        "as_of": lambda d: _mtime(TCG_DB)},
    "direct_progress": {
        "call": ns.direct_progress, "freshness": "data", "source": "inventory.db (file write time)",
        "as_of": lambda d: _mtime(TCG_DB)},
    "infra_summary": {
        "call": ns.infra_summary, "freshness": "data", "source": "dashboard.db health_checks",
        "as_of": lambda d: _sql_max(os.path.join(DATA_DIR, "dashboard.db"), "SELECT MAX(checked_at) FROM health_checks")},
    "goals_summary": {
        "call": ns.goals_summary, "freshness": "user", "source": "nexus.db goals", "as_of": lambda d: None},
    "maintenance_due": {
        "call": ns.maintenance_due, "freshness": "user", "source": "nexus.db maintenance", "as_of": lambda d: None},
    "currently_reading": {
        "call": ns.currently_reading, "freshness": "data", "source": "Obsidian Books folder",
        "as_of": lambda d: None, "retired": "vault mount retired 2026-08-06; books live in nexus.db"},
}

# Public names in nexus_sources that are not panel connectors (helpers, writes, lookups).
NOT_CONNECTORS = {
    "health", "cache_time", "cache_clear", "todoist_close", "download_poster_image",
    "tmdb_search", "igdb_search", "openlibrary_search",
    # Side data of the todoist_today fetch (same call, same age): home reads it next to
    # that connector's envelope, so it has no freshness of its own to declare.
    "todoist_extra",
}


def _is_empty(data):
    if data is None:
        return True
    if isinstance(data, (list, tuple, set, str)):
        return len(data) == 0
    if isinstance(data, dict):
        return not any(v not in (None, [], {}, "", 0) for v in data.values())
    return False


def envelope(name):
    """Run one connector and wrap it. Never raises."""
    spec = CONNECTORS[name]
    env = {"name": name, "source": spec["source"], "freshness": spec["freshness"],
           "data": None, "as_of": None, "empty_reason": None, "warning": None}
    try:
        data = spec["call"]()
    except Exception as e:  # connectors promise not to raise; say so if one does
        env["empty_reason"] = f"error: connector raised {type(e).__name__}"
        return env
    env["data"] = data
    try:
        env["as_of"] = _iso(spec["as_of"](data))
    except Exception as e:
        env["warning"] = f"as_of failed: {type(e).__name__}"
    h = ns.health(spec["health"]) if spec.get("health") else None
    if h and h["ok"] and h["why"]:
        env["warning"] = h["why"]
    if _is_empty(data):
        if spec.get("retired"):
            env["empty_reason"] = f"source_missing: {spec['retired']}"
        elif h and not h["ok"]:
            env["empty_reason"] = h["why"] or "error: unknown"
        elif spec["freshness"] == "data" and env["as_of"] is None:
            env["empty_reason"] = "source_missing: no data and no timestamp"
        else:
            env["empty_reason"] = "none_due"
    elif h and not h["ok"]:  # partial data + a failed leg (e.g. one of two calendars)
        env["warning"] = h["why"]
    return env


def all_envelopes():
    return {n: envelope(n) for n in CONNECTORS}
