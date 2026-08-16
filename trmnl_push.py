"""TRMNL merge-variable pushes — replaces the last three n8n TRMNL workflows.

n8n audit option (c), Aern 2026-08-16: move the remaining TRMNL pushes off n8n
onto a scheduled task, same pattern as Family Agenda Sync. Each push is
"fetch a Nexus endpoint -> POST merge_variables to a custom-plugin webhook";
n8n added nothing but a cron. Transforms below replicate the n8n Code nodes
exactly (same merge_variables keys), so the plugin markup is untouched.

Pushes and cadences (mirroring the retired n8n schedules):
    dashboard  q15m   /api/stats + /api/health  -> Aern Dashboard plugin
    tcg        q30m   /api/tcg-stats             -> TCG Business plugin
    season     q6h    /api/season                -> 72 Seasons plugin

Usage (inside the aernhome-dashboard container; host task runs `all` q15m):
    python trmnl_push.py all            # cadence-gated via /data/trmnl_push_state.json
    python trmnl_push.py tcg --force    # ignore cadence
    python trmnl_push.py all --dry-run  # print payloads, no POST, no state write

Plugin UUIDs default to the values lifted from the n8n export (webhook ids,
not credentials — same precedent as push_family_agenda_once.py); override
with TRMNL_DASHBOARD_UUID / TRMNL_TCG_UUID / TRMNL_SEASON_UUID if rotated.
"""
import datetime
import json
import os
import sys
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
BASE = os.environ.get("NEXUS_BASE", "http://127.0.0.1:5555")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE = os.path.join(DATA_DIR, "trmnl_push_state.json")

PLUGINS = {
    "dashboard": os.environ.get("TRMNL_DASHBOARD_UUID", "f99dc11e-b6fd-4abf-ab82-3cf15cd43269"),
    "tcg": os.environ.get("TRMNL_TCG_UUID", "577f5cb4-134e-4bf8-a786-6f546dd278a3"),
    "season": os.environ.get("TRMNL_SEASON_UUID", "365320df-e1d2-4829-8e25-f8ad5aaff04a"),
}
CADENCE_S = {"dashboard": 15 * 60, "tcg": 30 * 60, "season": 6 * 3600}
# a task fires q15m; allow ~2 min of jitter so a 15m cadence isn't skipped every other run
SLACK_S = 120


def _get(path, timeout=20):
    with urlopen(Request(BASE + path, headers={"User-Agent": "aernhome trmnl_push"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _round(v):
    return round(v or 0)


# --- transforms (1:1 with the n8n Code nodes) --------------------------------

def build_dashboard():
    stats = _get("/api/stats")
    health = _get("/api/health")
    name_map = {"n8n": "n8n", "jellyfin": "Jellyfin", "qbittorrent": "qBittorrent",
                "open-webui": "Open WebUI", "discord-relay": "Discord Rly", "cloudflared": "CF Tunnel",
                "scan-runner": "Scan Runner", "uptime-kuma": "Uptime Kuma", "home-assistant": "Home Asst."}
    svc = []
    for h in health if isinstance(health, list) else []:
        spark = "".join("|" if b else " " for b in (h.get("sparkline") or [])[-24:]).rjust(24)
        svc.append({"n": name_map.get(h.get("name")) or h.get("display_name") or h.get("name"),
                    "s": h.get("status") or "unknown", "sp": spark})
    any_down = any(s["s"] != "up" for s in svc)
    now = datetime.datetime.now(CT)
    if any_down:
        face, label = "[>_<]", "ALERT!"
    else:
        idx = (now.hour * 4 + now.minute // 15) % 3
        face = ["[^_^]", "[o_o]", "[-_-]"][idx]
        label = ["WORKING", "WATCHING", "RESTING"][idx]
    upd = now.strftime("%I:%M %p").lstrip("0")
    g = lambda k, f, d=0: ((stats.get(k) or {}).get(f)) if isinstance(stats.get(k), dict) else d
    mv = {
        "svc": svc,
        "dr": g("docker", "running") or 0, "dt": g("docker", "total") or 0,
        "cf": _round(g("c_drive", "free_gb")), "cp": _round(g("c_drive", "percent")),
        "gf": _round(g("g_drive", "free_gb")), "gp": _round(g("g_drive", "percent")),
        "ht": f"{(g('h_drive', 'free_gb') or 0) / 1024:.1f}", "hp": _round(g("h_drive", "percent")),
        "it": f"{(g('i_drive', 'free_gb') or 0) / 1024:.1f}", "ip": _round(g("i_drive", "percent")),
        "cpu": _round(g("cpu", "percent")), "ram": _round(g("ram", "percent")),
        "ru": f"{g('ram', 'used_gb') or 0:.1f}", "rt": f"{g('ram', 'total_gb') or 0:.1f}",
        "ba": face, "bl": label,
        "bs": g("bot", "summary", None), "bt": g("bot", "ts", None),
        "upd": upd, "down": any_down,
    }
    return {"merge_variables": mv}


def build_tcg():
    # /api/tcg-stats already returns {"merge_variables": {...}}; n8n posted it verbatim.
    return _get("/api/tcg-stats")


def build_season():
    s = _get("/api/season")
    mv = dict(s)
    mv["data"] = s
    return {"merge_variables": mv}


BUILDERS = {"dashboard": build_dashboard, "tcg": build_tcg, "season": build_season}


# --- push + state ------------------------------------------------------------

def _load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def push(name, payload, dry_run=False):
    body = json.dumps(payload).encode("utf-8")
    if dry_run:
        print(f"[dry-run] {name}: {len(body)} bytes -> {PLUGINS[name][:8]}... {body[:300].decode('utf-8', 'replace')}")
        return True, "dry-run"
    req = Request(f"https://trmnl.com/api/custom_plugins/{PLUGINS[name]}", data=body, method="POST",
                  headers={"Content-Type": "application/json", "User-Agent": "aernhome trmnl_push"})
    with urlopen(req, timeout=30) as resp:
        return True, f"HTTP {resp.status} {len(body)}B"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    which = args[0] if args else "all"
    targets = list(BUILDERS) if which == "all" else [which]
    unknown = [t for t in targets if t not in BUILDERS]
    if unknown:
        print(f"unknown target(s): {unknown}; choose from {list(BUILDERS)} or all")
        return 2
    force, dry = "--force" in flags, "--dry-run" in flags
    st = _load_state()
    now = time.time()
    stamp = datetime.datetime.now(CT).strftime("%Y-%m-%d %H:%M CT")
    rc = 0
    for t in targets:
        last = float((st.get(t) or {}).get("last_ok_ts") or 0)
        due = force or (now - last) >= (CADENCE_S[t] - SLACK_S)
        if not due:
            print(f"{stamp} {t}: skip (last ok {int((now - last) // 60)}m ago, cadence {CADENCE_S[t] // 60}m)")
            continue
        try:
            payload = BUILDERS[t]()
            ok, note = push(t, payload, dry_run=dry)
            print(f"{stamp} {t}: {note}")
            if ok and not dry:
                st[t] = {"last_ok_ts": now, "last_ok": stamp, "bytes": len(json.dumps(payload))}
        except Exception as e:  # keep going; one dead endpoint must not block the others
            rc = 1
            print(f"{stamp} {t}: FAILED {type(e).__name__}: {e}")
            st.setdefault(t, {})["last_error"] = f"{stamp} {type(e).__name__}: {e}"
    if not dry:
        _save_state(st)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
