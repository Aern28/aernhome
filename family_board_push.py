# created-by: opus
# created: 2026-10-08
# purpose: Family Week board (Gal/Matt shifts, meals, Jaina drop/pick, notes, restock) -> TRMNL plugin + HA webhook + family-board.json
# lifespan: infrastructure
# project: relay-move-out (discord-claude-relay/MOVE-OUT.md)
"""Family Board Push, moved out of claude-relay (was /workspace/family-board-push.js, built 2026-08-09).

A line-for-line port that runs on the Ashaman HOST with its own Python (google-api-python-client is
already installed there). The board logic is unchanged; only paths and hosts moved:
  service-account key + Mealie token  -> C:/Users/Matt/.relay-secrets (Matt-only ACL, staged 10/08)
  Mealie / Nexus                      -> 127.0.0.1 (was host.docker.internal / aernhome-dashboard)
  jaina-overrides.json, family-board.json -> C:/tcg-inventory (/data/tcg-inventory, /data/aernbot in the relay)
Times are Ashaman local time, which is Central (the relay converted to America/Chicago explicitly).

Usage: py family_board_push.py [--dry-run]
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

SECRETS = os.environ.get("RELAY_SECRETS_DIR", "C:/Users/Matt/.relay-secrets")
SA = os.path.join(SECRETS, "claudendar-service-account.json")
MEALIE_TOKEN = os.path.join(SECRETS, "mealie-token")
CAL_MATT = "mcarroll203@gmail.com"
CAL_GAL = "b672e5962b42a874240c3440c400888d0275a64f6d471ebcd5067b8325592db7@group.calendar.google.com"
TRMNL_URL = "https://trmnl.com/api/custom_plugins/7cc25d15-44e3-4a65-8ff0-37e588c23013"
HA_WEBHOOK = "http://192.168.1.70:8123/api/webhook/family-board-d8dad8d5e3e65bd1ee669e26c2830ecc7dd8931d"
MEALIE = "http://127.0.0.1:9925"
NEXUS = "http://127.0.0.1:5555"
SCHOOL_ID = "245e1964-1273-4ff7-ab6c-fead625072b4"
JAINA_FILE = "C:/tcg-inventory/jaina-overrides.json"
BOARD_FILE = "C:/tcg-inventory/aernbot/family-board.json"
DRY = "--dry-run" in sys.argv

# ---- ported from sync_family_agenda.py (Phoenix) via family-board-push.js ----
SHIFT_MAP = {
    "mc yellow": ("both", "Service"), "mc red": ("both", "Service"),
    "mc eve": ("pm", "Evening shift"), "mc eve 2": ("pm", "Evening shift"),
    "mc we eve": ("pm", "Evening shift"), "mc night": ("pm", "Night Shift"),
    "mc we call": ("both", "Service"), "bu 1": ("both", "Home"),
    "bu night": ("pm", "Backup Evening"), "wc 2": ("both", "Service"),
}
MATT_CODE_MAP = [
    ("gyn bu", "GYN Backup"), ("l&d night", "Labor"), ("l&d", "Labor"),
    ("post call", "Postcall"), ("clinic", "Clinic"), ("education", "Education"),
]
MATT_FALLBACK = "Service"
NO_SCHOOL_RE = re.compile(r"\bno school\b", re.I)
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def week_dates():
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


# TRMNL's edge answers 403 to Python's default "Python-urllib" agent (10/08); name ourselves.
UA = {"user-agent": "family-board-push/1.1 (aernhome)"}


def get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post_json(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**UA, "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def fetch_events(cal, calendar_id, dates):
    # timeMin/timeMax = midnight UTC + 5h, i.e. about midnight Central (as the relay did).
    start = f"{dates[0].isoformat()}T05:00:00Z"
    end = f"{(dates[6] + timedelta(days=1)).isoformat()}T05:00:00Z"
    try:
        r = cal.events().list(calendarId=calendar_id, singleEvents=True, orderBy="startTime",
                              maxResults=100, timeMin=start, timeMax=end,
                              timeZone="America/Chicago").execute()
        return r.get("items", [])
    except Exception as e:
        print(f"calendar {calendar_id[:18]} fetch failed: {str(e)[:80]}", file=sys.stderr)
        return []


def ev_local(ev):
    s = ev.get("start") or {}
    if s.get("date") and not s.get("dateTime"):
        return {"all_day": True, "date": s["date"]}
    if not s.get("dateTime"):
        return None
    dt = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone()
    end_h = None
    e = ev.get("end") or {}
    if e.get("dateTime"):
        end_h = datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00")).astimezone().hour
    return {"all_day": False, "date": dt.date().isoformat(), "hour": dt.hour, "min": dt.minute, "end_h": end_h}


def classify_gal(events):
    by_day, notes, timed = {}, {}, []

    def day(d):
        return by_day.setdefault(d, {"am": None, "pm": None, "amS": False, "pmS": False})

    for ev in events:
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        key = summary.lower()
        w = ev_local(ev)
        if not w:
            continue
        if w["all_day"]:
            if "gal off" in key:
                D = day(w["date"]); D["am"] = D["pm"] = "Off"; D["amS"] = D["pmS"] = True
            elif "gal working" in key:
                D = day(w["date"]); D["am"] = D["pm"] = "Service"; D["amS"] = D["pmS"] = True
            else:
                notes.setdefault(w["date"], []).append(summary)
            continue
        timed.append((w, key))
    for w, key in timed:                      # pass 1: shift codes win
        if key not in SHIFT_MAP:
            continue
        slots, cat = SHIFT_MAP[key]
        D = day(w["date"])
        if slots in ("am", "both"):
            D["am"] = cat; D["amS"] = True
        if slots in ("pm", "both"):
            D["pm"] = cat; D["pmS"] = True
    for w, key in timed:                      # pass 2: meetings fallback
        if key in SHIFT_MAP:
            continue
        slot = "am" if w["hour"] < 12 else "pm"
        D = day(w["date"])
        if not D[slot + "S"]:
            D[slot] = "Meetings"
    return by_day, notes


def classify_matt(events):
    by_day, notes = {}, {}

    def day(d):
        return by_day.setdefault(d, {"am": None, "pm": None})

    for ev in events:
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        key = summary.lower()
        w = ev_local(ev)
        if not w:
            continue
        if not key.startswith("gen"):
            # non-clinical -> NOTES candidates (skip fleet-generated noise)
            if not re.search(r"no call|first day of school menu", summary, re.I):
                if w["all_day"]:
                    label = summary
                else:
                    h = w["hour"]
                    label = f"{summary} {h % 12 or 12}{'a' if h < 12 else 'p'}"
                notes.setdefault(w["date"], []).append(label)
            continue
        cat = MATT_FALLBACK
        for sub, c in MATT_CODE_MAP:
            if sub in key:
                cat = c
                break
        if w["all_day"]:
            for slot in (["am"] if cat == "Postcall" else ["am", "pm"]):
                day(w["date"])[slot] = cat
            continue
        D = day(w["date"])
        if key.endswith(" am"):
            D["am"] = cat
        elif key.endswith(" pm"):
            D["pm"] = cat
        elif cat == "Night Shift":
            D["pm"] = cat
        elif cat == "Postcall":
            D["am"] = cat
        else:
            if w["hour"] < 12:
                D["am"] = cat
            if w["hour"] >= 12 or (w["end_h"] is not None and w["end_h"] > 13):
                D["pm"] = cat
    return by_day, notes


def school_lunch(dates):
    out = {}
    try:
        mmdd = dates[0].strftime("%m/%d/%Y")
        u = ("https://webapis.schoolcafe.com/api/CalendarView/GetWeeklyMenuitemsByGrade"
             f"?SchoolId={SCHOOL_ID}&ServingDate={urllib.parse.quote(mmdd, safe='')}"
             "&ServingLine=K-8%20Lunch&MealType=Lunch&Grade=KG&PersonId=null")
        wk = get_json(u, {"accept": "application/json"})
        for i in range(5):
            d = dates[i]
            cats = (wk or {}).get(f"{d.month}/{d.day}/{d.year}")
            if not cats:
                continue
            items = [re.sub(r"\s+", " ", x.get("MenuItemDescription") or "").strip()
                     for x in cats.get("MEAT/MEAT ALTERNATE & GRAINS", [])]
            rot = [n for n in items if n and not re.match(r"soy butter", n, re.I)]
            if rot:
                out[DAY_NAMES[i]] = rot[0][:22]
    except Exception:
        pass  # lunch optional
    return out


def mealie_dinner(dates):
    out = {}
    try:
        with open(MEALIE_TOKEN, encoding="utf-8") as f:
            tok = f.read().strip()
        u = f"{MEALIE}/api/households/mealplans?start_date={dates[0].isoformat()}&end_date={dates[6].isoformat()}&perPage=50"
        for it in get_json(u, {"authorization": f"Bearer {tok}"}).get("items", []):
            if (it.get("entryType") or "").lower() != "dinner":
                continue
            title = (((it.get("recipe") or {}).get("name")) or it.get("title") or "").strip()
            if not title:
                continue
            out[DAY_NAMES[date.fromisoformat(it["date"]).weekday()]] = title[:24]
    except Exception:
        pass  # dinner optional
    return out


def restock():
    out = {"count": 0, "top": ""}
    try:
        d = get_json(f"{NEXUS}/api/restock")
        out["count"] = d.get("count") or 0
        names = [i.get("item") for c in d.get("categories", []) for i in c.get("items", [])]
        out["top"] = ", ".join(str(s)[:18] for s in names[:3])
    except Exception:
        pass  # optional
    return out


def jaina_overrides():
    out = {}
    try:
        with open(JAINA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        for day_key, v in raw.items():
            dn = next((d for d in DAY_NAMES if d.lower().startswith(day_key.lower()[:3])), None)
            if dn:
                out[dn] = {"drop": (v.get("drop") or "")[:8], "pick": (v.get("pick") or "")[:8]}
    except Exception:
        pass  # no overrides file -> blank Jaina row
    return out


def calendar_client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        SA, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def main():
    if sys.stdout:  # None under pythonw (the scheduled task: no console window every 15 min)
        sys.stdout.reconfigure(encoding="utf-8")  # calendar titles carry emoji; cp1252 consoles choke
    dates = week_dates()
    cal = calendar_client()
    gal_by, gal_notes = classify_gal(fetch_events(cal, CAL_GAL, dates))
    matt_events = fetch_events(cal, CAL_MATT, dates)
    matt_by, matt_notes = classify_matt(matt_events)
    lunch, dinner, jaina, buy = school_lunch(dates), mealie_dinner(dates), jaina_overrides(), restock()
    # The Jaina row is a fixed weekly template, so it showed "Matt/Matt" on the 10/09
    # HISD PD day. An all-day "No school" event on Matt's calendar wins over it.
    no_school = {w["date"] for w in (ev_local(e) for e in matt_events
                                      if NO_SCHOOL_RE.search(e.get("summary") or ""))
                 if w and w["all_day"]}

    days = []
    for i, d in enumerate(dates):
        k, dn = d.isoformat(), DAY_NAMES[i]
        g, m = gal_by.get(k, {}), matt_by.get(k, {})
        din, lun = dinner.get(dn, ""), lunch.get(dn, "")
        meal = (f"{din} \u00b7 L: {lun}" if din else f"L: {lun}") if lun else din
        notes = " \u00b7 ".join((matt_notes.get(k, []) + gal_notes.get(k, []))[:2])[:60]
        days.append({
            "day": dn,
            "gal_am": g.get("am") or "", "gal_pm": g.get("pm") or "",
            "matt_am": m.get("am") or "", "matt_pm": m.get("pm") or "",
            "jaina_drop": "Home" if k in no_school else jaina.get(dn, {}).get("drop", ""),
            "jaina_pick": "Home" if k in no_school else jaina.get(dn, {}).get("pick", ""),
            "meal": meal, "meal_type": "", "notes": notes,
        })

    now = datetime.now()
    merge_variables = {
        "days": days,
        "week_label": f"{MONTHS[dates[0].month - 1]} {dates[0].day}\u2013{dates[6].day}",
        "today": DAY_NAMES[now.weekday()],
        "updated": f"{now.hour:02d}:{now.minute:02d}",
        "restock_count": buy["count"],
        "restock_top": buy["top"],
    }

    if DRY:
        for d in days:
            print(f"{d['day'].ljust(9)} G:{(d['gal_am'] + '/' + d['gal_pm']).ljust(22)} "
                  f"M:{(d['matt_am'] + '/' + d['matt_pm']).ljust(20)} "
                  f"J:{(d['jaina_drop'] + '/' + d['jaina_pick']).ljust(12)} meal='{d['meal']}' notes='{d['notes']}'")
        print("label:", merge_variables["week_label"], "| today:", merge_variables["today"])
        return 0

    status = post_json(TRMNL_URL, {"merge_variables": merge_variables}, 20)
    print(f"pushed to TRMNL: {status}")

    board = {"state": merge_variables["updated"],
             "attributes": {"friendly_name": "Family Board", "icon": "mdi:calendar-week", **merge_variables}}
    try:
        with open(BOARD_FILE, "w", encoding="utf-8") as f:
            json.dump(board, f, ensure_ascii=False)
        print("wrote family-board.json")
    except OSError as e:
        print(f"board json write skipped: {str(e)[:60]}")

    try:
        print(f"HA webhook push: {post_json(HA_WEBHOOK, board, 15)}")
    except Exception as e:
        print(f"HA webhook push failed (board stays stale until next cycle): {str(e)[:60]}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
