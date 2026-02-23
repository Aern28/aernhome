"""
AernHome Dashboard - Self-hosted home services dashboard
Flask backend with service health checks and system stats
"""

import os
import json
import time
import sqlite3
from flask import Flask, render_template, jsonify
import requests
import psutil
import shutil

try:
    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

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
        "public_url": "http://ashaman-1.tail125d67.ts.net:8096",
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
        "name": "whisper",
        "display_name": "Whisper Server",
        "url": "http://ashaman-1.tail125d67.ts.net:8100",
        "public_url": "http://ashaman-1.tail125d67.ts.net:8100",
        "check_type": "both",
        "docker_container": "whisper-server",
        "icon_emoji": "🎤",
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
    """Save health check result to database"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO health_checks (service_id, status, response_time_ms, error_message)
        VALUES (?, ?, ?, ?)
    """,
        (service_id, status, response_time_ms, error_message),
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

    return stats


@app.route("/")
def dashboard():
    """Main dashboard page"""
    return render_template("dashboard.html")


@app.route("/meal-planner")
def meal_planner():
    """Meal planner embedded page"""
    return render_template("meal-planner.html")


@app.route("/stretch-tracker")
def stretch_tracker():
    """Stretch tracker page"""
    return render_template("stretch-tracker.html")


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
    Returns: JSON with all service statuses
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM services WHERE enabled = 1")
    services = [dict(row) for row in cursor.fetchall()]
    conn.close()

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

        # Find public_url from DEFAULT_SERVICES config
        public_url = None
        for default_svc in DEFAULT_SERVICES:
            if default_svc["name"] == service["name"]:
                public_url = default_svc.get("public_url")
                break

        results.append(
            {
                "id": service["id"],
                "name": service["name"],
                "display_name": service["display_name"],
                "url": service["url"],
                "public_url": public_url,  # Add public URL for user clicks
                "icon_emoji": service["icon_emoji"],
                "status": health["status"],
                "response_time_ms": health["response_time_ms"],
                "error_message": health["error_message"],
            }
        )

    return jsonify(results)


@app.route("/api/stats")
def api_stats():
    """
    API endpoint for system stats
    Returns: JSON with docker, disk, cpu, ram stats
    """
    stats = get_system_stats()
    return jsonify(stats)


if __name__ == "__main__":
    init_db()
    # Bind to 0.0.0.0 to allow external access (Tailscale)
    app.run(host="0.0.0.0", port=5555, debug=False)
