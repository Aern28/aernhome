# created-by: opus
# created: 2026-10-09
# purpose: phone-width screenshots of every Nexus page + browser-side checks (JS errors, sideways scroll), gallery HTML, queue post on change
# lifespan: infrastructure
# project: nexus-verify
"""nexus_shots.py - what Nexus looks like on Aern's phone, page by page.

Runs on TRAINER (Playwright + Chromium live there), not in the container.
Reads the page list and server-side verdicts from Nexus GET /api/nexus-verify,
renders every /nexus page at phone size in headless Chromium, and records what
only a browser can see: JavaScript errors, failed page loads, and sideways
scroll (a page wider than the phone).

  py nexus_shots.py                 # screenshots + gallery, prints a summary
  py nexus_shots.py --post          # also post to the to_fleet queue when the problem set CHANGES
  py nexus_shots.py --out DIR       # default C:\\temp\\nexus-shots

Output: <out>\\latest\\{index.html, results.json, *.jpg}; the gallery is
self-contained (images inlined) so it can be published as one artifact.
Read-only against Nexus apart from the optional keyed queue post.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

BASE = os.environ.get("NEXUS_URL", "http://100.110.245.37:5555")
PHONE = {"width": 390, "height": 844}  # iPhone 14-class CSS viewport
MAX_SHOT_H = 2600                       # CSS px; long pages are cut, not shrunk
QUEUE_KEY = "nexus-shots"


def _slug(path):
    return re.sub(r"[^a-z0-9]+", "-", path.strip("/").lower()) or "home"


def fetch_verify(fresh=False):
    url = f"{BASE}/api/nexus-verify" + ("?fresh=1" if fresh else "")
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.load(r)


def shoot(pages, outdir):
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
        for pg in pages:
            path = pg["path"]
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m, e=errors: e.append(f"console: {m.text}"[:200]) if m.type == "error" else None)
            page.on("pageerror", lambda exc, e=errors: e.append(f"js: {exc}"[:200]))
            rec = {"path": path, "title": pg.get("title", ""), "verdict": pg.get("verdict", "?"),
                   "status": None, "errors": errors, "overflow_px": 0, "shot": None}
            try:
                resp = page.goto(BASE + path, wait_until="networkidle", timeout=45000)
                rec["status"] = resp.status if resp else None
                page.wait_for_timeout(500)
                rec["overflow_px"] = page.evaluate(
                    "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)")
                full_h = page.evaluate("document.documentElement.scrollHeight")
                shot = os.path.join(outdir, _slug(path) + ".jpg")
                page.screenshot(path=shot, type="jpeg", quality=55, full_page=True,
                                clip={"x": 0, "y": 0, "width": PHONE["width"], "height": min(full_h, MAX_SHOT_H)})
                rec["shot"] = os.path.basename(shot)
                rec["cut"] = full_h > MAX_SHOT_H
            except Exception as e:
                errors.append(f"load: {type(e).__name__}: {e}"[:200])
            page.close()
            results.append(rec)
        browser.close()
    return results


def problems(rec):
    out = []
    if rec["status"] != 200:
        out.append(f"HTTP {rec['status']}")
    if rec["errors"]:
        out.append(f"{len(rec['errors'])} browser error(s)")
    if rec["overflow_px"] > 4:
        out.append(f"{rec['overflow_px']}px sideways scroll")
    if rec["verdict"] not in ("PASS", "?"):
        out.append(f"verify {rec['verdict']}")
    return out


def gallery(results, verify, outdir):
    badge = {"PASS": "#2f7d4f", "STALE": "#b7791f", "DEAD": "#c53030", "UNKNOWN": "#6b7280"}
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = []
    for r in results:
        probs = problems(r)
        img = ""
        if r["shot"]:
            with open(os.path.join(outdir, r["shot"]), "rb") as f:
                img = f'<img src="data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}" alt="{html.escape(r["path"])} at phone width" loading="lazy">'
        notes = "".join(f"<li>{html.escape(e)}</li>" for e in r["errors"][:5])
        cards.append(f"""<figure class="card{' bad' if probs else ''}">
  <figcaption><span class="pill" style="background:{badge.get(r['verdict'], '#6b7280')}">{html.escape(r['verdict'])}</span>
  <b>{html.escape(r['title'] or r['path'])}</b> <code>{html.escape(r['path'])}</code>
  {'<div class="prob">' + html.escape('; '.join(probs)) + '</div>' if probs else ''}
  {'<ul>' + notes + '</ul>' if notes else ''}{'<div class="cut">cut at ' + str(MAX_SHOT_H) + 'px</div>' if r.get('cut') else ''}</figcaption>
  {img}
</figure>""")
    bad = sum(1 for r in results if problems(r))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nexus on a Phone</title>
<style>
:root{{--bg:#f6f6f4;--fg:#1d1d1b;--card:#fff;--muted:#6b6b66;--line:#e2e2dd}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#151514;--fg:#ecece8;--card:#1f1f1d;--muted:#a3a39c;--line:#33332f}}}}
:root[data-theme="dark"]{{--bg:#151514;--fg:#ecece8;--card:#1f1f1d;--muted:#a3a39c;--line:#33332f}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
header{{padding:20px 16px 8px;max-width:1400px;margin:auto}} h1{{margin:0 0 4px;font-size:22px}}
.sub{{color:var(--muted)}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;padding:16px;max-width:1400px;margin:auto}}
.card{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
.card.bad{{border-color:#c53030}}
figcaption{{padding:10px 12px;font-size:13px}} figcaption code{{color:var(--muted)}}
.pill{{color:#fff;border-radius:999px;padding:1px 8px;font-size:11px;margin-right:4px}}
.prob{{color:#c53030;margin-top:4px}} .cut{{color:var(--muted);margin-top:4px}}
ul{{margin:4px 0 0;padding-left:18px;color:var(--muted)}}
img{{display:block;width:100%;height:auto;border-top:1px solid var(--line)}}
</style></head><body>
<header><h1>Nexus on a phone</h1>
<div class="sub">{len(results)} pages at 390px wide · {bad} with problems · server verify: {html.escape(verify.get('worst', '?'))} · rendered {when} CT from Trainer</div></header>
<main>{''.join(cards)}</main></body></html>"""
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def post_if_changed(results, prev):
    """Keyed to_fleet post when the problem set changes; resolve when it clears."""
    sys.path.insert(0, "C:/tools")
    import nexus
    # Browser-only problems: verify verdicts are already on the fleet board (nexus_pages).
    def browser(r):
        return [p for p in problems(r) if not p.startswith("verify ")]
    now = {r["path"]: browser(r) for r in results if browser(r)}
    before = {r["path"]: browser(r) for r in (prev or []) if browser(r)}
    if now == before:
        return "unchanged"
    c = nexus.Client()
    if not now:
        for it in c.queue_list(direction="to_fleet", open_only=True):
            if it.get("key") == QUEUE_KEY:
                c.queue_resolve(it["id"], "nexus_shots: every page clean at phone width")
        return "cleared"
    lines = [f"Nexus phone check (Trainer nexus_shots.py): {len(now)} page(s) with problems."]
    lines += [f"- {p}: {'; '.join(v)}" for p, v in sorted(now.items())]
    lines.append("Gallery: C:\\temp\\nexus-shots\\latest\\index.html on Trainer.")
    c.queue_post("to_fleet", "\n".join(lines), source="py C:/Projects/aernhome/nexus_shots.py",
                 priority=3, project="nexus-verify", key=QUEUE_KEY)
    return "posted"


def main():
    ap = argparse.ArgumentParser(description="phone-width Nexus screenshots + browser checks")
    ap.add_argument("--out", default=r"C:\temp\nexus-shots")
    ap.add_argument("--post", action="store_true", help="queue post when the problem set changes")
    ap.add_argument("--fresh", action="store_true", help="recompute server verify first")
    args = ap.parse_args()
    outdir = os.path.join(args.out, "latest")
    os.makedirs(outdir, exist_ok=True)
    prev_path = os.path.join(outdir, "results.json")
    prev = None
    if os.path.exists(prev_path):
        with open(prev_path, encoding="utf-8") as f:
            prev = json.load(f).get("results")
    verify = fetch_verify(fresh=args.fresh)
    pages = [p for p in verify["pages"] if p["path"].startswith("/nexus")]
    results = shoot(pages, outdir)
    gallery(results, verify, outdir)
    with open(prev_path, "w", encoding="utf-8") as f:
        json.dump({"at": datetime.now().isoformat(timespec="seconds"), "results": results}, f, indent=1)
    bad = [r for r in results if problems(r)]
    for r in bad:
        print(f"{r['path']}: {'; '.join(problems(r))}")
        for e in r["errors"][:3]:
            print(f"    {e}")
    print(f"{len(results) - len(bad)}/{len(results)} pages clean at phone width -> {os.path.join(outdir, 'index.html')}")
    if args.post:
        print("queue:", post_if_changed(results, prev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
