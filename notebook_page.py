# created-by: opus
# created: 2026-10-12
# purpose: parse Aernbot's Notebook.md into the /nexus/feed/aernbot page model (entries, threads, opinion marking)
# lifespan: infrastructure
# project: notebook-page
"""notebook_page.py - the Aernbot Notebook as a readable page (Aern 10/12).

Replaces the old /nexus/feed/aernbot view, which listed the dead push store
(28 one-entry topic folders, raw markdown). This reads the live file the bot
writes in fleet-docs (`Aernbot/Notebook.md`, mounted read-only at /fleetdocs)
plus its thread files, and returns a view model for templates/nexus_notebook.html.
The design started as Aernbot's own mock (to_fleet 21727a7e).

Bot text is untrusted: every body goes through nexus_md.render_markdown (bleach
allowlist). The only markup added after sanitizing is a fixed class attribute.
"""
import os
import re
from datetime import datetime

import nexus_md

FLEETDOCS_DIR = os.environ.get("FLEETDOCS_DIR", "/fleetdocs")
NB_DIR = os.path.join(FLEETDOCS_DIR, "Aernbot")

# Thread colours: muted and distinct, none of Nexus's signal colours (violet/red/green).
THREAD_COLOURS = {
    "evidence-problem": "#c8a46a",
    "night-sky": "#7fa7d9",
    "sea-recoveries": "#5fb3b3",
    "game-balance": "#d98f7f",
    "gulf-garden": "#9cbf6a",
    "institutional-mortality": "#a8a0b8",
}
SPARE_COLOURS = ["#c48fb3", "#8fb3c4", "#b3a46f", "#a3b38f"]
WANDER = ("wander", "Wandering", "#8792a6")   # only an explicit "Thread: wander"
BEFORE = ("before", "Before threads", "#6b7385")   # untagged entries no topic hint places
BINS = {WANDER[0]: WANDER, BEFORE[0]: BEFORE}
# Older entries predate "Thread:" lines; map their freeform topic onto a thread.
TOPIC_HINTS = [
    (r"evidence|archaeolog|paleo|fossil|ice patch|navigation", "evidence-problem"),
    (r"astro|sky|space|cosmo|saturn|meteor", "night-sky"),
    (r"maritime|ship|sea|wreck", "sea-recoveries"),
    (r"tcg meta|balance|ban", "game-balance"),
    (r"garden|plant", "gulf-garden"),
    (r"institutional|studio", "institutional-mortality"),
]
# Opinion = a paragraph the bot opens with one of these, or a "> my read:" blockquote
# (the explicit marker; styled in CSS, no detection needed).
OPINION_LEAD = re.compile(r"<p>(?:My (?:read|take|guess|bet|riff|view)|Opinion\b|\(opinion)", re.I)


def _threads():
    """{slug: {name, question, status, colour}} from Aernbot/threads/*.md."""
    out, spare = {}, iter(SPARE_COLOURS)
    tdir = os.path.join(NB_DIR, "threads")
    try:
        files = sorted(f for f in os.listdir(tdir) if f.endswith(".md"))
    except OSError:
        files = []
    for f in files:
        slug = f[:-3]
        head = {}
        try:
            with open(os.path.join(tdir, f), encoding="utf-8") as fh:
                for line in fh.read().splitlines()[:6]:
                    k, sep, v = line.partition(": ")
                    if sep:
                        head[k.strip().lower()] = v.strip()
        except OSError:
            continue
        colour = THREAD_COLOURS.get(slug) or next(spare, WANDER[2])
        out[slug] = {"slug": slug, "name": slug.replace("-", " ").capitalize(),
                     "question": head.get("question", ""), "status": head.get("status", ""),
                     "next": head.get("next", ""), "colour": colour}
    return out


def _thread_for(topic, body, threads):
    m = re.search(r"^Thread:\s*([\w-]+)", body, re.M)
    if m and m.group(1) in threads:
        return m.group(1)
    if m and m.group(1) == WANDER[0]:
        return WANDER[0]
    for pat, slug in TOPIC_HINTS:
        if slug in threads and re.search(pat, topic, re.I):
            return slug
    return BEFORE[0]


def _render(md):
    html = nexus_md.render_markdown(md)
    return OPINION_LEAD.sub(lambda m: '<p class="nb-mine">' + m.group(0)[3:], html)


def parse(text, threads):
    """Notebook.md text -> newest-first entry dicts."""
    marker = text.find("<!-- ENTRIES BELOW")
    if marker >= 0:
        text = text[marker:].split("\n", 1)[-1]
    out = []
    for chunk in re.split(r"^## (?=\d{4}-\d{2}-\d{2} )", text, flags=re.M)[1:]:
        head, _, body = chunk.partition("\n")
        parts = [p.strip() for p in head.split("·")]
        if len(parts) < 3:
            continue
        try:
            when = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        title = re.sub(r"\s*\[find\]\s*$", "", "·".join(parts[2:]).strip())
        body = re.split(r"^---\s*$", body, flags=re.M)[0].strip()
        slug = _thread_for(parts[1], body, threads)
        body = re.sub(r"^Thread:.*$", "", body, flags=re.M).strip()
        blocks = [b for b in re.split(r"\n\s*\n", body) if b.strip()]
        lead = blocks[0] if blocks else ""
        rest = "\n\n".join(blocks[1:])
        out.append({"when": when, "topic": parts[1], "title": title, "slug": slug,
                    "title_html": _render(title).removeprefix("<p>").removesuffix("</p>"),
                    "lead_html": _render(lead), "rest_html": _render(rest)})
    out.sort(key=lambda e: e["when"], reverse=True)
    return out


def view(path=None):
    """Page model: day-grouped entries + the thread list for the dropdown."""
    threads = _threads()
    path = path or os.path.join(NB_DIR, "Notebook.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        error = None
    except OSError as e:
        text, mtime, error = "", None, f"{type(e).__name__}: {path}"
    entries = parse(text, threads)
    counts = {}
    for e in entries:
        t = threads.get(e["slug"])
        b = BINS.get(e["slug"], BEFORE)
        e["thread_name"] = t["name"] if t else b[1]
        e["colour"] = t["colour"] if t else b[2]
        counts[e["slug"]] = counts.get(e["slug"], 0) + 1
    for i, e in enumerate(entries):
        e["open"] = i == 0   # the newest entry reads in full; the rest collapse to their lead
    days, cur = [], None
    for e in entries:
        d = e["when"].date()
        if d != cur:
            days.append({"label": e["when"].strftime("%a %b ") + str(d.day), "entries": []})
            cur = d
        days[-1]["entries"].append(e)
    # Tabs: every thread that has an entry on the page, plus open threads, then the two bins.
    options = [{"slug": s, "name": t["name"], "question": t["question"], "next": t["next"],
                "colour": t["colour"], "count": counts.get(s, 0)}
               for s, t in threads.items() if counts.get(s) or t["status"] == "open"]
    options.sort(key=lambda o: (-o["count"], o["name"]))
    for (slug, name, colour), q in ((WANDER, "Tagged Thread: wander, outside any thread"),
                                    (BEFORE, "Written before threads existed")):
        if counts.get(slug):
            options.append({"slug": slug, "name": name, "question": q, "next": "",
                            "colour": colour, "count": counts[slug]})
    return {"days": days, "options": options, "total": len(entries),
            "mtime": mtime, "error": error}
