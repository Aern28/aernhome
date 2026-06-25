"""
nexus_sources.py — read-only connectors for the personal nexus home (Phase 1).

Each function surfaces ONE source for the /nexus dashboard and is safe to call
straight from a Flask route: all degrade to [] / a zeroed dict on any failure and
never raise. Read-only — they never mutate the sources. Secrets are fetched via
env / Bitwarden pattern only (never logged). Built + verified by the
nexus-phase1-connectors workflow (2026-06-24).
"""

import os
import datetime
import subprocess
import requests
import datetime as dt
import json
import sqlite3
from pathlib import Path
import re




def _get_todoist_token():
    """Resolve the Todoist API token without ever returning/printing it to logs.

    Order: TODOIST_TOKEN env var, then the Bitwarden CLI pattern
    (item 'Todoist API Token', notes field, requires BW_SESSION). Returns the
    token string or None. Never raises.
    """
    token = os.environ.get("TODOIST_TOKEN")
    if token:
        return token.strip()

    session = os.environ.get("BW_SESSION")
    if not session:
        return None
    try:
        out = subprocess.run(
            ["C:\\tools\\bw.exe", "get", "notes", "Todoist API Token",
             "--session", session],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            tok = (out.stdout or "").strip()
            return tok or None
    except Exception:
        return None
    return None


def todoist_today():
    """Return today + overdue Todoist tasks for the aernhome dashboard.

    Returns a list of dicts: {content, due (YYYY-MM-DD str or None),
    overdue_days (int), id (str), priority (int)}, sorted most-overdue then
    highest-priority first.

    The Todoist v1 API ignores server-side filter params, so this fetches all
    active tasks and filters client-side to due_date <= today. Undated tasks and
    future tasks are skipped. Degrades to [] on any failure (missing token,
    network error, unexpected payload) — never raises to the caller.
    """
    token = _get_todoist_token()
    if not token:
        return []

    try:
        resp = requests.get(
            "https://api.todoist.com/api/v1/tasks",
            headers={"Authorization": "Bearer %s" % token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    # v1 may return a bare list or a paginated {"results": [...]} envelope.
    if isinstance(data, dict):
        tasks = data.get("results") or data.get("items") or []
    elif isinstance(data, list):
        tasks = data
    else:
        return []

    today = datetime.date.today()
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        due = t.get("due")
        if not isinstance(due, dict):
            continue  # undated task — skip
        # due.date is "YYYY-MM-DD" or a datetime like "YYYY-MM-DDTHH:MM:SS"
        raw = due.get("date")
        if not raw:
            continue
        date_part = raw[:10]
        try:
            due_date = datetime.date.fromisoformat(date_part)
        except ValueError:
            continue
        if due_date > today:
            continue  # future task — skip (today + overdue only)

        try:
            priority = int(t.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1

        out.append({
            "content": t.get("content", ""),
            "due": date_part,
            "overdue_days": (today - due_date).days,
            "id": str(t.get("id", "")),
            "priority": priority,
        })

    out.sort(key=lambda r: (-r["overdue_days"], -r["priority"]))
    return out



def tcg_alerts() -> dict:
    """TCG business at-a-glance for the AernHome dashboard.

    Reads inventory.db (schema/aggregates only, never card-row dumps) and the
    dream-cards watchlist.json. Returns a safe dict and degrades gracefully:
    any missing/unreachable source leaves that piece zeroed/empty and never
    raises to the caller, so this is safe to call directly from a Flask route.

    Returns:
        {
          "inventory_value": float,   # sum(on-hand qty * latest market price)
          "sales_today":     int,     # distinct orders that hit a fulfillment stage today
          "reprice_due":     int,     # One Piece cards in inventory that moved >5% in 24h
          "grail_hits":      list,    # [{name, target, last_price}, ...] watchlist targets hit
          "events":          list,    # short human note strings (price staleness, grail count)
        }
    """
    out = {
        "inventory_value": 0.0,
        "sales_today": 0,
        "reprice_due": 0,
        "grail_hits": [],
        "events": [],
    }

    # --- inventory.db: aggregates only (no card-row dumps) -----------------
    inv_path = Path(os.environ.get("TCG_DB_PATH", r"C:/tcg-inventory/inventory.db"))
    if inv_path.exists():
        # immutable=1 + nolock=1: read even on a read-only / no-WAL mount
        # (Docker grpcfuse / SMB). Worst case = a snapshot stale by one txn.
        uri = f"file:{inv_path}?mode=ro&immutable=1&nolock=1"
        con = None
        try:
            con = sqlite3.connect(uri, uri=True)

            # Inventory value: latest market price per product * on-hand qty.
            try:
                row = con.execute(
                    """
                    WITH latest AS (
                      SELECT product_id, market_price,
                             ROW_NUMBER() OVER (
                               PARTITION BY product_id ORDER BY date DESC) AS rn
                      FROM prices)
                    SELECT COALESCE(SUM(i.quantity * l.market_price), 0)
                    FROM inventory i
                    JOIN latest l ON l.product_id = i.product_id AND l.rn = 1
                    WHERE i.quantity > 0
                    """
                ).fetchone()
                if row and row[0] is not None:
                    out["inventory_value"] = round(float(row[0]), 2)
            except sqlite3.Error:
                pass

            # Sales today: distinct orders that hit a fulfillment stage today.
            # The orders table is the freshest local sales signal (the email
            # capture tcg-sales.db lives on Ashaman; inventory.db's legacy
            # `sales` table is retired System A and stale).
            try:
                today = dt.date.today().isoformat()
                row = con.execute(
                    "SELECT COUNT(DISTINCT order_id) FROM orders "
                    "WHERE substr(COALESCE(shipped_at, packed_at, labeled_at), 1, 10) = ?",
                    (today,),
                ).fetchone()
                if row and row[0] is not None:
                    out["sales_today"] = int(row[0])
            except sqlite3.Error:
                pass

            # Reprice due: One Piece cards IN inventory whose market moved >5%
            # in the last 24h (mirrors tcg_plugin_data.query_top_movers radar).
            try:
                row = con.execute(
                    """
                    WITH latest AS (
                      SELECT product_id, market_price, date,
                             ROW_NUMBER() OVER (
                               PARTITION BY product_id ORDER BY date DESC) AS rn
                      FROM prices),
                    prev AS (
                      SELECT product_id, market_price, date,
                             ROW_NUMBER() OVER (
                               PARTITION BY product_id ORDER BY date DESC) AS rn
                      FROM prices WHERE date <= datetime('now', '-1 day'))
                    SELECT COUNT(*)
                    FROM latest l
                    JOIN prev p ON p.product_id = l.product_id AND p.rn = 1
                    JOIN products pr ON pr.id = l.product_id
                    JOIN inventory i ON i.product_id = l.product_id AND i.quantity > 0
                    WHERE l.rn = 1 AND p.market_price > 0 AND l.market_price >= 1
                      AND pr.category LIKE 'One Piece%'
                      AND ABS(((l.market_price - p.market_price)
                               / NULLIF(p.market_price, 0)) * 100) > 5
                    """
                ).fetchone()
                if row and row[0] is not None:
                    out["reprice_due"] = int(row[0])
            except sqlite3.Error:
                pass

            # Price freshness -> human event line (prices fetch ~3PM daily).
            try:
                row = con.execute("SELECT MAX(date) FROM prices").fetchone()
                last = row[0] if row else None
                if last:
                    try:
                        when = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=dt.timezone.utc)
                        age_h = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
                        if age_h >= 36:
                            out["events"].append(f"Prices {int(age_h // 24)}d stale")
                    except ValueError:
                        pass
            except sqlite3.Error:
                pass
        except sqlite3.Error:
            pass
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    # --- dream-cards watchlist: grail hits --------------------------------
    # Alert fires only when floor <= last_price <= target. The floor guards
    # against one stale/outlier sold-comp poisoning a thin grail's price stat
    # (e.g. the $1,650 Magikarp false positive sits below its $2,100 floor).
    wl_path = Path(
        os.environ.get("DREAM_CARDS_WATCHLIST", r"C:/projects/dream-cards/watchlist.json")
    )
    if wl_path.exists():
        try:
            data = json.loads(wl_path.read_text(encoding="utf-8"))
            for card in data.get("cards", []):
                try:
                    last = card.get("last_price")
                    target = card.get("target")
                    floor = card.get("floor")
                    if last is None or target is None:
                        continue
                    last = float(last)
                    target = float(target)
                    if last <= target and (floor is None or last >= float(floor)):
                        out["grail_hits"].append(
                            {
                                "name": card.get("name", "?"),
                                "target": target,
                                "last_price": last,
                            }
                        )
                except (TypeError, ValueError):
                    continue
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if out["grail_hits"]:
        out["events"].append(f"{len(out['grail_hits'])} grail target(s) hit")

    return out



def currently_reading(
    books_dir=r"C:\Users\matth\Obivault\Books",
    calibre_db=r"C:\Users\matth\Calibre Library\metadata.db",
):
    """Books currently being read, from Obsidian Books/*.md frontmatter.

    Matches status (case-insensitive) in {reading, currently reading, in progress} —
    the live vault uses "In progress"; "Reading" honored too. Resolves each note's own
    cover (Obsidian [[_covers/x.jpg]] wikilink -> abs path, or http(s) url passthrough);
    falls back to the Calibre cover.jpg when a note has no usable cover and title/author
    match. Returns a list of {title, author, cover}. Never raises; returns [] if the
    Books folder is absent/unreadable. Safe to call from a Flask route.
    """
    _READING = {"reading", "currently reading", "in progress"}

    def _parse_frontmatter(text):
        if not text.startswith("---"):
            return {}
        end = text.find("\n---", 3)
        if end == -1:
            return {}
        data = {}
        for line in text[3:end].splitlines():
            line = line.rstrip()
            if not line or line.lstrip().startswith(("#", "-")) or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            data[key] = val
        return data

    def _resolve_obsidian_cover(raw):
        if not raw:
            return None
        m = re.search(r"\[\[(.+?)\]\]", raw)
        target = (m.group(1) if m else raw).strip().strip('"').strip("'")
        if not target:
            return None
        if target.lower().startswith(("http://", "https://")):
            return target
        cand = os.path.normpath(os.path.join(books_dir, target.replace("/", os.sep)))
        return cand if os.path.isfile(cand) else None

    def _calibre_cover_lookup():
        index = {}
        lib_root = os.path.dirname(calibre_db)
        try:
            conn = sqlite3.connect("file:" + calibre_db + "?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT b.title AS title, b.path AS path, "
                    "b.has_cover AS has_cover, a.name AS author "
                    "FROM books b "
                    "LEFT JOIN books_authors_link bal ON bal.book = b.id "
                    "LEFT JOIN authors a ON a.id = bal.author"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return index
        for r in rows:
            if not r["has_cover"] or not r["path"]:
                continue
            cover_path = os.path.normpath(
                os.path.join(lib_root, r["path"], "cover.jpg")
            )
            if not os.path.isfile(cover_path):
                continue
            t = (r["title"] or "").strip().lower()
            a = (r["author"] or "").strip().lower()
            if t:
                index.setdefault((t, a), cover_path)
                index.setdefault((t, ""), cover_path)
        return index

    if not os.path.isdir(books_dir):
        return []
    try:
        names = [n for n in os.listdir(books_dir) if n.lower().endswith(".md")]
    except OSError:
        return []

    results = []
    pending = []  # (result_index, title, author) needing a Calibre cover fallback
    for name in sorted(names):
        path = os.path.join(books_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        fm = _parse_frontmatter(text)
        if (fm.get("status") or "").strip().lower() not in _READING:
            continue
        title = (fm.get("title") or os.path.splitext(name)[0]).strip()
        author = (fm.get("author") or "").strip() or None
        cover = _resolve_obsidian_cover(fm.get("cover"))
        if cover is None:
            pending.append((len(results), title, author))
        results.append({"title": title, "author": author, "cover": cover})

    if pending and os.path.isfile(calibre_db):
        index = _calibre_cover_lookup()
        if index:
            for idx, title, author in pending:
                t = title.strip().lower()
                a = (author or "").strip().lower()
                cover = index.get((t, a)) or index.get((t, ""))
                if cover:
                    results[idx]["cover"] = cover

    return results



def infra_summary():
    """Summarize service health from the latest stored health_checks row per
    enabled service in dashboard.db. Reuses the existing health machinery
    (services + health_checks tables) rather than doing live network checks,
    so it's fast and side-effect-free. Returns a safe empty summary if the
    DB or tables are missing/unreachable -- never raises to the caller.

    Returns: {"up": int, "total": int, "down": [display_name, ...]}
    """
    # Resolve the same DB path app.py uses (DATA_DIR env, /data in the container).
    data_dir = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
    db_path = os.path.join(data_dir, "dashboard.db")
    safe = {"up": 0, "total": 0, "down": []}

    try:
        if not os.path.exists(db_path):
            return safe
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # One row per enabled service joined to its most-recent health check.
        # LEFT JOIN so services with no checks yet still appear (counted not-up).
        cursor.execute(
            """
            SELECT s.display_name AS display_name, hc.status AS status
            FROM services s
            LEFT JOIN health_checks hc
                ON hc.id = (
                    SELECT id FROM health_checks
                    WHERE service_id = s.id
                    ORDER BY checked_at DESC, id DESC
                    LIMIT 1
                )
            WHERE s.enabled = 1
            ORDER BY s.display_name
            """
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        # Missing tables, locked DB, corrupt file -> degrade gracefully.
        return safe

    up = 0
    down = []
    for row in rows:
        # "up" is the only healthy status app.py records; down/degraded/unknown
        # and not-yet-checked services all count as not-up for the glance view.
        if row["status"] == "up":
            up += 1
        else:
            down.append(row["display_name"])

    return {"up": up, "total": len(rows), "down": down}



def goals_summary(limit=5):
    """Top active personal-nexus goals for the home dashboard.

    Reads the 'goals' table from nexus.db (env-resolved exactly like app.py:
    DATA_DIR/nexus.db). Returns a list of small dicts:
        {id, title, area, progress_pct, doc_link}
    filtered to status='active', ordered by sort ASC then updated_at DESC.

    Degrades to [] (never raises) if the db file, the goals table, or the
    expected columns are missing/unreachable -- safe to call from a Flask route.
    The table is empty for now, so this returns [] until goals are added.
    """
    data_dir = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
    db_path = os.path.join(data_dir, "nexus.db")
    if not os.path.exists(db_path):
        return []
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, area,
                   COALESCE(progress_pct, 0) AS progress_pct,
                   doc_link
            FROM goals
            WHERE status = 'active'
            ORDER BY sort ASC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "area": r["area"],
                "progress_pct": r["progress_pct"],
                "doc_link": r["doc_link"],
            }
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()



def maintenance_due():
    """Upcoming/overdue maintenance from nexus.db.

    Returns rows where completed=0 and due_date is within the next 7 days
    (overdue rows included), ordered by due_date. Each item is
    {id, task, category, due_date, days_until}. Safe for a Flask route:
    never raises, returns [] if the db/table is missing or empty.
    """
    db_path = os.path.join(
        os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "nexus.db"
    )
    if not os.path.exists(db_path):
        return []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task, category, due_date,
                   CAST(julianday(due_date) - julianday('now', 'localtime')
                        AS INTEGER) AS days_until
            FROM maintenance
            WHERE completed = 0
              AND due_date IS NOT NULL
              AND date(due_date) <= date('now', 'localtime', '+7 days')
            ORDER BY date(due_date) ASC, id ASC
            """
        )
        return [
            {
                "id": r["id"],
                "task": r["task"],
                "category": r["category"],
                "due_date": r["due_date"],
                "days_until": r["days_until"],
            }
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

