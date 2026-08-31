# created-by: fable
# created: 2026-08-31
# purpose: x-feed lane - store captured X items and render the fleet RSS feed (feed.xml)
# lifespan: infrastructure
# project: x-feed
"""Storage + RSS renderer for the x-feed lane (Trainer's x_capture.py posts here).

Streams: [follows] curated profiles, [algo] For You picks, [restock] merged live
from changedetection.io's native RSS. Burst-collapse keeps high-volume accounts
readable: >3 consecutive items from one handle within 2h render as one digest
item. He is the filter - nothing here judges content. See x-feed/project.md.
"""
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime

DATA_DIR = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
ITEMS_PATH = os.path.join(DATA_DIR, "feed_items.jsonl")
CD_TOKEN_PATH = os.path.join(DATA_DIR, "cd_rss_token.txt")
CD_RSS_URL = "http://192.168.1.141:5000/rss?token={token}"
KEEP = 2000
BURST_N = 3
BURST_WINDOW_S = 2 * 3600
_cd_cache = {"ts": 0.0, "items": []}


_LITTER = re.compile(
    r"^(·|\d+[smh]|[A-Z][a-z]{2} \d{1,2}(, \d{4})?|\d[\d.,]*[KM]?)$")


def _clean_body(body):
    """Strip X UI litter that survives inner_text capture: '·' separators,
    bare relative times/dates, bare engagement counts. Applied at render time
    so items captured before cleaner passes still display well."""
    out, prev_blank = [], False
    for l in (body or "").split("\n"):
        s = l.strip()
        if _LITTER.fullmatch(s):
            continue
        blank = not s
        if blank and prev_blank:
            continue
        prev_blank = blank
        out.append(l)
    return "\n".join(out).strip()


def _load():
    if not os.path.exists(ITEMS_PATH):
        return []
    out = []
    with open(ITEMS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def add_items(items):
    """Append new items (dedupe by link); returns number actually added."""
    existing = {i.get("link") for i in _load()}
    fresh = [i for i in items
             if i.get("link") and i["link"] not in existing and i.get("body")]
    if not fresh:
        return 0
    all_items = _load() + fresh
    all_items = all_items[-KEEP:]
    tmp = ITEMS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for i in all_items:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    os.replace(tmp, ITEMS_PATH)
    return len(fresh)


def _ts(i):
    try:
        return datetime.fromisoformat((i.get("ts") or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _collapse(items):
    """Newest-first burst collapse per handle within the window."""
    out, i = [], 0
    while i < len(items):
        j = i
        h = items[i].get("handle")
        while (j + 1 < len(items) and items[j + 1].get("handle") == h
               and abs((_ts(items[i]) - _ts(items[j + 1])).total_seconds())
               <= BURST_WINDOW_S):
            j += 1
        run = items[i:j + 1]
        if len(run) > BURST_N:
            bodies = "\n\n".join(
                f"({_ts(x).strftime('%H:%M')}) {_clean_body(x.get('body',''))}"
                for x in run)
            media = [m for x in run for m in (x.get("media") or [])]
            out.append({"handle": h, "link": run[0].get("link"),
                        "ts": run[0].get("ts"), "body": bodies, "media": media,
                        "stream": run[0].get("stream", "follows"),
                        "digest_n": len(run)})
        else:
            out.extend(run)
        i = j + 1
    return out


def _restock_items():
    """changedetection.io native RSS, cached 10 min; [] on any failure."""
    if time.time() - _cd_cache["ts"] < 600:
        return _cd_cache["items"]
    items = []
    try:
        token = open(CD_TOKEN_PATH, encoding="utf-8").read().strip()
        import requests
        r = requests.get(CD_RSS_URL.format(token=token), timeout=10)
        r.raise_for_status()
        for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
            seg = m.group(1)
            def tag(name):
                t = re.search(rf"<{name}>(.*?)</{name}>", seg, re.S)
                return html.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1",
                                            t.group(1), flags=re.S)).strip() if t else ""
            title = tag("title")
            # infra watches (scraper feeds) are not restocks - keep shops/Bandai
            if re.search(r"egman|limitless", title, re.I):
                continue
            items.append({"handle": "", "link": tag("link") or tag("guid"),
                          "ts": tag("pubDate"), "body": title,
                          "media": [], "stream": "restock", "rfc_date": tag("pubDate")})
    except Exception:
        items = _cd_cache["items"]  # keep last good on failure
    _cd_cache.update(ts=time.time(), items=items)
    return items


NOTEBOOK_PATH = os.environ.get(
    "NOTEBOOK_PATH", "/workspace/obivault/Aernbot/Notebook.md")


def notebook_entries(limit=5):
    """Live pull of Aernbot Notebook entries off the read-only workspace mount.
    Replaces the push producer that died with the 7/12 relay rebuild - a pull
    can't go stale. Header shape: '## 2026-08-30 18:34 · topic · Title'."""
    try:
        text = open(NOTEBOOK_PATH, encoding="utf-8").read()
    except OSError:
        return []
    out = []
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    for p in parts:
        lines = p.split("\n")
        m = re.match(r"([\d-]+ [\d:]+)\s*·\s*([^·]+)·\s*(.+)", lines[0])
        if not m:
            continue
        ts, topic, title = m.group(1), m.group(2).strip(), m.group(3).strip()
        body = "\n".join(l for l in lines[1:] if l.strip())[:400]
        out.append({"title": title, "body": body, "created_at": ts,
                    "tag": topic, "url": None})
    out.sort(key=lambda e: e["created_at"], reverse=True)
    return out[:limit]


def recent_items(limit=5):
    """Newest captured x-feed items in nexus_feed card shape."""
    items = sorted(_load(), key=_ts, reverse=True)[:limit]
    out = []
    for i in items:
        body = _clean_body(i.get("body", ""))
        out.append({"title": (i.get("handle") or "") or f"[{i.get('stream','')}]",
                    "body": body[:400],
                    "created_at": _ts(i).strftime("%Y-%m-%d %H:%M"),
                    "tag": i.get("stream"), "url": i.get("link")})
    return out


def render_rss(limit=200):
    items = sorted(_load(), key=_ts, reverse=True)[:limit]
    items = _collapse(items) + _restock_items()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0"><channel>',
           "<title>Aern X Feed</title>",
           "<link>http://100.110.245.37:5555/feed.xml</link>",
           "<description>old twitter: curated profiles + algo picks + restocks</description>"]
    for i in items:
        h = html.escape(i.get("handle") or "")
        stream = i.get("stream", "follows")
        n = i.get("digest_n")
        body = _clean_body(i.get("body", "")) if not n else i.get("body", "")
        if not n:
            i = dict(i, body=body)
        first = next((l for l in body.split("\n") if l.strip()), "")[:90]
        if n:
            title = html.escape(f"[{stream}] {h} - {n} posts")
        elif h:
            title = html.escape(f"[{stream}] {h}: {first}")
        else:
            title = html.escape(f"[{stream}] {first}")
        body_html = html.escape(i.get("body", "")).replace("\n", "<br/>")
        for m in (i.get("media") or [])[:8]:
            body_html += f'<br/><img src="{html.escape(m)}"/>'
        if i.get("rfc_date"):
            date = i["rfc_date"]
        else:
            date = format_datetime(_ts(i))
        out.append(
            "<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
            "<pubDate>{d}</pubDate><description><![CDATA[{b}]]></description>"
            "</item>".format(t=title, l=html.escape(i.get("link", "")), d=date,
                             b=body_html))
    out.append("</channel></rss>")
    return "\n".join(out)
