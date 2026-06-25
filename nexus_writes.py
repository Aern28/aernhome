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


def capture_to_goal(capture_id, area="personal"):
    """Promote a capture item into a goal, then mark the capture processed."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT text FROM capture WHERE id = ? AND processed_at IS NULL", (capture_id,)
        ).fetchone()
        if row is None:
            raise ValueError("no such open capture")
    gid = add_goal(row["text"], area)
    process_capture(capture_id)
    return gid


# ── Links (curated "connections to documents") ───────────────────────────────
def add_link(label, url, area=None):
    label, url = (label or "").strip(), (url or "").strip()
    if not label or not url:
        raise ValueError("label and url required")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO links (label, url, area) VALUES (?, ?, ?)", (label, url, area or None))
        return cur.lastrowid


def list_links(area=None):
    try:
        with _conn() as conn:
            q = "SELECT id, label, url, area FROM links"
            params = ()
            if area:
                q += " WHERE area = ?"; params = (area,)
            q += " ORDER BY sort ASC, label COLLATE NOCASE"
            return [dict(r) for r in conn.execute(q, params).fetchall()]
    except sqlite3.Error:
        return []


def delete_link(link_id):
    with _conn() as conn:
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))


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


# ── Books (book_status; seeded by nexus_books_import.py) ──────────────────────
_BOOK_ORDER = "CASE status WHEN 'reading' THEN 0 WHEN 'to-read' THEN 1 ELSE 2 END"


def list_books(status=None):
    """All tracked books (or one status). Each: {id,title,author,status,rating,cover_ref}."""
    try:
        with _conn() as conn:
            q = "SELECT id, title, author, status, rating, cover_ref FROM book_status"
            params = ()
            if status:
                q += " WHERE status = ?"; params = (status,)
            q += f" ORDER BY {_BOOK_ORDER}, title COLLATE NOCASE"
            return [dict(r) for r in conn.execute(q, params).fetchall()]
    except sqlite3.Error:
        return []


def reading_books():
    """Currently-reading rows for the home widget: [{id,title,author,cover}]."""
    return [
        {"id": b["id"], "title": b["title"], "author": b["author"], "cover": b["cover_ref"]}
        for b in list_books(status="reading")
    ]


def set_book_status(book_id, status):
    if status not in ("to-read", "reading", "read"):
        raise ValueError("bad status")
    with _conn() as conn:
        # stamp finished when moving to read; clear it otherwise
        if status == "read":
            conn.execute(
                "UPDATE book_status SET status = ?, finished = COALESCE(finished, date('now')), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, book_id))
        else:
            conn.execute(
                "UPDATE book_status SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, book_id))


def delete_book(book_id):
    with _conn() as conn:
        conn.execute("DELETE FROM book_status WHERE id = ?", (book_id,))


def book_cover_path(book_id):
    """Return the stored cover_ref for a book (used by the gated cover route)."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT cover_ref FROM book_status WHERE id = ?", (book_id,)).fetchone()
            return row["cover_ref"] if row else None
    except sqlite3.Error:
        return None


# ── Notes (pinned scratchpad — short persistent notes, NOT an Obsidian clone) ─
# The sticky-note layer between transient capture (inbox → process → clear) and a
# full Obsidian doc. Keep-around, editable, pinnable; link out to Obsidian when one
# grows up. Canonical in nexus.db.
def add_note(body, title=None, area=None):
    body = (body or "").strip()
    if not body:
        raise ValueError("empty note")
    if area not in ("personal", "work", "house", "tcg", None, ""):
        area = None
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, body, area) VALUES (?, ?, ?)",
            ((title or "").strip() or None, body, area or None))
        return cur.lastrowid


def list_notes(area=None):
    """All notes, pinned first then most-recently-updated. Each row is a dict."""
    try:
        with _conn() as conn:
            q = ("SELECT id, title, body, area, pinned, created_at, updated_at "
                 "FROM notes")
            params = ()
            if area:
                q += " WHERE area = ?"; params = (area,)
            q += " ORDER BY pinned DESC, updated_at DESC"
            return [dict(r) for r in conn.execute(q, params).fetchall()]
    except sqlite3.Error:
        return []


def pinned_notes(limit=4):
    """Pinned notes for the home widget: [{id,title,body}]."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, title, body FROM notes WHERE pinned = 1 "
                "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def update_note(note_id, body=None, title=None):
    sets, params = [], []
    if body is not None:
        b = body.strip()
        if not b:
            raise ValueError("empty note")
        sets.append("body = ?"); params.append(b)
    if title is not None:
        sets.append("title = ?"); params.append(title.strip() or None)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(note_id)
    with _conn() as conn:
        conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", params)


def set_note_pinned(note_id, pinned):
    with _conn() as conn:
        conn.execute(
            "UPDATE notes SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (1 if pinned else 0, note_id))


def delete_note(note_id):
    with _conn() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


# ── Media (media_status; tv/movie/game — covers via TMDB) ─────────────────────
# Generalized watch/play tracker. Surfaced as the TV shelf now; movies/games drop
# into the same table later. Canonical in nexus.db — the on-ramp to retiring the
# Notion Media Tracker (Phase 4). Posters are remote TMDB CDN URLs (no local file
# serving needed), so the shelf <img> points straight at image.tmdb.org.
_MEDIA_ORDER = "CASE status WHEN 'watching' THEN 0 WHEN 'want' THEN 1 ELSE 2 END"
_MEDIA_STATUSES = ("want", "watching", "watched")


def add_media(title, kind="tv", status="want", tmdb_id=None, poster_url=None,
              overview=None, year=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("empty title")
    if kind not in ("tv", "movie", "game"):
        kind = "tv"
    if status not in _MEDIA_STATUSES:
        status = "want"
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO media_status (kind, title, status, tmdb_id, poster_url,
                                         overview, year)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kind, title, status, tmdb_id or None, poster_url or None,
             (overview or "").strip() or None, (str(year).strip() if year else None)))
        return cur.lastrowid


def list_media(kind="tv", status=None):
    """Tracked media of one kind (or one status). Each row is a dict."""
    try:
        with _conn() as conn:
            q = ("SELECT id, kind, title, status, rating, progress, tmdb_id, "
                 "poster_url, overview, year FROM media_status WHERE kind = ?")
            params = [kind]
            if status:
                q += " AND status = ?"; params.append(status)
            q += f" ORDER BY {_MEDIA_ORDER}, title COLLATE NOCASE"
            return [dict(r) for r in conn.execute(q, params).fetchall()]
    except sqlite3.Error:
        return []


def watching_media(kind="tv"):
    """Currently-watching rows for the home widget: [{id,title,progress,poster_url}]."""
    return [
        {"id": m["id"], "title": m["title"], "progress": m["progress"],
         "poster_url": m["poster_url"]}
        for m in list_media(kind=kind, status="watching")
    ]


def set_media_status(media_id, status):
    if status not in _MEDIA_STATUSES:
        raise ValueError("bad status")
    with _conn() as conn:
        if status == "watched":
            conn.execute(
                "UPDATE media_status SET status = ?, "
                "finished = COALESCE(finished, date('now')), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, media_id))
        elif status == "watching":
            conn.execute(
                "UPDATE media_status SET status = ?, "
                "started = COALESCE(started, date('now')), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, media_id))
        else:
            conn.execute(
                "UPDATE media_status SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?", (status, media_id))


def set_media_progress(media_id, progress):
    """Free-text progress marker, e.g. 'S3E4'."""
    with _conn() as conn:
        conn.execute(
            "UPDATE media_status SET progress = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", ((progress or "").strip() or None, media_id))


def set_media_rating(media_id, rating):
    r = max(0, min(10, int(rating))) if rating not in (None, "") else None
    with _conn() as conn:
        conn.execute(
            "UPDATE media_status SET rating = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (r, media_id))


def delete_media(media_id):
    with _conn() as conn:
        conn.execute("DELETE FROM media_status WHERE id = ?", (media_id,))


# ── Feed (incoming generated briefs: n8n digests + Aernbot Notebook + …) ──────
def add_feed_item(source, title=None, body=None, url=None):
    source = (source or "").strip().lower()
    if not source:
        raise ValueError("source required")
    if not (title or body):
        raise ValueError("title or body required")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO feed_items (source, title, body, url) VALUES (?, ?, ?, ?)",
            (source, (title or "").strip() or None, (body or "").strip() or None,
             (url or "").strip() or None))
        return cur.lastrowid


def list_feed_items(source=None, limit=100):
    """Reverse-chron feed items, optionally filtered to one source."""
    try:
        with _conn() as conn:
            q = "SELECT id, source, title, body, url, created_at FROM feed_items"
            params = []
            if source:
                q += " WHERE source = ?"; params.append(source.lower())
            q += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(q, params).fetchall()]
    except sqlite3.Error:
        return []


def feed_sources(per_source=5):
    """For the per-source widget grid: {source: [latest N items]} in recency order
    of each source's newest item."""
    try:
        with _conn() as conn:
            srcs = [r[0] for r in conn.execute(
                "SELECT source FROM feed_items GROUP BY source "
                "ORDER BY MAX(created_at) DESC").fetchall()]
            out = {}
            for s in srcs:
                rows = conn.execute(
                    "SELECT id, source, title, body, url, created_at FROM feed_items "
                    "WHERE source = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (s, per_source)).fetchall()
                out[s] = [dict(r) for r in rows]
            return out
    except sqlite3.Error:
        return {}


def feed_source_counts():
    """{source: total item count} — so a card can show 'all N ->'."""
    try:
        with _conn() as conn:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT source, COUNT(*) FROM feed_items GROUP BY source").fetchall()}
    except sqlite3.Error:
        return {}


def latest_feed(limit=4):
    """Newest items across all sources, for the home teaser widget."""
    return list_feed_items(limit=limit)
