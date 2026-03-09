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

try:
    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

# Configuration
DATA_DIR = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
DB_PATH = os.path.join(DATA_DIR, "dashboard.db")
HTTP_TIMEOUT = 5  # seconds

# Default services configuration
DEFAULT_SERVICES = [
    {
        "name": "n8n",
        "display_name": "n8n Workflows",
        "url": "https://ashaman-1.tail125d67.ts.net:5678",
        "public_url": "https://ashaman-1.tail125d67.ts.net:5678",
        "check_type": "both",
        "docker_container": "n8n",
        "icon_emoji": "⚡",
        "enabled": 1,
    },
    {
        "name": "jellyfin",
        "display_name": "Jellyfin Media",
        "url": "http://ashaman-1.tail125d67.ts.net:8096",
        "public_url": "https://jellyfin.aern.dev",
        "check_type": "http",
        "docker_container": "jellyfin",
        "icon_emoji": "🎬",
        "enabled": 1,
    },
    {
        "name": "qbittorrent",
        "display_name": "qBittorrent",
        "url": "http://100.73.108.55:8080",
        "public_url": "http://100.73.108.55:8080",
        "check_type": "http",
        "docker_container": "qbittorrent",
        "icon_emoji": "🌊",
        "enabled": 1,
    },
    {
        "name": "open-webui",
        "display_name": "Open WebUI",
        "url": "http://host.docker.internal:3000",
        "public_url": "http://100.110.245.37:3000",
        "check_type": "http",
        "docker_container": "open-webui",
        "icon_emoji": "🧠",
        "enabled": 1,
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
        "url": "http://100.110.245.37:3002",
        "public_url": "http://100.110.245.37:3002",
        "check_type": "http",
        "docker_container": None,
        "icon_emoji": "🛡️",
        "enabled": 1,
    },
]


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

    # Seed default services (insert any missing)
    for service in DEFAULT_SERVICES:
        cursor.execute("SELECT id FROM services WHERE name = ?", (service["name"],))
        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO services (name, display_name, url, check_type, docker_container, icon_emoji, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    service["name"],
                    service["display_name"],
                    service["url"],
                    service["check_type"],
                    service["docker_container"],
                    service["icon_emoji"],
                    service["enabled"],
                ),
            )

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    return render_template("dashboard.html")


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
    public_urls = {s["name"]: s["public_url"] for s in DEFAULT_SERVICES}

    results = []
    for service in services:
        health = check_service_health(service)

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
    # Bind to 0.0.0.0 to allow external access (Tailscale)
    app.run(host="0.0.0.0", port=5555, debug=False)
