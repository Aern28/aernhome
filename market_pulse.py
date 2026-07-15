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


def chase_index(cur, category):
    """Top-15 chase cards with >=20 days coverage, mean-normalized, daily median."""
    sealed = " AND ".join(f"p.name NOT LIKE '{w}'" for w in SEALED_WORDS)
    top = cur.execute(f"""
        SELECT p.id, MAX(pr.market_price) AS latest, COUNT(DISTINCT pr.date) AS days
        FROM products p JOIN prices pr ON pr.product_id = p.id
        WHERE p.category = ? AND p.product_type NOT LIKE '%Sealed%' AND {sealed}
        GROUP BY p.id HAVING days >= 20 ORDER BY latest DESC LIMIT 15
    """, (category,)).fetchall()
    ids = [t[0] for t in top]
    if len(ids) < 5:
        return {}
    ph = ",".join("?" * len(ids))
    rows = cur.execute(f"""
        SELECT pr.product_id, pr.date, MAX(pr.market_price)
        FROM prices pr WHERE pr.product_id IN ({ph}) AND pr.market_price IS NOT NULL
        GROUP BY pr.product_id, pr.date
    """, ids).fetchall()
    series = defaultdict(dict)
    for pid, d, v in rows:
        series[pid][d] = v
    means = {pid: statistics.mean(s.values()) for pid, s in series.items() if len(s) >= 20}
    # forward-fill each card up to 7 days so a day's missing fetch can't shift
    # the median's composition (the 2026-07-15 false-rollover lesson)
    all_dates = sorted({d for pid in means for d in series[pid]})
    filled = {}
    for pid in means:
        f, last, last_d = {}, None, None
        for d in all_dates:
            if d in series[pid]:
                last, last_d = series[pid][d], date.fromisoformat(d)
            elif last is not None and (date.fromisoformat(d) - last_d).days > 7:
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
