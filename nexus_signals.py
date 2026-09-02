# created-by: fable
# created: 2026-09-02
# purpose: /nexus/signals data layer - parse daily TCG mover/egman/riftbound reports + HELD badges
# lifespan: infrastructure
# project: aernhome
"""Load the newest daily TCG signal reports (pushed hourly from Phoenix into the
/tcg mirror by mirror_push.ps1) and shape them for /nexus/signals.

Design: the reports are the source of truth (mover_scan.py, egman_delta.py,
riftbound_meta.py on Phoenix write reports/*.md daily at 7 AM). This module only
READS - parsing the movers markdown table into rows and joining card numbers
against the inventory mirror so every line answers "do we hold any?" inline.
The queue used to carry these as daily digests; per Aern 9/02 the page replaces
that (queue keeps only position-movers + confirmed egman signals).
"""
import glob
import os
import re
import sqlite3

REPORTS_DIR = os.environ.get("TCG_REPORTS_DIR", "/tcg/reports")
INV_DB = os.environ.get("TCG_DB_PATH", "/tcg/inventory.db")

# | +871% | ST14-017 Thousand Sunny (Reprint) | Premium Booster... | $2.33 | $0.24 | PLAY |
_ROW = re.compile(
    r"^\|\s*([+-][\d,]+%)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*\$([\d,.]+)\s*\|\s*\$([\d,.]+)\s*\|\s*(.*?)\s*\|\s*$")
# leading card number: OP17-058 / ST14-017 / EB04-032 / P-048 / 145/221 / 24/204
_NUM = re.compile(r"^([A-Z]{1,4}\d*-\d+\w*|\d+[a-z]?/\d+)\s+")


def _newest(prefix):
    paths = sorted(glob.glob(os.path.join(REPORTS_DIR, prefix + "*.md")))
    return paths[-1] if paths else None


def _held_numbers(con):
    """Set of card numbers with live inventory qty>0, from the hourly mirror."""
    try:
        rows = con.execute(
            "SELECT DISTINCT p.number FROM inventory i JOIN products p ON i.product_id = p.id "
            "WHERE i.quantity > 0 AND p.number IS NOT NULL AND p.number != ''").fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def _product_link(con, number, card, set_name):
    """(tcgplayer_url, image_url) for a mover row, best-effort. The report's card
    text is '<number> <products.name> [*Foil*]', so stripping the number token and
    the *Foil* marker usually recovers the exact products.name; fall back through
    looser matches rather than showing nothing."""
    clean = card
    if number and clean.startswith(number):
        clean = clean[len(number):].strip()
    clean = re.sub(r"\s*\*Foil\*\s*$", "", clean).strip()
    tries = []
    if clean:
        tries.append(("name = ? AND set_name = ?", (clean, set_name)))
        tries.append(("name = ?", (clean,)))
    # MTG-style bare collector numbers ('26 Elven Chorus') aren't caught by _NUM;
    # retry with a leading integer token stripped.
    bare = re.sub(r"^\d+[a-z]?\s+", "", clean)
    if bare != clean:
        tries.append(("name = ? AND set_name = ?", (bare, set_name)))
        tries.append(("name = ?", (bare,)))
    if number:
        tries.append(("number = ? AND set_name = ?", (number, set_name)))
        tries.append(("number = ?", (number,)))
    try:
        for where, params in tries:
            row = con.execute(
                f"SELECT tcgplayer_id, image_url FROM products WHERE {where} LIMIT 1",
                params).fetchone()
            if row and row[0]:
                url = f"https://www.tcgplayer.com/product/{row[0]}"
                return url, (row[1] or None)
    except sqlite3.Error:
        pass
    return None, None


def _parse_movers(path, held, con):
    """Return {'date': .., 'gainers': [...], 'drops': [...]} row dicts."""
    out = {"date": "", "gainers": [], "drops": []}
    if not path:
        return out
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"# Mover Scan\s*[-—]+\s*(.+)", text)
    if m:
        out["date"] = m.group(1).strip()
    bucket = None
    for line in text.splitlines():
        if line.startswith("## "):
            low = line.lower()
            bucket = "gainers" if "gainer" in low else ("drops" if "drop" in low else None)
            continue
        rm = _ROW.match(line)
        if not rm or bucket is None:
            continue
        pct, card, set_name, cur, ago, flags = rm.groups()
        if card.lower() in ("card", "---", ""):
            continue
        nm = _NUM.match(card)
        number = nm.group(1) if nm else None
        row_flags = [f for f in flags.split() if f]
        if number and number in held:
            row_flags.insert(0, "HELD")
        url, img = _product_link(con, number, card, set_name) if con else (None, None)
        out[bucket].append({
            "pct": pct, "pct_val": float(pct.replace("%", "").replace(",", "")),
            "card": card, "set": set_name, "cur": cur, "ago": ago,
            "flags": row_flags, "number": number, "url": url, "img": img,
        })
    return out


def _read_text(path, cap=20000):
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        t = f.read()
    return t if len(t) <= cap else t[:cap] + "\n\n[truncated - full file in tcg-inventory-tool/reports/]"


def load():
    try:
        con = sqlite3.connect(INV_DB)
    except sqlite3.Error:
        con = None
    held = _held_numbers(con) if con else set()
    movers_path = _newest("movers_")
    delta_path = _newest("egman_delta_")
    momentum_path = _newest("meta_momentum_")
    movers = _parse_movers(movers_path, held, con)
    if con:
        con.close()
    return {
        "movers": movers,
        "movers_file": os.path.basename(movers_path) if movers_path else None,
        "delta_text": _read_text(delta_path),
        "delta_file": os.path.basename(delta_path) if delta_path else None,
        "momentum_text": _read_text(momentum_path),
        "momentum_file": os.path.basename(momentum_path) if momentum_path else None,
        "held_count": len(held),
        "reports_present": os.path.isdir(REPORTS_DIR),
    }
