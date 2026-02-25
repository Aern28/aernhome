/**
 * AernHome Dashboard - Frontend Logic
 * Handles real-time updates of service health and system stats
 */

const UPDATE_INTERVAL = 30000; // 30 seconds
let updateTimer = null;

// Status color mapping
const STATUS_COLORS = {
    'up': 'bg-green-500',
    'down': 'bg-red-500',
    'degraded': 'bg-yellow-500',
    'unknown': 'bg-gray-500'
};

/**
 * Format response time for display
 */
function formatResponseTime(ms) {
    if (ms === null || ms === undefined) return '';
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * Render a sparkline SVG from an array of booleans (true=up, false=down)
 */
function renderSparkline(data) {
    if (!data || data.length === 0) return '';
    // Show last 48 checks max, each as a thin bar
    const points = data.slice(-48);
    const barW = 3;
    const gap = 1;
    const h = 16;
    const w = points.length * (barW + gap) - gap;
    const bars = points.map((up, i) => {
        const x = i * (barW + gap);
        const color = up ? '#22c55e' : '#ef4444'; // green-500 / red-500
        return `<rect x="${x}" y="0" width="${barW}" height="${h}" rx="1" fill="${color}" opacity="0.85"/>`;
    }).join('');
    return `<svg width="${w}" height="${h}" class="inline-block align-middle">${bars}</svg>`;
}

/**
 * Render a service card
 */
function renderServiceCard(service) {
    const statusColor = STATUS_COLORS[service.status] || STATUS_COLORS['unknown'];
    const responseTime = service.response_time_ms ? formatResponseTime(service.response_time_ms) : '';
    const errorMsg = service.error_message || '';
    const sparkline = renderSparkline(service.sparkline);

    const card = document.createElement('div');
    card.className = 'bg-dark-card border border-dark-border rounded-lg p-4 hover:border-blue-500 transition-colors';

    // Make card clickable if public URL exists
    if (service.public_url) {
        card.classList.add('cursor-pointer');
        card.onclick = () => window.open(service.public_url, '_blank');
    }

    card.innerHTML = `
        <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-2">
                <div class="w-3 h-3 rounded-full ${statusColor}"></div>
                <h3 class="text-lg font-semibold text-white">${service.display_name}</h3>
            </div>
            <span class="text-2xl">${service.icon_emoji}</span>
        </div>
        ${sparkline ? `<div class="mb-2" title="Last 24h uptime">${sparkline}</div>` : ''}
        <div class="flex items-center justify-between text-xs">
            <span class="text-gray-400">${service.status.toUpperCase()}</span>
            <span class="text-gray-500">${responseTime}</span>
        </div>
        ${errorMsg ? `<div class="text-xs text-red-400 mt-2 truncate" title="${errorMsg}">${errorMsg}</div>` : ''}
    `;

    return card;
}

/**
 * Update service health cards
 */
async function updateServices() {
    try {
        const response = await fetch('/api/health');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const services = await response.json();
        const grid = document.getElementById('services-grid');

        // Clear existing cards
        grid.innerHTML = '';

        // Render new cards
        services.forEach(service => {
            grid.appendChild(renderServiceCard(service));
        });

    } catch (error) {
        console.error('Failed to update services:', error);
        const grid = document.getElementById('services-grid');
        grid.innerHTML = '<div class="col-span-full text-center text-red-400">Failed to load services</div>';
    }
}

/**
 * Update system stats widgets
 */
async function updateStats() {
    try {
        const response = await fetch('/api/stats');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const stats = await response.json();

        // Update Docker stat
        const dockerStat = document.getElementById('docker-stat');
        if (stats.docker.error) {
            dockerStat.textContent = 'Error';
            dockerStat.className = 'text-2xl font-bold text-red-400';
        } else {
            dockerStat.textContent = `${stats.docker.running}/${stats.docker.total}`;
            dockerStat.className = 'text-2xl font-bold text-white';
        }

        // Update C: Drive stat
        const cDriveStat = document.getElementById('c-drive-stat');
        const cDrivePercent = document.getElementById('c-drive-percent');
        if (stats.c_drive.error) {
            cDriveStat.textContent = 'Error';
            cDrivePercent.textContent = '';
        } else {
            cDriveStat.textContent = `${stats.c_drive.used_gb} GB`;
            cDrivePercent.textContent = `${stats.c_drive.percent}% used of ${stats.c_drive.total_gb} GB`;
        }

        // Update H: Drive stat (Synology NAS)
        const hDriveStat = document.getElementById('h-drive-stat');
        const hDrivePercent = document.getElementById('h-drive-percent');
        if (stats.h_drive.error) {
            hDriveStat.textContent = 'Error';
            hDrivePercent.textContent = '';
        } else {
            hDriveStat.textContent = `${stats.h_drive.free_gb} GB free`;
            hDrivePercent.textContent = `of ${stats.h_drive.total_gb} GB (${stats.h_drive.percent}% used)`;
        }

        // Update I: Drive stat (Synology USB)
        const iDriveStat = document.getElementById('i-drive-stat');
        const iDrivePercent = document.getElementById('i-drive-percent');
        if (stats.i_drive.error) {
            iDriveStat.textContent = 'Error';
            iDrivePercent.textContent = '';
        } else {
            iDriveStat.textContent = `${stats.i_drive.free_gb} GB free`;
            iDrivePercent.textContent = `of ${stats.i_drive.total_gb} GB (${stats.i_drive.percent}% used)`;
        }

        // Update CPU stat
        const cpuStat = document.getElementById('cpu-stat');
        if (stats.cpu.error) {
            cpuStat.textContent = 'Error';
        } else {
            cpuStat.textContent = `${stats.cpu.percent}%`;
        }

        // Update RAM stat (inline in Docker card)
        const ramStat = document.getElementById('ram-stat');
        if (stats.ram.error) {
            ramStat.textContent = '--';
        } else {
            ramStat.textContent = stats.ram.used_gb;
        }

    } catch (error) {
        console.error('Failed to update stats:', error);
    }
}

/**
 * Update last refresh timestamp
 */
function updateTimestamp() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString();
    document.getElementById('last-update').textContent = `Last updated: ${timeStr}`;
}

/**
 * Main dashboard update function
 */
async function updateDashboard() {
    await Promise.all([
        updateServices(),
        updateStats()
    ]);
    updateTimestamp();
}

/**
 * Start auto-refresh
 */
function startAutoRefresh() {
    // Clear any existing timer
    if (updateTimer) {
        clearInterval(updateTimer);
    }

    // Set up new timer
    updateTimer = setInterval(updateDashboard, UPDATE_INTERVAL);
}

/**
 * Stop auto-refresh
 */
function stopAutoRefresh() {
    if (updateTimer) {
        clearInterval(updateTimer);
        updateTimer = null;
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Initial update
    updateDashboard();

    // Start auto-refresh
    startAutoRefresh();

    // Stop auto-refresh when page is hidden (battery/performance)
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopAutoRefresh();
        } else {
            updateDashboard();
            startAutoRefresh();
        }
    });
});
