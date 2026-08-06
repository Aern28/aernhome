"""
vault.py — read-only browser for markdown trees mounted into the container.

Originally a single-vault module (Aern's personal Obsidian repo at /vault);
generalized 2026-08-05 for the vault/fleet-docs split into a blueprint
FACTORY so the identical browse/render pipeline serves two roots:

  vault_bp     GET /nexus/vault[/...]      VAULT_DIR      (default /vault)
  fleetdocs_bp GET /nexus/fleetdocs[/...]  FLEETDOCS_DIR  (default /fleetdocs)

Both are bind-mounted read-only (see docker-compose.yml). This module never
writes anything; it lists what's on disk and renders .md files through the
exact same markdown -> bleach pipeline the Docs section uses
(nexus_md.render_markdown), so the sanitization guarantees are identical.

Wikilinks: Obsidian's [[Note]] / [[Note|label]] / [[Note#Heading]] syntax is
rewritten to real links (or muted plain text if unresolved) BEFORE the body
reaches nexus_md.render_markdown — see _convert_wikilinks(). Resolution is a
case-insensitive filename match anywhere in the SAME tree (the two roots do
not cross-resolve; post-split exactly one link crosses the boundary and it
degrades to muted text, which is the designed behavior for unresolved links).

Security: same Tailscale-only CF-header gate as every other /nexus/* route
(kept as a local copy, not imported from app.py, for the same reason
ledger.py keeps its own — app.py imports this module, so this module
importing app back would be circular). Path traversal is hardened in
_resolve_note_path(): the requested path is realpath'd and required to stay
under the root's realpath before anything is opened, and only .md files are
ever served.

Degrade-never-crash: a missing/unmounted dir renders a friendly empty state
instead of erroring; an unreadable file 404s instead of leaking a traceback.
"""

import os
import re
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, abort, current_app, render_template, request
from markupsafe import escape

import nexus_md

FLEETDOCS_DIR = os.environ.get("FLEETDOCS_DIR", "/fleetdocs")

_TITLE_LINE_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _is_nexus_allowed():
    """Same Tailscale-only gate as every /nexus/* route: allow only when the
    request has no CF-Connecting-IP header, i.e. it arrived straight over
    Tailscale/LAN and not through the public Cloudflare tunnel."""
    return request.headers.get("CF-Connecting-IP") is None


# ── Filesystem helpers ─────────────────────────────────────────────────────
def _tree_root(root_dir):
    """realpath of root_dir, or None if it doesn't exist / isn't a dir."""
    root = os.path.realpath(root_dir)
    return root if os.path.isdir(root) else None


def _iter_md_files(vault_root):
    """Yield (relpath, filename) for every *.md file under vault_root,
    skipping hidden dirs/files (.obsidian, .git, ...). relpath uses forward
    slashes regardless of platform, sorted for deterministic first-match
    wikilink resolution."""
    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, vault_root).replace(os.sep, "/")
            yield rel, name


def recent_notes(n=5):
    """Most recently modified notes in fleet-docs, for the Nexus home widget.
    (Pointed at the personal vault until the 2026-08-06 split retired that
    mount.) Degrades to [] on any failure — a missing tree must never break
    the home page."""
    root = _tree_root(FLEETDOCS_DIR)
    if not root:
        return []
    try:
        import time
        notes = []
        for rel, name in _iter_md_files(root):
            try:
                mtime = os.path.getmtime(os.path.join(root, rel))
            except OSError:
                continue
            notes.append((mtime, rel, name))
        notes.sort(reverse=True)
        out = []
        now = time.time()
        for mtime, rel, name in notes[:n]:
            age_h = (now - mtime) / 3600
            if age_h < 1:
                age = f"{int(age_h * 60)}m"
            elif age_h < 48:
                age = f"{int(age_h)}h"
            else:
                age = f"{int(age_h / 24)}d"
            out.append({"relpath": rel, "name": name[:-3], "age": age})
        return out
    except Exception:
        return []


def _build_wikilink_index(vault_root):
    """{stem_lower: relpath} for every note, first match wins (deterministic
    thanks to the sorted walk in _iter_md_files)."""
    index = {}
    for rel, name in _iter_md_files(vault_root):
        stem = name[:-3].lower()  # strip '.md'
        index.setdefault(stem, rel)
    return index


def _build_tree(vault_root):
    """Nested list of {'type': 'dir', 'name', 'children'} /
    {'type': 'file', 'name', 'relpath', 'mtime'} nodes, dirs-first
    alphabetical (case-insensitive) at every level. Directories with no
    markdown anywhere beneath them are never inserted, so the tree is
    automatically pruned of empty folders (e.g. attachment-only dirs)."""
    root: dict = {}
    for rel, name in _iter_md_files(vault_root):
        parts = rel.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append((name, rel))

    def convert(d):
        dirs = []
        for key, val in d.items():
            if key == "__files__":
                continue
            dirs.append({"type": "dir", "name": key, "children": convert(val)})
        files = []
        for name, rel in d.get("__files__", []):
            full = os.path.join(vault_root, rel.replace("/", os.sep))
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                mtime = ""
            files.append({"type": "file", "name": name, "relpath": rel, "mtime": mtime})
        dirs.sort(key=lambda n: n["name"].lower())
        files.sort(key=lambda n: n["name"].lower())
        return dirs + files

    return convert(root)


def _resolve_note_path(root_dir, relpath):
    """Turn a URL relpath into a safe absolute filesystem path, or None if
    it fails any check: no tree mounted, escapes root_dir, isn't a .md
    file, or doesn't exist. Never raises."""
    vault_root = _tree_root(root_dir)
    if vault_root is None:
        return None
    if not relpath or os.path.isabs(relpath) or relpath.startswith(("/", "\\")):
        return None
    if not relpath.lower().endswith(".md"):
        return None

    candidate = os.path.realpath(os.path.join(vault_root, relpath))

    # normcase so this is correct on Windows (case-insensitive, backslash)
    # dev/test environments as well as the Linux container in production.
    root_n = os.path.normcase(vault_root)
    cand_n = os.path.normcase(candidate)
    if cand_n != root_n and not cand_n.startswith(root_n + os.sep):
        return None

    if not os.path.isfile(candidate):
        return None
    return candidate


# ── Front matter + wikilinks ───────────────────────────────────────────────
def _parse_frontmatter(raw):
    """Split a note's raw text into (title_or_None, body_markdown), stripping
    a leading '---' ... '---' YAML-ish block if present. Only a `title:`
    line inside that block is honored (this is a browser, not a YAML
    parser); anything else in front matter is discarded along with the
    delimiters. No leading '---' at all -> no front matter, whole file is
    body."""
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, raw.strip("\n")

    title = None
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
        m = _TITLE_LINE_RE.match(lines[i])
        if m and title is None:
            title = m.group(1).strip().strip("\"'")

    if closing is None:
        # Unterminated '---' block — not real front matter, leave it alone.
        return None, raw.strip("\n")

    body = "\n".join(lines[closing + 1:]).strip("\n")
    return title, body


def _convert_wikilinks(body_md, index, base_url):
    """Rewrite [[Target]] / [[Target|Label]] / [[Target#Heading|Label]] to
    real <a> links (resolved) or muted <span> text (unresolved) BEFORE the
    body reaches markdown rendering. Resolution matches the final path
    segment of the target, case-insensitively, against every note's stem."""

    def repl(m):
        inner = m.group(1)
        if "|" in inner:
            target, label = inner.split("|", 1)
        else:
            target, label = inner, inner
        target, label = target.strip(), label.strip()

        # Strip Obsidian heading/block refs (#Heading, ^blockid) for lookup.
        name = re.split(r"[#^]", target, 1)[0].strip()
        name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if name.lower().endswith(".md"):
            name = name[:-3]

        rel = index.get(name.lower()) if name else None
        if rel:
            return f'<a href="{base_url}/{quote(rel)}">{escape(label)}</a>'
        return f'<span class="text-gray-500 italic">{escape(label)}</span>'

    return _WIKILINK_RE.sub(repl, body_md)


# ── Blueprint factory (Tailscale-only, like every /nexus/* route) ──────────
def make_tree_blueprint(bp_name, base_url, root_dir, label, icon, blurb):
    """Build a read-only markdown-tree browser blueprint. Called twice below
    — once for the personal vault, once for fleet-docs. Route endpoints are
    namespaced by bp_name, so registering both on one app is safe."""
    bp = Blueprint(bp_name, __name__)

    @bp.route(base_url)
    def tree_index():
        if not _is_nexus_allowed():
            abort(404)

        vault_root = _tree_root(root_dir)
        tree = _build_tree(vault_root) if vault_root is not None else None

        return render_template(
            "nexus_vault.html",
            sections=current_app.config.get("NEXUS_SECTIONS", []),
            active=base_url,
            base_url=base_url,
            label=label,
            icon=icon,
            blurb=blurb,
            vault_missing=(vault_root is None),
            tree=tree,
        )

    @bp.route(f"{base_url}/<path:relpath>")
    def tree_note(relpath):
        if not _is_nexus_allowed():
            abort(404)

        abspath = _resolve_note_path(root_dir, relpath)
        if abspath is None:
            abort(404)

        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            abort(404)

        title, body_md = _parse_frontmatter(raw)

        vault_root = _tree_root(root_dir)
        index = _build_wikilink_index(vault_root) if vault_root else {}
        body_md = _convert_wikilinks(body_md, index, base_url)
        html = nexus_md.render_markdown(body_md)

        display_relpath = relpath.replace("\\", "/")
        crumbs = [c for c in display_relpath.split("/") if c]
        fallback_title = os.path.splitext(crumbs[-1])[0] if crumbs else display_relpath

        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(abspath)).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime = None

        return render_template(
            "nexus_vault_note.html",
            sections=current_app.config.get("NEXUS_SECTIONS", []),
            active=base_url,
            base_url=base_url,
            label=label,
            icon=icon,
            title=title or fallback_title,
            html=html,
            relpath=display_relpath,
            crumbs=crumbs,
            mtime=mtime,
        )

    return bp


fleetdocs_bp = make_tree_blueprint(
    "fleetdocs", "/nexus/fleetdocs", FLEETDOCS_DIR, "Fleet Docs", "🗂️",
    "Read-only mirror of the fleet-docs repo — machine-written fleet knowledge.")


# /nexus/vault retired at Phase 2 of the vault/fleet-docs split (2026-08-06):
# the personal Obsidian vault is Sync-only now and no longer mounted here.
# Old bookmarks and stale links redirect to the successor tree.
vault_bp = Blueprint("vault", __name__)


@vault_bp.route("/nexus/vault")
def vault_redirect_index():
    from flask import redirect
    return redirect("/nexus/fleetdocs", code=302)


@vault_bp.route("/nexus/vault/<path:relpath>")
def vault_redirect_note(relpath):
    from flask import redirect
    return redirect(f"/nexus/fleetdocs/{quote(relpath)}", code=302)
