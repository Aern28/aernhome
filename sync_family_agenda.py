"""
sync_family_agenda.py — Gal's Google Calendar -> Notion "Family Week Agenda".

The Notion DB (id NOTION_FAMILY_AGENDA_DB below) is a fixed 7-row weekly
template, one row per weekday name (Monday..Sunday), with `select`-type
Gal AM / Gal PM properties (Clinic/Meetings/Postcall/Off/Home/Service/...).
Because the rows are weekday NAMES rather than dates, re-running this against
"this week" (Mon-Sun containing today, America/Chicago) naturally rolls the
same 7 rows forward to a new week's data on each run — no rollover logic
needed.

Auth: NOTION_TOKEN env var (see nexus_notion_import.py for the same pattern).
Calendar: same service-account pattern as nexus_sources.py's schedule_today,
reading CALENDAR_ID_GAL.

Usage:
    python sync_family_agenda.py            # sync this week, print a summary
    python sync_family_agenda.py --dry-run   # classify + print, no Notion writes
"""
import datetime
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

NOTION_VERSION = "2022-06-28"
NOTION_FAMILY_AGENDA_DB = "319d3d1e-e945-8155-b524-c4853c522032"
NOTES_MARKER = "[gal-sync]"

# code -> (slots, category). slots: "am", "pm", or "both".
SHIFT_MAP = {
    "mc yellow": ("both", "Service"),
    "mc red": ("both", "Service"),
    "mc eve": ("pm", "Evening shift"),
    "mc eve 2": ("pm", "Evening shift"),
    "mc we eve": ("pm", "Evening shift"),
    "mc night": ("pm", "Night Shift"),
    "mc we call": ("both", "Service"),
    "bu 1": ("both", "Home"),
    "bu night": ("pm", "Backup Evening"),
    "wc 2": ("both", "Service"),
}

# Matt's QGenda events (AutoSync onto his primary calendar, "GEN - ..." prefix).
# Substring -> category; slot comes from the AM/PM suffix or the event's times.
# Gyn BU = "GYN Backup" (its own label; "Labor" = actually scheduled on L&D).
MATT_CODE_MAP = [
    ("gyn bu", "GYN Backup"),
    ("l&d night", "Labor"),
    ("l&d", "Labor"),
    ("post call", "Postcall"),
    ("clinic", "Clinic"),
    ("education", "Education"),
]
MATT_FALLBACK = "Service"


def _token():
    t = os.environ.get("NOTION_TOKEN")
    if not t:
        sys.exit("No NOTION_TOKEN set.")
    return t.strip()


def _hdrs(token):
    return {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


def _api(url, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=_hdrs(token), method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        sys.exit(f"Notion API {e.code} on {url}\n{e.read().decode(errors='replace')[:500]}")
    except (URLError, TimeoutError) as e:
        sys.exit(f"Notion API unreachable: {e}")


def _plain(rich):
    return "".join(r.get("plain_text", "") for r in (rich or []))


def _week_dates(tz):
    """Monday..Sunday date objects for the week containing today."""
    today = datetime.datetime.now(tz).date()
    monday = today - datetime.timedelta(days=today.weekday())
    return [monday + datetime.timedelta(days=i) for i in range(7)]


def fetch_events(tz, cal_env, default_cal=""):
    """Events on one calendar for this week (Mon 00:00 - next Mon 00:00),
    same service-account pattern as nexus_sources._schedule_for. Returns raw
    Google Calendar API items; never raises — returns [] on any failure."""
    sa_path = os.environ.get(
        "GOOGLE_CALENDAR_SA", "/workspace/.credentials/claudendar-service-account.json")
    cal_id = os.environ.get(cal_env, default_cal)
    if not cal_id or not os.path.exists(sa_path):
        return []
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        return []
    dates = _week_dates(tz)
    start = datetime.datetime.combine(dates[0], datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=7)
    try:
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return svc.events().list(
            calendarId=cal_id, singleEvents=True, orderBy="startTime",
            timeMin=start.astimezone(datetime.timezone.utc).isoformat(),
            timeMax=end.astimezone(datetime.timezone.utc).isoformat(),
            timeZone="America/Chicago", maxResults=100,
        ).execute().get("items", [])
    except Exception:
        return []


def classify(events, tz):
    """events -> {date: {"am": cat_or_None, "pm": cat_or_None, "notes": [str,...]}}.

    Two passes so a recognized shift code always wins over a same-slot
    "Meetings" fallback, regardless of which event the calendar API happened
    to return first for that day."""
    by_day = {}

    def day(d):
        return by_day.setdefault(d, {"am": None, "pm": None, "notes": [],
                                      "am_shift": False, "pm_shift": False})

    timed = []
    for ev in events:
        s = ev.get("start", {})
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        key = summary.lower()

        if s.get("date") and not s.get("dateTime"):
            # All-day event.
            d = datetime.date.fromisoformat(s["date"])
            if "gal off" in key:
                day(d)["am"] = day(d)["pm"] = "Off"
                day(d)["am_shift"] = day(d)["pm_shift"] = True
            elif "gal working" in key:
                day(d)["am"] = day(d)["pm"] = "Service"
                day(d)["am_shift"] = day(d)["pm_shift"] = True
            else:
                day(d)["notes"].append(summary)
            continue

        if not s.get("dateTime"):
            continue
        when = datetime.datetime.fromisoformat(s["dateTime"]).astimezone(tz)
        timed.append((when, key))

    # Pass 1: recognized shift codes (take precedence).
    for when, key in timed:
        if key not in SHIFT_MAP:
            continue
        d = when.date()
        slots, category = SHIFT_MAP[key]
        if slots in ("am", "both"):
            day(d)["am"], day(d)["am_shift"] = category, True
        if slots in ("pm", "both"):
            day(d)["pm"], day(d)["pm_shift"] = category, True

    # Pass 2: everything else -> "Meetings", only into slots a shift hasn't claimed.
    for when, key in timed:
        if key in SHIFT_MAP:
            continue
        d = when.date()
        slot = "am" if when.hour < 12 else "pm"
        if not day(d)[f"{slot}_shift"]:
            day(d)[slot] = "Meetings"

    return by_day


def classify_matt(events, tz):
    """Matt's clinical week from QGenda AutoSync events on his primary calendar.
    ONLY '(GEN)' / 'GEN -' prefixed events count — personal appointments never
    reach the family board. No Meetings fallback. Returns
    {date: {"am": cat_or_None, "pm": cat_or_None}}."""
    by_day = {}

    def day(d):
        return by_day.setdefault(d, {"am": None, "pm": None})

    for ev in events:
        s = ev.get("start", {})
        e = ev.get("end", {})
        summary = (ev.get("summary") or "").strip()
        key = summary.lower()
        if not key.startswith("gen"):
            continue

        category = MATT_FALLBACK
        for sub, cat in MATT_CODE_MAP:
            if sub in key:
                category = cat
                break

        if s.get("date") and not s.get("dateTime"):
            d = datetime.date.fromisoformat(s["date"])
            slots = ("am",) if category == "Postcall" else ("am", "pm")
            for slot in slots:
                day(d)[slot] = category
            continue
        if not s.get("dateTime"):
            continue
        when = datetime.datetime.fromisoformat(s["dateTime"]).astimezone(tz)
        d = when.date()
        if key.endswith(" am"):
            day(d)["am"] = category
        elif key.endswith(" pm"):
            day(d)["pm"] = category
        elif category == "Night Shift":
            day(d)["pm"] = category
        elif category == "Postcall":
            day(d)["am"] = category
        else:
            end_dt = None
            if e.get("dateTime"):
                end_dt = datetime.datetime.fromisoformat(e["dateTime"]).astimezone(tz)
            if when.hour < 12:
                day(d)["am"] = category
            if when.hour >= 12 or (end_dt and end_dt.hour > 13):
                day(d)["pm"] = category

    return by_day


def sync(dry_run=False):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    dates = _week_dates(tz)
    events = fetch_events(tz, "CALENDAR_ID_GAL")
    by_day = classify(events, tz)
    matt_by_day = classify_matt(fetch_events(tz, "CALENDAR_ID_MATT"), tz)

    token = None if dry_run else _token()
    rows = None
    if not dry_run:
        data = _api(f"https://api.notion.com/v1/databases/{NOTION_FAMILY_AGENDA_DB}/query",
                    token, "POST", {"page_size": 20})
        rows = {}
        for r in data.get("results", []):
            title = _plain(r["properties"].get("Day", {}).get("title", []))
            rows[title.strip().lower()] = r

    # Only touch Matt columns when the calendar actually yielded events —
    # an unshared/failing calendar must not wipe hand-entered values with Off.
    matt_enabled = bool(os.environ.get("CALENDAR_ID_MATT")) and bool(matt_by_day)
    summary_lines = []
    for d in dates:
        weekday = d.strftime("%A")
        info = by_day.get(d, {"am": None, "pm": None, "notes": []})
        am = info["am"] or "Off"
        pm = info["pm"] or "Off"
        m_info = matt_by_day.get(d, {"am": None, "pm": None})
        m_am = m_info["am"] or "Off"
        m_pm = m_info["pm"] or "Off"
        note_text = "; ".join(info["notes"])
        summary_lines.append(f"{weekday} ({d.isoformat()}): Gal AM={am} PM={pm}"
                              + (f" | Matt AM={m_am} PM={m_pm}" if matt_enabled else "")
                              + (f"  notes: {note_text}" if note_text else ""))

        if dry_run:
            continue

        row = rows.get(weekday.lower())
        if not row:
            print(f"WARNING: no Notion row found for {weekday}", file=sys.stderr)
            continue

        existing_notes = _plain(row["properties"].get("Notes", {}).get("rich_text", []))
        kept = [ln for ln in existing_notes.split("\n") if not ln.startswith(NOTES_MARKER)]
        if note_text:
            kept.append(f"{NOTES_MARKER} {note_text}")
        new_notes = "\n".join(ln for ln in kept if ln.strip())

        props = {
            "Gal AM": {"select": {"name": am}},
            "Gal PM": {"select": {"name": pm}},
            "Notes": {"rich_text": [{"text": {"content": new_notes[:2000]}}]} if new_notes
                     else {"rich_text": []},
        }
        if matt_enabled:
            props["Matt AM"] = {"select": {"name": m_am}}
            props["Matt PM"] = {"select": {"name": m_pm}}
        _api(f"https://api.notion.com/v1/pages/{row['id']}", token, "PATCH", {"properties": props})

    return "\n".join(summary_lines)


if __name__ == "__main__":
    print(sync(dry_run="--dry-run" in sys.argv))
