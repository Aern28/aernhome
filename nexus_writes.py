"""
nexus_writes.py — read/write helpers for the mutable personal-nexus data
(capture, goals, maintenance) in nexus.db.

Separated from nexus_sources.py (read-only surfacing) because these MUTATE state.
All callers are behind the Tailscale-only _is_nexus_allowed() gate, so there is no
public path to these. Functions raise on bad input (the route turns that into a 400);
reads degrade to [] if the DB/table is missing.
"""
import os
import sqlite3
from datetime import date, timedelta


def _db_path():
    return os.path.join(os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "nexus.db")


def _conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── Capture ───────────────────────────────────────────────────────────────────
def add_capture(text, area=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty capture")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO capture (text, area) VALUES (?, ?)", (text, area or None)
        )
        return cur.lastrowid


def list_capture(limit=50, include_processed=False):
    try:
        with _conn() as conn:
            q = "SELECT id, text, area, created_at, processed_at FROM capture"
            if not include_processed:
                q += " WHERE processed_at IS NULL"
            q += " ORDER BY created_at DESC LIMIT ?"
            return [dict(r) for r in conn.execute(q, (limit,)).fetchall()]
    except sqlite3.Error:
        return []


def process_capture(capture_id):
    """Mark a capture item handled (it's been turned into a goal/task/maintenance)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE capture SET processed_at = CURRENT_TIMESTAMP WHERE id = ?", (capture_id,)
        )


# ── Goals ─────────────────────────────────────────────────────────────────────
def add_goal(title, area="personal", detail=None, target=None, due=None, doc_link=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("empty title")
    if area not in ("personal", "work", "house", "tcg"):
        area = "personal"
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO goals (title, area, detail, target, due, doc_link)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, area, detail or None, target or None, due or None, doc_link or None),
        )
        return cur.lastrowid


def list_goals(include_done=True):
    try:
        with _conn() as conn:
            q = ("SELECT id, title, area, detail, target, progress_pct, status, due, "
                 "doc_link, updated_at FROM goals")
            if not include_done:
                q += " WHERE status != 'done'"
            q += (" ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'parked' THEN 1 ELSE 2 END, "
                  "sort ASC, updated_at DESC")
            return [dict(r) for r in conn.execute(q).fetchall()]
    except sqlite3.Error:
        return []


def update_goal_progress(goal_id, progress_pct, note=None):
    pct = max(0, min(100, int(progress_pct)))
    with _conn() as conn:
        conn.execute(
            "UPDATE goals SET progress_pct = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pct, goal_id),
        )
        conn.execute(
            "INSERT INTO goal_updates (goal_id, note, progress_pct) VALUES (?, ?, ?)",
            (goal_id, (note or "").strip() or None, pct),
        )
        # Hitting 100% auto-marks done; stepping back below 100 reactivates.
        if pct >= 100:
            conn.execute("UPDATE goals SET status = 'done' WHERE id = ?", (goal_id,))


def set_goal_status(goal_id, status):
    if status not in ("active", "done", "parked"):
        raise ValueError("bad status")
    with _conn() as conn:
        conn.execute(
            "UPDATE goals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, goal_id),
        )


# ── Maintenance ───────────────────────────────────────────────────────────────
def add_maintenance(task, category=None, due_date=None, interval_days=None, notes=None):
    task = (task or "").strip()
    if not task:
        raise ValueError("empty task")
    iv = int(interval_days) if interval_days else None
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO maintenance (task, category, due_date, interval_days, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (task, category or None, due_date or None, iv, notes or None),
        )
        return cur.lastrowid


def list_maintenance(include_completed=False):
    try:
        with _conn() as conn:
            q = ("SELECT id, task, category, due_date, interval_days, last_done, "
                 "completed, notes FROM maintenance")
            if not include_completed:
                q += " WHERE completed = 0"
            q += " ORDER BY (due_date IS NULL), date(due_date) ASC, id ASC"
            return [dict(r) for r in conn.execute(q).fetchall()]
    except sqlite3.Error:
        return []


def complete_maintenance(maint_id):
    """Mark a maintenance task done. If it's recurring (interval_days set), roll the
    due_date forward by the interval and keep it active; otherwise mark completed."""
    today = date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT interval_days FROM maintenance WHERE id = ?", (maint_id,)
        ).fetchone()
        if row is None:
            raise ValueError("no such maintenance item")
        interval = row["interval_days"]
        if interval:
            next_due = (date.today() + timedelta(days=int(interval))).isoformat()
            conn.execute(
                "UPDATE maintenance SET last_done = ?, due_date = ?, completed = 0 WHERE id = ?",
                (today, next_due, maint_id),
            )
        else:
            conn.execute(
                "UPDATE maintenance SET last_done = ?, completed = 1 WHERE id = ?",
                (today, maint_id),
            )
