/**
 * Nexus TCG Ops strip — client-side render of /api/tcg-ops.
 * Pattern mirrors static/js/nexus_fleet.js (fetch -> render -> 30s
 * auto-refresh, paused while the tab is hidden). All markup uses classes
 * that already exist in the precompiled static/css/app.css — no new
 * Tailwind utility strings.
 */

const TCGOPS_UPDATE_INTERVAL = 30000; // 30 seconds
let tcgOpsUpdateTimer = null;

function tcgOpsEscapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

/**
 * Relative-time formatter, e.g. "just now" / "5m ago" / "3h ago" / "2d ago".
 * Same behavior as nexus_fleet.js's relativeTime().
 */
function tcgOpsRelativeTime(iso) {
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

function tcgOpsFormatMoney(n) {
    if (n == null || isNaN(n)) return '$0';
    return `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function tcgOpsFormatAgeHours(hours) {
    if (hours == null || isNaN(hours)) return 'age unknown';
    if (hours < 48) return `${hours.toFixed(1)}h old`;
    return `${(hours / 24).toFixed(1)}d old`;
}

function renderOutage(outage) {
    const countEl = document.getElementById('tcgops-outage-count');
    const valueEl = document.getElementById('tcgops-outage-value');
    const banner = document.getElementById('tcgops-outage-banner');
    if (!outage) {
        if (countEl) { countEl.textContent = '—'; countEl.className = 'text-2xl font-bold text-white'; }
        if (valueEl) valueEl.textContent = '—';
        if (banner) banner.classList.add('hidden');
        return;
    }
    const count = outage.count;
    const hasBacklog = typeof count === 'number' && count > 0;
    if (countEl) {
        countEl.textContent = count == null ? '—' : count;
        countEl.className = `text-2xl font-bold ${hasBacklog ? 'text-amber-400' : 'text-white'}`;
    }
    if (valueEl) {
        valueEl.textContent = outage.total_value != null
            ? `${tcgOpsFormatMoney(outage.total_value)} since ${outage.since || '?'}`
            : `since ${outage.since || '?'}`;
    }
    if (banner) banner.classList.toggle('hidden', !hasBacklog);
}

function renderHeldTile(held) {
    const countEl = document.getElementById('tcgops-held-count');
    if (!countEl) return;
    const count = held ? held.count : null;
    const hasHeld = typeof count === 'number' && count > 0;
    countEl.textContent = count == null ? '—' : count;
    countEl.className = `text-2xl font-bold ${hasHeld ? 'text-red-400' : 'text-white'}`;
}

function renderHeldList(held) {
    const card = document.getElementById('tcgops-held-card');
    const list = document.getElementById('tcgops-held-list');
    if (!list) return;

    const orders = held && Array.isArray(held.orders) ? held.orders : null;
    if (card) {
        const hasHeld = orders && orders.length > 0;
        card.className = `bg-dark-card border border-dark-border rounded-lg p-4${hasHeld ? ' border-l-4 border-l-red-500' : ''}`;
    }

    if (orders == null) {
        list.innerHTML = '<li class="text-sm text-gray-400">Unavailable.</li>';
        return;
    }
    if (!orders.length) {
        list.innerHTML = '<li class="text-sm text-gray-400">No held orders.</li>';
        return;
    }
    list.innerHTML = orders.map((o) => `
        <li class="flex items-center justify-between gap-2 text-sm">
            <span class="text-gray-200 truncate" title="${tcgOpsEscapeHtml(o.order_id)}">${tcgOpsEscapeHtml(o.order_id)}</span>
            <span class="shrink-0 flex items-center gap-2">
                <span class="text-gray-300">${tcgOpsFormatMoney(o.value)}</span>
                <span class="text-red-400 font-medium">${tcgOpsEscapeHtml(tcgOpsFormatAgeHours(o.age_hours))}</span>
            </span>
        </li>
    `).join('');
}

function renderAutoprocess(autoprocess) {
    const statusEl = document.getElementById('tcgops-autoprocess-status');
    const detailEl = document.getElementById('tcgops-autoprocess-detail');
    if (!statusEl) return;

    if (!autoprocess) {
        statusEl.textContent = 'unknown';
        statusEl.className = 'text-2xl font-bold text-gray-400';
        if (detailEl) { detailEl.textContent = 'no data'; detailEl.title = ''; }
        return;
    }
    const status = autoprocess.status || 'unknown';
    const colors = { up: 'text-emerald-400', warn: 'text-amber-400', down: 'text-red-400', unknown: 'text-gray-400' };
    statusEl.textContent = status;
    statusEl.className = `text-2xl font-bold ${colors[status] || 'text-gray-400'}`;
    if (detailEl) {
        const detail = autoprocess.detail || '';
        detailEl.textContent = detail || '—';
        detailEl.title = detail;
    }
}

function renderPriceList(prices) {
    const list = document.getElementById('tcgops-price-list');
    if (!list) return;

    if (!prices) {
        list.innerHTML = '<li class="text-sm text-gray-400">Unavailable.</li>';
        return;
    }

    const byCategory = prices.by_category;
    if (byCategory && Object.keys(byCategory).length) {
        const rows = Object.entries(byCategory).sort((a, b) => a[0].localeCompare(b[0]));
        list.innerHTML = rows.map(([game, lastDate]) => `
            <li class="flex items-center justify-between gap-2 text-sm">
                <span class="text-gray-200 truncate">${tcgOpsEscapeHtml(game)}</span>
                <span class="text-gray-400 shrink-0">${tcgOpsEscapeHtml(lastDate || '—')}</span>
            </li>
        `).join('');
        return;
    }

    if (prices.overall_last_date) {
        const rows = prices.overall_last_date_rows;
        list.innerHTML = `
            <li class="text-sm text-gray-200">Overall: ${tcgOpsEscapeHtml(prices.overall_last_date)}</li>
            <li class="text-xs text-gray-400">${rows != null ? `${rows.toLocaleString()} rows on that date` : ''}</li>
        `;
        return;
    }

    list.innerHTML = '<li class="text-sm text-gray-400">No price data.</li>';
}

function renderMirror(mirror) {
    const el = document.getElementById('tcgops-mirror-age');
    if (!el) return;
    if (!mirror || !mirror.mtime) {
        el.textContent = '—';
        el.className = 'text-2xl font-bold text-gray-400';
        return;
    }
    const ageMs = Date.now() - new Date(mirror.mtime).getTime();
    const ageH = ageMs / 3600000;
    el.textContent = tcgOpsRelativeTime(mirror.mtime);
    el.className = `text-2xl font-bold ${ageH > 50 ? 'text-red-400' : ageH > 26 ? 'text-amber-400' : 'text-white'}`;
}

function renderTcgOps(data) {
    renderOutage(data.orders && data.orders.outage_window_orders);
    renderHeldTile(data.orders && data.orders.held);
    renderHeldList(data.orders && data.orders.held);
    renderAutoprocess(data.autoprocess);
    renderPriceList(data.prices);
    renderMirror(data.mirror);

    const genEl = document.getElementById('tcgops-generated-at');
    if (genEl) genEl.textContent = tcgOpsRelativeTime(data.generated_at);
}

async function loadTcgOps() {
    try {
        const res = await fetch('/api/tcg-ops');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderTcgOps(data || {});
    } catch (err) {
        console.error('Failed to load TCG ops:', err);
    }
}

function startTcgOpsAutoRefresh() {
    if (tcgOpsUpdateTimer) clearInterval(tcgOpsUpdateTimer);
    tcgOpsUpdateTimer = setInterval(loadTcgOps, TCGOPS_UPDATE_INTERVAL);
}

function stopTcgOpsAutoRefresh() {
    if (tcgOpsUpdateTimer) {
        clearInterval(tcgOpsUpdateTimer);
        tcgOpsUpdateTimer = null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadTcgOps();
    startTcgOpsAutoRefresh();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopTcgOpsAutoRefresh();
        } else {
            loadTcgOps();
            startTcgOpsAutoRefresh();
        }
    });
});
