"""TCG Business plugin data fetcher.

Reads inventory.db (and aernbot.db when populated), emits a single JSON blob
matching the merge_variables shape the TRMNL Liquid template expects.

Run on Ashaman where both DBs live. n8n calls this via Execute Command, e.g.:

    py C:/projects/trmnl-plugins/tcg_plugin_data.py

stdout = JSON object with `merge_variables` ready to POST to TRMNL webhook.
stderr = diagnostic counts (visible in n8n execution log, ignored by template).

Override DB locations with env vars:
    TCG_DB_PATH       (default C:/tcg-inventory/inventory.db)
    AERNBOT_DB_PATH   (default C:/tcg-inventory/aernbot.db)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

INVENTORY_DB = Path(os.environ.get("TCG_DB_PATH", r"C:/tcg-inventory/inventory.db"))
AERNBOT_DB = Path(os.environ.get("AERNBOT_DB_PATH", r"C:/tcg-inventory/aernbot.db"))
TZ = ZoneInfo("America/Chicago")


def fmt_money(amount: float | None) -> str:
    if amount is None:
        return "$0"
    if amount >= 10000:
        return f"${amount/1000:.1f}k"
    if amount >= 1000:
        return f"${amount:,.0f}"
    return f"${amount:.2f}"


def fmt_pct_signed(pct: float | None) -> str:
    if pct is None:
        return "—"
    sign = "↑" if pct >= 0 else "↓"
    return f"{sign} {abs(pct):.0f}%"


def truncate(name: str, limit: int = 22) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def friendly_age(when_iso: str | None, now: dt.datetime) -> str:
    if not when_iso:
        return "?"
    try:
        # SQLite ISO strings can be naive; treat as UTC then localize
        when = dt.datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
    except ValueError:
        return when_iso[:16]
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    delta = now - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "now"
    if seconds < 3600:
        return f"{seconds//60}m ago"
    if seconds < 86400:
        return f"{seconds//3600}h ago"
    return f"{seconds//86400}d ago"


def query_sales(con: sqlite3.Connection) -> dict:
    cur = con.execute(
        """
        SELECT COALESCE(SUM(quantity * your_price), 0),
               COUNT(*),
               COUNT(DISTINCT order_id)
        FROM sales
        WHERE date_sold >= datetime('now', '-7 days')
        """
    )
    rev, _rows, orders = cur.fetchone()

    cur = con.execute(
        """
        SELECT COALESCE(SUM(quantity * your_price), 0)
        FROM sales
        WHERE date_sold >= datetime('now', '-14 days')
          AND date_sold <  datetime('now',  '-7 days')
        """
    )
    prev_rev = cur.fetchone()[0]

    delta_pct = None
    if prev_rev and prev_rev > 0:
        delta_pct = ((rev - prev_rev) / prev_rev) * 100

    avg = (rev / orders) if orders else 0
    return {
        "sr": fmt_money(rev),
        "sn": orders,
        "sa": fmt_money(avg),
        "sd": fmt_pct_signed(delta_pct) if delta_pct is not None else None,
    }


def query_inventory_value(con: sqlite3.Connection) -> dict:
    row = con.execute(
        """
        WITH latest_price AS (
          SELECT product_id, market_price,
                 ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT COALESCE(SUM(i.quantity * COALESCE(lp.market_price, i.your_price)), 0),
               COUNT(DISTINCT i.product_id)
        FROM inventory i
        LEFT JOIN latest_price lp ON lp.product_id = i.product_id AND lp.rn = 1
        WHERE i.quantity > 0
        """
    ).fetchone()
    value, skus = row
    return {"iv": fmt_money(value), "is": skus}


def query_top_movers(con: sqlite3.Connection) -> dict:
    cur = con.execute(
        """
        WITH latest AS (
          SELECT product_id, market_price, date,
                 ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY date DESC) AS rn
          FROM prices
        ),
        prev AS (
          SELECT product_id, market_price, date,
                 ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY date DESC) AS rn
          FROM prices
          WHERE date <= datetime('now', '-1 day')
        )
        SELECT pr.name, pr.set_name,
               l.market_price, p.market_price,
               ((l.market_price - p.market_price) / NULLIF(p.market_price, 0)) * 100 AS dpct
        FROM latest l
        JOIN prev p ON p.product_id = l.product_id AND p.rn = 1
        JOIN products pr ON pr.id = l.product_id
        JOIN inventory i ON i.product_id = l.product_id AND i.quantity > 0
        WHERE l.rn = 1 AND p.market_price > 0
          AND ABS(((l.market_price - p.market_price) / NULLIF(p.market_price, 0)) * 100) > 5
        ORDER BY dpct DESC
        """
    )
    rows = cur.fetchall()
    gainers = [
        {"n": truncate(r[0]), "p": fmt_money(r[2]), "d": f"+{r[4]:.0f}%"}
        for r in rows[:3]
        if r[4] is not None and r[4] > 0
    ]
    losers = [r for r in rows if r[4] is not None and r[4] < 0]
    losers.sort(key=lambda r: r[4])
    mvd = None
    if losers:
        r = losers[0]
        mvd = {"n": truncate(r[0]), "p": fmt_money(r[2]), "d": f"{r[4]:.0f}%"}
    return {"mvu": gainers, "mvd": mvd}


def query_positions(con: sqlite3.Connection, today: dt.date) -> list[dict]:
    cur = con.execute(
        """
        SELECT sp.card_name, sp.set_name, sp.card_number, sp.qty_remaining,
               sp.buy_price_per, sp.stop_loss_per, sp.buy_date, sp.max_hold_date,
               sp.thesis, lp.market_price
        FROM singles_positions sp
        LEFT JOIN (
          SELECT product_id, market_price,
                 ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY date DESC) AS rn
          FROM prices
        ) lp ON lp.product_id = sp.product_id AND lp.rn = 1
        WHERE sp.status = 'open'
        ORDER BY sp.buy_date
        """
    )
    out = []
    for (
        card,
        set_name,
        card_num,
        qty,
        buy,
        stop,
        buy_date,
        max_hold,
        thesis,
        current,
    ) in cur.fetchall():
        if current is None or buy is None:
            continue
        plp = ((current - buy) / buy) * 100
        pld_total = (current - buy) * qty
        try:
            opened = dt.date.fromisoformat(buy_date[:10])
        except (TypeError, ValueError):
            opened = today
        try:
            ends = dt.date.fromisoformat(max_hold[:10]) if max_hold else None
        except (TypeError, ValueError):
            ends = None
        day_n = (today - opened).days
        total = (ends - opened).days if ends else None
        # Best-effort catalyst extraction from thesis text — first phrase
        # mentioning a date-like token. Falls back to None.
        catalyst = None
        if thesis:
            import re

            m = re.search(
                r"(?:catalyst|catalys)[^.]*?(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2})",
                thesis,
                re.IGNORECASE,
            )
            if m:
                catalyst = m.group(1)

        ref = f"{set_name} · {card_num}" if card_num else set_name
        out.append(
            {
                "name": card,
                "ref": ref,
                "plp": f"{plp:+.1f}%",
                "plp_neg": plp < 0,
                "pld": ("-$" if pld_total < 0 else "+$") + f"{abs(pld_total):.2f}",
                "day": day_n,
                "total": total or "?",
                "stop": f"${stop:.0f}" if stop else "—",
                "cat": catalyst,
            }
        )
    return out


def query_to_ship(con: sqlite3.Connection) -> dict:
    row = con.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(order_total), 0)
        FROM orders
        WHERE shipped_at IS NULL
          AND status IN ('paid', 'labeled', 'packed', 'pending')
        """
    ).fetchone()
    n, total = row
    return {"tsn": n, "tsv": fmt_money(total)}


def query_prices_freshness(con: sqlite3.Connection, now: dt.datetime) -> dict:
    row = con.execute("SELECT MAX(date) FROM prices").fetchone()
    last = row[0] if row else None
    if not last:
        return {"pf": "—", "pfa": "never"}
    try:
        when = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return {"pf": last[:16], "pfa": "?"}
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    pf = when.astimezone(TZ).strftime("%-I:%M %p").lstrip("0") if os.name != "nt" else when.astimezone(TZ).strftime("%I:%M %p").lstrip("0")
    return {"pf": pf, "pfa": friendly_age(last, now)}


def query_aernbot_watchlist(con: sqlite3.Connection) -> dict:
    try:
        n = con.execute("SELECT COUNT(*) FROM watchlist WHERE active = 1").fetchone()[0]
        latest = con.execute(
            """
            SELECT ref, target_price FROM watchlist
            WHERE active = 1
            ORDER BY added_at DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {"wln": 0, "wll": None}
    if not n or not latest:
        return {"wln": 0, "wll": None}
    ref, target = latest
    return {"wln": n, "wll": f"{ref} @ ${target:.2f}" if target else ref}


def build_payload(
    inventory_db: Path | str | None = None,
    aernbot_db: Path | str | None = None,
) -> dict:
    """Run all queries and return the merge_variables dict.

    Importable by AernHome's /api/tcg-stats route. Path args override the
    module-level defaults (which use TCG_DB_PATH / AERNBOT_DB_PATH env vars).
    """
    inv_path = Path(inventory_db) if inventory_db else INVENTORY_DB
    ab_path = Path(aernbot_db) if aernbot_db else AERNBOT_DB

    now = dt.datetime.now(tz=dt.timezone.utc)
    today_local = now.astimezone(TZ).date()

    # immutable=1 + nolock=1 lets SQLite read DBs on read-only mounts (no WAL
    # access needed). Worst case under concurrent writes: a slightly stale
    # snapshot — fine for a 30-min plugin refresh.
    inv_uri = f"file:{inv_path}?mode=ro&immutable=1&nolock=1"
    ab_uri = f"file:{ab_path}?mode=ro&immutable=1&nolock=1"

    out: dict = {}
    inv = sqlite3.connect(inv_uri, uri=True)
    inv.row_factory = sqlite3.Row
    try:
        out.update(query_sales(inv))
        out.update(query_inventory_value(inv))
        out.update(query_top_movers(inv))
        out["pos"] = query_positions(inv, today_local)
        out.update(query_to_ship(inv))
        out.update(query_prices_freshness(inv, now))
    finally:
        inv.close()

    if ab_path.exists() and ab_path.stat().st_size > 0:
        ab = sqlite3.connect(ab_uri, uri=True)
        try:
            out.update(query_aernbot_watchlist(ab))
        finally:
            ab.close()
    else:
        out.update({"wln": 0, "wll": None})

    if os.name != "nt":
        out["dt"] = now.astimezone(TZ).strftime("%a %-I:%M %p · %b %-d")
    else:
        out["dt"] = now.astimezone(TZ).strftime("%a %I:%M %p · %b %d").replace(" 0", " ")
    return out


def main() -> None:
    payload = build_payload()
    print(json.dumps({"merge_variables": payload}, default=str))


if __name__ == "__main__":
    main()
