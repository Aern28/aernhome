"""market_pulse.py — daily chase-index monitor across TCG markets.

Born from the OP Market Lab session (2026-07-14/15). Vocabulary and thresholds
per Obivault "TCG Market Lexicon.md":
  - chase index: per game, top-15 chase cards (each mean-normalized), daily median
  - ignition:  index +5% over 7d after a flat prior month (range <= 6%)
  - rollover:  index <= 90% of its 8-week peak, where that peak was itself a run
  - surge:     +10% over 14d (continuation info, not a fresh ignition)

Runs on Ashaman host (DB mirror + queue API are local). Dry-run by default;
--post sends pulse alerts to the Nexus queue with on-disk dedupe so a
signature only alerts once per episode.

    py market_pulse.py            # print today's pulse
    py market_pulse.py --post     # also POST new alerts to the queue
"""
import argparse
import json
import sqlite3
import statistics
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DB = r"C:\tcg-inventory\inventory.db"
QUEUE_URL = "http://localhost:5555/api/queue"
STATE = Path.home() / ".market_pulse_state.json"

GAMES = [
    "One Piece Card Game",
    "Gundam Card Game",
    "Lorcana TCG",
    "Riftbound League of Legends Trading Card Game",
    "Pokemon",
    "Dragon Ball Super: Fusion World",
    "Union Arena",
]

SEALED_WORDS = ("%Booster%", "%Case%", "%Display%", "%Box%", "%Deck%")

# Card watchlist (Aern 2026-07-15: Green Mihawk vs new Green Shanks renaissance).
# number=None entries are SPOILER WATCHES: alert when a matching product first
# appears in the feed (new reveal hits TCGplayer listings), then start pricing.
WATCH = [
    {"label": "Green Mihawk L AA (OP14-020)", "number": "OP14-020", "name_like": "%Alternate%"},
    {"label": "Mihawk SEC AA (OP14-119)", "number": "OP14-119", "name_like": "%Alternate%"},
    {"label": "Mihawk Manga (OP14-119)", "number": "OP14-119", "name_like": "%Manga%"},
    {"label": "NEW Shanks Leader (spoiler watch)", "number": None,
     "name_like": "%Shanks%", "rarity": "L", "exclude_nums": ("OP09-001", "ST05-001")},
]


def chase_index(cur, category):
    """Top-15 chase cards with >=20 days coverage, mean-normalized, daily median."""
    sealed = " AND ".join(f"p.name NOT LIKE '{w}'" for w in SEALED_WORDS)
    # NOTE data model: the prices table is an EVENT LOG - a row appears only
    # when a card's price CHANGES; a missing day means "unchanged", so
    # forward-fill below is the correct reading, not interpolation.
    # Chase pool: active cards (>=5 repricing events in 21 days), nonzero
    # (zeros are feed noise), top-15 by recent average price.
    top = cur.execute(f"""
        SELECT p.id, AVG(pr.market_price) AS recent, COUNT(DISTINCT pr.date) AS days
        FROM products p JOIN prices pr ON pr.product_id = p.id
        WHERE p.category = ? AND p.product_type NOT LIKE '%Sealed%' AND {sealed}
          AND pr.market_price > 0 AND pr.date >= date('now', '-21 days')
        GROUP BY p.id HAVING days >= 5 ORDER BY recent DESC LIMIT 15
    """, (category,)).fetchall()
    ids = [t[0] for t in top]
    if len(ids) < 5:
        return {}
    ph = ",".join("?" * len(ids))
    rows = cur.execute(f"""
        SELECT pr.product_id, pr.date, MAX(pr.market_price)
        FROM prices pr WHERE pr.product_id IN ({ph}) AND pr.market_price > 0
        GROUP BY pr.product_id, pr.date
    """, ids).fetchall()
    series = defaultdict(dict)
    for pid, d, v in rows:
        series[pid][d] = v
    means = {pid: statistics.mean(s.values()) for pid, s in series.items() if len(s) >= 20}
    # forward-fill: in an event-log table an absent date means "price
    # unchanged", so carry the last value (cap 21 days in case a card
    # falls out of the fetch entirely)
    all_dates = sorted({d for pid in means for d in series[pid]})
    filled = {}
    for pid in means:
        f, last, last_d = {}, None, None
        for d in all_dates:
            if d in series[pid]:
                last, last_d = series[pid][d], date.fromisoformat(d)
            elif last is not None and (date.fromisoformat(d) - last_d).days > 21:
                last = None
            if last is not None:
                f[d] = last
        filled[pid] = f
    idx = {}
    for d in all_dates:
        vals = [filled[pid][d] / means[pid] for pid in means if d in filled[pid]]
        if len(vals) >= 5:
            idx[d] = statistics.median(vals)
    return idx


def nearest(idx, target, tol=4):
    """Index value at the date closest to `target` within tol days, else None."""
    best = None
    for d in idx:
        dd = abs((date.fromisoformat(d) - target).days)
        if dd <= tol and (best is None or dd < best[0]):
            best = (dd, idx[d])
    return best[1] if best else None


def diagnose(idx):
    """Return (signature, detail) per the Lexicon, or (None, weekly summary)."""
    if not idx:
        return None, "insufficient history"
    dates = sorted(idx)
    today = date.fromisoformat(dates[-1])
    now = idx[dates[-1]]
    wk1 = nearest(idx, today - timedelta(days=7))
    prior = [idx[d] for d in dates
             if 7 < (today - date.fromisoformat(d)).days <= 35]
    win8 = {d: idx[d] for d in dates if (today - date.fromisoformat(d)).days <= 56}
    base56 = nearest(idx, today - timedelta(days=56), tol=10)

    detail = f"now {now:.3f}"
    if wk1:
        detail += f", 7d {((now / wk1) - 1) * 100:+.1f}%"

    # rollover: >=10% off an 8-week peak that was itself a run (>=10% above the 56d-ago base)
    if win8 and base56:
        peak_d, peak_v = max(win8.items(), key=lambda kv: kv[1])
        if now <= 0.90 * peak_v and peak_v >= 1.10 * base56:
            return "rollover", (f"{detail} - {((now / peak_v) - 1) * 100:+.1f}% off peak "
                                f"{peak_v:.3f} ({peak_d}); wave leaving")
    # ignition: +5%/7d after a flat prior month
    if wk1 and prior and now / wk1 >= 1.05 and max(prior) / min(prior) <= 1.06:
        return "ignition", f"{detail} after flat month - mechanism unattributed, investigate"
    # surge continuation: +10%/14d
    wk2 = nearest(idx, today - timedelta(days=14))
    if wk2 and now / wk2 >= 1.10:
        return "surge", f"{detail}, 14d {((now / wk2) - 1) * 100:+.1f}% - wave live"
    return None, detail


def watch_report(cur):
    """Per-card watch: latest price + 7d/30d deltas (event-log aware), and
    spoiler watches that fire when a matching product first appears."""
    out = []
    today = date.today()
    for w in WATCH:
        if w["number"] is None:
            ph = " AND ".join(f"number != '{n}'" for n in w.get("exclude_nums", ()))
            rows = cur.execute(f"""
                SELECT number, name, set_name FROM products
                WHERE name LIKE ? AND rarity = ? AND {ph or '1=1'}
            """, (w["name_like"], w.get("rarity", "L"))).fetchall()
            if rows:
                found = "; ".join(f"{r[0]} {r[1][:36]} ({r[2][:20]})" for r in rows[:4])
                out.append((w["label"], "spoiler-hit", f"PRODUCT LISTED: {found}"))
            else:
                out.append((w["label"], None, "not yet listed"))
            continue
        # priced watch: latest value and values ~7d/30d back (last row <= cutoff)
        def px(cutoff):
            r = cur.execute("""
                SELECT pr.market_price FROM products p
                JOIN prices pr ON pr.product_id = p.id
                WHERE p.number = ? AND p.name LIKE ? AND pr.market_price > 0
                  AND pr.date <= ? ORDER BY pr.date DESC LIMIT 1
            """, (w["number"], w["name_like"], cutoff.isoformat())).fetchone()
            return r[0] if r else None
        now = px(today)
        if now is None:
            out.append((w["label"], None, "no price data"))
            continue
        d7, d30 = px(today - timedelta(days=7)), px(today - timedelta(days=30))
        s7 = f"7d {((now / d7) - 1) * 100:+.1f}%" if d7 else "7d n/a"
        s30 = f"30d {((now / d30) - 1) * 100:+.1f}%" if d30 else "30d n/a"
        sig = None
        if d7 and abs(now / d7 - 1) >= 0.10:
            sig = "watch-move"
        out.append((w["label"], sig, f"${now:,.2f} ({s7}, {s30})"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="POST new alerts to the queue")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()

    alerts = []
    print(f"MARKET PULSE {date.today().isoformat()}")
    for g in GAMES:
        sig, detail = diagnose(chase_index(cur, g))
        tag = f"[{sig.upper()}]" if sig else "[steady]"
        print(f"  {tag:<11} {g}: {detail}")
        if sig and state.get(g) != sig:
            alerts.append((g, sig, detail))
        state[g] = sig  # None clears the episode -> same sig can alert again later

    print("  -- watchlist --")
    for label, sig, detail in watch_report(cur):
        tag = f"[{sig.upper()}]" if sig else "[-]"
        print(f"  {tag:<14} {label}: {detail}")
        key = f"watch:{label}"
        if sig and state.get(key) != sig:
            alerts.append((label, sig, detail))
        state[key] = sig
    con.close()

    if args.post and alerts:
        for g, sig, detail in alerts:
            body = json.dumps({
                "dir": "to_fleet", "from": "market-pulse",
                "source": "market_pulse.py daily chase-index scan (lexicon thresholds)",
                "text": f"PULSE ALERT {sig.upper()} - {g}: {detail}. "
                        f"Vocabulary/definitions: Obivault 'TCG Market Lexicon.md'; "
                        f"playbook: 'OP Market Lab - What Drives Prices (2026-07).md'.",
            }).encode()
            req = urllib.request.Request(QUEUE_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                print("  queued:", json.load(r).get("id"), g, sig)

    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
