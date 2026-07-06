"""
backfill_posters.py — one-time-safe backfill to localize TV/game poster art into
DATA_DIR/media_covers/, mirroring nexus_books_import.materialize_covers for books.

media_status rows added before offline localization existed (or where a download
attempt failed at add-time) still hold their original TMDB/IGDB remote poster_url.
This walks all rows, downloads any still-remote poster into the local
media_covers/ store, and rewrites poster_url to the relative ref — the same
double-duty field books use for cover_ref (either a local ref or a remote url).

Idempotent: a row already pointing at an existing local file is left untouched, so
a rerun only retries what's still remote or still missing. Degrades to leaving the
remote url in place on any download failure — never raises, safe to rerun anytime.

Usage:
    py backfill_posters.py            # backfill tv/movie/game rows
    py backfill_posters.py --dry-run  # report only, no writes/downloads

Docker (run against the live container's data volume):
    docker exec <container> python backfill_posters.py
    docker exec <container> python backfill_posters.py --dry-run
"""
import os
import sys
import sqlite3

import nexus_writes as ns_writes


def _nexus_db():
    return os.path.join(os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "nexus.db")


def _is_remote(url):
    return bool(url) and str(url).lower().startswith(("http://", "https://"))


def _new_counts():
    return {"localized": 0, "already_local": 0, "skipped_no_url": 0, "failed": 0}


def run(dry_run=False):
    """Walk every media_status row and localize any remote poster_url still
    pointing at TMDB/IGDB. Returns {kind: counts}. Prints a summary. Never raises
    — a per-row download failure is counted, not propagated."""
    data_dir = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
    summary = {}
    try:
        conn = sqlite3.connect(_nexus_db())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, kind, poster_url FROM media_status").fetchall()
    except sqlite3.Error as e:
        print("[backfill_posters] could not open nexus.db: %s" % e)
        return summary

    for r in rows:
        kind = r["kind"] or "tv"
        url = r["poster_url"]
        counts = summary.setdefault(kind, _new_counts())

        if not url:
            counts["skipped_no_url"] += 1
            continue

        if not _is_remote(url):
            local_path = os.path.normpath(os.path.join(data_dir, url.replace("/", os.sep)))
            if os.path.isfile(local_path):
                counts["already_local"] += 1
            else:
                # Relative ref but the file is gone (media_covers/ wiped, etc.) and
                # the original remote url was overwritten when it was localized —
                # nothing safe to re-fetch from.
                counts["failed"] += 1
            continue

        # Still a remote http(s) url — localize it now.
        if dry_run:
            counts["localized"] += 1
            continue
        new_ref = ns_writes.localize_poster(kind, r["id"], url)
        if new_ref != url:
            conn.execute("UPDATE media_status SET poster_url = ? WHERE id = ?",
                         (new_ref, r["id"]))
            counts["localized"] += 1
        else:
            counts["failed"] += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print("[backfill_posters]%s" % (" (dry-run)" if dry_run else ""))
    if not summary:
        print("  no media_status rows found")
    for kind in sorted(summary):
        c = summary[kind]
        print("  %-6s localized=%d already_local=%d skipped_no_url=%d failed=%d"
              % (kind, c["localized"], c["already_local"], c["skipped_no_url"], c["failed"]))
    total_localized = sum(c["localized"] for c in summary.values())
    total_failed = sum(c["failed"] for c in summary.values())
    print("  total: localized=%d failed=%d" % (total_localized, total_failed))
    return summary


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
