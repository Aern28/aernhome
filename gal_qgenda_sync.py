# created-by: opus
# created: 2026-11-03
# purpose: keep Gal's QGenda shifts on the "Gal" Google calendar, from her ICS feed
# lifespan: infrastructure
# project: aernhome
"""One-way sync: QGenda ICS feed -> the existing "Gal" Google calendar (Aern 11/03).

Google can't subscribe a URL into an existing calendar, and every reader (skills, board, Today
page) already reads this calendar's id, so this job writes into it.

Safety, in code:
  - Touches ONLY events carrying the private tag createdBy=gal-qgenda-sync. Hand-entered events
    and the 7/10 one-time import are never modified or deleted.
  - A failed, empty or non-calendar feed writes nothing, so a feed outage can't wipe the shifts.
  - Only today-forward is synced; past events stay as history.
  - More than MAX_DELETES deletes in one run is refused (a truncated feed looks like mass swaps).
  - Titles are cleaned: the feed's "[Name]* comment" part names colleagues and is not copied.

An event's identity is (title, start, end), so a changed shift is a delete plus a create.

Usage: python gal_qgenda_sync.py [--dry-run]
Exit: 0 ok, 1 feed unusable, 2 too many deletes, 3 no write permission on the calendar, 4 anything else.
Every real run stamps data/gal_qgenda_sync_last.json for the Nexus board check gal_sync.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, time, timezone

SECRETS = os.environ.get("RELAY_SECRETS_DIR", "C:/Users/Matt/.relay-secrets")
SA = os.path.join(SECRETS, "claudendar-service-account.json")
URL_FILE = os.path.join(SECRETS, "gal-calendar-ics-url")
CAL_GAL = "b672e5962b42a874240c3440c400888d0275a64f6d471ebcd5067b8325592db7@group.calendar.google.com"
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "gal_qgenda_sync_last.log")
STAMP = os.path.join(HERE, "data", "gal_qgenda_sync_last.json")  # /data in the container; fleet.py reads it
TAG = "gal-qgenda-sync"
MAX_DELETES = 15
SHIFT_RE = re.compile(r"^(mc|bu|wc)\b", re.I)
_lines = []
_stats = {}


def log(msg):
    _lines.append(msg)
    if sys.stdout:  # None under pythonw
        print(msg)


def finish(code):
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} exit={code}\n" + "\n".join(_lines) + "\n")
    if "--dry-run" not in sys.argv:  # the stamp is the Nexus board's dead-man's switch; log lines never hold the URL
        with open(STAMP, "w", encoding="utf-8") as f:
            json.dump({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": code == 0,
                       "error": None if code == 0 else _lines[-1], **_stats}, f)
    sys.exit(code)


def clean_title(summary):
    """'MC Red 7a-3p [Name]* note' -> 'MC Red 7a-3p'. Keeps "called to MC": the board reads it."""
    raw = summary.replace("\\,", ",").replace("\\;", ";")
    title = re.split(r"[\[*]", raw, maxsplit=1)[0].strip()
    if "called to mc" in raw.lower():
        title += " (called to MC)"
    return title


def _utc(value):
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def parse_feed(text):
    """Feed text -> {(title, start_utc, end_utc)}. Every QGenda event seen so far is UTC and timed;
    anything else is skipped and counted, not guessed at."""
    wanted, skipped, ev = set(), 0, None
    for line in re.sub(r"\r?\n[ \t]", "", text).splitlines():
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            s, e, title = ev.get("DTSTART", ""), ev.get("DTEND", ""), clean_title(ev.get("SUMMARY", ""))
            if title and s.endswith("Z") and e.endswith("Z") and ev.get("STATUS") != "CANCELLED":
                wanted.add((title, _utc(s), _utc(e)))
            else:
                skipped += 1
            ev = None
        elif ev is not None and ":" in line:
            prop, value = line.split(":", 1)
            ev[prop.split(";", 1)[0]] = value
    return wanted, skipped


def _event_utc(part):
    return datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0)


def key_of(event):
    return (event.get("summary", ""), _event_utc(event["start"]), _event_utc(event["end"]))


def plan(wanted, tagged, since):
    """(keys to create, event ids to delete). Duplicate tagged events are deleted down to one."""
    wanted = {k for k in wanted if k[1] >= since}
    have, delete = set(), []
    for ev in tagged:
        k = key_of(ev)
        if k in wanted and k not in have:
            have.add(k)
        else:
            delete.append(ev["id"])
    return sorted(wanted - have, key=lambda k: k[1]), delete


def fetch_feed():
    with open(URL_FILE, encoding="utf-8") as f:
        url = f.read().strip()
    req = urllib.request.Request(url, headers={"user-agent": "gal-qgenda-sync/1.0 (aernhome)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def list_all(cal, **kw):
    out, token = [], None
    while True:
        r = cal.events().list(calendarId=CAL_GAL, singleEvents=True, maxResults=250, pageToken=token, **kw).execute()
        out += r.get("items", [])
        token = r.get("nextPageToken")
        if not token:
            return out


def main():
    dry = "--dry-run" in sys.argv
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        text = fetch_feed()
    except Exception as e:
        log(f"feed fetch failed: {type(e).__name__}; nothing written")  # never str(e): may carry the URL
        finish(1)
    if not text.lstrip("\ufeff").startswith("BEGIN:VCALENDAR"):
        log("feed is not a calendar; nothing written")
        finish(1)
    wanted, skipped = parse_feed(text)
    if not wanted:
        log("feed has no usable events; nothing written")
        finish(1)
    _stats["through"] = max(k[1] for k in wanted).astimezone().date().isoformat()

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    creds = service_account.Credentials.from_service_account_file(
        SA, scopes=["https://www.googleapis.com/auth/calendar.events"])
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

    since = datetime.combine(datetime.now().date(), time.min).astimezone().astimezone(timezone.utc)
    time_min = since.isoformat().replace("+00:00", "Z")
    tagged = [e for e in list_all(cal, timeMin=time_min, privateExtendedProperty=f"createdBy={TAG}")
              if (e.get("extendedProperties") or {}).get("private", {}).get("createdBy") == TAG
              and "dateTime" in e.get("start", {}) and _event_utc(e["start"]) >= since]
    creates, deletes = plan(wanted, tagged, since)
    others = [e for e in list_all(cal, timeMin=time_min)
              if (e.get("extendedProperties") or {}).get("private", {}).get("createdBy") != TAG
              and SHIFT_RE.match(e.get("summary") or "")]
    log(f"feed {len(wanted)} ({skipped} skipped) | tagged {len(tagged)} | create {len(creates)} | delete {len(deletes)}"
        f" | untagged shift-like from today {len(others)}")
    for e in others[:10]:
        log(f"  untagged: {(e.get('start') or {}).get('dateTime', (e.get('start') or {}).get('date'))} {e.get('summary')}")

    if len(deletes) > MAX_DELETES:
        log(f"refusing {len(deletes)} deletes (cap {MAX_DELETES}); nothing written")
        finish(2)
    if dry:
        for k in creates[:6]:
            log(f"  would create: {k[1].astimezone():%a %m/%d %H:%M} {k[0]}")
        log("dry run: nothing written")
        finish(0)
    try:
        for event_id in deletes:
            cal.events().delete(calendarId=CAL_GAL, eventId=event_id).execute()
        for title, start, end in creates:
            cal.events().insert(calendarId=CAL_GAL, body={
                "summary": title,
                "description": "Synced from QGenda by gal_qgenda_sync. Edits here get overwritten.",
                "start": {"dateTime": start.isoformat().replace("+00:00", "Z"), "timeZone": "America/Chicago"},
                "end": {"dateTime": end.isoformat().replace("+00:00", "Z"), "timeZone": "America/Chicago"},
                "transparency": "transparent",
                "extendedProperties": {"private": {"createdBy": TAG}},
            }).execute()
    except HttpError as e:
        if e.resp.status in (403, 404):
            log(f"no write permission on the Gal calendar (HTTP {e.resp.status}); share it with the service account")
            finish(3)
        raise
    _stats.update(created=len(creates), deleted=len(deletes))
    log(f"done: created {len(creates)}, deleted {len(deletes)}")
    finish(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # anything unplanned still stamps, so the board goes red
        log(f"sync failed: {type(e).__name__}")  # never str(e): may carry the URL
        finish(4)
