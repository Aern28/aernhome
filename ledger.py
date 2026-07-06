"""
ledger.py — the Life Ledger: a month-in-review keepsake, not a dashboard.

Storage is deliberately dumb: one markdown file per month at
/data/ledger/YYYY-MM.md. A Claude session (or Aern) writes the file — either
by dropping it on disk directly or POSTing to /api/ledger — and this module
just lists, reads, and renders what's there. No database, no editing UI in
v1: this is meant to accumulate quietly over years, not be fiddled with.

Each file may open with a single optional front-matter line:

    title: June, in review

    The month starts with a rained-out Little League game...

If that line is absent, the title defaults to "Month YYYY" derived from the
filename. Everything after the (optional) title line + its blank-line
separator is the markdown body, rendered through the exact same
markdown -> bleach pipeline the Docs section uses (nexus_md.render_markdown)
so the sanitization guarantees are identical.

Blueprint is registered the same way fleet.fleet_bp is in app.py:

    app.config["NEXUS_SECTIONS"] = NEXUS_SECTIONS
    app.register_blueprint(ledger.ledger_bp)

_is_nexus_allowed() is kept as a local copy (not imported from app.py) for
the same reason fleet.py keeps its own: app imports this module, so this
module importing app back would be circular.
"""

import os
import re
import tempfile

from flask import Blueprint, abort, current_app, jsonify, render_template, request

import nexus_md

ledger_bp = Blueprint("ledger", __name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
LEDGER_DIR = os.path.join(DATA_DIR, "ledger")

YM_RE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])$")
_TITLE_LINE_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.IGNORECASE)

MAX_CONTENT_BYTES = 200 * 1024  # 200KB

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _is_nexus_allowed():
    """Same Tailscale-only gate as app.py's _is_nexus_allowed(): allow only
    when the request has no CF-Connecting-IP header, i.e. it arrived straight
    over Tailscale/LAN and not through the public Cloudflare tunnel."""
    return request.headers.get("CF-Connecting-IP") is None


# ── Storage helpers ────────────────────────────────────────────────────────
def _month_path(ym):
    return os.path.join(LEDGER_DIR, f"{ym}.md")


def _default_title(ym):
    """'Month YYYY' derived from the filename, e.g. '2026-06' -> 'June 2026'."""
    year, month = ym.split("-")
    return f"{_MONTH_NAMES[int(month) - 1]} {year}"


def _parse_front_matter(raw):
    """Split a ledger file's raw text into (title_or_None, body_markdown).

    Recognizes a single leading 'title: ...' line, plus the blank line that
    conventionally follows it. Anything else at the top (e.g. a file that
    just starts with a markdown heading) is left untouched and becomes part
    of the body — front matter is optional, never required."""
    lines = raw.split("\n")
    title = None
    i = 0
    if lines and (m := _TITLE_LINE_RE.match(lines[0])):
        title = m.group(1).strip()
        i = 1
        if i < len(lines) and lines[i].strip() == "":
            i += 1
    body = "\n".join(lines[i:]).strip("\n")
    return title, body


def _teaser(body_md, max_words=40):
    """First ~40 words of the body, with the loudest markdown syntax stripped
    so a card preview reads as prose rather than raw markup."""
    text = re.sub(r"^#+\s*", "", body_md, flags=re.MULTILINE)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_>`#]", "", text)
    words = text.split()
    if not words:
        return ""
    teaser = " ".join(words[:max_words])
    if len(words) > max_words:
        teaser += "…"
    return teaser


def _read_month(ym):
    """Return (title_or_None, body_markdown) for an on-disk month, or None
    if the file doesn't exist / can't be read."""
    path = _month_path(ym)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    return _parse_front_matter(raw)


def _write_month_atomic(ym, content):
    """temp file + os.replace so a crash mid-write never corrupts the file a
    concurrent GET might be reading — same pattern fleet.py uses for
    fleet_state.json."""
    os.makedirs(LEDGER_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LEDGER_DIR, prefix=f".{ym}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, _month_path(ym))
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _neighbor(ym, delta):
    """Adjacent YYYY-MM string, `delta` months away (delta is -1 or +1)."""
    year, month = int(ym[:4]), int(ym[5:7])
    month += delta
    if month == 0:
        year -= 1
        month = 12
    elif month == 13:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def list_months():
    """Metadata for every valid month file on disk, reverse-chronological:
    [{ym, title, teaser, words}, ...]. Never raises — a missing/unreadable
    ledger dir just yields an empty list."""
    try:
        names = os.listdir(LEDGER_DIR)
    except OSError:
        return []

    yms = sorted(
        (name[:-3] for name in names if name.endswith(".md") and YM_RE.match(name[:-3])),
        reverse=True,
    )

    out = []
    for ym in yms:
        parsed = _read_month(ym)
        if parsed is None:
            continue
        title, body = parsed
        out.append({
            "ym": ym,
            "title": title or _default_title(ym),
            "teaser": _teaser(body),
            "words": len(body.split()),
        })
    return out


def get_month(ym):
    """Full rendered month dict, or None if no file exists for `ym`."""
    parsed = _read_month(ym)
    if parsed is None:
        return None
    title, body = parsed
    return {
        "ym": ym,
        "title": title or _default_title(ym),
        "body_md": body,
        "html": nexus_md.render_markdown(body),
        "words": len(body.split()),
    }


# ── Pages (Tailscale-only, like every /nexus/* route) ─────────────────────
@ledger_bp.route("/nexus/ledger")
def nexus_ledger_index():
    if not _is_nexus_allowed():
        abort(404)
    return render_template(
        "nexus_ledger.html",
        sections=current_app.config.get("NEXUS_SECTIONS", []),
        active="/nexus/ledger",
        months=list_months(),
    )


@ledger_bp.route("/nexus/ledger/<ym>")
def nexus_ledger_month(ym):
    if not _is_nexus_allowed():
        abort(404)
    if not YM_RE.match(ym):
        abort(404)
    month = get_month(ym)
    if month is None:
        abort(404)

    prev_ym, next_ym = _neighbor(ym, -1), _neighbor(ym, 1)
    return render_template(
        "nexus_ledger_month.html",
        sections=current_app.config.get("NEXUS_SECTIONS", []),
        active="/nexus/ledger",
        month=month,
        prev_ym=prev_ym if os.path.isfile(_month_path(prev_ym)) else None,
        next_ym=next_ym if os.path.isfile(_month_path(next_ym)) else None,
    )


# ── API (Tailscale-only; JSON in, JSON out — mirrors app.py's /api/nexus/*) ─
@ledger_bp.route("/api/ledger", methods=["GET"])
def api_ledger_list():
    if not _is_nexus_allowed():
        abort(404)
    months = list_months()
    return jsonify([{"ym": m["ym"], "title": m["title"], "words": m["words"]} for m in months])


@ledger_bp.route("/api/ledger", methods=["POST"])
def api_ledger_publish():
    """How a Claude session publishes a month: POST {"ym": "2026-06", "content": "..."}.
    `content` is the raw file contents — optional 'title: ...' line included —
    written verbatim, atomically, to /data/ledger/<ym>.md."""
    if not _is_nexus_allowed():
        abort(404)
    body = request.get_json(silent=True) or {}
    ym = (body.get("ym") or "").strip()
    content = body.get("content")

    if not YM_RE.match(ym):
        return jsonify({"ok": False, "error": "ym must match YYYY-MM"}), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify({"ok": False, "error": "content is required"}), 400
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return jsonify({"ok": False, "error": "content exceeds 200KB"}), 400

    _write_month_atomic(ym, content)
    title, month_body = _parse_front_matter(content)
    return jsonify({
        "ok": True,
        "ym": ym,
        "title": title or _default_title(ym),
        "words": len(month_body.split()),
    })
