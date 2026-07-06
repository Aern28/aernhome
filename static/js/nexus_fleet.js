/**
 * Nexus Fleet board — client-side render of /api/fleet.
 * Pattern mirrors static/js/dashboard.js (fetch -> render -> timestamp -> 30s
 * auto-refresh, paused while the tab is hidden) so the two health views stay
 * consistent. All markup is generated with classes that already exist in the
 * precompiled static/css/app.css — no new Tailwind utility strings.
 */

const FLEET_UPDATE_INTERVAL = 30000; // 30 seconds
let fleetUpdateTimer = null;

// Group id -> [icon, title], in display order.
const FLEET_GROUPS = [
    ['aernbot', '🤖', 'Aernbot'],
    ['tcg', '🃏', 'TCG Business'],
    ['infra', '🛰️', 'Infra'],
    ['host', '🖥️', 'Host'],
];

// status -> dot color (matches dashboard.js's STATUS_COLORS convention)
const FLEET_DOT = {
    up: 'bg-green-500',
    warn: 'bg-amber-500',
    down: 'bg-red-500',
    unknown: 'bg-gray-500',
};

// status -> summary-chip text color
const FLEET_TEXT = {
    up: 'text-emerald-400',
    warn: 'text-amber-400',
    down: 'text-red-400',
    unknown: 'text-gray-400',
};

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

/**
 * Relative-time formatter, e.g. "just now" / "5m ago" / "3h ago" / "2d ago".
 */
function relativeTime(iso) {
    if (!iso) return 'unknown';
    const then = new Date(iso).getTime();
    if (isNaN(then)) return 'unknown';
    const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (diffSec < 45) return 'just now';
    const mins = Math.floor(diffSec / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    const months = Math.floor(days / 30);
    if (months < 12) return `${months}mo ago`;
    const years = Math.floor(months / 12);
    return `${years}y ago`;
}

/**
 * Render the last-100-samples history array as an inline SVG sparkline strip
 * (one 1px-wide tick per sample). Colors are inline fill attributes, not
 * Tailwind classes, so this works regardless of what's in the precompiled CSS
 * — same trick dashboard.js uses for its service sparklines.
 */
function renderHistorySpark(history) {
    if (!history || !history.length) {
        return '<div class="text-[11px] text-gray-500">no history yet</div>';
    }
    const pts = history.slice(-100);
    const w = pts.length;
    const bars = pts.map((v, i) => {
        const color = v ? '#22c55e' : '#ef4444'; // green-500 / red-500
        return `<rect x="${i}" y="0" width="1" height="14" fill="${color}"/>`;
    }).join('');
    return `<svg viewBox="0 0 ${w} 14" preserveAspectRatio="none" width="100%" height="14" style="display:block">${bars}</svg>`;
}

function renderCheckCard(check) {
    const status = FLEET_DOT[check.status] ? check.status : 'unknown';
    const dot = FLEET_DOT[status];
    const accent = status === 'down' ? ' border-l-4 border-l-red-500'
        : status === 'warn' ? ' border-l-4 border-l-amber-500' : '';

    const card = document.createElement('div');
    card.className = `bg-dark-card border border-dark-border rounded-lg p-4${accent}`;
    card.innerHTML = `
        <div class="flex items-center justify-between gap-2 mb-2">
            <div class="flex items-center gap-2 min-w-0">
                <div class="w-3 h-3 rounded-full ${dot} shrink-0" title="${escapeHtml(status)}"></div>
                <h4 class="text-sm font-semibold text-white truncate">${escapeHtml(check.label || check.id || 'check')}</h4>
            </div>
            <span class="text-[11px] uppercase tracking-wide text-gray-400 shrink-0">${escapeHtml(status)}</span>
        </div>
        ${check.detail ? `<div class="text-xs text-gray-400 truncate mb-2" title="${escapeHtml(check.detail)}">${escapeHtml(check.detail)}</div>` : ''}
        <div class="mb-2">${renderHistorySpark(check.history)}</div>
        <div class="text-[11px] text-gray-500">since ${relativeTime(check.last_change)}</div>
    `;
    return card;
}

function renderGroupSection(groupKey, icon, title, checks) {
    const counts = { up: 0, warn: 0, down: 0, unknown: 0 };
    checks.forEach((c) => {
        const s = FLEET_DOT[c.status] ? c.status : 'unknown';
        counts[s] += 1;
    });

    const chips = [];
    if (counts.up) chips.push(`<span class="${FLEET_TEXT.up}">${counts.up} up</span>`);
    if (counts.down) chips.push(`<span class="${FLEET_TEXT.down}">${counts.down} down</span>`);
    if (counts.warn) chips.push(`<span class="${FLEET_TEXT.warn}">${counts.warn} warn</span>`);
    if (counts.unknown) chips.push(`<span class="${FLEET_TEXT.unknown}">${counts.unknown} unknown</span>`);
    const summary = chips.length ? chips.join(' · ') : '<span class="text-gray-400">no checks</span>';

    const section = document.createElement('section');
    section.className = 'mb-6';
    section.innerHTML = `
        <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
            <h3 class="text-lg font-semibold text-white">${icon} ${escapeHtml(title)}</h3>
            <span class="text-xs">${summary}</span>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" data-fleet-cards></div>
    `;

    const grid = section.querySelector('[data-fleet-cards]');
    if (checks.length) {
        checks.forEach((c) => grid.appendChild(renderCheckCard(c)));
    } else {
        grid.innerHTML = '<p class="text-sm text-gray-400">No checks in this group.</p>';
    }
    return section;
}

function renderAlertRow(alert) {
    const message = alert.message || `${alert.from || '?'} → ${alert.to || '?'}`;
    return `
        <li class="text-sm border-t border-dark-border first:border-0 pt-2 first:pt-0">
            <div class="flex items-center justify-between gap-2">
                <span class="text-gray-200">${escapeHtml(alert.check || 'unknown check')}</span>
                <span class="text-[11px] text-gray-400 shrink-0">${relativeTime(alert.ts)}</span>
            </div>
            <div class="text-xs text-gray-400">${escapeHtml(message)}</div>
        </li>
    `;
}

function renderFleet(data) {
    const checks = Array.isArray(data.checks) ? data.checks : [];

    // Group sections
    const groupsEl = document.getElementById('fleet-groups');
    if (groupsEl) {
        groupsEl.innerHTML = '';
        FLEET_GROUPS.forEach(([key, icon, title]) => {
            const groupChecks = checks.filter((c) => c.group === key);
            groupsEl.appendChild(renderGroupSection(key, icon, title, groupChecks));
        });
    }

    // Down banner
    const banner = document.getElementById('fleet-down-banner');
    if (banner) {
        const down = checks.filter((c) => c.status === 'down');
        if (down.length) {
            const list = banner.querySelector('[data-fleet-down-list]');
            if (list) list.textContent = down.map((c) => c.label || c.id).join(', ');
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    }

    // Recent alerts, newest first
    const alertsList = document.getElementById('fleet-alerts-list');
    if (alertsList) {
        const alerts = (Array.isArray(data.recent_alerts) ? data.recent_alerts.slice() : [])
            .sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
        alertsList.innerHTML = alerts.length
            ? alerts.map(renderAlertRow).join('')
            : '<li class="text-sm text-gray-400">No recent alerts.</li>';
    }

    // Server data freshness
    const genEl = document.getElementById('fleet-generated-at');
    if (genEl) genEl.textContent = relativeTime(data.generated_at);
}

async function loadFleet() {
    try {
        const res = await fetch('/api/fleet');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderFleet(data || {});
    } catch (err) {
        console.error('Failed to load fleet status:', err);
        const groupsEl = document.getElementById('fleet-groups');
        if (groupsEl) {
            groupsEl.innerHTML = '<div class="text-center text-red-400 text-sm">Failed to load fleet status.</div>';
        }
    }
    updateFleetTimestamp();
}

function updateFleetTimestamp() {
    const el = document.getElementById('fleet-last-update');
    if (el) el.textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
}

function startFleetAutoRefresh() {
    if (fleetUpdateTimer) clearInterval(fleetUpdateTimer);
    fleetUpdateTimer = setInterval(loadFleet, FLEET_UPDATE_INTERVAL);
}

function stopFleetAutoRefresh() {
    if (fleetUpdateTimer) {
        clearInterval(fleetUpdateTimer);
        fleetUpdateTimer = null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadFleet();
    startFleetAutoRefresh();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopFleetAutoRefresh();
        } else {
            loadFleet();
            startFleetAutoRefresh();
        }
    });
});
