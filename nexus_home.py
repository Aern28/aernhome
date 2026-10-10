# created-by: opus
# created: 2026-10-10
# purpose: view-model for the redesigned /nexus home (Aern-approved spec 10/10): date + weather, day strip, next up, waiting-on-you asks, tasks, binder pockets, fleet line
# lifespan: infrastructure
# project: nexus-panels
"""Nexus home view-model. Pure functions where possible so tests can pin them.

Brief (Aern 10/10): Nexus is HIS company brain, not the fleet's dashboard -
schedule, reminders, pending tasks, one tap to his most common work. The fleet
stays invisible unless something is broken.
"""
import datetime as dt
import html
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
STRIP_START_H, STRIP_END_H = 6, 22  # the day strip runs 6 am to 10 pm


# ── Date + weather ───────────────────────────────────────────────────────────
def date_label(now):
    return f"{now:%A}, {now:%B} {now.day}"


SCW_FEED = "https://spacecityweather.com/feed/"
_SCW_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
_scw_cache = {"at": 0.0, "val": None}
_scw_lock = threading.Lock()


def _plain(fragment):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(l for l in (re.sub(r"[ \t]+", " ", x).strip() for x in text.splitlines()) if l)


def _first_sentence(text, limit=150):
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    s = (m.group(1) if m else text.strip())
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."


def scw_lead(feed_xml, now):
    """The lead of today's Space City Weather post: the first sentence after "In brief:",
    else the post's first sentence. None when the newest post isn't from today (Central)."""
    root = ET.fromstring(feed_xml)
    item = root.find("channel/item")
    if item is None:
        return None
    posted = parsedate_to_datetime(item.findtext("pubDate")).astimezone(CT)
    if posted.date() != now.date():
        return None
    body = _plain(item.findtext("content:encoded", "", _SCW_NS) or item.findtext("description", ""))
    m = re.search(r"In brief:\s*(.+)", body, re.I | re.S)
    return _first_sentence(m.group(1) if m else body) or None


def _scw_cached(now):
    """Same feed the fleet's space-city-weather.py reads; fetched at most hourly."""
    with _scw_lock:
        if time.time() - _scw_cache["at"] < 3600:
            return _scw_cache["val"]
    val = None
    try:
        req = urllib.request.Request(SCW_FEED, headers={"User-Agent": "Mozilla/5.0 (aernhome Nexus home)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            val = scw_lead(r.read(), now)
    except Exception:
        val = None
    with _scw_lock:
        _scw_cache.update(at=time.time(), val=val)
    return val


def open_meteo_line(w, now):
    """Fallback one-liner from the Open-Meteo forecast byos.py already fetches."""
    daily = (w or {}).get("daily") or {}
    hi, lo = (daily.get("temperature_2m_max") or [None])[0], (daily.get("temperature_2m_min") or [None])[0]
    if hi is None or lo is None:
        return None
    hourly = (w or {}).get("hourly") or {}
    day = now.date().isoformat()
    rain = [p for t, p in zip(hourly.get("time") or [], hourly.get("precipitation_probability") or [])
            if str(t).startswith(day) and p is not None]
    line = f"High {round(hi)}, low {round(lo)}"
    if rain:
        line += f", {max(rain)}% chance of rain"
    return line + "."


def weather_line(now):
    lead = _scw_cached(now)
    if lead:
        return {"text": lead, "source": "Space City Weather"}
    try:
        import byos
        line = open_meteo_line(byos.data_weather(), now)
    except Exception:
        line = None
    return {"text": line, "source": "Open-Meteo"} if line else None


# ── Day strip + next up ──────────────────────────────────────────────────────
def _pct(t):
    """Position of a Central datetime on the 6am-10pm strip, clamped to 0-100."""
    hours = t.hour + t.minute / 60
    return max(0.0, min(100.0, (hours - STRIP_START_H) / (STRIP_END_H - STRIP_START_H) * 100))


def _short_time(t):
    s = f"{t:%I:%M}".lstrip("0")
    s = s[:-3] if s.endswith(":00") else s
    return f"{s} {'am' if t.hour < 12 else 'pm'}"


def day_strip(schedule, now, jaina=None):
    """Rows for the strip: Matt and Gal timed blocks today (clipped to 6am-10pm), Jaina's
    day as one labelled band, and the now tick. Each block: left/width (%) + label."""
    today = (schedule or {}).get("today") or {}
    rows = []
    if jaina:
        rows.append({"who": "jaina", "blocks": [{"left": 0, "width": 100, "label": jaina}]})
    for who, name in (("matt", "Matt"), ("gal", "Gal")):
        blocks = []
        for ev in today.get(who) or []:
            if ev.get("allday") or not ev.get("start"):
                continue
            try:
                s = dt.datetime.fromisoformat(ev["start"]).astimezone(CT)
                e = dt.datetime.fromisoformat(ev.get("end") or ev["start"]).astimezone(CT)
            except ValueError:
                continue
            if s.date() < now.date():
                s = dt.datetime.combine(now.date(), dt.time(0), tzinfo=CT)
            if e.date() > now.date():
                e = dt.datetime.combine(now.date(), dt.time(23, 59), tzinfo=CT)
            left, right = _pct(s), _pct(e)
            if right - left <= 0:
                continue
            blocks.append({"left": round(left, 2), "width": round(right - left, 2),
                           "label": f"{name}: {ev['summary']}, {_short_time(s)} to {_short_time(e)}",
                           # phone: the strip is ~360px wide, so name + hours only
                           "short": f"{name} {_short_time(s).split(' ')[0]} to {_short_time(e).split(' ')[0]}"})
        if blocks:
            rows.append({"who": who, "blocks": blocks})
    now_pct = _pct(now) if STRIP_START_H <= now.hour < STRIP_END_H else None
    aria = "; ".join(b["label"] for r in rows for b in r["blocks"]) or "Nothing on the calendars today"
    return {"rows": rows, "now": round(now_pct, 2) if now_pct is not None else None,
            "aria": f"Today from 6 am to 10 pm. Now {_short_time(now)}. {aria}."}


def jaina_label(schedule, now, overrides):
    """Jaina's day from the family-board rule (family_board_push.jaina_for)."""
    try:
        import family_board_push as fbp
    except Exception:
        return None
    allday = [ev.get("summary") or "" for ev in ((schedule or {}).get("today") or {}).get("matt") or []
              if ev.get("allday")]
    no_school = any(fbp.NO_SCHOOL_RE.search(s) for s in allday)
    drop, pick = fbp.jaina_for(fbp.DAY_NAMES[now.weekday()], no_school, overrides or {})
    if (not drop and not pick) or (drop == "Home" and pick == "Home"):
        return "Jaina home all day"
    parts = [f"{drop} drops off" if drop else "", f"{pick} picks up" if pick else ""]
    return "Jaina at school. " + ", ".join(p for p in parts if p) + "."


def next_up(schedule, now):
    """The next fixed commitment after now: Matt's first timed event, else Gal's, across
    today / tomorrow / the next week. Returns a short sentence or None."""
    sched = schedule or {}
    best = None
    for bucket in ("today", "tomorrow", "later"):
        for who in ("matt", "gal"):
            for ev in (sched.get(bucket) or {}).get(who) or []:
                if ev.get("allday") or not ev.get("start"):
                    continue
                try:
                    s = dt.datetime.fromisoformat(ev["start"]).astimezone(CT)
                except ValueError:
                    continue
                if s <= now:
                    continue
                key = (s, 0 if who == "matt" else 1)
                if best is None or key < best[0]:
                    best = (key, who, ev, s)
    if not best:
        return None
    _, who, ev, s = best
    if s.date() == now.date():
        day = "Today"
    elif s.date() == now.date() + dt.timedelta(days=1):
        day = "Tomorrow"
    else:
        day = f"{s:%A}"
    lead = "" if who == "matt" else "Gal: "
    return f"{day} {_short_time(s)}, {lead}{ev['summary']}"


# ── Waiting on you + tasks ───────────────────────────────────────────────────
def waiting(needs_items, twin_due, today):
    """Open to_aern asks for the home. Fleet checks (fleet line), held orders (Orders
    pocket), seat sessions and Todoist rows (tasks) live elsewhere, never twice; an ask
    whose Todoist twin Aern dated for later waits until that day."""
    out = []
    for it in needs_items or []:
        if it.get("source_kind") != "queue":
            continue
        due = (twin_due or {}).get(it.get("todoist_id") or "")
        if due and due > today.isoformat():
            continue
        out.append(it)
    out.sort(key=lambda i: int(i.get("priority") or 3))
    return out


def _when(due, today):
    try:
        d = dt.date.fromisoformat(due[:10])
    except (TypeError, ValueError):
        return ""
    delta = (d - today).days
    if delta < -1:
        return f"{-delta} days late"
    if delta == -1:
        return "yesterday"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 7:
        return f"{d:%a}"
    return f"{d:%b} {d.day}"


def tasks(today_list, upcoming, today, cap=8):
    rows = [{"id": t.get("id"), "text": t.get("content", ""), "when": _when(t.get("due"), today),
             "late": (t.get("overdue_days") or 0) > 0, "can_close": True} for t in today_list or []]
    rows += [{"id": t.get("id"), "text": t.get("content", ""), "when": _when(t.get("due"), today),
              "late": False, "can_close": True} for t in upcoming or []]
    return rows[:cap]


# ── Binder ───────────────────────────────────────────────────────────────────
def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def pockets(d):
    """Nine pockets, three rows (sell / buy / home). d holds the raw inputs; any missing
    input leaves that pocket's note static rather than guessing."""
    tb, ta = d.get("tcg_business") or {}, d.get("tcg_alerts") or {}
    wants, movers, restock = d.get("wants"), d.get("movers"), d.get("restock")

    reprice_due = ta.get("reprice_due")
    reprice = (_plural(reprice_due, "card") + " due" if reprice_due else "Nothing due",
               bool(reprice_due))

    held = d.get("held") or 0
    to_ship = tb.get("tsn")
    if held:
        orders = (_plural(held, "held order") + ". Needs you.", True)
    elif to_ship:
        orders = (f"{to_ship} to ship", False)
    elif to_ship == 0:
        orders = ("Nothing waiting", False)
    else:
        orders = ("Open orders", False)

    st = ta.get("sales_today")
    week = tb.get("sr")
    if st is None and not week:
        sales = ("Open sales", False)
    else:
        sales = (f"{st or 0} today, {week or '$0'} this week", False)

    if wants is None:
        want = ("Open the want board", False)
    elif wants.get("hits"):
        want = (_plural(wants["hits"], "hit") + " on targets", True)
    else:
        want = ("No hits", False)

    sealed = d.get("sealed")
    sealed_note = (f"{len(sealed.get('games') or [])} games tracked", False) if sealed else ("Box market", False)

    # Only cards he holds (live 10/10: an unowned promo at +3726% filled the pocket).
    mv = None
    if movers:
        def held(rows):
            return [m for m in rows or [] if "HELD" in (m.get("flags") or [])]
        if held(movers.get("drops")):
            m = held(movers["drops"])[0]
            mv = (f"{m.get('card')} {m.get('pct')}", True)
        elif held(movers.get("gainers")):
            m = held(movers["gainers"])[0]
            mv = (f"{m.get('card')} {m.get('pct')}", False)
    mover_note = mv or ("No moves on your cards", False)

    if restock is None:
        restock_note = ("Household list", False)
    elif restock.get("count"):
        cats = [c["label"] for c in restock.get("categories") or [] if c.get("count")][:2]
        restock_note = (_plural(restock["count"], "item") + (": " + " and ".join(cats).lower() if cats else ""), False)
    else:
        restock_note = ("Nothing to buy", False)

    family = (d.get("gal_today") or "Gal is off today", False)
    notebook = d.get("notebook")
    nb_note = (notebook, False) if notebook else ("Notebook", False)

    rows = [
        ("sell", [("Reprice", "/nexus/tcg#reprice", reprice), ("Orders to ship", "/nexus/tcg", orders),
                  ("Sales", "/nexus/tcg", sales)]),
        ("buy", [("Want board", "/nexus/wants", want), ("Sealed index", "/nexus/signals", sealed_note),
                 ("Movers", "/nexus/signals", mover_note)]),
        ("home", [("Restock", "/nexus/house", restock_note),
                  ("Family calendar", "https://calendar.google.com/calendar/r", family),
                  ("Aernbot notebook", "/nexus/feed/aernbot", nb_note)]),
    ]
    return [{"row": row, "name": name, "href": href, "note": note, "alert": alert}
            for row, items in rows for name, href, (note, alert) in items]


def gal_today(schedule):
    evs = [e for e in (((schedule or {}).get("today") or {}).get("gal") or []) if not e.get("allday")]
    if not evs:
        return None
    e = evs[0]
    try:
        s = dt.datetime.fromisoformat(e["start"]).astimezone(CT)
        en = dt.datetime.fromisoformat(e.get("end") or e["start"]).astimezone(CT)
        return f"Gal {_short_time(s)} to {_short_time(en)}"
    except (KeyError, ValueError):
        return f"Gal: {e.get('summary')}"


# ── TCG tab (glance; Aern 10/10: glance not workbench, Direct = one line, Grail watch cut) ──
TRACKED_SEALED = ("hololive", "onepiece", "gundam")


WANTS_FILE = "/tcg/want_prices_last.json"  # want_prices.py, 07:40 daily on Ashaman


def want_summary(today, path=WANTS_FILE):
    """want_prices_last.json when it is today's and ok, else None (the note stays static)."""
    import json
    with open(path, encoding="utf-8") as f:
        w = json.load(f)
    return w if w.get("ok") and str(w.get("at", ""))[:10] == today.isoformat() else None


def held_orders(orders):
    """[{order_id, value, age_hours}] -> rows for the amber block, oldest first."""
    rows = []
    for o in orders or []:
        h = o.get("age_hours")
        age = "" if h is None else (f"{int(h)}h" if h < 48 else f"{int(h // 24)} days")
        rows.append({"id": o.get("order_id"), "value": o.get("value"), "age": age, "hours": h or 0})
    return sorted(rows, key=lambda r: -r["hours"])


def held_movers(movers, cap=5):
    """Signal movers limited to cards in stock (HELD): drops first, then gainers."""
    if not movers:
        return []
    def held(rows):
        return [m for m in rows or [] if "HELD" in (m.get("flags") or [])]
    out = [{"card": m.get("card"), "set": m.get("set"), "pct": m.get("pct"), "cur": m.get("cur"), "down": True}
           for m in held(movers.get("drops"))]
    out += [{"card": m.get("card"), "set": m.get("set"), "pct": m.get("pct"), "cur": m.get("cur"), "down": False}
            for m in held(movers.get("gainers"))]
    return out[:cap]


def sealed_buys(sealed, cap=5):
    """Boxes in the tracked games that pass the sealed gate's market side: at or above
    125% of MSRP and flat-or-up over 30 and 90 days. Max landed = market / buy_ratio
    (the most you can pay and still clear the gate). Sorted by box/day velocity."""
    if not sealed:
        return []
    ratio = sealed.get("buy_ratio") or 1.25
    out = []
    for g in sealed.get("games") or []:
        if g.get("game") not in TRACKED_SEALED:
            continue
        for b in g.get("rows") or []:
            pct, s30, s90, mkt = b.get("pct_msrp"), b.get("slope30"), b.get("slope90"), b.get("market")
            if None in (pct, s30, s90, mkt) or pct < 125 or s30 < 0 or s90 < 0:
                continue
            out.append({"name": b.get("name"), "url": b.get("url"), "game": g.get("category") or g.get("game"),
                        "market": mkt, "max_landed": mkt / ratio, "velocity": b.get("velocity") or 0})
    out.sort(key=lambda r: -r["velocity"])
    return out[:cap]


def direct_line(d):
    """TCGplayer Direct progress as one sentence (Aern 10/10: no longer weekly-relevant)."""
    if not d:
        return None
    try:
        return (f"TCGplayer Direct: {d['elig_skus']:,} of {d['sku_target']:,} eligible SKUs, "
                f"{d['mo_orders']} of {d['sales_target']} sales a month, ${d['wk_avg']:.0f} of ${d['rev_target']:.0f} a week.")
    except (KeyError, TypeError, ValueError):
        return None


# ── Fleet ────────────────────────────────────────────────────────────────────
def fleet_line(status):
    """status = nexus_panels.status_line(checks) -> (level, text). Only when not ok."""
    level, text = status or ("ok", "")
    return None if level == "ok" else text
