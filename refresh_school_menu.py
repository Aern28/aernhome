# created-by: opus
# created: 2026-10-08
# purpose: weekly rebuild of the "School Menu" Google calendar from SchoolCafe (Parker ES lunch + breakfast)
# lifespan: infrastructure
# project: relay-move-out (discord-claude-relay/MOVE-OUT.md)
"""School Menu Refresh, moved out of claude-relay (was /workspace/refresh-school-menu.js, built 2026-08-09).

Presentation per Aern: ONE all-day event per school day, title = rotating lunch entrees (standing
everyday items auto-detected and demoted to the description) + a short breakfast tail. Idempotent:
clears the window (today .. +8 weeks) on the dedicated School Menu calendar and rebuilds it.

Runs on the Ashaman HOST; the service-account key lives in C:/Users/Matt/.relay-secrets.
  py refresh_school_menu.py             # clear + rebuild (the Sunday task)
  py refresh_school_menu.py --dry-run   # fetch + print the titles it WOULD write; no calendar writes
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

SECRETS = os.environ.get("RELAY_SECRETS_DIR", "C:/Users/Matt/.relay-secrets")
SA = os.path.join(SECRETS, "claudendar-service-account.json")
CALENDAR_ID = "745892540184045442ec04206bfa31cabf1c217e0c26e66e82352fe8a2d51a46@group.calendar.google.com"
SCHOOL_ID = "245e1964-1273-4ff7-ab6c-fead625072b4"  # Parker ES
GRADE = "KG"
WEEKS_AHEAD = 8           # window: today .. +8 weeks (HISD publishes rolling)
STANDING_FRACTION = 0.6   # item on >60% of menu days = everyday standing item
DRY = "--dry-run" in sys.argv
UA = {"user-agent": "school-menu-refresh/1.1 (aernhome)", "accept": "application/json"}


def mmdd(d):
    return d.strftime("%m/%d/%Y")


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


def menu_for(d, meal_type, serving_line):
    u = ("https://webapis.schoolcafe.com/api/CalendarView/GetDailyMenuitemsByGrade"
         f"?SchoolId={SCHOOL_ID}&ServingDate={urllib.parse.quote(mmdd(d), safe='')}"
         f"&ServingLine={urllib.parse.quote(serving_line, safe='')}&MealType={meal_type}&Grade={GRADE}&PersonId=null")
    try:
        data = get_json(u)
        if not isinstance(data, dict):
            return None
        cats = {}
        for cat, items in data.items():
            if not isinstance(items, list):
                continue
            # normalize whitespace: stray double/trailing spaces vary day to day and split counts
            names = []
            for i in items:
                n = re.sub(r"\s+", " ", i.get("MenuItemDescription") or "").strip()
                if n and n not in names:
                    names.append(n)
            if names:
                cats[cat] = names
        return cats or None
    except Exception:
        return None


def mains(cats):
    if not cats:
        return []
    return cats.get("MEAT/MEAT ALTERNATE & GRAINS") or next(iter(cats.values()), [])


def sig(n):
    # name FAMILY (first two words): HISD renames items mid-window
    return " ".join(n.lower().split()[:2])


def standing_sigs(day_lists, n_days):
    freq = {}
    for day in day_lists:
        for s in {sig(n) for n in day}:
            freq[s] = freq.get(s, 0) + 1
    return {s for s, c in freq.items() if c / n_days > STANDING_FRACTION}


def calendar_client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        SA, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    today = date.today()
    end = today + timedelta(days=WEEKS_AHEAD * 7)

    # serving-line lookup must hit a SCHOOL day (a Sunday returns nothing)
    probe = today
    while probe.weekday() >= 5:
        probe += timedelta(days=1)
    lines = {}
    for meal in ("Breakfast", "Lunch"):
        sl = get_json("https://webapis.schoolcafe.com/api/GetServiceLine"
                      f"?schoolid={SCHOOL_ID}&startdate={urllib.parse.quote(mmdd(probe), safe='')}"
                      f"&enddate={urllib.parse.quote(mmdd(probe), safe='')}&mealtype={meal}")
        lines[meal] = (sl[0].get("ServingLineDescription") if sl else None) or f"K-8 {meal}"
        print(f"{meal} serving line: {lines[meal]} (probed {mmdd(probe)})")

    # ---- pass 1: fetch everything (also finds standing items) ----
    days = []
    d = today
    while d <= end:
        if d.weekday() < 5:
            lunch = menu_for(d, "Lunch", lines["Lunch"])
            bfast = menu_for(d, "Breakfast", lines["Breakfast"])
            if lunch or bfast:
                days.append((d, lunch, bfast))
            time.sleep(0.15)
        d += timedelta(days=1)
    if not days:
        print("no menu days published in window - nothing to do")
        return 0

    lstand = standing_sigs([mains(l) for _, l, _ in days], len(days))
    bstand = standing_sigs([mains(b) for _, _, b in days], len(days))
    print("standing families (demoted from titles):", " | ".join(sorted(lstand)) or "(none)")

    events = []
    for day, lunch, bfast in days:
        rot = [n for n in mains(lunch) if sig(n) not in lstand]
        lunch_bit = " / ".join((rot or mains(lunch))[:2]) or "Lunch menu"
        brot = [n for n in mains(bfast) if sig(n) not in bstand]
        b_name = ((brot or mains(bfast)) or [""])[0]
        b_bit = (" \u00b7 \U0001f95e " + re.sub(r" Breakfast Sandwich$", " Sandwich", b_name, flags=re.I)[:30]) if b_name else ""
        title = ("\U0001f37d\ufe0f " + lunch_bit + b_bit)[:90]

        def fmt(label, cats):
            if not cats:
                return ""
            return f"{label}:\n" + "\n".join(f"  {c}: {', '.join(n)}" for c, n in cats.items()) + "\n\n"
        description = fmt("LUNCH", lunch) + fmt("BREAKFAST", bfast) + "\u2014 Parker ES via SchoolCafe, refreshed " + today.isoformat()
        events.append((day, title, description))

    if DRY:
        for day, title, _ in events:
            print(f"{day.isoformat()}  {title}")
        print(f"DRY RUN: would clear the window and create {len(events)} events")
        return 0

    cal = calendar_client()
    time_min = f"{today.isoformat()}T12:00:00Z"
    time_max = f"{end.isoformat()}T12:00:00Z"
    # ---- clear the window (only service-account-owned events live on this calendar) ----
    cleared, page = 0, None
    while True:
        res = cal.events().list(calendarId=CALENDAR_ID, timeMin=time_min, timeMax=time_max,
                                maxResults=250, pageToken=page, singleEvents=True).execute()
        for ev in res.get("items", []):
            cal.events().delete(calendarId=CALENDAR_ID, eventId=ev["id"]).execute()
            cleared += 1
        page = res.get("nextPageToken")
        if not page:
            break
    # ---- rebuild ----
    for day, title, description in events:
        cal.events().insert(calendarId=CALENDAR_ID, body={
            "summary": title, "description": description,
            "start": {"date": day.isoformat()}, "end": {"date": (day + timedelta(days=1)).isoformat()},
            "transparency": "transparent"}).execute()
        time.sleep(0.2)
    print(f"DONE: cleared {cleared}, created {len(events)} events ({events[0][0]} .. {events[-1][0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
