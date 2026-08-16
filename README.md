# AernHome Dashboard

Self-hosted home services dashboard for monitoring and managing Docker services, system stats, and embedded apps.

## Features

- **Service Monitoring**: Real-time health checks defined in `/data/services.json` — n8n, Jellyfin, qBittorrent, Discord relay, Cloudflare Tunnel, Home Assistant, and more (Uptime Kuma retired 2026-08-03)
- **System Stats**: Docker container counts, disk usage, CPU, and RAM monitoring
- **Embedded Apps**: Meal planner with drag-and-drop interface
- **Auto-refresh**: Dashboard updates every 30 seconds
- **Dark Theme**: Easy-on-the-eyes Tailwind-based UI

## Architecture

- **Backend**: Flask (Python 3.11)
- **Frontend**: Tailwind CDN + Vanilla JavaScript (no build step)
- **Database**: SQLite, bind-mounted from the host at `C:/projects/aernhome/data`
  (container path `/data`) — `dashboard.db` and `nexus.db` both live there. Not
  on the NAS; the NAS (`H:`/`I:` drives) is only used for disk-usage stats and
  the nexus backup destination (see below).
- **Docker**: Runs in Docker Desktop on Ashaman
- **Health Checks**: HTTP endpoints + Docker API integration, driven by
  `/data/services.json` (see [Configuration](#configuration) below)

## Setup

### Prerequisites

- Docker Desktop running on Ashaman
- Tailscale for remote access
- (Optional) H: drive mapped to `\\192.168.1.118\home` (Synology NAS) — only
  needed for the H:/I: disk-usage cards and nexus backups, not for the app's
  own database

### Initial Deployment (on Ashaman)

```powershell
# Clone repository
cd C:\projects
git clone https://github.com/Aern28/aernhome.git
cd aernhome

# Data directory is created automatically on first run at
# C:\projects\aernhome\data (bind-mounted to /data in the container)

# Build and start container
docker compose build
docker compose up -d

# View logs
docker compose logs -f
```

### Update Workflow

The container bind-mounts the repo checkout straight into `/app` (see
`docker-compose.yml`), so most changes need **no rebuild** — just a restart:

```powershell
cd C:\projects\aernhome
git pull
docker restart aernhome-dashboard
```

Only rebuild the image when `requirements.txt` (Python dependencies) changes:

```powershell
docker compose build aernhome
docker compose up -d
```

**Dev host**: Trainer (desktop) is the current dev host — NenTera (laptop) is
down for an SSD replacement, expected back ~2026-07-08. Once NenTera is
restored, development can happen from either machine; either way, deploys
land the same way above, run directly on Ashaman (a full `docker compose
build` reliably fails over SSH — BuildKit/credential-helper + Tailscale DERP
issues — so builds specifically need a local/RDP session on Ashaman; `git
pull` + `docker restart` work fine over SSH).

## Usage

### Access

- **Local (Ashaman)**: http://localhost:5555
- **Tailscale (from anywhere)**: http://ashaman.tail125d67.ts.net:5555

### Service Health Checks

Monitored services are defined in `/data/services.json` (see
[Configuration](#configuration)), not hardcoded. As of this writing, the
default set is:

| Service | Check Type | Endpoint | Container | Notes |
|---------|------------|----------|------------|-------|
| n8n Workflows | HTTP + Docker | :5678 | n8n | `deprecated: true` — barely used, kept alive for legacy automations |
| Jellyfin Media | HTTP | :8096 | jellyfin | |
| qBittorrent | HTTP | :8080 (LAN only) | qbittorrent | `lan_only: true` — a failed check reports "unknown (LAN-only)" instead of "down" since it's unreachable from Tailscale/remote by design |
| Open WebUI | HTTP | :3000 | open-webui | disabled by default |
| Discord Relay | Docker | - | claude-relay | |
| Cloudflare Tunnel | Docker | - | cloudflared-tunnel | |
| Scan Runner | Docker | - | scan-runner | |
| Uptime Kuma | HTTP | :3001 | uptime-kuma | |
| Home Assistant | HTTP | :8123 | - | |
| AdGuard Home | HTTP | :3000 | - | disabled by default |

### System Stats

- **Docker**: Running/total container count
- **H: Drive**: NAS disk usage (total, used, free, percent)
- **CPU**: Current CPU usage percentage
- **RAM**: Memory usage (total, used, percent)

### Meal Planner

- Accessible at `/meal-planner` route
- Drag-and-drop interface for weekly meal planning
- localStorage persistence (per-browser)
- Supports cook meals (ingredients) and order meals (restaurants)
- Category filtering and custom meal creation

## Project Structure

```
aernhome/
├── app.py                      # Flask backend with health checks
├── fleet.py                    # Fleet sentinel blueprint + Signal alerting
├── docker-compose.yml          # Container configuration
├── Dockerfile                  # Python 3.11-slim image
├── requirements.txt            # Python dependencies
├── data-seed/
│   └── services.json           # Git-tracked reference copy of the default services config
├── host_collector/
│   └── fleet_host_stats.ps1    # Scheduled task feeding the Fleet board (host-level checks)
├── static/
│   ├── js/dashboard.js         # Real-time update logic
│   └── recipes.json            # Meal planner data
├── templates/
│   ├── base.html               # Base template with Tailwind
│   ├── dashboard.html          # Main dashboard view
│   └── meal-planner.html       # Meal planner page
├── data/                       # Bind-mounted at runtime (gitignored) — dashboard.db,
│                                # nexus.db, services.json, host_stats.json, ...
└── README.md
```

## Database Schema

**services table:**
```sql
CREATE TABLE services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    url TEXT,
    check_type TEXT NOT NULL,        -- 'http', 'docker', or 'both'
    docker_container TEXT,
    icon_emoji TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**health_checks table:**
```sql
CREATE TABLE health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL,
    status TEXT NOT NULL,            -- 'up', 'down', 'degraded', 'unknown'
    response_time_ms INTEGER,
    error_message TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (service_id) REFERENCES services (id)
);
```

## Configuration

### Adding / Editing Services

Service definitions are **not** hardcoded — edit `/data/services.json` on the
host (`C:\projects\aernhome\data\services.json`) and restart the container.
No code change, no rebuild:

```json
{
    "name": "service-name",
    "display_name": "Service Display Name",
    "url": "http://host.docker.internal:PORT",
    "check_type": "http",
    "docker_container": "container-name",
    "icon_emoji": "🚀",
    "enabled": 1,
    "public_url": "https://...",
    "lan_only": false,
    "deprecated": false,
    "notes": ""
}
```

- `url` may be `null` for Docker-only checks; `check_type` is `http`,
  `docker`, or `both`.
- `lan_only: true` — a failed HTTP check reports as `unknown (LAN-only,
  unreachable from here)` instead of `down`, for services only reachable
  from the home LAN (not Tailscale/remote).
- `deprecated` / `notes` are documentation-only today; the UI has no badge
  for them yet.
- Full field-by-field schema is documented in the `_comment` key at the top
  of the generated `services.json` file, and in `data-seed/services.json`
  (the git-tracked reference copy of the shipped defaults).

```powershell
notepad C:\projects\aernhome\data\services.json
docker restart aernhome-dashboard
```

If `/data/services.json` doesn't exist yet (fresh host), the app writes it
from its built-in defaults on first boot. If it exists but is malformed
JSON, the app logs a warning and falls back to the built-in defaults for
that run — it never crashes, and the bad file is left alone so you can fix
it.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/data` | Data directory inside the container; bind-mounted from `C:/projects/aernhome/data` on the host. Holds `dashboard.db`, `nexus.db`, `services.json`, `host_stats.json`, `nas_stats.json`. |
| `FLASK_ENV` | `production` | Flask environment |
| `TZ` | `America/Chicago` | Container timezone |
| `AERNHOME_UNLOCK_TOKEN` | *(none)* | `?unlock=<token>` cookie to reveal service links through the Cloudflare tunnel |
| `SIGNAL_HTTP_URL` | *(none)* | signal-cli HTTP daemon URL for Fleet sentinel alerts (points at the `signal-cli` container over `relay-net`) |
| `SIGNAL_ACCOUNT` | *(none)* | Signal account (sender) for Fleet alerts |
| `SIGNAL_ALERT_TO` | *(none)* | Signal recipient for Fleet alerts |

`SIGNAL_ACCOUNT`/`SIGNAL_ALERT_TO` live in the gitignored `.env` next to
`docker-compose.yml`. If any of the three Signal variables is unset, alerts
just log-and-skip — nothing errors. See `NEXUS_DEPLOY.md` for the nexus
connector tokens (`TODOIST_TOKEN`, `TMDB_TOKEN`, `IGDB_CLIENT_*`, etc.).

### Fleet Board & Host Stats

`/nexus/fleet` (Tailscale-only) is the live source of truth for infra health —
Aernbot, TCG automation, and host-level checks that can't be observed from
inside a container. It's fed by two things:

- **`host_collector/fleet_host_stats.ps1`** — runs on Ashaman as a Windows
  Scheduled Task named **"Fleet Host Stats"**, every 10 minutes, writing
  `data/host_stats.json`. Checks include the TCG AutoProcess task, nexus/
  supersaiyan backups, Chrome CDP, disk space, Tailscale, and the Matt
  interactive session. See `host_collector/README.md` for the full schema
  and setup command.
- **`fleet.py`** (Flask blueprint) — the in-app sentinel that reads those
  checks plus its own state, and can alert over Signal (see env vars above)
  when something flips to `down`.

**Known issue**: `nexus_backup.py`'s daily backup to `H:` is currently
failing (the mapped drive isn't reliably visible from the Task Scheduler
context that runs it) — the Fleet board's `nexus_backup` check exists
specifically to surface this. Tracked there, not yet fixed.

## Troubleshooting

### Container won't start
```powershell
# Check logs
docker compose logs -f

# Verify the data directory is accessible (created automatically if missing)
ls C:\projects\aernhome\data

# Rebuild from scratch
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Services showing as "down" incorrectly
```powershell
# Check if service is actually running
docker ps

# Verify HTTP endpoint is accessible from container
docker exec -it aernhome-dashboard curl http://host.docker.internal:5678
```
If the service is `lan_only` in `services.json` (e.g. qBittorrent), a
failure shows as `unknown (LAN-only, unreachable from here)`, not `down` —
that's expected and not a bug.

### Database locked errors
```powershell
# Stop container
docker compose down

# Check if database is corrupted
sqlite3 C:\projects\aernhome\data\dashboard.db "PRAGMA integrity_check;"

# If corrupted, delete and recreate (services.json is untouched, so services
# reseed automatically on next start)
rm C:\projects\aernhome\data\dashboard.db
docker compose up -d
```

## Future Enhancements

- [ ] Custom domain with Cloudflare tunnel
- [ ] Authentication layer (Cloudflare Access or Flask HTTP auth)
- [ ] Historical uptime charts
- [ ] Discord alert notifications on service down
- [ ] Service management UI (add/edit/delete via web)
- [ ] Mobile app wrapper
- [ ] Additional embedded apps (TCG inventory, photo review)

## Tech Stack

- **Backend**: Flask 3.0.0
- **HTTP Client**: requests 2.31.0
- **Docker Integration**: docker-py 7.0.0
- **System Monitoring**: psutil 5.9.6
- **Frontend**: Tailwind CSS 3.x (CDN)
- **JavaScript**: Vanilla ES6+
- **Database**: SQLite 3

## License

Private project for personal use.

## Author

Aern28 (Matthew Carroll)
