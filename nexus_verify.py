# created-by: opus
# created: 2026-10-09
# purpose: verify every Nexus page against nexus_feature_map.json - renders, sources fresh, Tailscale-only, no unmapped routes
# lifespan: infrastructure
# project: nexus-verify
"""nexus_verify.py - is each Nexus page showing something true and current?

nexus_smoketest.py proves pages return 200 and connectors return the right
TYPE. That passed on 10/09 while /nexus/vault had no mount to serve (retired
8/06) and currently_reading() could only ever return []: "empty" looked like
"all clear". This verifier works from nexus_feature_map.json instead:

  page     GET via the Flask test client -> 200 and no error text
  guard    same GET as a Cloudflare request -> 404 (Tailscale-only, /nexus and /api)
  sources  each declared source probed for its as-of -> PASS / STALE / DEAD
  drift    every GET route is mapped or explicitly excluded (else DRIFT)

READ-ONLY: opens files and SQLite read-only, writes nothing. Runs inside
aernhome-dashboard (paths are container paths):

  docker exec aernhome-dashboard python /app/nexus_verify.py [--json] [--only /nexus/tcg]

Exit 0 = every page PASS, 1 = anything STALE/DEAD/DRIFT. fleet.py's
check_nexus_pages() calls verify() and puts the summary on /api/fleet.
"""
import argparse
import glob
import importlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "nexus_feature_map.json")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
RANK = {"PASS": 0, "UNKNOWN": 1, "STALE": 2, "DEAD": 3, "DRIFT": 3}
FLEET_TO_VERDICT = {"up": "PASS", "warn": "STALE", "down": "DEAD", "unknown": "UNKNOWN"}
ERROR_MARKERS = ("Traceback (most recent call last)", "Internal Server Error", "jinja2.exceptions")


def load_map(path=MAP_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _expand(p):
    return p.replace("{DATA_DIR}", DATA_DIR)


def _parse_ts(value):
    """ISO or SQLite 'YYYY-MM-DD HH:MM:SS'; naive = UTC (CURRENT_TIMESTAMP)."""
    if value is None:
        return None
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_h(ts):
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def _fmt_h(h):
    return f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d"


def _fresh(ts, src, what):
    """Turn an as-of timestamp into a verdict against stale_after_h."""
    if ts is None:
        return "DEAD", f"{what}: no timestamp"
    age = _age_h(ts)
    limit = src.get("stale_after_h")
    if limit is not None and age > limit:
        return "STALE", f"{what} {_fmt_h(age)} old (limit {_fmt_h(limit)})"
    return "PASS", f"{what} {_fmt_h(age)} old"


def _mtime(path):
    return datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone.utc)


def _fleet_checks():
    try:
        with open(os.path.join(DATA_DIR, "fleet_state.json"), encoding="utf-8") as f:
            return json.load(f).get("checks", {})
    except (OSError, ValueError):
        return {}


def probe(src, fleet_checks):
    """One source -> (verdict, evidence). Never raises."""
    kind = src.get("kind")
    try:
        if kind == "user_store":
            db = _expand(src["db"])
            return ("PASS", "user data (no freshness bar)") if os.path.exists(db) else ("DEAD", f"missing {db}")
        if kind == "fleet_check":
            c = fleet_checks.get(src["check"])
            if not c:
                return "UNKNOWN", f"fleet check {src['check']} not in fleet_state.json"
            return FLEET_TO_VERDICT.get(c.get("status"), "UNKNOWN"), f"{src['check']}: {c.get('detail', '')}"[:160]
        if kind == "file_age":
            p = _expand(src["file"])
            if not os.path.exists(p):
                return "DEAD", f"missing {p}"
            return _fresh(_mtime(p), src, os.path.basename(p))
        if kind == "glob_newest":
            hits = glob.glob(_expand(src["glob"]))
            if not hits:
                return "DEAD", f"no match {src['glob']}"
            newest = max(hits, key=os.path.getmtime)
            return _fresh(_mtime(newest), src, os.path.basename(newest))
        if kind == "sqlite_max":
            db = _expand(src["db"])
            if not os.path.exists(db):
                return "DEAD", f"missing {db}"
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(src["sql"]).fetchone()
            finally:
                con.close()
            return _fresh(_parse_ts(row[0] if row else None), src, os.path.basename(db))
        if kind == "json_field":
            p = _expand(src["file"])
            if not os.path.exists(p):
                return "DEAD", f"missing {p}"
            with open(p, encoding="utf-8") as f:
                val = json.load(f)
            for key in src["field"].split("."):
                val = val.get(key) if isinstance(val, dict) else None
            return _fresh(_parse_ts(val), src, f"{os.path.basename(p)}:{src['field']}")
        if kind == "dir_present":
            d = src["dir"]
            if not os.path.isdir(d):
                return "DEAD", f"{d} not mounted"
            return ("PASS", f"{d}: {len(os.listdir(d))} entries") if os.listdir(d) else ("DEAD", f"{d} empty")
        if kind == "agenda_board":
            return _probe_agenda_board()
        if kind == "connector":
            import nexus_contract as nc
            env = nc.envelope(src["name"])
            reason, warning = env["empty_reason"], env["warning"]
            if reason and reason != "none_due":
                return "DEAD", f"{src['name']}: {reason}"
            if warning:
                return "STALE", f"{src['name']}: {warning}"
            what = f"{src['name']} ({'empty: none due' if reason else 'has data'})"
            if env["as_of"]:
                return _fresh(_parse_ts(env["as_of"]), src, what)
            return "PASS", f"{what}, {env['freshness']} source, no as_of"
        if kind == "nonempty":
            mod, fn = src["call"].split(":")
            out = getattr(importlib.import_module(mod), fn)()
            return ("PASS", f"{src['call']} -> {len(out)} items") if out else ("DEAD", f"{src['call']} returned empty")
        return "UNKNOWN", f"unknown source kind {kind!r}"
    except Exception as e:  # a probe bug must not hide the other sources
        return "UNKNOWN", f"probe crashed: {e}"[:160]


AGENDA_DONE_GRACE_H = 18  # a project closed after one /goodmorning is ticked by the next


def agenda_board_findings(lines_by_which, projects, now=None):
    """Unchecked agenda lines that disagree with the board.
    lines_by_which: {"daily": [...], "weekly": [...]} from second_brain._agenda_lines.
    Returns (unknown, carried_done): "which: <id>" strings."""
    now = now or datetime.now(timezone.utc)
    unknown, carried = [], []
    for which, lines in lines_by_which.items():
        for ln in lines or []:
            if ln.get("kind") != "item" or ln.get("checked") is not False:
                continue
            for link in ln.get("projects", []):
                if not link["found"]:
                    unknown.append(f"{which}: {link['id']}")
            if ln.get("auto_done"):
                for link in ln["projects"]:
                    since = _parse_ts((projects.get(link["id"]) or {}).get("status_changed_at"))
                    if since is None or (now - since).total_seconds() / 3600 > AGENDA_DONE_GRACE_H:
                        carried.append(f"{which}: {link['id']}")
    return sorted(set(unknown)), sorted(set(carried))


def _probe_agenda_board():
    import second_brain as sb
    projects = sb._project_index()
    doc = sb.load_agenda()
    lines = {w: sb._agenda_lines((doc.get(w) or {}).get("content"), projects) for w in ("daily", "weekly")}
    tagged = sum(1 for ls in lines.values() for ln in ls if ln.get("projects"))
    unknown, carried = agenda_board_findings(lines, projects)
    if not unknown and not carried:
        return "PASS", f"{tagged} tagged agenda lines agree with the board"
    parts = []
    if carried:
        parts.append(f"unticked lines for projects done >{AGENDA_DONE_GRACE_H}h: {', '.join(carried)}")
    if unknown:
        parts.append(f"tags with no board project: {', '.join(unknown)}")
    return "STALE", "; ".join(parts)[:300]


def _get_app():
    """The running app when called from the sentinel (app.py runs as __main__);
    an import when run standalone."""
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "app") and hasattr(main.app, "test_client"):
        return main.app
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    return importlib.import_module("app").app


def check_page(client, page):
    """(verdict, evidence) for the render + the Tailscale-only guard."""
    path = page["path"]
    r = client.get(path)
    if r.status_code != 200:
        return "DEAD", f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    for m in ERROR_MARKERS:
        if m in body:
            return "DEAD", f"HTTP 200 but body contains {m!r}"
    cf = client.get(path, headers={"CF-Connecting-IP": "203.0.113.9"})
    if page.get("cf_expect") == "decoy":
        cf_body = cf.get_data(as_text=True)
        if cf.status_code == 404 or (len(cf_body) <= 64 and cf_body != body):
            return "PASS", f"HTTP 200, {len(body) // 1024}KB, CF->decoy"
        return "DEAD", f"PUBLIC: Cloudflare request got HTTP {cf.status_code} with {len(cf_body)} bytes (decoy expected)"
    if cf.status_code != 404:
        return "DEAD", f"PUBLIC: Cloudflare request got HTTP {cf.status_code}, expected 404"
    return "PASS", f"HTTP 200, {len(body) // 1024}KB, CF->404"


def post_guards(fmap, client):
    """Write endpoints that answered a Cloudflare-shaped POST with anything but 404.
    Empty JSON body: an ungated handler rejects it (400) rather than writing."""
    bad = []
    for path in fmap.get("post_guards", []):
        r = client.post(path, json={}, headers={"CF-Connecting-IP": "203.0.113.9"})
        if r.status_code != 404:
            bad.append(f"{path} -> HTTP {r.status_code}")
    return bad


def drift(fmap, app):
    mapped = {p["path"] for p in fmap["pages"]} | set(fmap.get("excluded", {}))
    rules = {r.rule for r in app.url_map.iter_rules() if "GET" in r.methods}
    return sorted(rules - mapped), sorted({p["path"] for p in fmap["pages"]} - rules)


def verify(only=None, map_path=MAP_PATH):
    """Run everything. Returns {"pages": [...], "unmapped": [...], "gone": [...], "worst": verdict}."""
    fmap = load_map(map_path)
    app = _get_app()
    client = app.test_client()
    fleet_checks = _fleet_checks()
    pages = []
    for page in fmap["pages"]:
        if only and page["path"] != only:
            continue
        verdict, evidence = check_page(client, page)
        sources = []
        for src in page.get("sources", []):
            v, ev = probe(src, fleet_checks)
            sources.append({"id": src["id"], "kind": src["kind"], "verdict": v, "evidence": ev,
                            "fallback": bool(src.get("fallback")),
                            **({"note": src["note"]} if v != "PASS" and src.get("note") else {})})
        worst = max([verdict] + [s["verdict"] for s in sources if not s["fallback"]], key=RANK.get)
        pages.append({"path": page["path"], "title": page.get("title", ""), "verdict": worst,
                      "render": {"verdict": verdict, "evidence": evidence}, "sources": sources})
    unmapped, gone = ([], []) if only else drift(fmap, app)
    open_posts = [] if only else post_guards(fmap, client)
    verdicts = ([p["verdict"] for p in pages] + (["DRIFT"] if unmapped or gone else [])
                + (["DEAD"] if open_posts else []))
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pages": pages, "unmapped": unmapped, "gone": gone, "open_posts": open_posts,
            "worst": max(verdicts, key=RANK.get) if verdicts else "PASS"}


def summary(result):
    """One line for the fleet board."""
    bad = [p for p in result["pages"] if p["verdict"] != "PASS"]
    parts = []
    for p in sorted(bad, key=lambda p: -RANK[p["verdict"]]):
        why = next((s for s in p["sources"] if s["verdict"] == p["verdict"] and not s.get("fallback")), None)
        parts.append(f"{p['path']} {p['verdict']}" + (f" ({why['id']})" if why else ""))
    if result.get("open_posts"):
        parts.append(f"PUBLIC WRITE: {', '.join(result['open_posts'])}")
    if result["unmapped"]:
        parts.append(f"DRIFT unmapped: {', '.join(result['unmapped'])}")
    if result["gone"]:
        parts.append(f"DRIFT mapped but no route: {', '.join(result['gone'])}")
    ok = len(result["pages"]) - len(bad)
    return f"{ok}/{len(result['pages'])} pages PASS" + ("; " + "; ".join(parts) if parts else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    ap.add_argument("--only", help="verify a single mapped path")
    ap.add_argument("--map", default=MAP_PATH)
    args = ap.parse_args()
    result = verify(only=args.only, map_path=args.map)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        for p in result["pages"]:
            print(f"[{p['verdict']:7}] {p['path']}  - {p['render']['evidence']}")
            for s in p["sources"]:
                print(f"      {s['verdict']:7} {s['id']}{' (fallback, not counted)' if s['fallback'] else ''}: {s['evidence']}")
                if s.get("note"):
                    print(f"              note: {s['note']}")
        guards = len(load_map(args.map).get("post_guards", []))
        if not args.only:
            print(f"[{'DEAD' if result['open_posts'] else 'PASS':7}] post guards: "
                  + ("; ".join(result["open_posts"]) or f"{guards} write endpoints 404 to a public POST"))
        for r in result["unmapped"]:
            print(f"[DRIFT  ] unmapped GET route {r} - add it to nexus_feature_map.json pages or excluded")
        for r in result["gone"]:
            print(f"[DRIFT  ] mapped path {r} has no route")
        print("\n" + summary(result))
    return 0 if result["worst"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
