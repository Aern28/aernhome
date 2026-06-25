"""
nexus_books_import.py — seed/enrich nexus.db book_status from the Obsidian Book
Tracker (C:\\Users\\matth\\Obivault\\Books\\*.md) + Calibre cover art.

book_status is canonical going forward: a normal re-run INSERTS new books and fills
missing covers/metadata but does NOT clobber a status you changed in the app. Pass
--force to resync statuses from Obsidian (e.g. after a bulk edit in the vault).

Usage:
    py nexus_books_import.py            # seed/enrich (preserve app status changes)
    py nexus_books_import.py --force    # also overwrite status from Obsidian
    py nexus_books_import.py --dry-run  # report only, no writes
"""
import os
import re
import sys
import shutil
import sqlite3

BOOKS_DIR = os.environ.get("OBSIDIAN_BOOKS", r"C:\Users\matth\Obivault\Books")
CALIBRE_DB = os.environ.get("CALIBRE_DB", r"C:\Users\matth\Calibre Library\metadata.db")


def _nexus_db():
    return os.path.join(os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "nexus.db")


def _norm_status(raw):
    s = (raw or "").strip().lower()
    if s in ("done", "read", "finished", "complete", "completed"):
        return "read"
    if s in ("reading", "in progress", "currently reading", "started"):
        return "reading"
    return "to-read"


def _norm_title(t):
    # fold smart quotes + whitespace for cross-source matching
    return re.sub(r"\s+", " ", (t or "").replace("’", "'").replace("‘", "'")).strip().lower()


def _parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "-")) or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        data[key.strip()] = val
    return data


def _obsidian_cover(raw):
    if not raw:
        return None
    m = re.search(r"\[\[(.+?)\]\]", raw)
    target = (m.group(1) if m else raw).strip().strip("'\"")
    if not target:
        return None
    if target.lower().startswith(("http://", "https://")):
        return target
    cand = os.path.normpath(os.path.join(BOOKS_DIR, target.replace("/", os.sep)))
    return cand if os.path.isfile(cand) else None


def _calibre_index():
    """{norm_title: cover_path} for Calibre books that have a cover."""
    index = {}
    if not os.path.isfile(CALIBRE_DB):
        return index
    lib_root = os.path.dirname(CALIBRE_DB)
    try:
        conn = sqlite3.connect("file:" + CALIBRE_DB + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT title, path, has_cover FROM books").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return index
    for title, path, has_cover in rows:
        if not has_cover or not path:
            continue
        cover = os.path.normpath(os.path.join(lib_root, path, "cover.jpg"))
        if os.path.isfile(cover):
            index.setdefault(_norm_title(title), cover)
    return index


def run_import(force_status=False, dry_run=False):
    if not os.path.isdir(BOOKS_DIR):
        print(f"[skip] Obsidian Books dir not found: {BOOKS_DIR}")
        return {"inserted": 0, "updated": 0, "total": 0}

    cal = _calibre_index()
    conn = sqlite3.connect(_nexus_db())
    conn.row_factory = sqlite3.Row
    inserted = updated = total = 0

    # Index existing rows by NORMALIZED title (folds curly quotes/whitespace) so the
    # dedup match is consistent. SQL `lower(title)` alone left curly-apostrophe titles
    # (e.g. "Assassin's Quest") unmatched on re-run -> silent duplicate inserts.
    existing_index = {}
    for row in conn.execute(
        "SELECT id, title, cover_ref, author FROM book_status").fetchall():
        existing_index.setdefault(_norm_title(row["title"]), row)

    for name in sorted(os.listdir(BOOKS_DIR)):
        if not name.lower().endswith(".md"):
            continue
        try:
            text = open(os.path.join(BOOKS_DIR, name), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        fm = _parse_frontmatter(text)
        if not fm.get("title") and not name:
            continue
        total += 1
        title = (fm.get("title") or os.path.splitext(name)[0]).strip()
        author = (fm.get("author") or "").strip() or None
        status = _norm_status(fm.get("status"))
        rating = None
        try:
            rating = int(fm["rating"]) if fm.get("rating") else None
        except (ValueError, KeyError):
            rating = None
        finished = (fm.get("finished") or "").strip() or None
        cover = _obsidian_cover(fm.get("cover")) or cal.get(_norm_title(title))

        existing = existing_index.get(_norm_title(title))

        if existing:
            sets, params = [], []
            if not existing["cover_ref"] and cover:
                sets.append("cover_ref = ?"); params.append(cover)
            if not existing["author"] and author:
                sets.append("author = ?"); params.append(author)
            if force_status:
                sets.append("status = ?"); params.append(status)
            if sets:
                sets.append("updated_at = CURRENT_TIMESTAMP")
                if not dry_run:
                    conn.execute(
                        f"UPDATE book_status SET {', '.join(sets)} WHERE id = ?",
                        params + [existing["id"]],
                    )
                updated += 1
        else:
            if not dry_run:
                conn.execute(
                    """INSERT INTO book_status (title, author, status, rating, finished, cover_ref)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (title, author, status, rating, finished, cover),
                )
            # register so a second same-titled note this run can't dupe it
            existing_index[_norm_title(title)] = {
                "id": None, "cover_ref": cover, "author": author}
            inserted += 1

    if not dry_run:
        conn.commit()
    conn.close()
    print(f"[books] {total} obsidian notes -> inserted {inserted}, updated {updated}"
          + (" (dry-run)" if dry_run else ""))
    return {"inserted": inserted, "updated": updated, "total": total}


def materialize_covers(dry_run=False):
    """Copy each book's local cover file into DATA_DIR/book_covers/<id>.<ext> and
    rewrite cover_ref to that relative path, so covers travel with nexus.db to any
    host (Ashaman can't see NenTera's Calibre/Obsidian). http(s) covers and refs
    already under book_covers/ are left alone. Idempotent."""
    data_dir = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
    dest_dir = os.path.join(data_dir, "book_covers")
    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)
    conn = sqlite3.connect(_nexus_db())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, cover_ref FROM book_status WHERE cover_ref IS NOT NULL"
    ).fetchall()
    moved = 0
    for r in rows:
        ref = r["cover_ref"]
        low = ref.lower()
        if low.startswith(("http://", "https://")) or ref.startswith("book_covers/"):
            continue  # remote or already portable
        if not os.path.isfile(ref):
            continue
        ext = os.path.splitext(ref)[1].lower() or ".jpg"
        rel = "book_covers/%d%s" % (r["id"], ext)
        if not dry_run:
            shutil.copyfile(ref, os.path.join(data_dir, rel.replace("/", os.sep)))
            conn.execute("UPDATE book_status SET cover_ref = ? WHERE id = ?", (rel, r["id"]))
        moved += 1
    if not dry_run:
        conn.commit()
    conn.close()
    print("[covers] materialized %d local covers -> %s%s"
          % (moved, dest_dir, " (dry-run)" if dry_run else ""))
    return moved


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_import(force_status="--force" in sys.argv, dry_run=dry)
    materialize_covers(dry_run=dry)
