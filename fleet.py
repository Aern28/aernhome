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
CANARY_PATH = os.path.join(DATA_DIR, "canary.json")
CANARY_STALE_H = 26
POSTAGE_PATH = os.path.join(DATA_DIR, "postage.json")
POSTAGE_STALE_H = 3  # Trainer postage-check.py pushes every 30 min  # nightly cadence; same slack check_mirror_fresh() gives the daily TCG price fetch
CENSUS_PATH = os.path.join(DATA_DIR, "fleet-census.json")
SPECSURF_PATH = os.path.join(DATA_DIR, "spec-surfacing.json")
SPECSURF_STALE_H = 30  # Trainer task 'Spec Surfacing Check' runs daily
CENSUS_STALE_D = 9  # Trainer task 'Fleet Census' runs weekly (Sun 05:00); 9d = one missed run

FINANCE_VERDICT_PATH = os.path.join(DATA_DIR, "finance_verdict.json")
FINANCE_VERDICT_STALE_DAYS = 10  # Weekly Books Publish (Trainer, Sundays) refreshes this; stale now
                                 # means "automation missed 2+ runs", not "monthly close missed" —
                                 # a missed monthly close shows via the verdict's bank_as_of field.

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
    "scan_runner": ("Aernbot scan-runner", "aernbot"),
    "containers": ("Containers", "infra"),
    "qbittorrent": ("qBittorrent (NAS)", "infra"),
    "trainer_ollama": ("Trainer Ollama (5080)", "infra"),
    "mirror_fresh": ("TCG Mirror", "tcg"),
    "price_fresh": ("TCG Prices", "tcg"),
    "daily_chain": ("TCG Daily Chain (07:00)", "tcg"),
    "disk": ("Disk Space", "infra"),
    "signup_webhook": ("aern-signup Notifications", "infra"),
    "home_assistant": ("Home Assistant (Pi)", "infra"),
    "keep_sync": ("Keep ⇄ Restock sync", "infra"),
    "byos_device": ("TRMNL (BYOS)", "infra"),
    "byos_deps": ("BYOS/Keep deps", "infra"),
}

_STATUS_ICON = {"up": "✅", "warn": "⚠️", "down": "\U0001F534", "unknown": "❔"}
_STATUS_RANK = {"up": 0, "unknown": 1, "warn": 2, "down": 3}

_stop_event = threading.Event()


def _to_central_str(iso_str):
    """'2026-08-03T20:17:51+00:00' -> '2026-08-03 15:17 CDT'. None if unparseable.

    Companion for every UTC timestamp this API emits. Storage stays UTC; this is
    purely so a reader cannot mistake it for wall-clock time.
    """
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        when = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.astimezone(CENTRAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return None


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


def check_daily_chain():
    """Did the 07:00 TCG chain actually land today?

    Added 2026-09-04, after the canon cutover moved the chain onto Ashaman.
    check_price_fresh() only warns at >2 DAYS stale, so a single missed 07:00 run
    reads as "up" and Aern would not hear about it until two mornings later. This
    check closes that window: prices.date is a DATE, so a successful run leaves
    MAX(date) == today.

    Timezone-safe by construction: we compare UTC dates and only assert the run
    "should" have landed after 14:00 UTC (09:00 CDT / 08:00 CST), which is
    comfortably past the 07:00 local trigger without needing tzdata in the image.
    """
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
        last_d = dt.date.fromisoformat(str(last)[:10])
    except ValueError:
        return ("unknown", f"unparseable date: {last}")

    now = dt.datetime.now(dt.timezone.utc)
    days_behind = (now.date() - last_d).days
    if days_behind <= 0:
        return ("up", f"today's 07:00 chain landed (prices dated {last_d})")
    if now.hour < 14:
        return ("up", f"chain not due yet today (prices dated {last_d})")
    if days_behind == 1:
        return ("warn", f"07:00 chain did NOT land - prices still dated {last_d}; "
                        f"kick: Start-ScheduledTask -TaskName 'TCG Inventory - Daily Price Fetch'")
    return ("down", f"07:00 chain has not landed in {days_behind}d - prices dated {last_d}")


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


def check_qbittorrent():
    """qBittorrent WebUI on the Synology (rides gluetun's network namespace —
    died silently for 2 days when gluetun restarted 2026-07-04, hence this).
    Checked via LAN IP; the container can reach the LAN through the bridge."""
    try:
        r = requests.get("http://192.168.1.118:8080", timeout=8)
        if r.status_code < 400:
            return ("up", f"WebUI HTTP {r.status_code}")
        return ("down", f"WebUI HTTP {r.status_code}")
    except requests.RequestException as e:
        return ("down", f"unreachable: {type(e).__name__}")


def check_home_assistant():
    """Home Assistant on the Raspberry Pi (192.168.1.70:8123). The Pi also
    serves the whole house's DNS via AdGuard, so if it dies the internet
    "breaks" for everyone — it was the only fleet node with no monitoring
    until 2026-08-01. Unauthenticated /manifest.json is enough for liveness;
    no token needed, so no secret lives in this container."""
    try:
        r = requests.get("http://192.168.1.70:8123/manifest.json", timeout=8)
        if r.status_code < 400:
            return ("up", f"HTTP {r.status_code}")
        return ("down", f"HTTP {r.status_code}")
    except requests.RequestException as e:
        return ("down", f"unreachable: {type(e).__name__}")


def check_scan_runner():
    """Aernbot scan-runner LIVENESS — is the scheduler still cycling?

    Ported from Uptime Kuma's push monitor when Kuma was retired 2026-08-03.
    Worth keeping because it catches a failure `check_containers` structurally
    cannot: scan-runner can sit there "running" with a wedged scheduler, and a
    container-status check calls that healthy.

    Kuma had this beat and it worked -- but zero of its six monitors were wired
    to a notification provider, so it observed failures and told nobody. Here it
    rides the board's existing Signal alerting.

    Beat = log lines tagged [SOURCE:scheduler].

    WINDOW: the scan loop cycles roughly every 65 minutes, so the quiet gap
    BETWEEN cycles legitimately exceeds an hour. This shipped with a 60-minute
    window on 2026-08-03 and false-alarmed within the hour (last beat 20:00:28,
    next 21:04:12 — a normal gap). 2h clears the real cadence with headroom;
    anything tighter alerts once per cycle forever, which trains you to ignore
    the board. Re-check this if the scan schedule ever changes.
    """
    if not DOCKER_AVAILABLE:
        return ("unknown", "docker library not available")
    try:
        client = docker.from_env()
        c = client.containers.get("scan-runner")
        if c.status != "running":
            return ("down", f"container {c.status}")
        window_s = 7200  # 2h — see WINDOW note above; cycle is ~65m
        raw = c.logs(since=int(time.time()) - window_s, tail=600)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        beats = [ln for ln in text.splitlines() if "[SOURCE:scheduler]" in ln]
        if not beats:
            return ("down", "no scheduler activity in 2h — wedged?")
        if len(beats) < 2:
            return ("warn", f"only {len(beats)} scheduler event in 2h")
        return ("up", f"scheduler alive ({len(beats)} events/2h)")
    except Exception as e:
        return ("unknown", f"{type(e).__name__}")


def check_trainer_ollama():
    """Trainer's RTX 5080 Ollama endpoint (tailscale serve, tailnet-only) —
    the fleet's inference node; Aernbot's cheap-thought chores prefer it over
    Ashaman's 2070S. Aern intentionally pauses it for gaming via /game-mode,
    so unreachable is a warn with that explanation, never a red alert."""
    base = "http://100.99.191.118:11434"
    try:
        r = requests.get(base + "/api/version", timeout=5)
        if r.status_code != 200:
            return ("warn", f"HTTP {r.status_code}")
        ver = r.json().get("version", "?")
        try:
            models = requests.get(base + "/api/ps", timeout=5).json().get("models", [])
            resident = ", ".join(m.get("name", "?") for m in models) if models else "idle"
        except requests.RequestException:
            resident = "residency unknown"
        return ("up", f"v{ver} · {resident} · 5080")
    except requests.RequestException as e:
        return ("warn", f"unreachable ({type(e).__name__}) — game-mode or Trainer down; Aernbot on 2070S fallback")


def check_signup_webhook():
    """aern-signup booking-notification webhook, probed end-to-end via the SAME
    route the booking app uses (host.docker.internal -> published :5678 -> n8n),
    so it catches both the Docker port-proxy break AND the workflow being
    unregistered. The latter is the 2026-07-28 incident: n8n booted with the
    workflow deactivated (a CLI import or user-management reset can do this),
    every booking POST 404'd "webhook not registered", and a real student's
    invite was lost invisibly. The workflow's Code node no-ops event="probe"
    (guard added same day), so this sends zero emails; each probe does log one
    trivial n8n execution — throttled to every 10 min (the fleet loop is 60s,
    which would otherwise mean 1,440 junk executions/day)."""
    now = time.time()
    if now - _signup_probe_cache["ts"] < 600 and _signup_probe_cache["result"]:
        return _signup_probe_cache["result"]
    try:
        r = requests.post(
            "http://host.docker.internal:5678/webhook/aern-signup",
            json={"event": "probe"}, timeout=10)
    except requests.RequestException as e:
        result = ("down", f"n8n unreachable via app's route: {type(e).__name__} "
                          "(port-proxy or n8n down — bookings are NOT notifying)")
    else:
        if r.status_code == 200:
            result = ("up", "probe accepted (workflow registered)")
        elif r.status_code == 404:
            result = ("down", "webhook NOT registered — workflow deactivated in n8n; "
                              "bookings are silently losing invites. Activate "
                              "'aern-signup booking notifications' in the n8n UI.")
        else:
            result = ("warn", f"unexpected HTTP {r.status_code}")
    _signup_probe_cache.update(ts=now, result=result)
    return result


_signup_probe_cache = {"ts": 0.0, "result": None}


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


KEEP_SYNC_STAMP = os.path.join(DATA_DIR, "keep_sync_last.json")
KEEP_SYNC_STALE_H = 1.0   # host task "Keep Sync" runs q15m
BYOS_STATE = os.path.join(DATA_DIR, "byos", "state.json")
BYOS_SILENT_H = 2.0       # battery idle polls every 15 min; 2h silent = dead battery / lost Wi-Fi


def check_keep_sync():
    """Dead-man's switch for keep_sync.py (Google Keep "Grocery List" <-> Nexus restock,
    2026-09-02). The script stamps /data/keep_sync_last.json every run, ok or not; a
    stale stamp means the host task died, ok=false means Keep auth/network failed."""
    try:
        age_h = (time.time() - os.path.getmtime(KEEP_SYNC_STAMP)) / 3600
        with open(KEEP_SYNC_STAMP, "r", encoding="utf-8") as f:
            st = json.load(f)
    except OSError:
        return ("unknown", "no stamp yet - keep_sync.py has not run")
    except json.JSONDecodeError as e:
        return ("unknown", f"stamp unreadable: {e}"[:200])
    if age_h > KEEP_SYNC_STALE_H:
        return ("warn", f"last run {age_h:.1f}h ago (host task 'Keep Sync' stale)")
    if not st.get("ok"):
        return ("warn", f"last run failed: {st.get('error')}"[:200])
    c = st.get("counts") or {}
    return ("up", f"{st.get('open')} open · synced {_to_central_str(st.get('at'))} · "
                  f"{sum(c.values())} change(s) last run")


def check_byos_device():
    """The TRMNL on the wall, once it points at Nexus (byos.py). Reads the device
    registry: last poll time, battery voltage, what's on screen, last render error."""
    try:
        with open(BYOS_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except OSError:
        return ("unknown", "no device paired yet (byos state missing)")
    except json.JSONDecodeError as e:
        return ("unknown", f"state unreadable: {e}"[:200])
    devs = st.get("devices") or {}
    if not devs:
        return ("unknown", "no device paired yet")
    mac, d = max(devs.items(), key=lambda kv: kv[1].get("last_seen") or "")
    try:
        seen = dt.datetime.fromisoformat(d["last_seen"])
        age_h = (dt.datetime.now(seen.tzinfo) - seen).total_seconds() / 3600
    except (KeyError, ValueError, TypeError):
        return ("unknown", f"{d.get('friendly_id')}: never polled")
    volts = (d.get("volts") or [None])[-1]
    ld = st.get("last_display") or {}
    detail = (f"{d.get('friendly_id')} polled {age_h * 60:.0f}m ago · {volts} V · fw {d.get('fw')} · "
              f"showing {ld.get('screen')}{' (pinned)' if ld.get('pinned') else ''}")
    if age_h > BYOS_SILENT_H:
        return ("warn", f"silent {age_h:.1f}h — battery dead or off Wi-Fi · " + detail)
    if ld.get("error"):
        return ("warn", f"render error: {ld['error']}"[:120] + " · " + detail)
    if isinstance(volts, (int, float)) and volts < 3.4:
        return ("warn", "battery low · " + detail)
    return ("up", detail)


def check_byos_deps():
    """The BYOS renderer + Keep sync need packages that are in requirements.txt but only
    reach the container via an image rebuild (console-only on Ashaman) or a
    `docker exec pip install` bridge. A recreate without a rebuild silently drops the
    bridge — this check is what makes that loud."""
    missing = []
    # a pip bridge applied while Nexus is running stayed "missing" until a restart
    # (2026-09-02) — the import system caches site-packages listings; drop them first
    import importlib
    importlib.invalidate_caches()
    for mod, pkg in (("PIL", "Pillow"), ("liquid", "python-liquid"), ("websocket", "websocket-client"),
                     ("gkeepapi", "gkeepapi"), ("gpsoauth", "gpsoauth")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        return ("down", "missing in container: " + ", ".join(missing) +
                " — docker compose build (console) or docker exec aernhome-dashboard pip install <pkgs>")
    return ("up", "Pillow, python-liquid, websocket-client, gkeepapi, gpsoauth importable")


SIMPLE_CHECKS = {
    "relay_alive": check_relay_alive,
    "containers": check_containers,
    "signal_cli": check_signal_cli,
    "scan_runner": check_scan_runner,
    "qbittorrent": check_qbittorrent,
    "trainer_ollama": check_trainer_ollama,
    "mirror_fresh": check_mirror_fresh,
    "price_fresh": check_price_fresh,
    "daily_chain": check_daily_chain,
    "disk": check_disk,
    "signup_webhook": check_signup_webhook,
    "home_assistant": check_home_assistant,
    "keep_sync": check_keep_sync,
    "byos_device": check_byos_device,
    "byos_deps": check_byos_deps,
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


def check_canary():
    """Fan out /data/canary.json's "checks" list into individual fleet
    checks (group "tcg", ids prefixed canary_). That file is written nightly
    by canary/tcg_selector_canary.py, running on Ashaman via Task Scheduler
    in the same Python env as tcgsales-automation — it probes the TCGplayer
    seller portal, PirateShip, LetterTrackPro, and TCGCSV for selector/markup
    drift so it's caught before the unattended sales pipeline hits it
    mid-run. Staleness follows the same dead-man's-switch shape as
    check_host_stats(): missing file -> single unknown check (canary hasn't
    run yet); file older than CANARY_STALE_H -> single warn check (the
    canary job itself is presumed stuck/not running) instead of trusting
    stale per-selector data.

    Returns a list of (id, label, group, status, detail) tuples.
    """
    try:
        age_h = (time.time() - os.path.getmtime(CANARY_PATH)) / 3600
    except OSError:
        return [("canary_missing", "Selector Canary", "tcg", "unknown", "canary.json not found — canary not yet run")]

    if age_h > CANARY_STALE_H:
        return [("canary_stale", "Selector Canary", "tcg", "warn", f"canary.json is {age_h:.1f}h old (canary stale)")]

    try:
        with open(CANARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [("canary_error", "Selector Canary", "tcg", "unknown", f"read error: {e}"[:200])]

    items = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [("canary_error", "Selector Canary", "tcg", "unknown", "no checks reported")]

    out = []
    for c in items:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        out.append((
            f"canary_{c['id']}",
            c.get("label", c["id"]),
            "tcg",
            c.get("status", "unknown"),
            c.get("detail", ""),
        ))
    return out or [("canary_error", "Selector Canary", "tcg", "unknown", "no valid checks reported")]


def check_spec_surfacing():
    """BUILD #8 Phase 5 dead-man's switch. Written by Trainer's
    C:/tools/spec-surfacing-check.py (daily).

    On 2026-08-17 a spec DECLINED exactly as designed, wrote a receipt saying
    "Surfaced via Step 2 queue lane", and no queue item ever appeared. The audit
    log, the receipts and the journal were all complete; the only missing step
    was the one nothing was watching, so it stayed invisible for six days.

    This check lives OUTSIDE the relay on purpose: a receipt that claims it
    surfaced cannot be the thing that verifies surfacing.
    """
    try:
        age_h = (time.time() - os.path.getmtime(SPECSURF_PATH)) / 3600
    except OSError:
        return [("spec_surfacing_missing", "Spec surfacing", "aernbot", "unknown",
                 "spec-surfacing.json not found - Trainer check not yet run")]
    if age_h > SPECSURF_STALE_H:
        return [("spec_surfacing_stale", "Spec surfacing", "aernbot", "warn",
                 f"spec-surfacing.json is {age_h:.0f}h old - Trainer check stale")]
    try:
        with open(SPECSURF_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [("spec_surfacing_error", "Spec surfacing", "aernbot", "unknown",
                 f"read error: {e}"[:200])]
    out = []
    for c in (data.get("checks") or []):
        if isinstance(c, dict) and c.get("id"):
            out.append((c["id"], c.get("label", "Spec surfacing"), "aernbot",
                        c.get("status", "unknown"), c.get("detail", "")))
    return out or [("spec_surfacing_error", "Spec surfacing", "aernbot", "unknown",
                    "no checks reported")]


def check_census():
    """Surface the weekly fleet census (Trainer task 'Fleet Census', Sun 05:00, repo
    Aern28/fleet-discovery-audit) so good-morning reads it. A census nobody reads is
    the failure mode the project was built to avoid, and pushing the file without
    wiring it here would have recreated exactly that.

    Reports the headline counts plus anything the census flagged as worth acting on.
    Same dead-man's-switch shape as check_postage().
    """
    try:
        age_d = (time.time() - os.path.getmtime(CENSUS_PATH)) / 86400
    except OSError:
        return [("census_missing", "Fleet Census", "fleet", "unknown",
                 "fleet-census.json not found - Trainer 'Fleet Census' task not yet run")]
    if age_d > CENSUS_STALE_D:
        return [("census_stale", "Fleet Census", "fleet", "warn",
                 f"census is {age_d:.0f}d old - Trainer 'Fleet Census' task (Sun 05:00) has missed a run")]
    try:
        with open(CENSUS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [("census_error", "Fleet Census", "fleet", "unknown", f"read error: {e}"[:200])]
    c = d.get("counts") or {}
    hosts = d.get("hosts") or {}
    flags = [x for x in (d.get("flags") or []) if x]
    head = (f"{len(hosts)} hosts | {c.get('infrastructure', 0)} infrastructure | "
            f"{c.get('dormant', 0)} dormant | census {age_d:.0f}d old")
    # Routine census flags (dirty repos, https remotes, depth caps) are SEAT
    # chores, not Aern's -- a warn here lands in the For-Aern "Needs you" lane
    # via _needs_from_fleet(), and the chronic depth-cap flag made that card
    # permanent (Aern 9/02: "nothingburger unless there's something I can do").
    # Status stays "up" with the flags in detail (visible on /nexus/fleet +
    # good-morning); warn is reserved for missing/stale/error above, where the
    # census pipeline itself needs a human.
    if flags:
        return [("census", "Fleet Census", "fleet", "up", head + " | " + "; ".join(flags[:3]))]
    return [("census", "Fleet Census", "fleet", "up", head + " | nothing flagged")]


def check_postage():
    """Fan out /data/postage.json's "checks" list into fleet checks (group "tcg", ids prefixed
    postage_). Written every 30 min by Trainer's C:/tools/postage-check.py (Aern 8/19: good-morning /
    goodnight should screen LetterTrack + Pirate Ship automatically) - it reads Ashaman's inventory.db
    order lifecycle and NAS finance.db postage bookings, both of which only Trainer can reach together.
    Same dead-man's-switch shape as check_canary().
    """
    try:
        age_h = (time.time() - os.path.getmtime(POSTAGE_PATH)) / 3600
    except OSError:
        return [("postage_missing", "Postage Screen", "tcg", "unknown", "postage.json not found - Trainer postage-check not yet run")]
    if age_h > POSTAGE_STALE_H:
        return [("postage_stale", "Postage Screen", "tcg", "warn", f"postage.json is {age_h:.1f}h old (Trainer postage-check task stale)")]
    try:
        with open(POSTAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [("postage_error", "Postage Screen", "tcg", "unknown", f"read error: {e}"[:200])]
    items = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [("postage_error", "Postage Screen", "tcg", "unknown", "no checks reported")]
    out = []
    for c in items:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        out.append((f"postage_{c['id']}", c.get("label", c["id"]), "tcg", c.get("status", "unknown"), c.get("detail", "")))
    return out or [("postage_error", "Postage Screen", "tcg", "unknown", "no valid checks reported")]


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

    try:
        canary_items = check_canary()
    except Exception as e:
        canary_items = [("canary_error", "Selector Canary", "tcg", "unknown", f"check crashed: {e}"[:200])]
    for cid, label, group, status, detail in canary_items:
        results[cid] = {"label": label, "group": group, "status": status, "detail": detail}

    try:
        postage_items = check_postage()
    except Exception as e:
        postage_items = [("postage_error", "Postage Screen", "tcg", "unknown", f"check crashed: {e}"[:200])]
    for cid, label, group, status, detail in postage_items:
        results[cid] = {"label": label, "group": group, "status": status, "detail": detail}

    # Kept in its OWN try: sharing one with postage meant a census crash would
    # silently blank the postage checks too.
    try:
        census_items = check_census() + check_spec_surfacing()
    except Exception as e:
        census_items = [("census_error", "Fleet Census", "fleet", "unknown", f"check crashed: {e}"[:200])]
    for cid, label, group, status, detail in census_items:
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

    # Retire checks that no longer exist (e.g. canary_missing after the first
    # real canary run, or a renamed host-collector check) — otherwise ghosts
    # linger on the board forever. Only when this pass actually produced
    # results, so a total run_all_checks failure can't wipe the board.
    if results:
        for cid in [c for c in checks_state if c not in results]:
            del checks_state[cid]

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


# ── TCG business ops (GET /api/tcg-ops) ───────────────────────────────────
def _tcg_ops_connect():
    """Open inventory.db read-only. Raises sqlite3.Error/OSError on failure —
    callers are responsible for the try/except (same immutable=1+nolock=1
    convention as check_price_fresh / tcg_plugin_data.py, needed for
    Docker Desktop's grpcfuse read-only bind mount)."""
    uri = f"file:{TCG_DB_PATH}?mode=ro&immutable=1&nolock=1"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _parse_ts_utc(value):
    """Best-effort parse of the orders table's timestamp columns. Values are
    written by app code as naive ISO strings (no offset); treated as UTC —
    same convention as tcg_plugin_data.friendly_age() and check_price_fresh()
    above, kept consistent so ages/freshness read the same everywhere."""
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when


def _tcg_ops_orders(con, now):
    """Order-lifecycle snapshot. Every sub-query is independently wrapped so
    one bad column/query degrades just that field to None, never the whole
    response. No buyer/PII columns (buyer_name, shipping_address) are ever
    selected — order ids, values, statuses, and timestamps only."""
    out = {
        "status_counts_30d": None,
        "held": {"count": None, "orders": None},
        "most_recent_order_at": None,
    }

    # labeled_at is the timestamp an order first lands in this table (set at
    # insert time), i.e. the closest thing to a "recorded on this mirror" clock
    # — used below for the 30d bucket and ages.
    try:
        cutoff30 = (now - dt.timedelta(days=30)).isoformat()
        rows = con.execute(
            "SELECT status, COUNT(*) FROM orders WHERE labeled_at >= ? GROUP BY status",
            (cutoff30,),
        ).fetchall()
        out["status_counts_30d"] = {r[0] or "unknown": r[1] for r in rows}
    except sqlite3.Error as e:
        print(f"[fleet] tcg-ops status_counts_30d query failed: {e}")

    try:
        rows = con.execute(
            "SELECT order_id, order_total, labeled_at FROM orders WHERE status = 'held' ORDER BY labeled_at ASC"
        ).fetchall()
        held = []
        for r in rows:
            when = _parse_ts_utc(r["labeled_at"])
            age_hours = round((now - when).total_seconds() / 3600, 1) if when else None
            held.append({"order_id": r["order_id"], "value": r["order_total"], "age_hours": age_hours})
        out["held"]["count"] = len(held)
        out["held"]["orders"] = held
    except sqlite3.Error as e:
        print(f"[fleet] tcg-ops held query failed: {e}")

    try:
        row = con.execute("SELECT MAX(labeled_at) FROM orders").fetchone()
        out["most_recent_order_at"] = row[0] if row else None
    except sqlite3.Error as e:
        print(f"[fleet] tcg-ops most_recent_order_at query failed: {e}")

    return out


def _tcg_ops_prices(con):
    """Price freshness. Per-category (products.category) MAX(date) via a join
    — measured ~0.7s over 744k price rows / 35k products on the real mirror
    (idx_prices_product_date makes it a single covering-index scan + temp
    group-by), acceptable for a Tailscale-only page polled every 30s. Falls
    back to an overall MAX(date) + row count for that date if the join errors
    for any reason (e.g. a much larger table on a future mirror)."""
    out = {"by_category": None, "overall_last_date": None, "overall_last_date_rows": None}

    try:
        row = con.execute("SELECT MAX(date) FROM prices").fetchone()
        last_date = row[0] if row else None
        out["overall_last_date"] = last_date
        if last_date:
            cnt = con.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (last_date,)).fetchone()
            out["overall_last_date_rows"] = cnt[0] if cnt else None
    except sqlite3.Error as e:
        print(f"[fleet] tcg-ops overall price freshness query failed: {e}")

    try:
        rows = con.execute(
            "SELECT pr.category, MAX(p.date) FROM prices p "
            "JOIN products pr ON pr.id = p.product_id "
            "GROUP BY pr.category"
        ).fetchall()
        out["by_category"] = {(r[0] or "Unknown"): r[1] for r in rows}
    except sqlite3.Error as e:
        print(f"[fleet] tcg-ops by-category price freshness query failed: {e}")

    return out


def _tcg_ops_autoprocess():
    """Passthrough of the tcg_autoprocess entry from /data/host_stats.json
    (written by host_collector/fleet_host_stats.ps1 on Ashaman). None if the
    file is missing/unparseable or that check id isn't present."""
    try:
        with open(HOST_STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    items = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    for c in items:
        if isinstance(c, dict) and c.get("id") == "tcg_autoprocess":
            return c
    return None


def _tcg_ops_mirror():
    """inventory.db file mtime + size — the read-only mount's own freshness,
    independent of what's inside it."""
    try:
        st = os.stat(TCG_DB_PATH)
    except OSError:
        return {"mtime": None, "size_bytes": None}
    mtime = dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc).isoformat()
    return {"mtime": mtime, "size_bytes": st.st_size}


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
            # UI contract: oldest-first 1/0 ticks (1 = up); internal state keeps
            # the richer {status, ts} records.
            "history": [1 if h.get("status") == "up" else 0
                        for h in entry.get("history", []) if isinstance(h, dict)],
        }
        for cid, entry in checks_state.items()
    ]
    checks_out.sort(key=lambda c: (c["group"], c["label"]))
    # `_local` companions: stored time is UTC (correct), but every Claude seat
    # reads this raw JSON and misreads bare UTC as wall-clock. Happened twice on
    # 2026-08-03/04 -- alert times were narrated as local while being UTC, and an
    # agenda stamp of "20:17" was read as 8pm when it was 15:17 CDT. The browser
    # UI was never affected (it renders relative time). Additive only.
    alerts_out = []
    for a in state.get("recent_alerts", [])[-20:]:
        if isinstance(a, dict) and a.get("ts"):
            local = _to_central_str(a["ts"])
            alerts_out.append({**a, "ts_local": local} if local else a)
        else:
            alerts_out.append(a)
    gen = state.get("generated_at")
    return jsonify({
        "generated_at": gen,
        "generated_at_local": _to_central_str(gen),
        "checks": checks_out,
        "recent_alerts": alerts_out,
    })


@fleet_bp.route("/api/tcg-ops")
def api_tcg_ops():
    """TCG business-ops strip: order lifecycle (30d status counts), held
    orders, autoprocess status, price freshness, and mirror age. (The
    2026-07-03 NenTera-outage backlog counter was retired 2026-07-13 —
    Phoenix's q15min Order Sync resumed 7/10 and parity was verified.)
    Every section is independently wrapped so a single failing
    query/file read degrades that section to nulls — this route never 500s.
    No buyer/PII fields (buyer_name, shipping_address) are ever queried."""
    if not _is_nexus_allowed():
        abort(404)

    now = dt.datetime.now(dt.timezone.utc)

    orders = {
        "status_counts_30d": None,
        "held": {"count": None, "orders": None},
        "most_recent_order_at": None,
    }
    prices = {"by_category": None, "overall_last_date": None, "overall_last_date_rows": None}

    if os.path.exists(TCG_DB_PATH):
        con = None
        try:
            con = _tcg_ops_connect()
            orders = _tcg_ops_orders(con, now)
            prices = _tcg_ops_prices(con)
        except (sqlite3.Error, OSError) as e:
            print(f"[fleet] tcg-ops db open failed: {e}")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
    else:
        print(f"[fleet] tcg-ops: {TCG_DB_PATH} not found")

    try:
        autoprocess = _tcg_ops_autoprocess()
    except Exception as e:
        print(f"[fleet] tcg-ops autoprocess read crashed: {e}")
        autoprocess = None

    try:
        mirror = _tcg_ops_mirror()
    except Exception as e:
        print(f"[fleet] tcg-ops mirror stat crashed: {e}")
        mirror = {"mtime": None, "size_bytes": None}

    return jsonify({
        "generated_at": now.isoformat(),
        "orders": orders,
        "autoprocess": autoprocess,
        "prices": prices,
        "mirror": mirror,
    })


@fleet_bp.route("/api/finance-verdict")
def api_finance_verdict():
    """The Verdict card: passthrough of /data/finance_verdict.json, written
    monthly by aern-finance/finance_verdict.py (finance-check loop). Same
    Tailscale-only gate as the rest of the Nexus API. 404s (not 500s) when
    the file is missing, same as _is_nexus_allowed()'s gate failure, so the
    frontend's "hide gracefully" and "wrong network" paths look identical to
    a caller — the card itself distinguishes "missing" from "stale" by
    checking for a body."""
    if not _is_nexus_allowed():
        abort(404)

    try:
        with open(FINANCE_VERDICT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[fleet] finance-verdict read failed: {e}")
        return jsonify({"available": False}), 404

    if not isinstance(data, dict):
        return jsonify({"available": False}), 404

    stale = None
    generated_at = data.get("generated_at")
    if generated_at:
        try:
            when = dt.datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            age_days = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400
            stale = age_days > FINANCE_VERDICT_STALE_DAYS
        except ValueError:
            stale = None

    data["available"] = True
    data["stale"] = stale
    return jsonify(data)
