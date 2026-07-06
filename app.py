"""
AernHome Dashboard - Self-hosted home services dashboard
Flask backend with service health checks and system stats
"""

import os
import json
import time
import sqlite3
from email.utils import formatdate
from datetime import datetime
from flask import Flask, render_template, jsonify, Response, send_from_directory, abort, request, make_response, redirect
import requests
import psutil
import shutil
import nexus_writes as ns_writes
import nexus_md
import fleet
import second_brain
import ledger
import todoist_bridge
import vault

# Unlock token for showing service links through Cloudflare Tunnel
# Visit aern.dev/?unlock=<token> to set cookie, ?lock to clear
UNLOCK_TOKEN = os.environ.get("AERNHOME_UNLOCK_TOKEN", "")


def _is_internal_request():
    """Check if the request is internal (Tailscale/LAN) or unlocked via cookie.

    Internal: no CF-Connecting-IP header (direct Tailscale/LAN access).
    Unlocked: 'aern_internal' cookie matches the unlock token.
    """
    if request.headers.get("CF-Connecting-IP") is None:
        return True
    return bool(UNLOCK_TOKEN and request.cookies.get("aern_internal") == UNLOCK_TOKEN)


def _is_nexus_allowed():
    """Strict gate for the personal Nexus (goals, books, house, TCG, capture).

    Tailscale-only by design: allow ONLY when there is no CF-Connecting-IP header,
    i.e. the request arrived straight over Tailscale/LAN and NOT through the public
    Cloudflare tunnel. Unlike _is_internal_request(), the unlock cookie does NOT open
    this — there is no way to reach the nexus or its write APIs from the public internet.
    """
    return request.headers.get("CF-Connecting-IP") is None

try:
    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.register_blueprint(fleet.fleet_bp)
app.register_blueprint(second_brain.sb_bp)
app.register_blueprint(ledger.ledger_bp)
app.register_blueprint(vault.vault_bp)


@app.context_processor
def inject_asset_version():
    """Cache-bust /static/css/app.css by its mtime so a freshly rebuilt stylesheet
    (npm run build:css) is fetched immediately instead of serving a stale, browser-
    cached copy — which otherwise makes newly added Tailwind classes render unstyled."""
    try:
        return {"css_ver": int(os.path.getmtime(os.path.join(app.static_folder, "css", "app.css")))}
    except OSError:
        return {"css_ver": ""}


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

# Configuration
DATA_DIR = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
DB_PATH = os.path.join(DATA_DIR, "dashboard.db")
NEXUS_DB_PATH = os.path.join(DATA_DIR, "nexus.db")  # personal nexus (Tailscale-only)
SERVICES_CONFIG_PATH = os.path.join(DATA_DIR, "services.json")
HTTP_TIMEOUT = 5  # seconds

# Built-in fallback service definitions. These are ONLY used to (a) seed
# /data/services.json the very first time the app runs on a host, and (b) as a
# safety net if that file ever goes missing or unparsable mid-life. Day-to-day,
# edit /data/services.json instead of this list — see load_services() below.
DEFAULT_SERVICES = [
    {
        "name": "n8n",
        "display_name": "n8n Workflows",
        "url": "http://n8n:5678/healthz",
        "public_url": "https://ashaman.tail125d67.ts.net:5678",
        "check_type": "both",
        "docker_container": "n8n",
        "icon_emoji": "⚡",
        "enabled": 1,
        # Barely used day-to-day but still running for a couple of legacy n8n
        # automations; kept as a card rather than removed outright. The current
        # dashboard UI has no badge for this flag (display purely cosmetic
        # today) — it exists so the intent is recorded for whoever looks next.
        "deprecated": True,
        "notes": "Barely used; kept alive for legacy automations. Candidate for retirement (see NEXUS_DEPLOY.md Phase 4).",
    },
    {
        "name": "jellyfin",
        "display_name": "Jellyfin Media",
        "url": "http://ashaman.tail125d67.ts.net:8096",
        "public_url": "https://jellyfin.aern.dev",
        "check_type": "http",
        "docker_container": "jellyfin",
        "icon_emoji": "🎬",
        "enabled": 1,
    },
    {
        "name": "qbittorrent",
        "display_name": "qBittorrent",
        # qBittorrent's web UI is only reachable from the home LAN, not over
        # Tailscale — the container running this dashboard (and anyone hitting
        # it remotely) won't be able to reach it. "lan_only" tells the checker
        # to report a failed check as "unknown (LAN-only)" instead of "down",
        # since "down" would be misleading (the service is fine; we just can't
        # see it from here). See check_service_health()/_apply_lan_only().
        "url": "http://192.168.1.118:8080",
        "public_url": "http://192.168.1.118:8080",
        "check_type": "http",
        "docker_container": "qbittorrent",
        "icon_emoji": "🌊",
        "enabled": 1,
        "lan_only": True,
    },
    {
        "name": "open-webui",
        "display_name": "Open WebUI",
        "url": "http://host.docker.internal:3000",
        "public_url": "http://100.110.245.37:3000",
        "check_type": "http",
        "docker_container": "open-webui",
        "icon_emoji": "🧠",
        "enabled": 0,
    },
    {
        "name": "discord-relay",
        "display_name": "Discord Relay",
        "url": None,
        "public_url": None,  # No web interface
        "check_type": "docker",
        "docker_container": "claude-relay",
        "icon_emoji": "🤖",
        "enabled": 1,
    },
    {
        "name": "cloudflared",
        "display_name": "Cloudflare Tunnel",
        "url": None,
        "public_url": None,  # No web interface
        "check_type": "docker",
        "docker_container": "cloudflared-tunnel",
        "icon_emoji": "☁️",
        "enabled": 1,
    },
    {
        "name": "scan-runner",
        "display_name": "Scan Runner",
        "url": None,
        "public_url": None,  # No web interface
        "check_type": "docker",
        "docker_container": "scan-runner",
        "icon_emoji": "📦",
        "enabled": 1,
    },
    {
        "name": "uptime-kuma",
        "display_name": "Uptime Kuma",
        "url": "http://host.docker.internal:3001",
        "public_url": "http://100.110.245.37:3001",
        "check_type": "http",
        "docker_container": "uptime-kuma",
        "icon_emoji": "📊",
        "enabled": 1,
    },
    {
        "name": "home-assistant",
        "display_name": "Home Assistant",
        "url": "http://192.168.1.70:8123",
        "public_url": "http://192.168.1.70:8123",
        "check_type": "http",
        "docker_container": None,
        "icon_emoji": "🏠",
        "enabled": 1,
    },
    {
        "name": "adguard-home",
        "display_name": "AdGuard Home",
        "url": "http://192.168.1.70:3000",
        "public_url": "http://192.168.1.70:3000",
        "check_type": "http",
        "docker_container": None,
        "icon_emoji": "🛡️",
        "enabled": 0,
    },
]

# Schema documentation written as the "_comment" key of the generated
# /data/services.json, so the file is self-describing for whoever edits it.
SERVICES_JSON_SCHEMA_COMMENT = (
    "AernHome dashboard service definitions. Edit this file and restart the "
    "container (`docker restart aernhome-dashboard`) to add, remove, or change a "
    "monitored service card — no code change or rebuild needed. Fields per entry: "
    "name (unique slug, required), display_name (required), url (health-check URL, "
    "or null for docker-only checks), check_type ('http'|'docker'|'both', required), "
    "docker_container (container name for docker checks, or null), icon_emoji "
    "(required), enabled (1 or 0, default 1), public_url (optional — link opened "
    "when the card is clicked by an internal/unlocked client; defaults to null), "
    "lan_only (optional bool — a failed HTTP check reports as 'unknown (LAN-only, "
    "unreachable from here)' instead of 'down', for services only reachable from "
    "the home LAN), deprecated (optional bool, documentation-only — the current UI "
    "has no badge for it), notes (optional free text, documentation-only). "
    "If this file is missing, it is (re)written from the app's built-in defaults on "
    "next startup. If it exists but fails to parse, a warning is logged and the "
    "built-in defaults are used for that run only — this file is left untouched so "
    "you can fix it."
)


def _write_default_services_file():
    """One-time migration: persist DEFAULT_SERVICES to disk so it becomes the
    editable source of truth. Best-effort — if the write fails (e.g. read-only
    mount), we just log it and carry on using the in-memory defaults."""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {"_comment": SERVICES_JSON_SCHEMA_COMMENT, "services": DEFAULT_SERVICES}
    try:
        with open(SERVICES_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote default service config to {SERVICES_CONFIG_PATH}")
    except OSError as e:
        print(f"WARNING: could not write {SERVICES_CONFIG_PATH} ({e}); using built-in defaults for this run")


def load_services():
    """Load service definitions for the dashboard.

    If /data/services.json exists, load service definitions from it. If it
    doesn't exist yet, write the built-in DEFAULT_SERVICES out to that path
    (one-time migration) and load from there. Defensive: any malformed/invalid
    file logs a warning and falls back to the built-in defaults in memory —
    this must never crash startup, and the bad file is left alone on disk so
    it can be inspected/fixed rather than silently overwritten.
    """
    if not os.path.exists(SERVICES_CONFIG_PATH):
        _write_default_services_file()
        return DEFAULT_SERVICES

    try:
        with open(SERVICES_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        services = data.get("services") if isinstance(data, dict) else data
        if not isinstance(services, list) or not services:
            raise ValueError("expected a non-empty 'services' list")
        for s in services:
            if not isinstance(s, dict) or not s.get("name") or not s.get("check_type"):
                raise ValueError(f"service entry missing required 'name'/'check_type': {s!r}")
        return services
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"WARNING: failed to load {SERVICES_CONFIG_PATH} ({e}); falling back to built-in defaults for this run")
        return DEFAULT_SERVICES


# The working service set for this run (loaded from /data/services.json, or
# the built-in defaults — see load_services()). init_db() seeds/updates the DB
# from this; SERVICES_BY_NAME is used at check time to look up display-only
# flags (lan_only, deprecated) that aren't stored in the services table.
SERVICES = load_services()
SERVICES_BY_NAME = {s["name"]: s for s in SERVICES}


def init_db():
    """Initialize SQLite database with services and health_checks tables"""
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create services table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            url TEXT,
            check_type TEXT NOT NULL,
            docker_container TEXT,
            icon_emoji TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create health_checks table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS health_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            response_time_ms INTEGER,
            error_message TEXT,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (service_id) REFERENCES services (id)
        )
    """)

    # Seed services from config (insert new, update existing to match config).
    # .get() with fallbacks throughout: services.json is hand-edited, so a
    # service entry missing an optional key should degrade gracefully rather
    # than crash the whole app at startup.
    for service in SERVICES:
        name = service.get("name")
        if not name:
            continue  # load_services() already validated this, but stay defensive
        display_name = service.get("display_name", name)
        url = service.get("url")
        check_type = service.get("check_type", "http")
        docker_container = service.get("docker_container")
        icon_emoji = service.get("icon_emoji", "🔧")
        enabled = service.get("enabled", 1)

        cursor.execute("SELECT id FROM services WHERE name = ?", (name,))
        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO services (name, display_name, url, check_type, docker_container, icon_emoji, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (name, display_name, url, check_type, docker_container, icon_emoji, enabled),
            )
        else:
            cursor.execute(
                """
                UPDATE services SET display_name=?, url=?, check_type=?, docker_container=?, icon_emoji=?, enabled=?
                WHERE name=?
            """,
                (display_name, url, check_type, docker_container, icon_emoji, enabled, name),
            )

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_nexus_db():
    """Get a connection to the personal nexus DB (writable canonical store)."""
    conn = sqlite3.connect(NEXUS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_nexus_db():
    """Initialize nexus.db — the writable canonical store for the personal nexus.

    Holds the data the nexus OWNS (capture, goals, maintenance, book status, links).
    Read-only sources (inventory.db, Calibre, Todoist, Obsidian) are surfaced live,
    never copied here. Idempotent: CREATE TABLE IF NOT EXISTS.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(NEXUS_DB_PATH)
    cur = conn.cursor()

    # Universal quick-capture / inbox — the friction-free "get it out of my head" box.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS capture (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            area TEXT,                       -- personal|work|house|tcg|null (unfiled)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP           -- set when triaged into a goal/task/maintenance
        )
    """)

    # Goals — personal/work/house/tcg, with progress + a link to the backing doc.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT 'personal',   -- personal|work|house|tcg
            detail TEXT,
            target TEXT,
            progress_pct INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',    -- active|done|parked
            due DATE,
            doc_link TEXT,                            -- Obsidian note / spec / dashboard URL
            sort INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Goal updates — the "living" history of progress notes.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS goal_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            note TEXT,
            progress_pct INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (goal_id) REFERENCES goals (id)
        )
    """)

    # Maintenance — replaces the Notion Maintenance Log. Recurring via interval_days.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS maintenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            category TEXT,                   -- home|car|appliance|yard|medical|other
            due_date DATE,
            interval_days INTEGER,           -- null = one-off; set = recurring
            last_done DATE,
            completed INTEGER DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Book status — writable status layer; seeded from Calibre + Obsidian (Phase 3),
    # canonical for reading/read going forward.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS book_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calibre_id INTEGER,              -- link back to Calibre metadata.db when known
            title TEXT NOT NULL,
            author TEXT,
            status TEXT NOT NULL DEFAULT 'to-read',  -- to-read|reading|read
            rating INTEGER,
            started DATE,
            finished DATE,
            cover_ref TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Notes — pinned scratchpad. Short, persistent, glanceable notes; the sticky-note
    # layer between transient capture and a full Obsidian doc. NOT an Obsidian clone.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            body TEXT NOT NULL,
            area TEXT,                       -- personal|work|house|tcg|null
            pinned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Media status — generalized watch/play tracker (tv|movie|game). Surfaced as the
    # TV/Games shelves; canonical going forward (on-ramp to retiring the Notion Media
    # Tracker). poster_url is localized into DATA_DIR/media_covers/ on add (mirrors
    # book_status.cover_ref) and served through /nexus/media_cover/<id>; it falls
    # back to the remote TMDB/IGDB CDN url when the download failed.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'tv',         -- tv|movie|game
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'want',     -- want|watching|watched
            rating INTEGER,
            progress TEXT,                           -- free text, e.g. "S3E4"
            tmdb_id INTEGER,
            poster_url TEXT,
            overview TEXT,
            year TEXT,
            started DATE,
            finished DATE,
            sort INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Feed — unified stream of incoming generated briefs (n8n digests + Aernbot
    # Notebook + any future source), tagged by source. Read-only/pull; producers
    # POST to /api/nexus/feed. Surfaced as a per-source widget grid on /nexus/feed.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feed_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,            -- slug: weather|optcg|resp|aernbot|...
            title TEXT,
            body TEXT,
            url TEXT,                        -- optional link out
            tag TEXT,                        -- optional sub-grouping (e.g. Aernbot topic)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feed_source_time ON feed_items (source, created_at DESC)")
    # idempotent add for DBs created before the tag column existed
    try:
        cur.execute("ALTER TABLE feed_items ADD COLUMN tag TEXT")
    except sqlite3.OperationalError:
        pass  # column already present

    # Docs — long-form reference content authored in markdown, rendered natively in
    # Nexus. The home for rich formats (tables, headings, playbooks) that outgrow a
    # sticky Note but shouldn't be exiled to Obsidian. body_md is the source of truth;
    # HTML is rendered + sanitized at read time (nexus_md.render_markdown).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,       -- url key, derived from title
            title TEXT NOT NULL,
            body_md TEXT NOT NULL,           -- markdown source (canonical)
            area TEXT,                       -- personal|work|house|tcg|null
            tags TEXT,                       -- free-form, comma-joined (normalized lower)
            pinned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # idempotent add for docs tables created before the tags column existed
    try:
        cur.execute("ALTER TABLE docs ADD COLUMN tags TEXT")
    except sqlite3.OperationalError:
        pass  # column already present

    # Links — curated "connections to documents" (Obsidian notes, specs, dashboards).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            area TEXT,
            sort INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print(f"Nexus database initialized at {NEXUS_DB_PATH}")


def check_http_health(url):
    """
    Check HTTP endpoint health
    Returns: (status, response_time_ms, error_message)
    """
    try:
        start = time.time()
        response = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        response_time = int((time.time() - start) * 1000)

        # 200 or 302 (redirects) count as success
        if response.status_code in [200, 302]:
            return ("up", response_time, None)
        else:
            return ("down", response_time, f"HTTP {response.status_code}")
    except requests.exceptions.Timeout:
        return ("down", HTTP_TIMEOUT * 1000, "Timeout")
    except requests.exceptions.ConnectionError:
        return ("down", None, "Connection refused")
    except Exception as e:
        return ("down", None, str(e))


def check_docker_health(container_name):
    """
    Check Docker container status
    Returns: (status, error_message)
    """
    if not DOCKER_AVAILABLE:
        return ("unknown", "Docker library not available")

    try:
        client = docker.from_env()
        container = client.containers.get(container_name)

        if container.status == "running":
            return ("up", None)
        else:
            return ("down", f"Container status: {container.status}")
    except docker.errors.NotFound:
        return ("down", "Container not found")
    except Exception as e:
        return ("down", str(e))


def check_service_health(service):
    """
    Check overall service health based on check_type
    Returns: dict with status, response_time_ms, error_message
    """
    result = {"status": "unknown", "response_time_ms": None, "error_message": None}

    check_type = service["check_type"]

    # HTTP check
    if check_type in ["http", "both"] and service["url"]:
        http_status, response_time, error = check_http_health(service["url"])
        result["status"] = http_status
        result["response_time_ms"] = response_time
        result["error_message"] = error

    # Docker check (only if HTTP passed or HTTP not applicable)
    if check_type in ["docker", "both"] and service["docker_container"]:
        if check_type == "docker" or result["status"] == "up":
            docker_status, docker_error = check_docker_health(
                service["docker_container"]
            )
            if check_type == "docker":
                result["status"] = docker_status
                result["error_message"] = docker_error
            elif docker_status != "up":
                # HTTP passed but Docker is down - mark as degraded
                result["status"] = "degraded"
                result["error_message"] = docker_error

    return result


def _apply_lan_only(service_name, health):
    """Reinterpret a failed check for a `lan_only`-flagged service.

    Some services (e.g. qBittorrent) are only reachable from the home LAN —
    Tailscale/remote access can't reach them by design. For those, a failed
    HTTP/docker check is expected, not a real outage, so a "down" is
    downgraded to "unknown" with an honest explanation instead of paging
    someone (or just looking alarming) for a service that's actually fine.
    Mutates and returns `health` in place; a no-op for non-lan_only services
    or checks that already came back "up".
    """
    config = SERVICES_BY_NAME.get(service_name, {})
    if config.get("lan_only") and health["status"] == "down":
        health["status"] = "unknown"
        health["error_message"] = "LAN-only, unreachable from here"
    return health


def save_health_check(service_id, status, response_time_ms, error_message):
    """Save health check result to database and prune old records"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO health_checks (service_id, status, response_time_ms, error_message)
        VALUES (?, ?, ?, ?)
    """,
        (service_id, status, response_time_ms, error_message),
    )
    # Prune records older than 7 days
    cursor.execute(
        "DELETE FROM health_checks WHERE checked_at < datetime('now', '-7 days')"
    )
    conn.commit()
    conn.close()


def get_system_stats():
    """
    Get system statistics
    Returns: dict with docker, c_drive, h_drive, i_drive, cpu, ram stats
    """
    stats = {
        "docker": {"running": 0, "total": 0, "error": None},
        "c_drive": {
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0,
            "error": None,
        },
        "g_drive": {
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0,
            "error": None,
        },
        "h_drive": {
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0,
            "error": None,
        },
        "i_drive": {
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0,
            "error": None,
        },
        "cpu": {"percent": 0, "error": None},
        "ram": {"total_gb": 0, "used_gb": 0, "percent": 0, "error": None},
        "bot": {"summary": None, "ts": None, "error": None},
    }

    # Docker stats
    if DOCKER_AVAILABLE:
        try:
            client = docker.from_env()
            containers = client.containers.list(all=True)
            stats["docker"]["total"] = len(containers)
            stats["docker"]["running"] = len(
                [c for c in containers if c.status == "running"]
            )
        except Exception as e:
            stats["docker"]["error"] = str(e)
    else:
        stats["docker"]["error"] = "Docker library not available"

    # C: Drive stats (Ashaman local storage) - mounted as /host_c
    try:
        c_usage = shutil.disk_usage("/host_c")
        stats["c_drive"]["total_gb"] = round(c_usage.total / (1024**3), 1)
        stats["c_drive"]["used_gb"] = round(c_usage.used / (1024**3), 1)
        stats["c_drive"]["free_gb"] = round(c_usage.free / (1024**3), 1)
        stats["c_drive"]["percent"] = round((c_usage.used / c_usage.total) * 100, 1)
    except Exception as e:
        stats["c_drive"]["error"] = str(e)

    # G: Drive stats (Ashaman Docker disk) - mounted as /host_g
    try:
        g_usage = shutil.disk_usage("/host_g")
        stats["g_drive"]["total_gb"] = round(g_usage.total / (1024**3), 1)
        stats["g_drive"]["used_gb"] = round(g_usage.used / (1024**3), 1)
        stats["g_drive"]["free_gb"] = round(g_usage.free / (1024**3), 1)
        stats["g_drive"]["percent"] = round((g_usage.used / g_usage.total) * 100, 1)
    except Exception as e:
        stats["g_drive"]["error"] = str(e)

    # NAS drive stats (Synology) - read from host-side JSON
    try:
        nas_stats_path = os.path.join(
            os.environ.get("DATA_DIR", "/data"), "nas_stats.json"
        )
        with open(nas_stats_path, "r") as f:
            nas = json.load(f)
        for drive_key in ("h_drive", "i_drive"):
            if drive_key in nas:
                drive_data = nas[drive_key]
                if "error" in drive_data:
                    stats[drive_key]["error"] = drive_data["error"]
                else:
                    stats[drive_key]["total_gb"] = drive_data["total_gb"]
                    stats[drive_key]["used_gb"] = drive_data["used_gb"]
                    stats[drive_key]["free_gb"] = drive_data["free_gb"]
                    stats[drive_key]["percent"] = drive_data["percent"]
    except FileNotFoundError:
        stats["h_drive"]["error"] = "NAS stats not yet collected"
        stats["i_drive"]["error"] = "NAS stats not yet collected"
    except Exception as e:
        stats["h_drive"]["error"] = str(e)
        stats["i_drive"]["error"] = str(e)

    # CPU and RAM stats - Get from Docker host info instead of container
    if DOCKER_AVAILABLE:
        try:
            client = docker.from_env()
            info = client.info()

            # CPU - Docker doesn't expose live CPU%, use psutil as fallback
            # This will show container CPU but better than nothing
            try:
                stats["cpu"]["percent"] = round(psutil.cpu_percent(interval=0.1), 1)
            except:
                stats["cpu"]["percent"] = 0
                stats["cpu"]["error"] = "CPU monitoring unavailable"

            # RAM - Get host memory from Docker info
            total_mem_bytes = info.get("MemTotal", 0)
            stats["ram"]["total_gb"] = round(total_mem_bytes / (1024**3), 1)

            # Calculate used memory from Docker stats
            # MemTotal - MemFree (approximation since Docker doesn't expose exact used)
            # Fallback to psutil for more accurate container view
            try:
                mem = psutil.virtual_memory()
                # Use host total from Docker, but calculate used% from actual available
                stats["ram"]["used_gb"] = round(
                    (total_mem_bytes - mem.available) / (1024**3), 1
                )
                stats["ram"]["percent"] = round(
                    (1 - (mem.available / total_mem_bytes)) * 100, 1
                )
            except:
                stats["ram"]["used_gb"] = 0
                stats["ram"]["percent"] = 0
                stats["ram"]["error"] = "RAM monitoring unavailable"

        except Exception as e:
            stats["cpu"]["error"] = str(e)
            stats["ram"]["error"] = str(e)
    else:
        stats["cpu"]["error"] = "Docker not available"
        stats["ram"]["error"] = "Docker not available"

    # Aernbot last task — read from memories.jsonl (claude-workspace volume)
    try:
        last_exchange = None
        with open("/workspace/memories.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "exchange":
                        last_exchange = entry
                except json.JSONDecodeError:
                    continue
        if last_exchange:
            raw = last_exchange.get("summary", "")
            stats["bot"]["summary"] = raw[:60] + ("..." if len(raw) > 60 else "")
            stats["bot"]["ts"] = last_exchange.get("timestamp", "")[:16]
    except FileNotFoundError:
        stats["bot"]["error"] = "no data"
    except Exception as e:
        stats["bot"]["error"] = "unavailable"

    return stats


@app.route("/robots.txt")
def robots_txt():
    return send_from_directory(app.static_folder, "robots.txt", mimetype="text/plain")


@app.route("/sw.js")
def service_worker():
    """Serve the PWA service worker from root so its scope covers the whole app."""
    resp = make_response(
        send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"  # always re-check for SW updates
    return resp


@app.route("/manifest.webmanifest")
def web_manifest():
    """Explicit route guarantees the right content-type regardless of host mimetype config."""
    return send_from_directory(
        app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json"
    )


@app.route("/")
def dashboard():
    """Main dashboard page. ?unlock=<token> sets cookie, ?lock clears it."""
    if UNLOCK_TOKEN and request.args.get("unlock") == UNLOCK_TOKEN:
        resp = make_response(redirect("/"))
        resp.set_cookie("aern_internal", UNLOCK_TOKEN, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
        return resp
    if "lock" in request.args:
        resp = make_response(redirect("/"))
        resp.delete_cookie("aern_internal")
        return resp
    # Over Tailscale the root drops you straight into the Nexus (the thing you
    # actually want at home). ?dash shows this services dashboard instead — that's
    # also where the Nexus "← dashboard" link points, so there's no redirect loop.
    # Public requests arrive via Cloudflare with CF-Connecting-IP, so
    # _is_nexus_allowed() is False and they always get the public dashboard.
    if _is_nexus_allowed() and "dash" not in request.args:
        return redirect("/nexus")
    return render_template("dashboard.html", show_nexus=_is_nexus_allowed())


@app.route("/meal-planner")
def meal_planner():
    """Meal planner embedded page"""
    return render_template("meal-planner.html")


@app.route("/stretch-tracker")
def stretch_tracker():
    """Stretch tracker page"""
    return render_template("stretch-tracker.html")


PODCAST_DIR = os.path.join(os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "podcast")
PODCAST_ALLOWED_EXT = {".mp3", ".jpg", ".jpeg", ".png"}


def load_podcast_data():
    """Load episode metadata from episodes.json, compute file sizes and RFC 2822 dates."""
    json_path = os.path.join(PODCAST_DIR, "episodes.json")
    try:
        with open(json_path, "r") as f:
            episodes = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    for ep in episodes:
        # Compute file size for enclosure length
        filepath = os.path.join(PODCAST_DIR, ep["filename"])
        try:
            ep["file_size"] = os.path.getsize(filepath)
        except OSError:
            ep["file_size"] = 0

        # Convert date to RFC 2822 for RSS
        try:
            dt = datetime.strptime(ep["date"], "%Y-%m-%d")
            ep["pub_date_rfc"] = formatdate(dt.timestamp(), localtime=False, usegmt=True)
        except (ValueError, KeyError):
            ep["pub_date_rfc"] = ""

    return episodes


@app.route("/podcast")
def podcast():
    """Podcast landing page with audio players and subscribe info."""
    episodes = load_podcast_data()
    return render_template("podcast.html", episodes=episodes)


@app.route("/podcast/feed.xml")
def podcast_feed():
    """RSS 2.0 podcast feed with iTunes namespace."""
    episodes = load_podcast_data()
    xml = render_template("podcast-feed.xml", episodes=episodes)
    return Response(xml, mimetype="application/rss+xml")


@app.route("/podcast/<path:filename>")
def podcast_file(filename):
    """Serve podcast media files (MP3s, cover art) with extension whitelist."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in PODCAST_ALLOWED_EXT:
        abort(403)
    return send_from_directory(PODCAST_DIR, filename)


@app.route("/projects")
def projects():
    """Projects overview page"""
    return render_template("projects.html")


# ── Personal Nexus (Tailscale-only) ───────────────────────────────────────────
NEXUS_SECTIONS = [
    ("/nexus",            "Home",        "🏡", "Today at a glance"),
    ("/nexus/feed",       "Feed",        "📡", "Digests & Aernbot notebook"),
    ("/nexus/goals",      "Goals",       "🎯", "Personal · work · house · TCG"),
    ("/nexus/books",      "Books",       "📚", "Reading & read"),
    ("/nexus/tv",         "TV",          "📺", "Watching & watched"),
    ("/nexus/games",      "Games",       "🎮", "Backlog & playing"),
    ("/nexus/notes",      "Notes",       "📝", "Pinned scratchpad"),
    ("/nexus/docs",       "Docs",        "📄", "Reference & playbooks"),
    ("/nexus/house",      "House",       "🏠", "Maintenance & workflows"),
    ("/nexus/tcg",        "TCG",         "🃏", "Business reminders & ops"),
    ("/nexus/infra",      "Infra",       "🛰️", "Homelab health"),
    ("/nexus/fleet",      "Fleet",       "📶", "Aernbot · TCG · infra · host uptime"),
    ("/nexus/aern",       "For Aern",    "🎯", "Everything waiting on you"),
    ("/nexus/queue",      "Queue",       "📮", "You ↔ the fleet"),
    ("/nexus/ledger",     "Ledger",      "📔", "The year, written down"),
    ("/nexus/vault",      "Vault",       "🗄️", "The Obsidian source, read-only"),
]

# Blueprints (second_brain, ledger) render nexus pages outside app.py and pull
# the shared nav from config.
app.config["NEXUS_SECTIONS"] = NEXUS_SECTIONS


@app.route("/nexus")
def nexus_home():
    """Personal nexus landing. Tailscale-only — 404 to anything via Cloudflare.

    Surfaces the read-only "one glance" widgets. Every connector degrades to a safe
    empty value and never raises, so a dead source just renders an empty card.
    """
    if not _is_nexus_allowed():
        abort(404)
    import nexus_sources as ns
    data = {
        "schedule": ns.schedule_today(),
        "oura": ns.oura_summary(),
        "goals": ns.goals_summary(),
        "tasks": ns.todoist_today(),
        "maintenance": ns.maintenance_due(),
        "tcg": ns.tcg_alerts(),
        # prefer the canonical book_status (once seeded); fall back to live Obsidian
        "books": ns_writes.reading_books() or ns.currently_reading(),
        "watching": ns_writes.watching_media(),
        "playing": ns_writes.watching_media(kind="game"),
        "infra": ns.infra_summary(),
    }
    data["captures"] = ns_writes.list_capture(limit=8)
    data["vault_recent"] = vault.recent_notes(5)
    data["links"] = ns_writes.list_links()
    data["notes"] = ns_writes.pinned_notes()
    # source-diverse teaser so once-daily digests aren't buried under Aernbot's volume
    data["feed"] = [{"meta": _feed_meta(f["source"]), **f} for f in ns_writes.latest_feed_diverse(5)]
    return render_template("nexus.html", sections=NEXUS_SECTIONS, active="/nexus", data=data)


# ── Nexus section pages (all Tailscale-only) ──────────────────────────────────
@app.route("/nexus/goals")
def nexus_goals():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_goals.html", sections=NEXUS_SECTIONS, active="/nexus/goals",
                           goals=ns_writes.list_goals())


@app.route("/nexus/house")
def nexus_house():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_house.html", sections=NEXUS_SECTIONS, active="/nexus/house",
                           items=ns_writes.list_maintenance())


@app.route("/nexus/tcg")
def nexus_tcg():
    if not _is_nexus_allowed():
        abort(404)
    import nexus_sources as ns
    return render_template("nexus_tcg.html", sections=NEXUS_SECTIONS, active="/nexus/tcg",
                           tcg=ns.tcg_alerts(), biz=ns.tcg_business(),
                           direct=ns.direct_progress())


@app.route("/nexus/books")
def nexus_books():
    if not _is_nexus_allowed():
        abort(404)
    books = ns_writes.list_books()
    if books:
        shelf = {
            "reading": [b for b in books if b["status"] == "reading"],
            "to-read": [b for b in books if b["status"] == "to-read"],
            "read": [b for b in books if b["status"] == "read"],
        }
        return render_template("nexus_books.html", sections=NEXUS_SECTIONS,
                               active="/nexus/books", shelf=shelf, seeded=True)
    # not seeded yet — show live currently-reading from Obsidian
    import nexus_sources as ns
    return render_template("nexus_books.html", sections=NEXUS_SECTIONS,
                           active="/nexus/books", reading=ns.currently_reading(), seeded=False)


# Cover roots the cover route is allowed to serve from (defense vs path traversal).
# book_covers/ under DATA_DIR is the portable, canonical location (materialized by
# nexus_books_import.py) so covers travel with nexus.db to any host; the Calibre /
# Obsidian roots stay whitelisted for legacy absolute refs on the dev box.
_COVER_ROOTS = [
    os.path.normpath(os.path.join(DATA_DIR, "book_covers")),
    os.path.normpath(os.path.dirname(
        os.environ.get("CALIBRE_DB", r"C:\Users\matth\Calibre Library\metadata.db"))),
    os.path.normpath(os.environ.get("OBSIDIAN_BOOKS", r"C:\Users\matth\Obivault\Books")),
]


@app.route("/nexus/cover/<int:book_id>")
def nexus_cover(book_id):
    """Serve a book's cover. Tailscale-only; resolves the stored cover_ref and only
    serves files under a whitelisted root (or redirects an http(s) cover). Relative
    refs (book_covers/<id>.jpg) resolve under DATA_DIR — portable across hosts."""
    if not _is_nexus_allowed():
        abort(404)
    ref = ns_writes.book_cover_path(book_id)
    if not ref:
        abort(404)
    if str(ref).lower().startswith(("http://", "https://")):
        return redirect(ref)
    path = ref if os.path.isabs(ref) else os.path.join(DATA_DIR, ref)
    path = os.path.normpath(path)
    if not any(path.startswith(root + os.sep) for root in _COVER_ROOTS) or not os.path.isfile(path):
        abort(404)
    return send_from_directory(os.path.dirname(path), os.path.basename(path))


# Root the media-cover route is allowed to serve local files from — same pattern
# as _COVER_ROOTS for books. media_covers/ under DATA_DIR is the only local root
# (no legacy absolute refs here; TV/games posters were always TMDB/IGDB urls).
_MEDIA_COVER_ROOTS = [os.path.normpath(os.path.join(DATA_DIR, "media_covers"))]


@app.route("/nexus/media_cover/<int:mid>")
def nexus_media_cover(mid):
    """Serve a TV/game poster. Tailscale-only; resolves the stored poster_url and
    only serves files under the whitelisted media_covers/ root (or redirects an
    http(s) poster still pending localization / whose download failed).
    Mirrors nexus_cover() for books exactly."""
    if not _is_nexus_allowed():
        abort(404)
    ref = ns_writes.media_poster_path(mid)
    if not ref:
        abort(404)
    if str(ref).lower().startswith(("http://", "https://")):
        return redirect(ref)
    path = ref if os.path.isabs(ref) else os.path.join(DATA_DIR, ref)
    path = os.path.normpath(path)
    if not any(path.startswith(root + os.sep) for root in _MEDIA_COVER_ROOTS) or not os.path.isfile(path):
        abort(404)
    return send_from_directory(os.path.dirname(path), os.path.basename(path))


@app.route("/api/nexus/book/<int:book_id>/status", methods=["POST"])
def api_nexus_book_status(book_id):
    body = _nexus_json()
    try:
        ns_writes.set_book_status(book_id, body.get("status", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/book/<int:book_id>/delete", methods=["POST"])
def api_nexus_book_delete(book_id):
    _nexus_json()
    ns_writes.delete_book(book_id)
    return jsonify({"ok": True})


@app.route("/nexus/infra")
def nexus_infra():
    if not _is_nexus_allowed():
        abort(404)
    import nexus_sources as ns
    return render_template("nexus_infra.html", sections=NEXUS_SECTIONS, active="/nexus/infra",
                           infra=ns.infra_summary())


@app.route("/nexus/fleet")
def nexus_fleet():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_fleet.html", sections=NEXUS_SECTIONS, active="/nexus/fleet")


@app.route("/nexus/tv")
def nexus_tv():
    if not _is_nexus_allowed():
        abort(404)
    shows = ns_writes.list_media(kind="tv")
    shelf = {
        "watching": [m for m in shows if m["status"] == "watching"],
        "want": [m for m in shows if m["status"] == "want"],
        "watched": [m for m in shows if m["status"] == "watched"],
    }
    import nexus_sources as ns
    return render_template("nexus_tv.html", sections=NEXUS_SECTIONS, active="/nexus/tv",
                           shelf=shelf, tmdb=bool(ns._get_tmdb_token()))


@app.route("/nexus/games")
def nexus_games():
    if not _is_nexus_allowed():
        abort(404)
    games = ns_writes.list_media(kind="game")
    shelf = {
        "watching": [m for m in games if m["status"] == "watching"],
        "want": [m for m in games if m["status"] == "want"],
        "watched": [m for m in games if m["status"] == "watched"],
    }
    import nexus_sources as ns
    cid, _ = ns._get_igdb_creds()
    return render_template("nexus_games.html", sections=NEXUS_SECTIONS, active="/nexus/games",
                           shelf=shelf, igdb=bool(cid))


@app.route("/nexus/notes")
def nexus_notes():
    if not _is_nexus_allowed():
        abort(404)
    return render_template("nexus_notes.html", sections=NEXUS_SECTIONS, active="/nexus/notes",
                           notes=ns_writes.list_notes())


@app.route("/nexus/docs")
def nexus_docs():
    if not _is_nexus_allowed():
        abort(404)
    tag = (request.args.get("tag") or "").strip().lower() or None
    return render_template("nexus_docs.html", sections=NEXUS_SECTIONS, active="/nexus/docs",
                           docs=ns_writes.list_docs(tag=tag),
                           all_tags=ns_writes.all_doc_tags(), active_tag=tag)


@app.route("/nexus/docs/<slug>")
def nexus_doc(slug):
    if not _is_nexus_allowed():
        abort(404)
    doc = ns_writes.get_doc(slug)
    if doc is None:
        abort(404)
    doc["html"] = nexus_md.render_markdown(doc["body_md"])
    return render_template("nexus_doc.html", sections=NEXUS_SECTIONS, active="/nexus/docs",
                           doc=doc)


# Display metadata for feed sources (icon + label). Producers send only a slug;
# unknown slugs fall back to a generic icon + title-cased name.
# Declared in display order. Every source here renders on the Feed page even with
# no recent items, so a digest that stopped posting is visibly absent (diagnosable)
# rather than silently missing. (Weather digest retired — Aern doesn't use it.)
FEED_SOURCE_META = {
    "aernbot":     ("🤖", "Aernbot Notebook"),
    "optcg":       ("🏴‍☠️", "OPTCG Digest"),
    "optcg-video": ("📺", "OPTCG Video"),
    "twitter":     ("🐦", "OPTCG Twitter"),
    "resp":        ("🤧", "Resp Illness"),
}


def _feed_meta(slug):
    icon, label = FEED_SOURCE_META.get(slug, ("📡", slug.replace("-", " ").title()))
    return {"slug": slug, "icon": icon, "label": label}


@app.route("/nexus/feed")
def nexus_feed():
    if not _is_nexus_allowed():
        abort(404)
    by_source = ns_writes.feed_sources(per_source=5)
    counts = ns_writes.feed_source_counts()
    # Every KNOWN source in declared order (even empty), then any unknown source
    # that has posted — so a silent digest shows as "no recent items", not absent.
    order = list(FEED_SOURCE_META.keys())
    sources = order + [s for s in by_source if s not in set(order)]
    cards = [{"meta": _feed_meta(s), "entries": by_source.get(s, []),
              "total": counts.get(s, 0)} for s in sources]
    return render_template("nexus_feed.html", sections=NEXUS_SECTIONS, active="/nexus/feed",
                           cards=cards)


@app.route("/nexus/feed/<source>")
def nexus_feed_source(source):
    if not _is_nexus_allowed():
        abort(404)
    items = ns_writes.list_feed_items(source=source, limit=300)
    # Group by tag (topic) when items carry tags; tag groups keep first-seen
    # (recency) order, each with its newest-first items.
    groups, idx = [], {}
    for it in items:
        tag = (it.get("tag") or "Other")
        if tag not in idx:
            idx[tag] = len(groups)
            groups.append({"tag": tag, "entries": []})
        groups[idx[tag]]["entries"].append(it)
    has_tags = any(it.get("tag") for it in items)
    return render_template("nexus_feed_source.html", sections=NEXUS_SECTIONS, active="/nexus/feed",
                           meta=_feed_meta(source), items=items, groups=groups, has_tags=has_tags)


# ── Nexus write APIs (Tailscale-only; JSON in, JSON out) ──────────────────────
def _nexus_json():
    """Require the nexus gate and return the parsed JSON body (or {})."""
    if not _is_nexus_allowed():
        abort(404)
    return request.get_json(silent=True) or {}


@app.route("/api/nexus/capture", methods=["POST"])
def api_nexus_capture():
    body = _nexus_json()
    try:
        cid = ns_writes.add_capture(body.get("text", ""), body.get("area"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": cid})


@app.route("/api/nexus/capture/<int:cid>/process", methods=["POST"])
def api_nexus_capture_process(cid):
    _nexus_json()
    ns_writes.process_capture(cid)
    return jsonify({"ok": True})


@app.route("/api/nexus/capture/<int:cid>/to-goal", methods=["POST"])
def api_nexus_capture_to_goal(cid):
    body = _nexus_json()
    try:
        gid = ns_writes.capture_to_goal(cid, body.get("area", "personal"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "goal_id": gid})


@app.route("/api/nexus/link", methods=["POST"])
def api_nexus_link_create():
    body = _nexus_json()
    try:
        lid = ns_writes.add_link(body.get("label", ""), body.get("url", ""), body.get("area"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": lid})


@app.route("/api/nexus/link/<int:lid>/delete", methods=["POST"])
def api_nexus_link_delete(lid):
    _nexus_json()
    ns_writes.delete_link(lid)
    return jsonify({"ok": True})


@app.route("/api/nexus/goal", methods=["POST"])
def api_nexus_goal_create():
    body = _nexus_json()
    try:
        gid = ns_writes.add_goal(
            body.get("title", ""), body.get("area", "personal"), body.get("detail"),
            body.get("target"), body.get("due"), body.get("doc_link"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": gid})


@app.route("/api/nexus/goal/<int:gid>/progress", methods=["POST"])
def api_nexus_goal_progress(gid):
    body = _nexus_json()
    try:
        ns_writes.update_goal_progress(gid, body.get("progress_pct", 0), body.get("note"))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/goal/<int:gid>/status", methods=["POST"])
def api_nexus_goal_status(gid):
    body = _nexus_json()
    try:
        ns_writes.set_goal_status(gid, body.get("status", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/maintenance", methods=["POST"])
def api_nexus_maint_create():
    body = _nexus_json()
    try:
        mid = ns_writes.add_maintenance(
            body.get("task", ""), body.get("category"), body.get("due_date"),
            body.get("interval_days"), body.get("notes"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": mid})


@app.route("/api/nexus/maintenance/<int:mid>/done", methods=["POST"])
def api_nexus_maint_done(mid):
    _nexus_json()
    try:
        task_name = ns_writes.complete_maintenance(mid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # Crossing off in the Nexus closes the Todoist twin too (the daily push job
    # created it); best-effort — Todoist being down never breaks the crossing.
    todoist_closed = todoist_bridge.close_by_content(
        todoist_bridge.MAINT_PREFIX + task_name)
    return jsonify({"ok": True, "todoist_closed": todoist_closed})


@app.route("/api/nexus/todoist/<task_id>/close", methods=["POST"])
def api_nexus_todoist_close(task_id):
    _nexus_json()
    import nexus_sources as ns
    return jsonify({"ok": ns.todoist_close(task_id)})


# ── Notes ─────────────────────────────────────────────────────────────────────
@app.route("/api/nexus/note", methods=["POST"])
def api_nexus_note_create():
    body = _nexus_json()
    try:
        nid = ns_writes.add_note(body.get("body", ""), body.get("title"), body.get("area"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": nid})


@app.route("/api/nexus/note/<int:nid>", methods=["POST"])
def api_nexus_note_update(nid):
    body = _nexus_json()
    try:
        ns_writes.update_note(nid, body.get("body"), body.get("title"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/note/<int:nid>/pin", methods=["POST"])
def api_nexus_note_pin(nid):
    body = _nexus_json()
    ns_writes.set_note_pinned(nid, bool(body.get("pinned")))
    return jsonify({"ok": True})


@app.route("/api/nexus/note/<int:nid>/delete", methods=["POST"])
def api_nexus_note_delete(nid):
    _nexus_json()
    ns_writes.delete_note(nid)
    return jsonify({"ok": True})


# ── Docs (long-form markdown reference) ───────────────────────────────────────
@app.route("/api/nexus/doc", methods=["POST"])
def api_nexus_doc_create():
    body = _nexus_json()
    try:
        res = ns_writes.add_doc(body.get("title", ""), body.get("body_md", ""),
                                body.get("area"), body.get("tags"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **res})


@app.route("/api/nexus/doc/<int:did>", methods=["POST"])
def api_nexus_doc_update(did):
    body = _nexus_json()
    try:
        ns_writes.update_doc(did, body.get("body_md"), body.get("title"), body.get("tags"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/doc/<int:did>/pin", methods=["POST"])
def api_nexus_doc_pin(did):
    body = _nexus_json()
    ns_writes.set_doc_pinned(did, bool(body.get("pinned")))
    return jsonify({"ok": True})


@app.route("/api/nexus/doc/<int:did>/delete", methods=["POST"])
def api_nexus_doc_delete(did):
    _nexus_json()
    ns_writes.delete_doc(did)
    return jsonify({"ok": True})


# ── Media (TV / movie / game) ─────────────────────────────────────────────────
@app.route("/api/nexus/media", methods=["POST"])
def api_nexus_media_create():
    body = _nexus_json()
    title = (body.get("title") or "").strip()
    kind = body.get("kind", "tv")
    status = body.get("status", "want")
    # Auto-enrich cover + overview when creds are available; degrade gracefully to a
    # title-only entry if not (manual add always works). Games use IGDB (Twitch creds),
    # tv/movie use TMDB.
    import nexus_sources as ns
    if not title:
        hit = {}
    elif kind == "game":
        hit = ns.igdb_search(title)
    else:
        hit = ns.tmdb_search(title, kind)
    try:
        mid = ns_writes.add_media(
            title, kind=kind, status=status,
            tmdb_id=hit.get("tmdb_id"), poster_url=hit.get("poster_url"),
            overview=hit.get("overview"), year=hit.get("year"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": mid, "enriched": bool(hit)})


@app.route("/api/nexus/media/<int:mid>/status", methods=["POST"])
def api_nexus_media_status(mid):
    body = _nexus_json()
    try:
        ns_writes.set_media_status(mid, body.get("status", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/media/<int:mid>/progress", methods=["POST"])
def api_nexus_media_progress(mid):
    body = _nexus_json()
    ns_writes.set_media_progress(mid, body.get("progress", ""))
    return jsonify({"ok": True})


@app.route("/api/nexus/media/<int:mid>/rating", methods=["POST"])
def api_nexus_media_rating(mid):
    body = _nexus_json()
    try:
        ns_writes.set_media_rating(mid, body.get("rating"))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/nexus/media/<int:mid>/delete", methods=["POST"])
def api_nexus_media_delete(mid):
    _nexus_json()
    ns_writes.delete_media(mid)
    return jsonify({"ok": True})


# ── Feed ingest (n8n digests + Aernbot Notebook push to here) ─────────────────
@app.route("/api/nexus/feed", methods=["POST"])
def api_nexus_feed():
    body = _nexus_json()
    try:
        fid = ns_writes.add_feed_item(
            body.get("source", ""), body.get("title"), body.get("body"),
            body.get("url"), body.get("tag"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": fid})


@app.route("/privacy")
def privacy():
    """Privacy policy for Twilio compliance"""
    return """<!DOCTYPE html>
<html><head><title>Privacy Policy - aern.dev</title>
<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;color:#e0e0e0;background:#1a1a2e;line-height:1.6}h1,h2{color:#fff}</style>
</head><body>
<h1>Privacy Policy</h1>
<p><strong>Last updated:</strong> February 20, 2026</p>
<h2>What We Collect</h2>
<p>When you interact with our SMS service, we collect your phone number for the sole purpose of sending and receiving text messages you have opted into.</p>
<h2>How We Use Your Information</h2>
<p>Your phone number is used only to deliver the SMS messages you requested. We do not sell, share, or distribute your personal information to third parties.</p>
<h2>Data Retention</h2>
<p>We retain your phone number only as long as you are subscribed to our messaging service. You may opt out at any time by replying STOP.</p>
<h2>Third-Party Services</h2>
<p>We use Twilio to send and receive SMS messages. Twilio's privacy policy is available at <a href="https://www.twilio.com/legal/privacy" style="color:#7ec8e3">twilio.com/legal/privacy</a>.</p>
<h2>Contact</h2>
<p>For privacy questions, contact us at the number provided in our messages.</p>
</body></html>"""


@app.route("/tc")
def terms():
    """Terms and conditions for Twilio compliance"""
    return """<!DOCTYPE html>
<html><head><title>Terms &amp; Conditions - aern.dev</title>
<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;color:#e0e0e0;background:#1a1a2e;line-height:1.6}h1,h2{color:#fff}</style>
</head><body>
<h1>Terms &amp; Conditions</h1>
<p><strong>Last updated:</strong> February 20, 2026</p>
<h2>SMS Messaging Service</h2>
<p>By opting in to receive SMS messages, you agree to the following terms:</p>
<ul>
<li>Message frequency may vary.</li>
<li>Message and data rates may apply.</li>
<li>Reply <strong>STOP</strong> at any time to unsubscribe.</li>
<li>Reply <strong>HELP</strong> for assistance.</li>
</ul>
<h2>Consent</h2>
<p>By providing your phone number, you consent to receive SMS messages from us. Consent is not a condition of any purchase.</p>
<h2>Liability</h2>
<p>We are not liable for any delays or failures in message delivery. Carriers are not liable for delayed or undelivered messages.</p>
<h2>Changes</h2>
<p>We may update these terms at any time. Continued use of the service constitutes acceptance of updated terms.</p>
<h2>Contact</h2>
<p>For questions about these terms, contact us at the number provided in our messages.</p>
</body></html>"""


@app.route("/api/health")
def api_health():
    """
    API endpoint for service health checks
    Returns: JSON with all service statuses (internal only)
    """
    if not _is_internal_request():
        return jsonify({"status": "ok"})
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM services WHERE enabled = 1")
    services = [dict(row) for row in cursor.fetchall()]

    # Fetch last 24h of health checks for sparklines (one query for all services)
    cursor.execute("""
        SELECT service_id, status, checked_at
        FROM health_checks
        WHERE checked_at >= datetime('now', '-24 hours')
        ORDER BY checked_at ASC
    """)
    sparkline_rows = cursor.fetchall()
    conn.close()

    # Group sparkline data by service_id
    sparklines = {}
    for row in sparkline_rows:
        sid = row["service_id"]
        if sid not in sparklines:
            sparklines[sid] = []
        sparklines[sid].append(row["status"] == "up")

    # Internal clients (Tailscale/LAN) get clickable service links; public internet gets none
    show_links = _is_internal_request()
    public_urls = {s["name"]: s.get("public_url") for s in SERVICES}

    results = []
    for service in services:
        health = check_service_health(service)
        health = _apply_lan_only(service["name"], health)

        # Save health check to database
        save_health_check(
            service["id"],
            health["status"],
            health["response_time_ms"],
            health["error_message"],
        )

        results.append(
            {
                "id": service["id"],
                "name": service["name"],
                "display_name": service["display_name"],
                "public_url": public_urls.get(service["name"]) if show_links else None,
                "icon_emoji": service["icon_emoji"],
                "status": health["status"],
                "response_time_ms": health["response_time_ms"],
                "error_message": health["error_message"],
                "sparkline": sparklines.get(service["id"], []),
            }
        )

    return jsonify(results)


@app.route("/api/stats")
def api_stats():
    """
    API endpoint for system stats
    Returns: JSON with docker, disk, cpu, ram stats (internal only)
    Error messages are sanitized to avoid leaking internal paths.
    """
    if not _is_internal_request():
        return jsonify({"status": "ok"})
    stats = get_system_stats()
    # Sanitize error messages — replace detailed errors with generic ones
    for key in stats:
        if isinstance(stats[key], dict) and stats[key].get("error"):
            stats[key]["error"] = "unavailable"
    return jsonify(stats)


@app.route("/api/tcg-stats")
def api_tcg_stats():
    """TRMNL TCG Business plugin payload.

    Reads inventory.db (read-only) via the vendored fetcher in /trmnl_scripts.
    Returns the same merge_variables shape the TRMNL Liquid template expects.
    Internal only — no external exposure.
    """
    if not _is_internal_request():
        return jsonify({"status": "ok"})

    try:
        from tcg_plugin_data import build_payload  # vendored alongside app.py
    except ImportError as e:
        return jsonify({"error": "fetcher unavailable", "detail": str(e)}), 500

    inv_db = os.environ.get("TCG_DB_PATH", "/tcg/inventory.db")
    aer_db = os.environ.get("AERNBOT_DB_PATH", "/tcg/aernbot.db")
    try:
        payload = build_payload(inventory_db=inv_db, aernbot_db=aer_db)
    except sqlite3.OperationalError:
        return jsonify({"error": "inventory.db unavailable"}), 503
    except Exception as e:
        return jsonify({"error": "fetch failed", "detail": str(e)[:200]}), 500
    return jsonify({"merge_variables": payload})


# 72 Japanese micro-seasons (七十二候)
# Each entry: (month, day_start, day_end, number, kanji, romaji, english,
#              solar_term, solar_term_romaji, solar_term_english, pentad, season)
# day_end is inclusive. Seasons that cross month boundaries use day_end=31/32 as
# a sentinel — the lookup function handles the boundary crossing logic.
_MICRO_SEASONS = [
    # --- Spring ---
    (2,  4,  8,  1, "東風解凍", "Harukaze kōri o toku",         "East wind melts the ice",           "立春", "Risshun", "Beginning of Spring", 1, "Spring"),
    (2,  9, 13,  2, "黄鶯睍睆", "Kōō kenkan su",                "Bush warblers start singing",        "立春", "Risshun", "Beginning of Spring", 2, "Spring"),
    (2, 14, 18,  3, "魚上氷",   "Uo kōri o izuru",              "Fish emerge from the ice",           "立春", "Risshun", "Beginning of Spring", 3, "Spring"),
    (2, 19, 23,  4, "土脉潤起", "Tsuchi no shō uruoi okoru",    "Rain moistens the soil",             "雨水", "Usui",    "Rain Water",          1, "Spring"),
    (2, 24, 28,  5, "霞始靆",   "Kasumi hajimete tanabiku",     "Mist starts to linger",              "雨水", "Usui",    "Rain Water",          2, "Spring"),
    (3,  1,  5,  6, "草木萠動", "Sōmoku mebae izuru",           "Grass sprouts, trees bud",           "雨水", "Usui",    "Rain Water",          3, "Spring"),
    (3,  6, 10,  7, "蟄虫啓戸", "Sugomori mushi to o hiraku",   "Hibernating insects surface",        "啓蟄", "Keichitsu","Awakening of Insects",1, "Spring"),
    (3, 11, 15,  8, "桃始笑",   "Momo hajimete saku",           "First peach blossoms",               "啓蟄", "Keichitsu","Awakening of Insects",2, "Spring"),
    (3, 16, 20,  9, "菜虫化蝶", "Namushi chō to naru",          "Caterpillars become butterflies",    "啓蟄", "Keichitsu","Awakening of Insects",3, "Spring"),
    (3, 21, 25, 10, "雀始巣",   "Suzume hajimete sukū",         "Sparrows start to nest",             "春分", "Shunbun", "Spring Equinox",      1, "Spring"),
    (3, 26, 30, 11, "櫻始開",   "Sakura hajimete saku",         "First cherry blossoms",              "春分", "Shunbun", "Spring Equinox",      2, "Spring"),
    # Mar 31 – Apr 4 (crosses month boundary; stored as month=3, day_start=31, day_end=35 sentinel)
    (3, 31, 35, 12, "雷乃発声", "Kaminari sunawachi koe o hassu","Distant thunder",                   "春分", "Shunbun", "Spring Equinox",      3, "Spring"),
    (4,  5,  9, 13, "玄鳥至",   "Tsubame kitaru",               "Swallows return",                    "清明", "Seimei",  "Pure Brightness",     1, "Spring"),
    (4, 10, 14, 14, "鴻雁北",   "Kōgan kaeru",                  "Wild geese fly north",               "清明", "Seimei",  "Pure Brightness",     2, "Spring"),
    (4, 15, 19, 15, "虹始見",   "Niji hajimete arawaru",        "First rainbows",                     "清明", "Seimei",  "Pure Brightness",     3, "Spring"),
    (4, 20, 24, 16, "葭始生",   "Ashi hajimete shōzu",          "First reeds sprout",                 "穀雨", "Kokuu",   "Grain Rain",          1, "Spring"),
    (4, 25, 29, 17, "霜止出苗", "Shimo yamite nae izuru",       "Last frost, rice seedlings grow",    "穀雨", "Kokuu",   "Grain Rain",          2, "Spring"),
    # Apr 30 – May 4 (crosses month boundary; stored as month=4, day_start=30, day_end=35 sentinel)
    (4, 30, 35, 18, "牡丹華",   "Botan hana saku",              "Peonies bloom",                      "穀雨", "Kokuu",   "Grain Rain",          3, "Spring"),
    # --- Summer ---
    (5,  5,  9, 19, "蛙始鳴",   "Kawazu hajimete naku",         "Frogs start singing",                "立夏", "Rikka",   "Beginning of Summer", 1, "Summer"),
    (5, 10, 14, 20, "蚯蚓出",   "Mimizu izuru",                 "Worms surface",                      "立夏", "Rikka",   "Beginning of Summer", 2, "Summer"),
    (5, 15, 20, 21, "竹笋生",   "Takenoko shōzu",               "Bamboo shoots sprout",               "立夏", "Rikka",   "Beginning of Summer", 3, "Summer"),
    (5, 21, 25, 22, "蚕起食桑", "Kaiko okite kuwa o hamu",      "Silkworms feast on mulberry",        "小満", "Shōman",  "Lesser Fullness",     1, "Summer"),
    (5, 26, 30, 23, "紅花栄",   "Benibana sakau",               "Safflowers bloom",                   "小満", "Shōman",  "Lesser Fullness",     2, "Summer"),
    # May 31 – Jun 5 (crosses month boundary; stored as month=5, day_start=31, day_end=36 sentinel)
    (5, 31, 36, 24, "麦秋至",   "Mugi no toki itaru",           "Wheat ripens",                       "小満", "Shōman",  "Lesser Fullness",     3, "Summer"),
    (6,  6, 10, 25, "蟷螂生",   "Kamakiri shōzu",               "Praying mantises hatch",             "芒種", "Bōshu",   "Grain in Ear",        1, "Summer"),
    (6, 11, 15, 26, "腐草為蛍", "Kusaretaru kusa hotaru to naru","Fireflies emerge",                  "芒種", "Bōshu",   "Grain in Ear",        2, "Summer"),
    (6, 16, 20, 27, "梅子黄",   "Ume no mi kibamu",             "Plums turn yellow",                  "芒種", "Bōshu",   "Grain in Ear",        3, "Summer"),
    (6, 21, 26, 28, "乃東枯",   "Natsukarekusa karuru",         "Self-heal withers",                  "夏至", "Geshi",   "Summer Solstice",     1, "Summer"),
    # Jun 27 – Jul 1 (crosses month boundary; stored as month=6, day_start=27, day_end=32 sentinel)
    (6, 27, 32, 29, "菖蒲華",   "Ayame hana saku",              "Irises bloom",                       "夏至", "Geshi",   "Summer Solstice",     2, "Summer"),
    (7,  2,  6, 30, "半夏生",   "Hange shōzu",                  "Crow-dipper sprouts",                "夏至", "Geshi",   "Summer Solstice",     3, "Summer"),
    (7,  7, 11, 31, "温風至",   "Atsukaze itaru",               "Warm winds blow",                    "小暑", "Shōsho",  "Lesser Heat",         1, "Summer"),
    (7, 12, 16, 32, "蓮始開",   "Hasu hajimete hiraku",         "Lotus flowers bloom",                "小暑", "Shōsho",  "Lesser Heat",         2, "Summer"),
    (7, 17, 22, 33, "鷹乃学習", "Taka sunawachi waza o narau",  "Hawks learn to fly",                 "小暑", "Shōsho",  "Lesser Heat",         3, "Summer"),
    (7, 23, 28, 34, "桐始結花", "Kiri hajimete hana o musubu",  "Paulownia trees flower",             "大暑", "Taisho",  "Greater Heat",        1, "Summer"),
    # Jul 29 – Aug 2 (crosses month boundary; stored as month=7, day_start=29, day_end=33 sentinel)
    (7, 29, 33, 35, "土潤溽暑", "Tsuchi uruōte mushi atsushi",  "Earth is damp, air humid",           "大暑", "Taisho",  "Greater Heat",        2, "Summer"),
    (8,  3,  6, 36, "大雨時行", "Taiu tokidoki furu",           "Great rains sometimes fall",          "大暑", "Taisho",  "Greater Heat",        3, "Summer"),
    # --- Autumn ---
    (8,  7, 11, 37, "涼風至",   "Suzukaze itaru",               "Cool winds arrive",                  "立秋", "Risshū",  "Beginning of Autumn", 1, "Autumn"),
    (8, 12, 16, 38, "寒蝉鳴",   "Higurashi naku",               "Evening cicadas sing",               "立秋", "Risshū",  "Beginning of Autumn", 2, "Autumn"),
    (8, 17, 22, 39, "蒙霧升降", "Fukaki kiri matō",             "Dense fog descends",                 "立秋", "Risshū",  "Beginning of Autumn", 3, "Autumn"),
    (8, 23, 27, 40, "綿柎開",   "Wata no hana shibe hiraku",    "Cotton flowers bloom",               "処暑", "Shosho",  "End of Heat",         1, "Autumn"),
    # Aug 28 – Sep 1 (crosses month boundary; stored as month=8, day_start=28, day_end=32 sentinel)
    (8, 28, 32, 41, "天地始粛", "Tenchi hajimete samushi",      "Heat begins to subside",             "処暑", "Shosho",  "End of Heat",         2, "Autumn"),
    (9,  2,  7, 42, "禾乃登",   "Kokumono sunawachi minoru",    "Rice ripens",                        "処暑", "Shosho",  "End of Heat",         3, "Autumn"),
    (9,  8, 12, 43, "草露白",   "Kusa no tsuyu shiroshi",       "Dew glistens white on grass",        "白露", "Hakuro",  "White Dew",           1, "Autumn"),
    (9, 13, 17, 44, "鶺鴒鳴",   "Sekirei naku",                 "Wagtails sing",                      "白露", "Hakuro",  "White Dew",           2, "Autumn"),
    (9, 18, 22, 45, "玄鳥去",   "Tsubame saru",                 "Swallows leave",                     "白露", "Hakuro",  "White Dew",           3, "Autumn"),
    (9, 23, 27, 46, "雷乃収声", "Kaminari sunawachi koe o osamu","Thunder ceases",                    "秋分", "Shūbun",  "Autumn Equinox",      1, "Autumn"),
    # Sep 28 – Oct 2 (crosses month boundary; stored as month=9, day_start=28, day_end=32 sentinel)
    (9, 28, 32, 47, "蟄虫坏戸", "Mushi kakurete to o fusagu",   "Insects hide and seal doors",        "秋分", "Shūbun",  "Autumn Equinox",      2, "Autumn"),
    (10, 3,  7, 48, "水始涸",   "Mizu hajimete karuru",         "Farmers drain fields",               "秋分", "Shūbun",  "Autumn Equinox",      3, "Autumn"),
    (10, 8, 12, 49, "鴻雁来",   "Kōgan kitaru",                 "Wild geese return",                  "寒露", "Kanro",   "Cold Dew",            1, "Autumn"),
    (10,13, 17, 50, "菊花開",   "Kiku no hana hiraku",          "Chrysanthemums bloom",               "寒露", "Kanro",   "Cold Dew",            2, "Autumn"),
    (10,18, 22, 51, "蟋蟀在戸", "Kirigirisu to ni ari",         "Crickets chirp by the door",         "寒露", "Kanro",   "Cold Dew",            3, "Autumn"),
    (10,23, 27, 52, "霜始降",   "Shimo hajimete furu",          "First frost",                        "霜降", "Sōkō",    "Frost Falls",         1, "Autumn"),
    # Oct 28 – Nov 1 (crosses month boundary; stored as month=10, day_start=28, day_end=32 sentinel)
    (10,28, 32, 53, "霎時施",   "Kosame tokidoki furu",         "Light rains sometimes fall",         "霜降", "Sōkō",    "Frost Falls",         2, "Autumn"),
    (11, 2,  6, 54, "楓蔦黄",   "Momiji tsuta kibamu",          "Maples and ivy turn yellow",         "霜降", "Sōkō",    "Frost Falls",         3, "Autumn"),
    # --- Winter ---
    (11, 7, 11, 55, "山茶始開", "Tsubaki hajimete hiraku",      "Camellias bloom",                    "立冬", "Rittō",   "Beginning of Winter", 1, "Winter"),
    (11,12, 16, 56, "地始凍",   "Chi hajimete kōru",            "Ground starts to freeze",            "立冬", "Rittō",   "Beginning of Winter", 2, "Winter"),
    (11,17, 21, 57, "金盞香",   "Kinsenka saku",                "Daffodils bloom",                    "立冬", "Rittō",   "Beginning of Winter", 3, "Winter"),
    (11,22, 26, 58, "虹蔵不見", "Niji kakurete miezu",          "Rainbows hide",                      "小雪", "Shōsetsu","Lesser Snow",         1, "Winter"),
    # Nov 27 – Dec 1 (crosses month boundary; stored as month=11, day_start=27, day_end=32 sentinel)
    (11,27, 32, 59, "朔風払葉", "Kitakaze konoha o harau",      "North wind blows leaves",            "小雪", "Shōsetsu","Lesser Snow",         2, "Winter"),
    (12, 2,  6, 60, "橘始黄",   "Tachibana hajimete kibamu",    "Mandarin oranges turn yellow",       "小雪", "Shōsetsu","Lesser Snow",         3, "Winter"),
    (12, 7, 11, 61, "閉塞成冬", "Sora samuku fuyu to naru",     "Cold sets in, winter arrives",       "大雪", "Taisetsu","Greater Snow",        1, "Winter"),
    (12,12, 16, 62, "熊蟄穴",   "Kuma ana ni komoru",           "Bears retreat to dens",              "大雪", "Taisetsu","Greater Snow",        2, "Winter"),
    (12,17, 21, 63, "鱖魚群",   "Sake no uo muragaru",          "Salmon gather in rivers",            "大雪", "Taisetsu","Greater Snow",        3, "Winter"),
    (12,22, 26, 64, "乃東生",   "Natsukarekusa shōzu",          "Self-heal sprouts",                  "冬至", "Tōji",    "Winter Solstice",     1, "Winter"),
    (12,27, 31, 65, "麋角解",   "Sawashika no tsuno otsuru",    "Deer shed antlers",                  "冬至", "Tōji",    "Winter Solstice",     2, "Winter"),
    # Jan 1-4 wraps to next year; stored as month=12, day_start=32, day_end=35 sentinel
    (12,32, 35, 66, "雪下出麦", "Yuki watarite mugi nobiru",    "Wheat sprouts under snow",           "冬至", "Tōji",    "Winter Solstice",     3, "Winter"),
    (1,  5,  9, 67, "芹乃栄",   "Seri sunawachi sakau",         "Parsley flourishes",                 "小寒", "Shōkan",  "Lesser Cold",         1, "Winter"),
    (1, 10, 14, 68, "水泉動",   "Shimizu atataka o fukumu",     "Springs thaw",                       "小寒", "Shōkan",  "Lesser Cold",         2, "Winter"),
    (1, 15, 19, 69, "雉始雊",   "Kiji hajimete naku",           "Pheasants start to call",            "小寒", "Shōkan",  "Lesser Cold",         3, "Winter"),
    (1, 20, 24, 70, "款冬華",   "Fuki no hana saku",            "Butterburs bud",                     "大寒", "Daikan",  "Greater Cold",        1, "Winter"),
    (1, 25, 29, 71, "水沢腹堅", "Sawamizu kōri tsumeru",        "Ice thickens on streams",            "大寒", "Daikan",  "Greater Cold",        2, "Winter"),
    # Jan 30 – Feb 3 (crosses month boundary; stored as month=1, day_start=30, day_end=34 sentinel)
    (1, 30, 34, 72, "鶏始乳",   "Niwatori hajimete toya ni tsuku","Hens begin to lay",               "大寒", "Daikan",  "Greater Cold",        3, "Winter"),
]

# Lookup table: maps (month, day_of_month) to season index
# Built at module load so the route itself is O(1)
_SEASON_BY_MONTH_DAY: dict[tuple[int, int], int] = {}

for _i, _s in enumerate(_MICRO_SEASONS):
    _m, _d_start, _d_end = _s[0], _s[1], _s[2]
    for _d in range(_d_start, _d_end + 1):
        # Sentinel days beyond the real month end map to the next month's early days
        _days_in_month = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                          7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
        _real_max = _days_in_month.get(_m, 31)
        if _d <= _real_max:
            _SEASON_BY_MONTH_DAY[(_m, _d)] = _i
        else:
            # Overflow into the next month
            _next_m = (_m % 12) + 1
            _next_d = _d - _real_max
            _SEASON_BY_MONTH_DAY[(_next_m, _next_d)] = _i


def _get_current_micro_season(month: int, day: int) -> dict:
    """
    Return the micro-season dict for the given month and day.

    Args:
        month: Calendar month (1-12).
        day: Day of month (1-31).

    Returns:
        Dict with all micro-season fields, or an error dict if not found.
    """
    idx = _SEASON_BY_MONTH_DAY.get((month, day))
    if idx is None:
        return {"error": f"No micro-season found for {month}/{day}"}

    s = _MICRO_SEASONS[idx]
    (m, d_start, d_end, number, kanji, romaji, english,
     solar_term, solar_term_romaji, solar_term_english, pentad, season) = s

    # Build human-readable start/end using the canonical spec dates, not sentinels
    _month_abbr = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                   7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    _days_in = {1:31,2:28,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}

    real_start_d = d_start if d_start <= _days_in.get(m, 31) else d_start - _days_in.get(m, 31)
    real_start_m = m if d_start <= _days_in.get(m, 31) else (m % 12) + 1

    real_end_d = d_end if d_end <= _days_in.get(m, 31) else d_end - _days_in.get(m, 31)
    real_end_m = m if d_end <= _days_in.get(m, 31) else (m % 12) + 1

    start_str = f"{_month_abbr[real_start_m]} {real_start_d}"
    end_str   = f"{_month_abbr[real_end_m]} {real_end_d}"

    return {
        "number": number,
        "total": 72,
        "kanji": kanji,
        "romaji": romaji,
        "english": english,
        "start": start_str,
        "end": end_str,
        "solar_term": solar_term,
        "solar_term_romaji": solar_term_romaji,
        "solar_term_english": solar_term_english,
        "pentad": pentad,
        "season": season,
    }


@app.route("/api/season")
def api_season():
    """
    API endpoint for the current Japanese 72 micro-season (七十二候).
    Returns: JSON with season number, kanji, romaji, English description,
             date range, solar term, pentad, and astronomical season.
    """
    today = datetime.now()
    result = _get_current_micro_season(today.month, today.day)
    return jsonify(result)


if __name__ == "__main__":
    init_db()
    init_nexus_db()
    fleet.start_sentinel()
    # Bind to 0.0.0.0 to allow external access (Tailscale)
    app.run(host="0.0.0.0", port=5555, debug=False)
