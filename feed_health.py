# created-by: opus
# created: 2026-09-23
# purpose: per-source feed freshness (newest item vs expected cadence) for /nexus/feed badges + the feed_fresh fleet check
# lifespan: infrastructure
# project: aernhome
"""Feed staleness: every producer on /nexus/feed exits 0 when it breaks.

9/22: X-Feed sat on 8/31 for three weeks (rig logged out, run-hidden.vbs
swallowed the exit code) and OPTCG Twitter/Video sat on 9/15 (every event in
events.json past active_until). Nobody noticed until Aern eyeballed the page.
This module makes "newest item older than the source's cadence allows" loud.

Timestamps: feed_items.created_at is SQLite CURRENT_TIMESTAMP (UTC); x-feed
items carry ISO UTC; Notebook headers are Central wall-clock.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import nexus_writes as ns_writes
import xfeed

CENTRAL = ZoneInfo("America/Chicago")

# Hours of silence before a source is stale. Roughly 1.5-2x the real cadence:
# producers that only post on findings (twitter/optcg-video) get slack.
MAX_SILENCE_H = {
    "aernbot": 36,        # QID heartbeats, ~2-3 Notebook entries/day
    "x-feed": 24,         # Trainer "X Feed Capture" ~3x/day
    "optcg": 108,         # Ashaman "OPTCG Digest" every 3 days
    "twitter": 48,        # Tournament Watch q4h, findings-only
    "optcg-video": 48,    # same producer
    "resp": 216,          # weekly Sunday digest
}


def _parse_utc(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def newest(slug):
    """Aware UTC datetime of the source's newest item, or None."""
    if slug == "aernbot":
        e = xfeed.notebook_entries(1)
        if not e:
            return None
        try:
            local = datetime.strptime(e[0]["created_at"], "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return local.replace(tzinfo=CENTRAL).astimezone(timezone.utc)
    if slug == "x-feed":
        items = xfeed._load()
        return max((xfeed._ts(i) for i in items), default=None)
    rows = ns_writes.list_feed_items(source=slug, limit=1)
    return _parse_utc(rows[0]["created_at"]) if rows else None


def status(slug, now=None):
    """{'stale': bool, 'age_h': float|None, 'max_h': int|None} for one source."""
    max_h = MAX_SILENCE_H.get(slug)
    ts = newest(slug)
    now = now or datetime.now(timezone.utc)
    age_h = (now - ts).total_seconds() / 3600 if ts else None
    stale = bool(max_h) and (age_h is None or age_h > max_h)
    return {"stale": stale, "age_h": age_h, "max_h": max_h}


def all_status():
    return {s: status(s) for s in MAX_SILENCE_H}


def fmt_age(h):
    if h is None:
        return "never"
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d"
