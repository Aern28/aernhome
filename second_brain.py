"""
second_brain.py — Seat-handoff, bidirectional queue, and "needs Aern" lane for
the Aernbot/TCG/infra homelab fleet.

Three durable JSON documents (atomic temp+os.replace writes, same convention
as fleet.py's save_state_atomic):

  /data/seat.json   — the fleet's in-flight project state, so any Claude
                       session on any machine can read/write "what's going on"
                       from one URL instead of reconstructing the world.
  /data/queue.json  — a flat list of non-urgent findings/questions, tagged
                       dir="to_aern" (agent -> human) or dir="to_fleet"
                       (human -> next-session work), each carrying a `source`
                       receipt (url/path/command).
  /data/agenda.json — the daily + weekly agenda markdown blobs (the files
                       /whats-next, /good-morning, /goodnight and
                       /start-of-week maintain), published here so every
                       machine's skills read/write ONE agenda instead of
                       per-machine ~/.claude copies. Content stays markdown —
                       the skills' checkbox format is the schema.

/api/needs-aern aggregates a priority-ordered "everything waiting on the
human" list from four independently-guarded sources: open to_aern queue
items, down/warn fleet_state.json checks, held TCG orders (counts/values
only — no buyer/PII fields, same rule as fleet.py's tcg-ops endpoint), and
seat projects with blocked_on == "aern".

Every route is gated by the same CF-header-absence check used throughout
app.py / fleet.py (Tailscale/LAN only, never the public Cloudflare tunnel),
degrades instead of raising wherever a file/db read could fail, and never
returns a 500 for a missing/corrupt data file — it just serves an empty/
partial doc so a single bad write can't take the pages down.
"""

import os
import re
import json
import secrets
import sqlite3
import tempfile
import time
import datetime as dt

from flask import Blueprint, jsonify, request, abort, render_template, current_app

sb_bp = Blueprint("second_brain", __name__)

# ── Config ─────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEAT_PATH = os.path.join(DATA_DIR, "seat.json")
QUEUE_PATH = os.path.join(DATA_DIR, "queue.json")
AGENDA_PATH = os.path.join(DATA_DIR, "agenda.json")
FLEET_STATE_PATH = os.path.join(DATA_DIR, "fleet_state.json")
TCG_DB_PATH = os.environ.get("TCG_DB_PATH", "/tcg/inventory.db")

VALID_PROJECT_STATUS = {"active", "blocked", "parked", "done"}
# to_review (info-streams plan, Aern 10/09): bot/task output that needs Aern's
# thought, walked with him in /goodmorning. Deliberately NOT mirrored to Todoist.
VALID_QUEUE_DIR = {"to_aern", "to_fleet", "to_review"}
VALID_QUEUE_STATUS = {"open", "done"}
VALID_AGENDA_WHICH = {"daily", "weekly"}
VALID_PRIORITY = {1, 2, 3}

# Canon (Aern 10/09, seat project nexus-canon): the seat board is canon for fleet
# work. A queue item may carry `project_id`; when that project is marked done or
# leaves the board, its open items close themselves. Project-less items die by
# `key` supersession or by expiry (`ttl_hours`, with a per-dir default below).
# On 10/09 an open item ("spec needed before the weekend build") outlived the
# work it pointed at by two days and three sessions copied it as live state.
PROJECT_ID_MAX = 80
QUEUE_TTL_MAX_H = 24 * 90
# Real to-dos (to_aern, to_fleet) never expire by default; review surfaces do.
QUEUE_DEFAULT_TTL_H = {"to_review": 24 * 7}
# Agenda lines link to a project with an inline tag, e.g. "- [ ] Ship it {p:nexus-canon}".
AGENDA_TAG_RE = re.compile(r"\{p:([A-Za-z0-9._-]{1,80})\}")
AGENDA_ITEM_RE = re.compile(r"^(\s*)[-*]\s+\[( |x|X)\]\s?(.*)$")
AGENDA_DATE_RE = re.compile(r"^#\s.*?(\d{4}-\d{2}-\d{2})")


# ── Gate (same convention as fleet.py / app.py) ───────────────────────────
def _is_nexus_allowed():
    """Tailscale/LAN-only gate: allow only when the request has no
    CF-Connecting-IP header, i.e. it did not arrive through the public
    Cloudflare tunnel. Kept as a local copy (not imported from app.py) to
    avoid a circular import — app imports this module, not the other way."""
    return request.headers.get("CF-Connecting-IP") is None


def _sb_json():
    """Require the nexus gate and return the parsed JSON body (or {}) —
    mirrors app.py's _nexus_json()."""
    if not _is_nexus_allowed():
        abort(404)
    return request.get_json(silent=True) or {}


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Stored timestamps are UTC ISO, which is correct for storage and a trap for
# readers. On 2026-08-03/04 BOTH a seat and Aern misread an agenda `updated_at`
# of "20:17" as 8pm wall-clock; it was 15:17 CDT. The seat then reported alert
# times in UTC while narrating them as local. The browser UI never had this
# problem (it renders relative time), so the trap is specific to anything
# reading the RAW JSON -- i.e. every Claude seat.
#
# Fix: ship a companion human-readable field next to each UTC one. Additive, so
# nothing that already parses `updated_at` breaks.
_CENTRAL = None
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _CENTRAL = _ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - zoneinfo/tzdata unavailable
    _CENTRAL = None


def _local_str(iso_str):
    """'2026-08-03T20:17:51+00:00' -> '2026-08-03 15:17 CDT'. None on bad input."""
    if not iso_str or not isinstance(iso_str, str) or _CENTRAL is None:
        return None
    try:
        when = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.astimezone(_CENTRAL).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return None


def _with_local(entry, *fields):
    """Return a copy of `entry` with `<field>_local` added for each field present."""
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    for f in fields:
        local = _local_str(entry.get(f))
        if local:
            out[f + "_local"] = local
    return out


# ── Generic atomic JSON load/save (mirrors fleet.py's load_state/save_state_atomic) ─
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default()
        return data
    except (OSError, json.JSONDecodeError):
        return default()


def _save_json_atomic(path, data):
    """temp file + os.replace so a crash mid-write never corrupts the file a
    concurrent GET request might be reading."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".sb_tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _default_seat():
    return {"updated_at": None, "updated_by": None, "projects": []}


def _default_queue():
    return {"items": []}


def load_seat():
    doc = _load_json(SEAT_PATH, _default_seat)
    doc.setdefault("updated_at", None)
    doc.setdefault("updated_by", None)
    projects = doc.get("projects")
    doc["projects"] = projects if isinstance(projects, list) else []
    return doc


def save_seat_atomic(doc):
    _save_json_atomic(SEAT_PATH, doc)


def load_queue():
    doc = _load_json(QUEUE_PATH, _default_queue)
    items = doc.get("items")
    doc["items"] = items if isinstance(items, list) else []
    return doc


def save_queue_atomic(doc):
    _save_json_atomic(QUEUE_PATH, doc)


# Serialize load->mutate->save across the shared JSON docs (seat/queue/agenda).
# _save_json_atomic makes each individual write crash-safe, but concurrent POSTs
# (multiple fleet machines; the threaded waitress server) still interleave
# load-modify-save and silently lose an update / a queue receipt. This process-wide
# lock closes that race — mandatory now that waitress serves requests on threads.
import threading
import functools
_STORE_LOCK = threading.RLock()


def _serialized(fn):
    """Hold _STORE_LOCK for the whole handler so its load->mutate->save runs
    atomically against every other JSON-store writer."""
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with _STORE_LOCK:
            return fn(*args, **kwargs)
    return _wrapped


def _short_id():
    return secrets.token_hex(4)  # 8 hex chars


def _slugify(title):
    slug = "".join(c.lower() if c.isalnum() else "-" for c in (title or "")).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "project"


def _clean_links(raw):
    """Best-effort coercion of the links list — drop anything that isn't a dict
    with at least a ref, never raise on malformed input, and strip dangerous URL
    schemes so a ref can't become a javascript:/data: XSS vector when rendered as
    an <a href>. Refs are often file paths (C:/…, /nexus/vault/…), so this
    denylists the unsafe schemes rather than allowlisting http/https."""
    from urllib.parse import urlparse
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("ref"):
                ref = str(item["ref"])
                if urlparse(ref).scheme.lower() in ("javascript", "data", "vbscript"):
                    continue
                out.append({"label": str(item.get("label") or item.get("ref")), "ref": ref})
    return out


def _clean_project(raw, existing=None):
    """Normalize a project dict from request JSON, filling gaps from an
    existing record when this is an upsert (so a partial PATCH-style POST
    doesn't blow away fields the caller didn't mention)."""
    existing = existing or {}
    title = raw.get("title", existing.get("title", "")) or ""
    pid = raw.get("id") or existing.get("id") or _slugify(title) + "-" + _short_id()[:4]
    status = raw.get("status", existing.get("status", "active"))
    if status not in VALID_PROJECT_STATUS:
        status = existing.get("status", "active")
    return {
        "id": str(pid),
        "title": title,
        "status": status,
        "detail": raw.get("detail", existing.get("detail", "")) or "",
        "next_step": raw.get("next_step", existing.get("next_step", "")) or "",
        "blocked_on": raw.get("blocked_on", existing.get("blocked_on")),
        "links": _clean_links(raw.get("links", existing.get("links", []))),
    }


# ── Queue <-> seat canon helpers ───────────────────────────────────────────
def _project_index(seat=None):
    """{project_id: project} for the current board (never raises)."""
    try:
        doc = seat if seat is not None else load_seat()
        return {p.get("id"): p for p in doc["projects"] if isinstance(p, dict) and p.get("id")}
    except Exception:
        return {}


def _close_queue_item(item, note, via=None):
    """Mark one item done in-memory and close its Todoist twin (best-effort).
    The caller saves the queue. Shared by resolve, key supersession, project
    close and expiry so every path closes the twin the same way."""
    item["status"] = "done"
    item["resolved_at"] = _now_iso()
    if note:
        item["resolution_note"] = note
    if via:
        item["resolved_via"] = via
    try:
        import todoist_bridge
        if item.get("todoist_id"):
            todoist_bridge.close_task(item["todoist_id"])
        elif item.get("dir") == "to_aern":
            todoist_bridge.close_by_content(todoist_bridge.QUEUE_PREFIX + item.get("text", ""))
    except Exception:
        pass


def _close_linked_items(notes_by_project):
    """Close every OPEN queue item linked to a project in `notes_by_project`
    ({project_id: note}). Returns the closed item ids. Runs under the store lock
    (re-entrant, so it is safe from inside a seat write)."""
    if not notes_by_project:
        return []
    closed = []
    with _STORE_LOCK:
        doc = load_queue()
        for item in doc["items"]:
            if (isinstance(item, dict) and item.get("status") == "open"
                    and item.get("project_id") in notes_by_project):
                _close_queue_item(item, notes_by_project[item["project_id"]], via="project")
                closed.append(item.get("id"))
        if closed:
            save_queue_atomic(doc)
    return closed


def _sweep_expired():
    """Close open items whose expires_at has passed ("aged out", never deleted).
    Lazy: called from queue reads/writes instead of a scheduled task (Aern 10/09,
    Q12) - every seat and bot hits the queue all day. Cheap: local JSON only."""
    try:
        now = dt.datetime.now(dt.timezone.utc)
        with _STORE_LOCK:
            doc = load_queue()
            changed = False
            for item in doc["items"]:
                if not isinstance(item, dict) or item.get("status") != "open":
                    continue
                exp = item.get("expires_at")
                if not exp:
                    continue
                try:
                    when = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when <= now:
                    _close_queue_item(item, f"aged out (expired {_local_str(exp) or exp})", via="expiry")
                    changed = True
            if changed:
                save_queue_atomic(doc)
    except Exception as e:
        print(f"[second_brain] queue expiry sweep failed: {e}")


def _enrich_items(items, projects=None):
    """Copies of `items` with live project_status/project_title for linked items
    (computed, never stored: the board is the canon)."""
    projects = projects if projects is not None else _project_index()
    out = []
    for i in items:
        if isinstance(i, dict) and i.get("project_id"):
            i = dict(i)
            p = projects.get(i["project_id"])
            i["project_status"] = p.get("status") if p else "unknown"
            if p:
                i["project_title"] = p.get("title")
        out.append(i)
    return out


# ── /api/seat ──────────────────────────────────────────────────────────────
@sb_bp.route("/api/seat")
def api_seat_get():
    if not _is_nexus_allowed():
        abort(404)
    return jsonify(_with_local(load_seat(), "updated_at"))


@sb_bp.route("/api/seat", methods=["POST"])
@_serialized
def api_seat_post():
    body = _sb_json()
    doc = load_seat()
    updated_by = body.get("updated_by") or doc.get("updated_by") or "unknown"

    # Optimistic concurrency (added 2026-08-03), same contract as /api/agenda:
    # pass base_updated_at (the updated_at you READ) and a stale write is
    # rejected with 409 + the current doc instead of silently winning.
    #
    # This matters most for the "projects" full-replace branch below, which
    # will happily drop every project a caller did not know about. Prefer the
    # "project" (singular) upsert whenever you are changing ONE project -- it
    # merges by id and cannot clobber a sibling seat's entries at all.
    base = body.get("base_updated_at")
    if base and doc.get("updated_at") and doc.get("updated_at") != base:
        return jsonify({
            "ok": False,
            "error": "conflict: seat changed since you read it - merge and retry",
            "current_updated_at": doc.get("updated_at"),
            "current_updated_by": doc.get("updated_by"),
            "current_projects": doc.get("projects"),
        }), 409

    # Status before this write, to detect projects that close (-> done) or leave
    # the board; their linked open queue items close after the save (Q3/Q9).
    before = {p.get("id"): p.get("status") for p in doc["projects"] if isinstance(p, dict)}

    if isinstance(body.get("projects"), list):
        # Full replace. DANGEROUS: anything absent from the payload is deleted.
        by_id = {p.get("id"): p for p in doc["projects"] if isinstance(p, dict)}
        new_projects = []
        for raw in body["projects"]:
            if not isinstance(raw, dict):
                continue
            existing = by_id.get(raw.get("id"))
            new_projects.append(_clean_project(raw, existing))
        doc["projects"] = new_projects
    elif isinstance(body.get("project"), dict):
        # Upsert by id.
        raw = body["project"]
        existing = None
        idx = None
        for i, p in enumerate(doc["projects"]):
            if isinstance(p, dict) and raw.get("id") and p.get("id") == raw.get("id"):
                existing, idx = p, i
                break
        cleaned = _clean_project(raw, existing)
        if idx is not None:
            doc["projects"][idx] = cleaned
        else:
            doc["projects"].append(cleaned)
    else:
        return jsonify({"ok": False, "error": "body must include 'project' or 'projects'"}), 400

    doc["updated_at"] = _now_iso()
    doc["updated_by"] = updated_by

    try:
        save_seat_atomic(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500

    after = {p.get("id"): p for p in doc["projects"] if isinstance(p, dict)}
    notes = {}
    for pid, old_status in before.items():
        if pid not in after:
            notes[pid] = f"auto: project {pid} removed from the board ({updated_by})"
        elif old_status != "done" and after[pid].get("status") == "done":
            notes[pid] = f"auto: project {pid} marked done ({updated_by})"
    closed = []
    try:
        closed = _close_linked_items(notes)
    except Exception as e:
        print(f"[second_brain] linked-item close failed: {e}")

    return jsonify({"ok": True, "seat": doc, "closed_queue_items": closed})


@sb_bp.route("/api/seat/prune", methods=["POST"])
@_serialized
def api_seat_prune():
    body = _sb_json()
    doc = load_seat()
    before = len(doc["projects"])
    pruned_ids = [p.get("id") for p in doc["projects"] if isinstance(p, dict) and p.get("status") == "done"]
    doc["projects"] = [p for p in doc["projects"] if isinstance(p, dict) and p.get("status") != "done"]
    removed = before - len(doc["projects"])
    doc["updated_at"] = _now_iso()
    doc["updated_by"] = body.get("updated_by") or doc.get("updated_by") or "unknown"

    try:
        save_seat_atomic(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500

    # Removal is a close (Q9): never leave a linked item pointing at nothing.
    closed = []
    try:
        closed = _close_linked_items({pid: f"auto: project {pid} pruned from the board" for pid in pruned_ids})
    except Exception as e:
        print(f"[second_brain] linked-item close on prune failed: {e}")

    return jsonify({"ok": True, "removed": removed, "seat": doc, "closed_queue_items": closed})


# ── /api/queue ─────────────────────────────────────────────────────────────
@sb_bp.route("/api/queue")
def api_queue_get():
    if not _is_nexus_allowed():
        abort(404)
    # Lazy housekeeping (Q12): expiry every read, Todoist sync-back throttled.
    _sweep_expired()
    _sync_todoist_completions()
    doc = load_queue()
    items = doc["items"]

    dir_filter = request.args.get("dir")
    if dir_filter in VALID_QUEUE_DIR:
        items = [i for i in items if i.get("dir") == dir_filter]

    status_filter = request.args.get("status")
    if status_filter in VALID_QUEUE_STATUS:
        items = [i for i in items if i.get("status") == status_filter]

    # project_status is live from the board. Items on a PARKED project are still
    # returned (callers like /goodmorning hide them, Q3); never stored.
    return jsonify({"items": _enrich_items(items)})


@sb_bp.route("/api/queue", methods=["POST"])
@_serialized
def api_queue_post():
    body = _sb_json()
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required"}), 400

    direction = body.get("dir")
    if direction not in VALID_QUEUE_DIR:
        return jsonify({"ok": False, "error": f"dir must be one of {sorted(VALID_QUEUE_DIR)}"}), 400

    try:
        priority = int(body.get("priority", 2))
    except (TypeError, ValueError):
        priority = 2
    if priority not in VALID_PRIORITY:
        priority = 2

    # Optional supersession key (Aern 9/02: "let the signal queue resolve
    # themselves"). Daily snapshot posts — MOVERS, EGMAN DELTA — are never
    # "done", only replaced by tomorrow's; /nexus/signals is their durable view.
    # A new item carrying a key auto-resolves every OPEN item in the same dir
    # with the same key. No TTL on purpose: a day the producer skips leaves the
    # last snapshot open, which is the right signal (stale ≠ resolved). Items
    # without a key (position-movers, domain requests, seat handoffs) are real
    # to-dos and keep needing a human/seat resolve.
    key = (body.get("key") or "").strip()[:64] or None

    # Canon link (Q1/Q5/Q13): optional. An unknown id is stored and FLAGGED,
    # never rejected - a rejected post is a lost alert.
    warnings = []
    project_id = str(body.get("project_id") or "").strip()[:PROJECT_ID_MAX] or None
    project_unknown = False
    if project_id and project_id not in _project_index():
        project_unknown = True
        warnings.append(f"project_id '{project_id}' is not on the board - stored and flagged")

    # Expiry (Q4): explicit ttl_hours wins; else a per-dir default for
    # project-less items. Linked items close with their project instead.
    ttl_h = None
    if body.get("ttl_hours") not in (None, ""):
        try:
            ttl_h = max(1, min(int(body.get("ttl_hours")), QUEUE_TTL_MAX_H))
        except (TypeError, ValueError):
            warnings.append("ttl_hours ignored (not an integer)")
    elif not project_id:
        ttl_h = QUEUE_DEFAULT_TTL_H.get(direction)
    expires_at = ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=ttl_h)).isoformat()
                  if ttl_h else None)

    item = {
        "id": _short_id(),
        "dir": direction,
        "text": text,
        "key": key,
        # Optional triage lane for the For-Aern page: "read" items collapse into
        # the pinned "Read & done" group; everything else is the priority lane.
        "effort": body.get("effort") if body.get("effort") in ("read", "quick", "hands-on") else None,
        # Receipts matter more than politeness here — an empty source is
        # allowed (never 500s) but every template/page nudges toward filling
        # it in, since "where did this come from" is the whole point.
        "source": (body.get("source") or "").strip(),
        "priority": priority,
        "created_at": _now_iso(),
        "created_by": body.get("created_by") or "unknown",
        "status": "open",
        "resolved_at": None,
        "project_id": project_id,
        "expires_at": expires_at,
    }
    if project_unknown:
        item["project_unknown"] = True

    # For-Aern items ride his normal GTD flow: mirror into Todoist (best-effort;
    # a Todoist outage never blocks the queue write). Stored id lets resolve
    # close the twin precisely.
    if direction == "to_aern":
        try:
            import todoist_bridge
            item["todoist_id"] = todoist_bridge.create_task(
                todoist_bridge.QUEUE_PREFIX + text, priority=priority)
        except Exception:
            item["todoist_id"] = None

    doc = load_queue()
    superseded = []
    if key:
        now = _now_iso()
        for old in doc["items"]:
            if (old.get("status") == "open" and old.get("dir") == direction
                    and old.get("key") == key):
                # to_aern twins ride Todoist; closed like a manual resolve would
                _close_queue_item(old, f"superseded by {item['id']}")
                superseded.append(old["id"])
    doc["items"].append(item)
    try:
        save_queue_atomic(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500
    _sweep_expired()

    return jsonify({"ok": True, "id": item["id"], "item": item, "superseded": superseded,
                    "warnings": warnings})


@sb_bp.route("/api/queue/resolve", methods=["POST"])
@_serialized
def api_queue_resolve():
    """Resolve a queue item, optionally recording WHY it closed.

    The `note` field used to be accepted and silently discarded (fixed 2026-08-24).
    CLAUDE.md has documented the contract as {id, note} for every seat in the fleet,
    so every "resolved with a note" until now threw its reasoning away and still
    returned ok:true — leaving a graveyard of closed items with no explanation.
    Stored as `resolution_note`; the resolved column on /nexus/queue renders it.

    Also now returns the item, matching /reopen and /tag. Returning a bare {ok:true}
    is what made the silent discard invisible to callers checking the response.
    """
    body = _sb_json()
    item_id = body.get("id")
    if not item_id:
        return jsonify({"ok": False, "error": "id is required"}), 400
    note = (body.get("note") or "").strip()

    doc = load_queue()
    found = None
    for item in doc["items"]:
        if item.get("id") == item_id:
            # Note only set when supplied: re-resolving after a reopen must not
            # wipe an existing note. The Todoist twin closes best-effort.
            _close_queue_item(item, note)
            found = item
            break

    if not found:
        return jsonify({"ok": False, "error": "no queue item with that id"}), 404

    try:
        save_queue_atomic(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500

    return jsonify({"ok": True, "item": found})


@sb_bp.route("/api/queue/reopen", methods=["POST"])
@_serialized
def api_queue_reopen():
    """Revive a resolved queue item (e.g. a fat-fingered ✓ Read on mobile).
    Flips status back to open; leaves the (now-closed) Todoist twin alone —
    re-resolving later just re-closes it harmlessly."""
    body = _sb_json()
    item_id = body.get("id")
    if not item_id:
        return jsonify({"ok": False, "error": "id is required"}), 400
    doc = load_queue()
    for item in doc["items"]:
        if item.get("id") == item_id:
            item["status"] = "open"
            item["resolved_at"] = None
            try:
                save_queue_atomic(doc)
            except OSError as e:
                return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500
            return jsonify({"ok": True, "item": item})
    return jsonify({"ok": False, "error": "no queue item with that id"}), 404


@sb_bp.route("/api/queue/tag", methods=["POST"])
@_serialized
def api_queue_tag():
    """Set (or clear) the triage effort tag on an existing open queue item."""
    body = _sb_json()
    item_id = body.get("id")
    effort = body.get("effort")
    if effort not in ("read", "quick", "hands-on", None):
        return jsonify({"ok": False, "error": "effort must be read|quick|hands-on|null"}), 400
    doc = load_queue()
    for item in doc["items"]:
        if item.get("id") == item_id:
            item["effort"] = effort
            try:
                save_queue_atomic(doc)
            except OSError as e:
                return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500
            return jsonify({"ok": True, "item": item})
    return jsonify({"ok": False, "error": "no queue item with that id"}), 404


# ── /api/agenda ────────────────────────────────────────────────────────────
def _default_agenda():
    return {"daily": None, "weekly": None}


def load_agenda():
    doc = _load_json(AGENDA_PATH, _default_agenda)
    for which in VALID_AGENDA_WHICH:
        entry = doc.get(which)
        doc[which] = entry if isinstance(entry, dict) and isinstance(entry.get("content"), str) else None
    return doc


def save_agenda_atomic(doc):
    _save_json_atomic(AGENDA_PATH, doc)


@sb_bp.route("/api/agenda")
def api_agenda_get():
    if not _is_nexus_allowed():
        abort(404)
    doc = load_agenda()
    which = request.args.get("which")
    if which in VALID_AGENDA_WHICH:
        return jsonify({which: _with_local(doc[which], "updated_at")})
    return jsonify({k: _with_local(v, "updated_at") for k, v in doc.items()})


@sb_bp.route("/api/agenda", methods=["POST"])
@_serialized
def api_agenda_post():
    body = _sb_json()
    which = body.get("which")
    if which not in VALID_AGENDA_WHICH:
        return jsonify({"ok": False, "error": f"which must be one of {sorted(VALID_AGENDA_WHICH)}"}), 400
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"ok": False, "error": "content (non-empty markdown string) is required"}), 400

    doc = load_agenda()
    current = doc.get(which)

    # Optimistic concurrency (added 2026-08-03). This endpoint was pure
    # last-writer-wins on the WHOLE document, so two seats writing in the same
    # window silently clobbered each other. It happened for real on 8/03:
    # phoenix-claude wrote at 15:17 CDT without the afternoon's work, then
    # trainer-claude wrote at 17:25 over the top -- neither seat ever saw a
    # conflict, and the losing content simply vanished.
    #
    # Callers that pass base_updated_at (the updated_at they READ) get a 409
    # plus the current document, so they can merge and retry instead of
    # overwriting blind. Omitting the field preserves the old behaviour, so
    # nothing that already posts here breaks.
    base = body.get("base_updated_at")
    if base and current and current.get("updated_at") != base:
        return jsonify({
            "ok": False,
            "error": "conflict: agenda changed since you read it - merge and retry",
            "current_updated_at": current.get("updated_at"),
            "current_updated_by": current.get("updated_by"),
            "current_content": current.get("content"),
        }), 409

    # Project-tag guard (Q15): five skills rewrite this blob through an LLM, and a
    # dropped {p:<id>} tag silently unlinks a line from the board. A save may not
    # lose a tag that sat on a still-UNCHECKED line unless the caller ticks that
    # line (the tag survives), names it in drop_tags (a deliberate delete), the
    # project is done or off the board, or this is a new day's agenda (the H1 date
    # changed - not carrying yesterday's line forward is a decision, not a slip).
    projects = _project_index()
    warnings = []
    if current and isinstance(current.get("content"), str):
        old_date, new_date = _agenda_date(current["content"]), _agenda_date(content)
        if not (old_date and new_date and old_date != new_date):
            drop = {str(t) for t in (body.get("drop_tags") or []) if t}
            kept = _agenda_tags(content)[1]
            missing = sorted(
                t for t in _agenda_tags(current["content"])[0] - kept - drop
                if t in projects and projects[t].get("status") != "done")
            if missing:
                return jsonify({
                    "ok": False,
                    "error": ("agenda save would drop project tag(s) from unchecked line(s): "
                              + ", ".join("{p:%s}" % t for t in missing)
                              + ". Keep the tag on the line, tick the line, or pass drop_tags to delete it on purpose."),
                    "missing_tags": missing,
                }), 422
    unknown = sorted(t for t in _agenda_tags(content)[1] if t not in projects)
    if unknown:
        warnings.append("tag(s) not on the board: " + ", ".join(unknown))

    doc[which] = {
        "content": content,
        "updated_at": _now_iso(),
        "updated_by": body.get("updated_by") or "unknown",
    }

    try:
        save_agenda_atomic(doc)
    except OSError as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"[:200]}), 500

    return jsonify({"ok": True, which: {k: v for k, v in doc[which].items() if k != "content"},
                    "warnings": warnings})


def _agenda_date(content):
    for line in (content or "").splitlines():
        m = AGENDA_DATE_RE.match(line)
        if m:
            return m.group(1)
    return None


def _agenda_tags(content):
    """(tags on unchecked lines, every tag anywhere) for an agenda blob."""
    unchecked, every = set(), set()
    for line in (content or "").splitlines():
        tags = set(AGENDA_TAG_RE.findall(line))
        every |= tags
        m = AGENDA_ITEM_RE.match(line)
        if m and m.group(2) == " ":
            unchecked |= tags
    return unchecked, every


def _agenda_lines(content, projects):
    """Parse an agenda blob into display lines with LIVE project status (Q10/Q16).
    Display only: the stored markdown is never rewritten here."""
    out = []
    for raw in (content or "").splitlines():
        if not raw.strip():
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if h:
            out.append({"kind": "heading", "level": len(h.group(1)), "text": h.group(2).strip()})
            continue
        m = AGENDA_ITEM_RE.match(raw)
        b = None if m else re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if m:
            indent, text, checked = len(m.group(1)), m.group(3), m.group(2) != " "
        elif b:
            indent, text, checked = len(b.group(1)), b.group(2), None
        else:
            out.append({"kind": "text", "text": raw.strip()})
            continue
        links = []
        for pid in AGENDA_TAG_RE.findall(text):
            p = projects.get(pid)
            links.append({"id": pid, "found": bool(p),
                          "status": p.get("status") if p else "unknown",
                          "title": (p or {}).get("title"),
                          "next_step": (p or {}).get("next_step"),
                          "blocked_on": (p or {}).get("blocked_on")})
        clean = AGENDA_TAG_RE.sub("", text).rstrip()
        # A done project shows its unchecked line as done; /goodmorning ticks it for real.
        auto_done = bool(checked is False and links and all(l["status"] == "done" for l in links))
        out.append({"kind": "item" if checked is not None else "bullet", "indent": indent,
                    "checked": checked, "auto_done": auto_done, "text": clean, "projects": links})
    return out


@sb_bp.route("/api/agenda/view")
def api_agenda_view():
    """Daily + weekly agenda parsed for the /nexus/aern display, with each
    {p:<id>} tag resolved against the board at read time."""
    if not _is_nexus_allowed():
        abort(404)
    doc = load_agenda()
    projects = _project_index()
    out = {}
    for which in ("daily", "weekly"):
        entry = doc.get(which)
        if not entry:
            out[which] = None
            continue
        meta = _with_local({k: v for k, v in entry.items() if k != "content"}, "updated_at")
        meta["lines"] = _agenda_lines(entry.get("content"), projects)
        out[which] = meta
    return jsonify(out)


# ── /api/needs-aern ────────────────────────────────────────────────────────
# Reverse half of the Todoist mirror (the forward half: a to_aern POST creates
# a "Nexus: " twin, and /api/queue/resolve closes it). A twin completed — or
# deleted — IN Todoist auto-resolves its queue item here, so checking off on
# the phone counts everywhere. Throttled to one pass per _TODOIST_SYNC_MIN_S,
# and it only acts on a DEFINITIVELY successful active-task fetch:
# todoist_bridge.active_task_ids() returns None on any failure, because an
# outage reading as "no active tasks" would mass-resolve the whole lane.
_TODOIST_SYNC_MIN_S = 180
_todoist_sync_last = 0.0


def _sync_todoist_completions():
    global _todoist_sync_last
    now = time.time()
    if now - _todoist_sync_last < _TODOIST_SYNC_MIN_S:
        return
    _todoist_sync_last = now
    try:
        doc = load_queue()
        pending = [i for i in doc["items"]
                   if isinstance(i, dict) and i.get("dir") == "to_aern"
                   and i.get("status") == "open" and i.get("todoist_id")]
        if not pending:
            return
        import todoist_bridge
        active = todoist_bridge.active_task_ids()  # None = fetch failed
        if active is None:
            return
        with _STORE_LOCK:
            doc = load_queue()  # reload under the lock; never clobber a write
            changed = False
            for item in doc["items"]:
                if (isinstance(item, dict) and item.get("dir") == "to_aern"
                        and item.get("status") == "open" and item.get("todoist_id")
                        and str(item["todoist_id"]) not in active):
                    item["status"] = "done"
                    item["resolved_at"] = _now_iso()
                    item["resolved_via"] = "todoist"
                    changed = True
            if changed:
                save_queue_atomic(doc)
    except Exception as e:
        print(f"[second_brain] todoist reverse-sync failed: {e}")


def _needs_from_todoist():
    """(e) Today/overdue Todoist tasks — the personal-GTD half of the morning
    view. "Nexus: " queue twins are excluded at the source (nexus_sources
    filters them out of todoist_today) so queue items never double-list.
    Degrades to [] on any failure."""
    out = []
    try:
        import nexus_sources
        for t in nexus_sources.todoist_today() or []:
            if not isinstance(t, dict):
                continue
            overdue = t.get("overdue_days") or 0
            due = t.get("due")
            detail = f"due {due}" + (f" · {overdue}d overdue" if overdue > 0 else "") if due else ""
            out.append({
                "source_kind": "todoist",
                "todoist_id": str(t.get("id")),
                "title": str(t.get("content", ""))[:140],
                "detail": detail,
                # Todoist API priority is inverted (4=urgent … 1=none)
                "priority": 1 if (overdue > 0 or t.get("priority") == 4)
                            else (2 if t.get("priority") == 3 else 3),
            })
    except Exception as e:
        print(f"[second_brain] needs-aern todoist aggregation failed: {e}")
    return out


def _needs_from_queue():
    """(a) Open to_aern queue items."""
    out = []
    try:
        doc = load_queue()
        projects = _project_index()
        for item in doc["items"]:
            if item.get("dir") == "to_aern" and item.get("status") == "open":
                # Parked means stop raising it (Q3): linked items wait, unseen.
                pid = item.get("project_id")
                if pid and (projects.get(pid) or {}).get("status") == "parked":
                    continue
                out.append({
                    "source_kind": "queue",
                    "id": item.get("id"),
                    "effort": item.get("effort"),
                    "title": item.get("text", "")[:140],
                    "detail": f"source: {item.get('source')}" if item.get("source") else "",
                    "priority": item.get("priority") if item.get("priority") in VALID_PRIORITY else 2,
                    "ref": item.get("source") or "/nexus/queue",
                })
    except Exception as e:
        print(f"[second_brain] needs-aern queue aggregation failed: {e}")
    return out


def _needs_from_fleet():
    """(b) fleet_state.json checks with status down|warn. down -> priority 1."""
    out = []
    try:
        with open(FLEET_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        checks = state.get("checks", {}) if isinstance(state, dict) else {}
        for cid, entry in checks.items():
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status not in ("down", "warn"):
                continue
            out.append({
                "source_kind": "fleet",
                "title": entry.get("label", cid),
                "detail": entry.get("detail", ""),
                "priority": 1 if status == "down" else 2,
                "ref": "/nexus/fleet",
            })
    except (OSError, json.JSONDecodeError):
        pass  # fleet_state.json missing/unparseable — nothing to surface, not an error
    except Exception as e:
        print(f"[second_brain] needs-aern fleet aggregation failed: {e}")
    return out


def _needs_from_tcg_held():
    """(c) Held TCG orders — count/ids/values only, NEVER buyer fields
    (buyer_name, shipping_address), same rule fleet.py's tcg-ops enforces."""
    out = []
    if not os.path.exists(TCG_DB_PATH):
        return out
    uri = f"file:{TCG_DB_PATH}?mode=ro&immutable=1&nolock=1"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        rows = con.execute(
            "SELECT order_id, order_total FROM orders WHERE status = 'held' ORDER BY labeled_at ASC"
        ).fetchall()
        if rows:
            ids = [str(r[0]) for r in rows]
            total_value = sum((r[1] or 0) for r in rows)
            out.append({
                "source_kind": "tcg_held",
                "title": f"{len(rows)} held order(s)",
                "detail": f"total ${total_value:.2f} — ids: {', '.join(ids[:10])}" + (" …" if len(ids) > 10 else ""),
                "priority": 1 if len(rows) >= 5 else 2,
                "ref": "/nexus/tcg",
            })
    except sqlite3.Error as e:
        print(f"[second_brain] needs-aern tcg held query failed: {e}")
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return out


def _needs_from_seat():
    """(d) Seat projects with blocked_on == 'aern'."""
    out = []
    try:
        doc = load_seat()
        for p in doc["projects"]:
            if isinstance(p, dict) and p.get("blocked_on") == "aern":
                out.append({
                    "source_kind": "seat",
                    "title": p.get("title", p.get("id", "project")),
                    "detail": p.get("next_step") or p.get("detail") or "",
                    "priority": 1,
                    "ref": "/nexus/seat",
                })
    except Exception as e:
        print(f"[second_brain] needs-aern seat aggregation failed: {e}")
    return out


@sb_bp.route("/api/needs-aern")
def api_needs_aern():
    if not _is_nexus_allowed():
        abort(404)

    _sweep_expired()
    _sync_todoist_completions()

    items = []
    items.extend(_needs_from_queue())
    items.extend(_needs_from_fleet())
    items.extend(_needs_from_tcg_held())
    items.extend(_needs_from_seat())
    items.extend(_needs_from_todoist())

    items.sort(key=lambda i: i.get("priority", 3))

    return jsonify({"generated_at": _now_iso(), "items": items})


# ── Pages ──────────────────────────────────────────────────────────────────
# The orchestrator (app.py) owns NEXUS_SECTIONS (the nav strip's list of
# (href, label, icon, desc) tuples that nexus_base.html iterates over). One
# line the orchestrator must add so these pages get the same nav as the rest
# of Nexus:
#
#     app.config["NEXUS_SECTIONS"] = NEXUS_SECTIONS
#
# Without it these pages still render (fallback to an empty nav strip) —
# they just won't show the other Nexus tabs.
def _sections():
    return current_app.config.get("NEXUS_SECTIONS", [])


@sb_bp.route("/nexus/seat")
def nexus_seat_page():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_seat.html", sections=_sections(), active="/nexus/seat")


@sb_bp.route("/nexus/queue")
def nexus_queue_page():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_queue.html", sections=_sections(), active="/nexus/queue")


@sb_bp.route("/nexus/aern")
def nexus_aern_page():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_aern.html", sections=_sections(), active="/nexus/aern")
