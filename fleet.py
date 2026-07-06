"""
fleet.py — Fleet board sentinel for the Aernbot/TCG/infra homelab.

A background daemon thread polls a fixed set of health checks every
FLEET_INTERVAL seconds, persists current state + rolling history to
/data/fleet_state.json (atomic temp+rename write), and sends Signal alerts on
state transitions plus a 07:00 America/Chicago daily digest. A Flask
Blueprint exposes the current state at GET /api/fleet.

Every check is wrapped so a single failure (dead socket, missing file, locked
db, docker API hiccup) degrades that one check to "unknown" and can never
kill the sentinel loop or the Flask process — mirrors the graceful-degradation
convention used throughout app.py / nexus_sources.py.
"""

import os
import json
import time
import sqlite3
import tempfile
import threading
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, jsonify, request, abort

try:
    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


fleet_bp = Blueprint("fleet", __name__)

# ── Config ─────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "fleet_state.json")
HOST_STATS_PATH = os.path.join(DATA_DIR, "host_stats.json")

WORKSPACE_ALIVE_PATH = "/workspace/.bot_alive"
TCG_DB_PATH = os.environ.get("TCG_DB_PATH", "/tcg/inventory.db")

FLEET_INTERVAL = int(os.environ.get("FLEET_INTERVAL", "60") or "60")
HISTORY_LEN = 100
ALERT_COOLDOWN_S = 10 * 60  # max 1 alert per check per 10 min
DIGEST_HOUR = 7  # 07:00 America/Chicago daily digest
CENTRAL_TZ = ZoneInfo("America/Chicago")

SIGNAL_HTTP_URL = os.environ.get("SIGNAL_HTTP_URL", "")
SIGNAL_ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")
SIGNAL_ALERT_TO = os.environ.get("SIGNAL_ALERT_TO", "")

# id -> (label, group). Groups surfaced on the board: aernbot | tcg | infra | host
CHECK_META = {
    "relay_alive": ("Aernbot Relay", "aernbot"),
    "signal_cli": ("Signal CLI", "aernbot"),
    "containers": ("Containers", "infra"),
    "mirror_fresh": ("TCG Mirror", "tcg"),
    "price_fresh": ("TCG Prices", "tcg"),
    "disk": ("Disk Space", "infra"),
}

_STATUS_ICON = {"up": "✅", "warn": "⚠️", "down": "\U0001F534", "unknown": "❔"}
_STATUS_RANK = {"up": 0, "unknown": 1, "warn": 2, "down": 3}

_stop_event = threading.Event()


# ── Individual checks (each returns (status, detail); never raises) ──────────
def check_relay_alive():
    """mtime of /workspace/.bot_alive, touched ~every 30s by the relay bot."""
    try:
        age = time.time() - os.path.getmtime(WORKSPACE_ALIVE_PATH)
    except OSError:
        return ("unknown", "heartbeat file not found")
    if age < 120:
        return ("up", f"heartbeat {int(age)}s ago")
    if age < 300:
        return ("warn", f"heartbeat {int(age)}s ago")
    return ("down", f"heartbeat {int(age)}s ago")


def check_containers():
    """Crash-loop detector: any container restarting, or exited despite an
    unless-stopped restart policy (i.e. it should be running but isn't).

    Lists ALL containers (not just running ones) on purpose — an exited
    container with restart-policy unless-stopped is exactly the failure this
    check exists to catch, and it wouldn't appear in a running-only list.
    """
    if not DOCKER_AVAILABLE:
        return ("unknown", "docker library not available")
    try:
        client = docker.from_env()
        containers = client.containers.list(all=True)
        bad = []
        running = 0
        for c in containers:
            if c.status == "running":
                running += 1
            restart_policy = ((c.attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")
            if c.status == "restarting":
                bad.append(f"{c.name}:restarting")
            elif c.status == "exited" and restart_policy == "unless-stopped":
                bad.append(f"{c.name}:exited")
        if bad:
            return ("down", "crash-looping: " + ", ".join(bad))
        return ("up", f"{running}/{len(containers)} running")
    except Exception as e:
        return ("unknown", f"docker error: {e}"[:200])


def check_signal_cli():
    if not DOCKER_AVAILABLE:
        return ("unknown", "docker library not available")
    try:
        client = docker.from_env()
        c = client.containers.get("signal-cli")
        if c.status == "running":
            return ("up", "running")
        return ("down", f"status: {c.status}")
    except docker.errors.NotFound:
        return ("down", "container not found")
    except Exception as e:
        return ("unknown", f"docker error: {e}"[:200])


def check_mirror_fresh():
    """mtime of /tcg/inventory.db — daily fetch cadence."""
    try:
        age_h = (time.time() - os.path.getmtime(TCG_DB_PATH)) / 3600
    except OSError:
        return ("unknown", "inventory.db not found")
    if age_h > 50:
        return ("down", f"{age_h:.1f}h old")
    if age_h > 26:
        return ("warn", f"{age_h:.1f}h old")
    return ("up", f"{age_h:.1f}h old")


def check_price_fresh():
    """MAX(date) from the prices table in inventory.db (~3PM daily fetch)."""
    if not os.path.exists(TCG_DB_PATH):
        return ("unknown", "inventory.db not found")
    uri = f"file:{TCG_DB_PATH}?mode=ro&immutable=1&nolock=1"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        row = con.execute("SELECT MAX(date) FROM prices").fetchone()
    except sqlite3.Error as e:
        return ("unknown", f"query failed: {e}"[:200])
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    last = row[0] if row else None
    if not last:
        return ("unknown", "no price rows")
    try:
        when = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return ("unknown", f"unparseable date: {last}")
    age_d = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400
    if age_d > 4:
        return ("down", f"prices {age_d:.1f}d stale")
    if age_d > 2:
        return ("warn", f"prices {age_d:.1f}d stale")
    return ("up", f"prices {age_d:.1f}d old")


def check_disk():
    """psutil disk_usage on / and /data; reports the worse of the two."""
    try:
        import psutil
    except ImportError:
        return ("unknown", "psutil not available")

    worst = "up"
    parts = []
    for label, path in (("/", "/"), ("/data", DATA_DIR)):
        try:
            usage = psutil.disk_usage(path)
            pct_free = (usage.free / usage.total * 100) if usage.total else 0.0
        except OSError as e:
            parts.append(f"{label}: unavailable ({e})")
            continue
        if pct_free < 5:
            status = "down"
        elif pct_free < 15:
            status = "warn"
        else:
            status = "up"
        if _STATUS_RANK[status] > _STATUS_RANK[worst]:
            worst = status
        parts.append(f"{label}: {pct_free:.1f}% free")
    if not parts:
        return ("unknown", "no mount points readable")
    return (worst, "; ".join(parts))


SIMPLE_CHECKS = {
    "relay_alive": check_relay_alive,
    "containers": check_containers,
    "signal_cli": check_signal_cli,
    "mirror_fresh": check_mirror_fresh,
    "price_fresh": check_price_fresh,
    "disk": check_disk,
}


def check_host_stats():
    """Fan out /data/host_stats.json's "checks" list into individual fleet
    checks (group "host"). Dead-man's switch: if the file is missing or older
    than 30 min, the host-side collector itself is presumed dead, so emit a
    single host_collector down/unknown check instead of stale per-item data.

    Returns a list of (id, label, group, status, detail) tuples.
    """
    try:
        age_min = (time.time() - os.path.getmtime(HOST_STATS_PATH)) / 60
    except OSError:
        return [("host_collector", "Host Collector", "host", "unknown", "host_stats.json not found")]

    if age_min > 30:
        return [("host_collector", "Host Collector", "host", "down", f"stale {age_min:.0f}m (collector likely dead)")]

    try:
        with open(HOST_STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [("host_collector", "Host Collector", "host", "unknown", f"read error: {e}"[:200])]

    items = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [("host_collector", "Host Collector", "host", "unknown", "no checks reported")]

    out = []
    for c in items:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        out.append((
            c["id"],
            c.get("label", c["id"]),
            "host",
            c.get("status", "unknown"),
            c.get("detail", ""),
        ))
    return out or [("host_collector", "Host Collector", "host", "unknown", "no valid checks reported")]


def run_all_checks():
    """Run every check, each wrapped so one crash can't take down the rest.
    Returns {id: {"label", "group", "status", "detail"}}."""
    results = {}

    for cid, fn in SIMPLE_CHECKS.items():
        label, group = CHECK_META[cid]
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "unknown", f"check crashed: {e}"[:200]
        results[cid] = {"label": label, "group": group, "status": status, "detail": detail}

    try:
        host_items = check_host_stats()
    except Exception as e:
        host_items = [("host_collector", "Host Collector", "host", "unknown", f"check crashed: {e}"[:200])]
    for cid, label, group, status, detail in host_items:
        results[cid] = {"label": label, "group": group, "status": status, "detail": detail}

    return results


# ── State persistence (atomic write) ──────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("checks", {})
    state.setdefault("alerts", {})
    state.setdefault("recent_alerts", [])
    state.setdefault("last_digest_date", None)
    return state


def save_state_atomic(state):
    """temp file + os.replace so a crash mid-write never corrupts the file
    a concurrent /api/fleet request might be reading."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".fleet_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_PATH)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# ── Signal alerting ────────────────────────────────────────────────────────
def _send_signal(message):
    """POST a JSON-RPC 2.0 `send` to signal-cli's HTTP daemon. Degrades to a
    log-and-skip (returns False) if env vars are missing or the call fails —
    never raises to the sentinel loop."""
    if not (SIGNAL_HTTP_URL and SIGNAL_ACCOUNT and SIGNAL_ALERT_TO):
        print("[fleet] Signal not configured (SIGNAL_HTTP_URL/ACCOUNT/ALERT_TO) — skipping alert:\n" + message)
        return False
    payload = {
        "jsonrpc": "2.0",
        "method": "send",
        "params": {
            "account": SIGNAL_ACCOUNT,
            "recipient": [SIGNAL_ALERT_TO],
            "message": message,
        },
        "id": str(int(time.time() * 1000)),
    }
    url = SIGNAL_HTTP_URL.rstrip("/") + "/api/v1/rpc"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f"[fleet] signal send failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            print(f"[fleet] signal send returned JSON-RPC error: {body['error']}")
            return False
        return True
    except Exception as e:
        print(f"[fleet] signal send failed: {e}")
        return False


def _rate_limited(last_sent_iso, now):
    if not last_sent_iso:
        return False
    try:
        last = dt.datetime.fromisoformat(last_sent_iso)
    except ValueError:
        return False
    return (now - last).total_seconds() < ALERT_COOLDOWN_S


def _build_digest(checks_state, now):
    local_now = now.astimezone(CENTRAL_TZ)
    lines = [f"Fleet daily digest — {local_now.strftime('%Y-%m-%d %H:%M %Z')}"]

    by_group = {}
    for cid, entry in checks_state.items():
        by_group.setdefault(entry.get("group", "infra"), []).append((cid, entry))

    for group in ("aernbot", "tcg", "infra", "host"):
        items = sorted(by_group.get(group, []), key=lambda kv: kv[1].get("label", kv[0]))
        if not items:
            continue
        lines.append(f"\n{group.upper()}:")
        for _cid, entry in items:
            icon = _STATUS_ICON.get(entry.get("status"), "•")
            lines.append(f"  {icon} {entry.get('label')}: {entry.get('status')}")

    # Flaps in the last 24h, derived from each check's rolling history.
    cutoff = now - dt.timedelta(hours=24)
    flap_lines = []
    for _cid, entry in checks_state.items():
        history = entry.get("history", [])
        changes = 0
        last_status = None
        for h in history:
            try:
                ts = dt.datetime.fromisoformat(h["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if ts < cutoff:
                last_status = h.get("status")
                continue
            if last_status is not None and h.get("status") != last_status:
                changes += 1
            last_status = h.get("status")
        if changes:
            flap_lines.append(f"  {entry.get('label')}: {changes} change(s)")

    lines.append("\nFlaps (24h):")
    lines.extend(flap_lines if flap_lines else ["  none"])

    return "\n".join(lines)


# ── Sentinel pass ──────────────────────────────────────────────────────────
def sentinel_pass():
    """Run every check once, update persisted state/history, fire alerts on
    transitions, and send the 07:00 daily digest once per day. Returns the
    updated state dict. Never raises — every stage is independently guarded
    so a bad check or a Signal outage can't stop state from being persisted."""
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.isoformat()

    state = load_state()
    checks_state = state["checks"]
    alerts_state = state["alerts"]

    try:
        results = run_all_checks()
    except Exception as e:
        print(f"[fleet] run_all_checks crashed unexpectedly: {e}")
        results = {}

    transitions = []  # (cid, old_status)
    for cid, r in results.items():
        prev = checks_state.get(cid)
        prev_status = prev.get("status") if prev else None
        entry = prev or {"history": []}
        entry["label"] = r["label"]
        entry["group"] = r["group"]
        entry["detail"] = r["detail"]
        entry["status"] = r["status"]
        if prev_status != r["status"]:
            entry["last_change"] = now_iso
            if prev_status is not None:  # skip the very first observation (no baseline yet)
                transitions.append((cid, prev_status))
        else:
            entry.setdefault("last_change", now_iso)
        history = entry.get("history", [])
        history.append({"ts": now_iso, "status": r["status"]})
        entry["history"] = history[-HISTORY_LEN:]
        checks_state[cid] = entry

    # Alert on: transition INTO warn/down/unknown, or recovery TO up.
    alert_lines = []
    for cid, old_status in transitions:
        new_status = checks_state[cid]["status"]
        into_bad = new_status in ("warn", "down", "unknown")
        recovered = new_status == "up" and old_status in ("warn", "down", "unknown")
        if not (into_bad or recovered):
            continue
        if _rate_limited(alerts_state.get(cid, {}).get("last_sent"), now):
            continue
        label = checks_state[cid]["label"]
        if recovered:
            alert_lines.append(f"{_STATUS_ICON['up']} RECOVERED: {label} ({old_status} → up)")
        else:
            icon = _STATUS_ICON.get(new_status, "•")
            detail = checks_state[cid].get("detail") or ""
            suffix = f" — {detail}" if detail else ""
            alert_lines.append(f"{icon} {label}: {old_status} → {new_status}{suffix}")
        alerts_state[cid] = {"last_sent": now_iso}

    if alert_lines:
        message = "Fleet alert:\n" + "\n".join(alert_lines)
        try:
            sent = _send_signal(message)
        except Exception as e:
            print(f"[fleet] alert send crashed: {e}")
            sent = False
        state["recent_alerts"].append({"ts": now_iso, "message": message, "sent": sent})
        state["recent_alerts"] = state["recent_alerts"][-20:]

    # Daily digest at 07:00 America/Chicago (guarded by last_digest_date so a
    # 60s poll interval doesn't resend it every pass through the hour).
    local_now = now.astimezone(CENTRAL_TZ)
    today_str = local_now.strftime("%Y-%m-%d")
    if local_now.hour == DIGEST_HOUR and state.get("last_digest_date") != today_str:
        try:
            digest = _build_digest(checks_state, now)
            sent = _send_signal(digest)
        except Exception as e:
            print(f"[fleet] digest build/send crashed: {e}")
            digest, sent = None, False
        if digest is not None:
            state["recent_alerts"].append({"ts": now_iso, "message": digest, "sent": sent})
            state["recent_alerts"] = state["recent_alerts"][-20:]
        state["last_digest_date"] = today_str

    state["generated_at"] = now_iso

    try:
        save_state_atomic(state)
    except Exception as e:
        print(f"[fleet] failed to persist fleet_state.json: {e}")

    return state


def _sentinel_loop():
    print(f"[fleet] sentinel starting, interval={FLEET_INTERVAL}s")
    while not _stop_event.is_set():
        try:
            sentinel_pass()
        except Exception as e:
            print(f"[fleet] sentinel pass crashed: {e}")
        _stop_event.wait(FLEET_INTERVAL)


def start_sentinel():
    """Start the background sentinel daemon thread. Safe to call once at app
    startup; the thread dies with the process (daemon=True)."""
    t = threading.Thread(target=_sentinel_loop, name="fleet-sentinel", daemon=True)
    t.start()
    return t


# ── API ────────────────────────────────────────────────────────────────────
def _is_nexus_allowed():
    """Same Tailscale-only gate as app.py's _is_nexus_allowed(): allow only
    when the request has no CF-Connecting-IP header, i.e. it arrived straight
    over Tailscale/LAN and not through the public Cloudflare tunnel. Kept as
    a local copy (not imported from app.py) to avoid a circular import — app
    imports fleet, so fleet importing app back would be circular."""
    return request.headers.get("CF-Connecting-IP") is None


@fleet_bp.route("/api/fleet")
def api_fleet():
    if not _is_nexus_allowed():
        abort(404)
    state = load_state()
    checks_state = state.get("checks", {})
    checks_out = [
        {
            "id": cid,
            "label": entry.get("label", cid),
            "group": entry.get("group", "infra"),
            "status": entry.get("status", "unknown"),
            "detail": entry.get("detail", ""),
            "last_change": entry.get("last_change"),
            "history": entry.get("history", []),
        }
        for cid, entry in checks_state.items()
    ]
    checks_out.sort(key=lambda c: (c["group"], c["label"]))
    return jsonify({
        "generated_at": state.get("generated_at"),
        "checks": checks_out,
        "recent_alerts": state.get("recent_alerts", [])[-20:],
    })
