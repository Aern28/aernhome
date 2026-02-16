"""
AernHome Dashboard - Self-hosted home services dashboard
Flask backend with service health checks and system stats
"""

import os
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
        "url": "http://100.110.245.37:5678",  # Health check via Tailscale
        "public_url": "http://100.110.245.37:5678",  # User access URL
        "check_type": "both",
        "docker_container": "n8n",
        "icon_emoji": "⚡",
        "enabled": 1,
    },
    {
        "name": "jellyfin",
        "display_name": "Jellyfin Media",
        "url": "http://100.110.245.37:8096",
        "public_url": "http://100.110.245.37:8096",
        "check_type": "docker",  # Changed to docker-only check since Jellyfin not running
        "docker_container": "jellyfin",
        "icon_emoji": "🎬",
        "enabled": 0,  # Disabled since not running
    },
    {
        "name": "whisper",
        "display_name": "Whisper Server",
        "url": "http://100.110.245.37:8100",  # Health check via Tailscale
        "public_url": "http://100.110.245.37:8100",
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

    # Seed default services if table is empty
    cursor.execute("SELECT COUNT(*) FROM services")
    if cursor.fetchone()[0] == 0:
        for service in DEFAULT_SERVICES:
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
    Returns: dict with docker, c_drive, h_drive, cpu, ram stats
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

    # H: Drive stats (Synology NAS) - mounted as /host_h
    try:
        h_usage = shutil.disk_usage("/host_h")
        stats["h_drive"]["total_gb"] = round(h_usage.total / (1024**3), 1)
        stats["h_drive"]["used_gb"] = round(h_usage.used / (1024**3), 1)
        stats["h_drive"]["free_gb"] = round(h_usage.free / (1024**3), 1)
        stats["h_drive"]["percent"] = round((h_usage.used / h_usage.total) * 100, 1)
    except Exception as e:
        stats["h_drive"]["error"] = str(e)

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
            total_mem_bytes = info.get('MemTotal', 0)
            stats["ram"]["total_gb"] = round(total_mem_bytes / (1024**3), 1)

            # Calculate used memory from Docker stats
            # MemTotal - MemFree (approximation since Docker doesn't expose exact used)
            # Fallback to psutil for more accurate container view
            try:
                mem = psutil.virtual_memory()
                # Use host total from Docker, but calculate used% from actual available
                stats["ram"]["used_gb"] = round((total_mem_bytes - mem.available) / (1024**3), 1)
                stats["ram"]["percent"] = round((1 - (mem.available / total_mem_bytes)) * 100, 1)
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


@app.route("/projects")
def projects():
    """Projects overview page"""
    return render_template("projects.html")


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
