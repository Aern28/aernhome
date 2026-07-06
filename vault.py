"""
vault.py — read-only browser for the Obsidian vault mounted at VAULT_DIR.

The vault (Aern's personal Obsidian repo) is bind-mounted read-only into the
container (default /vault, overridable via the VAULT_DIR env var — see
docker-compose.yml). This module never writes anything; it lists what's on
disk and renders .md files through the exact same markdown -> bleach
pipeline the Docs section uses (nexus_md.render_markdown), so the
sanitization guarantees are identical.

Two pages:
  GET /nexus/vault              — folder tree + case-insensitive name filter (client-side JS)
  GET /nexus/vault/<relpath>    — a single rendered note, wikilinks resolved

Wikilinks: Obsidian's [[Note]] / [[Note|label]] / [[Note#Heading]] syntax is
rewritten to real links (or muted plain text if unresolved) BEFORE the body
reaches nexus_md.render_markdown — see _convert_wikilinks(). Resolution is a
case-insensitive filename match anywhere in the vault, matching only the
final path segment of the link target (Obsidian links are usually just a
bare note name, but a few authors write `Folder/Note` — either way we match
by filename, per the same "closest sibling" simplicity as ledger.py).

Security: same Tailscale-only CF-header gate as every other /nexus/* route
(kept as a local copy, not imported from app.py, for the same reason
ledger.py keeps its own — app.py imports this module, so this module
importing app back would be circular). Path traversal is hardened in
_resolve_note_path(): the requested path is realpath'd and required to stay
under VAULT_DIR's realpath before anything is opened, and only .md files are
ever served.

Degrade-never-crash: a missing/unmounted vault dir renders a friendly empty
state instead of erroring; an unreadable file 404s instead of leaking a
traceback.
"""

import os
import re
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, abort, current_app, render_template, request
from markupsafe import escape

import nexus_md

vault_bp = Blueprint("vault", __name__)

VAULT_DIR = os.environ.get("VAULT_DIR", "/vault")

_TITLE_LINE_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _is_nexus_allowed():
    """Same Tailscale-only gate as every /nexus/* route: allow only when the
    request has no CF-Connecting-IP header, i.e. it arrived straight over
    Tailscale/LAN and not through the public Cloudflare tunnel."""
    return request.headers.get("CF-Connecting-IP") is None


# ── Filesystem helpers ─────────────────────────────────────────────────────
def _vault_root():
    """realpath of VAULT_DIR, or None if it doesn't exist / isn't a dir."""
    root = os.path.realpath(VAULT_DIR)
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


def _resolve_note_path(relpath):
    """Turn a URL relpath into a safe absolute filesystem path, or None if
    it fails any check: no vault mounted, escapes VAULT_DIR, isn't a .md
    file, or doesn't exist. Never raises."""
    vault_root = _vault_root()
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


def _convert_wikilinks(body_md, index):
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
            return f'<a href="/nexus/vault/{quote(rel)}">{escape(label)}</a>'
        return f'<span class="text-gray-500 italic">{escape(label)}</span>'

    return _WIKILINK_RE.sub(repl, body_md)


# ── Pages (Tailscale-only, like every /nexus/* route) ──────────────────────
@vault_bp.route("/nexus/vault")
def nexus_vault_index():
    if not _is_nexus_allowed():
        abort(404)

    vault_root = _vault_root()
    tree = _build_tree(vault_root) if vault_root is not None else None

    return render_template(
        "nexus_vault.html",
        sections=current_app.config.get("NEXUS_SECTIONS", []),
        active="/nexus/vault",
        vault_missing=(vault_root is None),
        tree=tree,
    )


@vault_bp.route("/nexus/vault/<path:relpath>")
def nexus_vault_note(relpath):
    if not _is_nexus_allowed():
        abort(404)

    abspath = _resolve_note_path(relpath)
    if abspath is None:
        abort(404)

    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        abort(404)

    title, body_md = _parse_frontmatter(raw)

    vault_root = _vault_root()
    index = _build_wikilink_index(vault_root) if vault_root else {}
    body_md = _convert_wikilinks(body_md, index)
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
        active="/nexus/vault",
        title=title or fallback_title,
        html=html,
        relpath=display_relpath,
        crumbs=crumbs,
        mtime=mtime,
    )
