# created-by: opus
# created: 2026-10-09
# purpose: view-models for the Nexus card pattern (freshness labels, Needs-you split, day schedule, status line, tabs)
# lifespan: infrastructure
# project: nexus-panels
"""nexus_panels.py - the card pattern Aern settled on 10/09 (nexus-panels grill).

Pure functions: no Flask, no I/O, so tests/test_nexus_panels.py covers them.

  freshness(env, stale_after_h)  small grey "2m ago · Todoist"; state fresh | stale | down
  needs_view(items)              Needs you: decisions + fixes (max 5, "+N more"),
                                 board projects waiting on a session collapse to one line,
                                 Todoist due today (not overdue) goes to the Today card
  schedule_view(sched, now)      after 18:00: tomorrow first, then today's not-yet-started timed events
  status_line(checks)            the one fleet line at the top of home
  tab_for(path)                  which bottom tab a page belongs to
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
NEEDS_CAP = 5
EVENING_HOUR = 18

# Bottom tab bar (Aern 10/09: Home / Aern / TCG / Media / More; House lives in More).
TABS = [
    ("home", "/nexus", "Home"),
    ("aern", "/nexus/aern", "Aern"),
    ("tcg", "/nexus/tcg", "TCG"),
    ("media", "/nexus/media", "Media"),
    ("more", "/nexus/more", "More"),
]
_TAB_PREFIX = [
    ("/nexus/aern", "aern"), ("/nexus/queue", "aern"), ("/nexus/seat", "aern"),
    ("/nexus/tcg", "tcg"), ("/nexus/signals", "tcg"), ("/nexus/inventory", "tcg"),
    ("/nexus/wants", "tcg"), ("/nexus/vintage", "tcg"), ("/nexus/theories", "tcg"),
    ("/nexus/media", "media"), ("/nexus/books", "media"), ("/nexus/tv", "media"),
    ("/nexus/games", "media"),
]


def tab_for(path):
    path = (path or "").rstrip("/") or "/nexus"
    if path == "/nexus":
        return "home"
    for prefix, tab in _TAB_PREFIX:
        if path == prefix or path.startswith(prefix + "/"):
            return tab
    return "more"


def _ago(hours):
    if hours < 1 / 60:
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def _short_source(source):
    return (source or "").split(" (")[0]


def freshness(env, stale_after_h=None, now=None):
    """Card corner label from a nexus_contract envelope."""
    now = now or datetime.now(timezone.utc)
    src = _short_source(env.get("source"))
    reason = env.get("empty_reason")
    if reason and reason != "none_due":
        return {"state": "down", "label": f"down · {src}", "why": reason}
    as_of = env.get("as_of")
    if not as_of:
        return {"state": "fresh", "label": src, "why": env.get("warning") or ""}
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError:
        return {"state": "fresh", "label": src, "why": ""}
    hours = (now - ts).total_seconds() / 3600
    stale = (stale_after_h is not None and hours > stale_after_h) or bool(env.get("warning"))
    return {"state": "stale" if stale else "fresh", "label": f"{_ago(hours)} · {src}",
            "why": env.get("warning") or (f"older than {stale_after_h}h" if stale else "")}


def _action(item):
    kind = item.get("source_kind")
    if kind == "todoist" and item.get("todoist_id"):
        return {"kind": "todoist-close", "id": item["todoist_id"], "label": "Done"}
    if kind == "queue" and item.get("id"):
        return {"kind": "queue-resolve", "id": item["id"], "label": "Resolve"}
    ref = item.get("ref") or ""
    if ref.startswith("/nexus"):
        return {"kind": "link", "href": ref, "label": "Open"}
    return None


def needs_view(items, cap=NEEDS_CAP):
    """Split needs-aern items into the home blocks."""
    needs, sessions, today = [], [], []
    for it in items or []:
        kind = it.get("source_kind")
        if kind == "seat":
            sessions.append(it)
        elif kind == "todoist" and not (it.get("overdue_days") or 0) > 0:
            today.append(it)
        else:
            needs.append({**it, "action": _action(it)})
    needs.sort(key=lambda i: int(i.get("priority") or 3))
    return {"items": needs[:cap], "more": max(0, len(needs) - cap),
            "sessions": sessions, "today": [{**t, "action": _action(t)} for t in today]}


def _start_minutes(when):
    """'1:00 PM' -> 780; all-day / unparsable -> None."""
    try:
        t = datetime.strptime(str(when).strip(), "%I:%M %p")
    except ValueError:
        return None
    return t.hour * 60 + t.minute


def schedule_view(sched, now=None):
    """[{label, groups:[{who, events}]}] in display order."""
    now = (now or datetime.now(timezone.utc)).astimezone(CENTRAL)
    sched = sched or {}
    today, tomorrow = sched.get("today") or {}, sched.get("tomorrow") or {}

    def groups(day, keep=lambda e: True):
        out = []
        for who, label in (("matt", "Matt"), ("gal", "Gal")):
            evs = [e for e in (day.get(who) or []) if keep(e)]
            if evs:
                out.append({"who": label, "events": evs})
        return out

    if now.hour < EVENING_HOUR:
        return [{"label": "Today", "groups": groups(today)}]
    mins = now.hour * 60 + now.minute
    rest = groups(today, lambda e: (_start_minutes(e.get("when")) or -1) > mins)
    days = [{"label": "Tomorrow", "groups": groups(tomorrow)}]
    if rest:
        days.append({"label": "Rest of today", "groups": rest})
    return days


def status_line(checks):
    """('ok'|'warn'|'down', text) from fleet_state checks."""
    checks = checks or {}
    down = [c.get("label", k) for k, c in checks.items() if c.get("status") == "down"]
    warn = [c.get("label", k) for k, c in checks.items() if c.get("status") == "warn"]
    if not checks:
        return "warn", "Fleet status unavailable"
    if down:
        return "down", f"Fleet: {len(down)} down ({', '.join(down[:2])})" + (f", {len(warn)} warning(s)" if warn else "")
    if warn:
        return "warn", f"Fleet: {len(warn)} warning(s) ({', '.join(warn[:2])})"
    return "ok", f"Fleet: all {len(checks)} checks up"
