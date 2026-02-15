# AernHome Dashboard

Self-hosted home services dashboard for monitoring and managing Docker services, system stats, and embedded apps.

## Features

- **Service Monitoring**: Real-time health checks for n8n, Jellyfin, qBittorrent, Discord relay, OPTCG digest, and Gluetun VPN
- **System Stats**: Docker container counts, disk usage, CPU, and RAM monitoring
- **Embedded Apps**: Meal planner with drag-and-drop interface
- **Auto-refresh**: Dashboard updates every 30 seconds
- **Dark Theme**: Easy-on-the-eyes Tailwind-based UI

## Architecture

- **Backend**: Flask (Python 3.11)
- **Frontend**: Tailwind CDN + Vanilla JavaScript (no build step)
- **Database**: SQLite on NAS (`H:\aernhome\dashboard.db`)
- **Docker**: Runs in Docker Desktop on Ashaman
- **Health Checks**: HTTP endpoints + Docker API integration

## Setup

### Prerequisites

- Docker Desktop running on Ashaman
- H: drive mapped to `\\192.168.1.118\home` (Synology NAS)
- Tailscale for remote access

### Initial Deployment (on Ashaman)

```powershell
# Clone repository
cd C:\projects
git clone https://github.com/Aern28/aernhome.git
cd aernhome

# Create database directory on NAS
mkdir H:\aernhome

# Build and start container
docker compose build
docker compose up -d

# View logs
docker compose logs -f
```

### Update Workflow

**On NenTera (laptop):**
```bash
cd /c/projects/aernhome
# Make changes
git add .
git commit -m "Description of changes"
git push
```

**On Ashaman (desktop via SSH):**
```powershell
ssh -o ConnectTimeout=30 Matt@100.110.245.37
cd C:\projects\aernhome
git pull
docker compose build
docker compose restart
```

## Usage

### Access

- **Local (Ashaman)**: http://localhost:5555
- **Tailscale (from anywhere)**: http://ashaman.tail125d67.ts.net:5555

### Service Health Checks

The dashboard monitors these services:

| Service | Check Type | Endpoint | Container |
|---------|------------|----------|-----------|
| n8n Workflows | HTTP | :5678 | n8n |
| Jellyfin Media | HTTP | :8096 | jellyfin |
| qBittorrent | HTTP | :8080 | qbittorrent |
| Discord Relay | Docker | - | claude-relay |
| OPTCG Digest | Docker | - | optcg-digest |
| Gluetun VPN | Docker | - | gluetun |

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
├── docker-compose.yml          # Container configuration
├── Dockerfile                  # Python 3.11-slim image
├── requirements.txt            # Python dependencies
├── static/
│   ├── js/dashboard.js         # Real-time update logic
│   └── recipes.json            # Meal planner data
├── templates/
│   ├── base.html               # Base template with Tailwind
│   ├── dashboard.html          # Main dashboard view
│   └── meal-planner.html       # Meal planner page
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

### Adding New Services

Edit `app.py` and add to `DEFAULT_SERVICES`:

```python
{
    'name': 'service-name',
    'display_name': 'Service Display Name',
    'url': 'http://host.docker.internal:PORT',  # or None for Docker-only
    'check_type': 'http',  # or 'docker' or 'both'
    'docker_container': 'container-name',
    'icon_emoji': '🚀',
    'enabled': 1
}
```

Then rebuild and restart the container:
```powershell
docker compose build
docker compose restart
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/data` | Database directory (mounted to H:\aernhome) |
| `FLASK_ENV` | `production` | Flask environment |
| `TZ` | `America/Chicago` | Container timezone |

## Troubleshooting

### Container won't start
```powershell
# Check logs
docker compose logs -f

# Verify H: drive is accessible
ls H:\aernhome

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

### Database locked errors
```powershell
# Stop container
docker compose down

# Check if database is corrupted
sqlite3 H:\aernhome\dashboard.db "PRAGMA integrity_check;"

# If corrupted, delete and recreate
rm H:\aernhome\dashboard.db
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
