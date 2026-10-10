/**
 * nexus_sb.js — Second Brain: seat handoff, bidirectional queue, needs-Aern
 * lane. Shared by nexus_seat.html / nexus_queue.html / nexus_aern.html.
 *
 * Same fetch -> render -> timestamp -> auto-refresh (paused while the tab is
 * hidden) pattern as static/js/nexus_fleet.js, generalized to whichever of
 * the three pages is actually present (checked via DOM markers, so one file
 * can be shared across all three without errors on the other two).
 */

const SB_REFRESH_INTERVAL = 60000; // 60 seconds
let sbRefreshTimer = null;

function sbEscapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function sbRelativeTime(iso) {
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

function sbIsUrl(s) {
    return typeof s === 'string' && /^https?:\/\//i.test(s.trim());
}

/** Render a `source`/`ref` receipt: a link when it looks like a URL, code
 * text otherwise (a file path or shell command). */
function sbRenderSource(value, cssClass) {
    if (!value) return '';
    if (sbIsUrl(value)) {
        return `<a href="${sbEscapeHtml(value)}" target="_blank" rel="noopener" class="${cssClass || 'text-blue-400 hover:underline'} break-words">${sbEscapeHtml(value)}</a>`;
    }
    return `<code class="font-mono text-[11px] bg-dark-bg border border-dark-border rounded px-1.5 py-0.5 text-gray-300 break-words">${sbEscapeHtml(value)}</code>`;
}

async function sbFetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

async function sbPostJson(url, body) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        return await res.json();
    } catch (e) {
        return { ok: false, error: String(e) };
    }
}

// ── /nexus/seat ─────────────────────────────────────────────────────────────
const SEAT_GROUPS = [
    ['active', '🟢', 'Active'],
    ['blocked', '🟡', 'Blocked'],
    ['parked', '⏸️', 'Parked'],
    ['done', '✅', 'Done — clear with /api/seat/prune'],
];

function sbRenderProjectCard(p) {
    const links = Array.isArray(p.links) ? p.links : [];
    const linksHtml = links.length
        ? `<div class="flex flex-wrap gap-2 mt-2">${links.map((l) =>
            `<a href="${sbEscapeHtml(l.ref)}" target="_blank" rel="noopener" class="text-[11px] px-2 py-0.5 rounded-full bg-dark-bg border border-dark-border text-blue-400 hover:underline">${sbEscapeHtml(l.label || l.ref)}</a>`
        ).join('')}</div>`
        : '';

    const div = document.createElement('div');
    div.className = 'bg-dark-card border border-dark-border rounded-lg p-4';
    div.innerHTML = `
        <div class="flex items-start justify-between gap-2 mb-2">
            <h4 class="text-sm font-semibold text-white">${sbEscapeHtml(p.title || p.id)}</h4>
            ${p.blocked_on ? `<span class="text-[11px] uppercase tracking-wide text-amber-400 shrink-0">blocked on ${sbEscapeHtml(p.blocked_on)}</span>` : ''}
        </div>
        ${p.detail ? `<div class="text-sm text-gray-300 mb-2">${sbEscapeHtml(p.detail)}</div>` : ''}
        ${p.next_step ? `<div class="bg-dark-bg border border-dark-border rounded-md px-3 py-2 text-sm text-white"><span class="text-[11px] uppercase tracking-wide text-gray-400">Next</span><br>${sbEscapeHtml(p.next_step)}</div>` : ''}
        ${linksHtml}
    `;
    return div;
}

function sbRenderSeatGroups(doc) {
    const el = document.getElementById('seat-groups');
    if (!el) return false;
    const projects = Array.isArray(doc.projects) ? doc.projects : [];
    el.innerHTML = '';

    SEAT_GROUPS.forEach(([status, icon, title]) => {
        const items = projects.filter((p) => (p.status || 'active') === status);
        if (!items.length) return;
        const section = document.createElement('section');
        section.className = 'mb-6';
        section.innerHTML = `<h3 class="text-lg font-semibold text-white mb-3">${icon} ${sbEscapeHtml(title)} <span class="text-xs font-normal text-gray-400">(${items.length})</span></h3>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" data-seat-cards></div>`;
        const grid = section.querySelector('[data-seat-cards]');
        items.forEach((p) => grid.appendChild(sbRenderProjectCard(p)));
        el.appendChild(section);
    });

    if (!el.children.length) {
        el.innerHTML = '<p class="text-sm text-gray-400">No projects tracked yet — POST one to /api/seat.</p>';
    }

    const atEl = document.getElementById('seat-updated-at');
    if (atEl) atEl.textContent = sbRelativeTime(doc.updated_at);
    const byEl = document.getElementById('seat-updated-by');
    if (byEl) byEl.textContent = doc.updated_by || 'unknown';

    return true;
}

async function sbLoadSeat() {
    try {
        const doc = await sbFetchJson('/api/seat');
        sbRenderSeatGroups(doc || {});
    } catch (err) {
        console.error('Failed to load seat:', err);
        const el = document.getElementById('seat-groups');
        if (el) el.innerHTML = '<div class="text-center text-red-400 text-sm">Failed to load seat.</div>';
    }
}

// ── /nexus/queue ─────────────────────────────────────────────────────────────
const SB_PRIORITY_TEXT = { 1: 'text-red-400', 2: 'text-amber-400', 3: 'text-gray-400' };

function sbRenderQueueItem(item, resolvable) {
    const pColor = SB_PRIORITY_TEXT[item.priority] || SB_PRIORITY_TEXT[3];
    const li = document.createElement('li');
    li.className = 'bg-dark-card border border-dark-border rounded-lg p-3';
    li.dataset.queueId = item.id;
    // Queue asks (10/10): the ask leads; an item with options resolves only through
    // an option tap, so the plain Resolve button is dropped for it.
    const opts = Array.isArray(item.options) ? item.options : [];
    const choose = resolvable && opts.length;
    li.innerHTML = `
        <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
                ${item.ask ? `<div class="text-sm font-semibold text-white break-words mb-1">${sbEscapeHtml(item.ask)}</div>` : ''}
                <div class="nx-clamp text-sm text-gray-200 break-words">${sbEscapeHtml(item.text)}</div>
                <div class="flex flex-wrap items-center gap-2 mt-1 text-[11px] text-gray-400">
                    <span class="${pColor} font-semibold">P${sbEscapeHtml(item.priority)}</span>
                    <span>${sbEscapeHtml(item.created_by || 'unknown')}</span>
                    <span>${sbRelativeTime(item.created_at)}</span>
                </div>
                ${item.source ? `<div class="mt-1">${sbRenderSource(item.source)}</div>` : ''}
                ${item.resolution_note ? `<div class="mt-2 pl-2 border-l-2 border-dark-border text-[11px] text-gray-400 whitespace-pre-wrap break-words">${sbEscapeHtml(item.resolution_note)}</div>` : ''}
                ${choose ? `<div class="nx-opts mt-2">${opts.map((o, i) => `<button data-sb-action="queue-choose" data-id="${sbEscapeHtml(item.id)}" data-choice="${sbEscapeHtml(o)}" class="nx-opt${i === 0 ? ' nx-opt-rec' : ''}">${i === 0 ? '★ ' : ''}${sbEscapeHtml(o)}</button>`).join('')}</div>` : ''}
            </div>
            ${resolvable && !choose ? `<button data-sb-action="queue-resolve" data-id="${sbEscapeHtml(item.id)}" class="shrink-0 px-2 py-1 rounded-md bg-blue-600 hover:bg-blue-500 text-white text-xs">Resolve</button>` : ''}
        </div>
    `;
    return li;
}

function sbRenderQueueColumn(items, openElId, doneElId) {
    const open = items.filter((i) => i.status === 'open')
        .sort((a, b) => (a.priority - b.priority) || String(a.created_at || '').localeCompare(String(b.created_at || '')));
    const done = items.filter((i) => i.status === 'done')
        .sort((a, b) => String(b.resolved_at || '').localeCompare(String(a.resolved_at || '')));

    const openEl = document.getElementById(openElId);
    if (openEl) {
        openEl.innerHTML = '';
        if (open.length) {
            open.forEach((item) => openEl.appendChild(sbRenderQueueItem(item, true)));
        } else {
            openEl.innerHTML = '<li class="text-sm text-gray-400">Nothing open.</li>';
        }
    }

    const doneEl = document.getElementById(doneElId);
    if (doneEl) {
        doneEl.innerHTML = '';
        if (done.length) {
            done.forEach((item) => doneEl.appendChild(sbRenderQueueItem(item, false)));
        } else {
            doneEl.innerHTML = '<li class="text-xs text-gray-400">Nothing resolved yet.</li>';
        }
    }
}

function sbRenderQueue(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    sbRenderQueueColumn(items.filter((i) => i.dir === 'to_aern'), 'queue-to-aern-open', 'queue-to-aern-done');
    sbRenderQueueColumn(items.filter((i) => i.dir === 'to_review'), 'queue-to-review-open', 'queue-to-review-done');
    sbRenderQueueColumn(items.filter((i) => i.dir === 'to_fleet'), 'queue-to-fleet-open', 'queue-to-fleet-done');
    const el = document.getElementById('queue-last-update');
    if (el) el.textContent = new Date().toLocaleTimeString();
}

async function sbLoadQueue() {
    try {
        const data = await sbFetchJson('/api/queue');
        sbRenderQueue(data || {});
        sbWireClamps();
    } catch (err) {
        console.error('Failed to load queue:', err);
        ['queue-to-aern-open', 'queue-to-review-open', 'queue-to-fleet-open'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.innerHTML = '<li class="text-center text-red-400 text-sm">Failed to load queue.</li>';
        });
    }
}

function sbWireQueueForm() {
    const form = document.querySelector('[data-sb-queue-form]');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = {};
        form.querySelectorAll('[name]').forEach((el) => {
            if (el.value !== '') body[el.name] = el.value;
        });
        if (body.priority) body.priority = parseInt(body.priority, 10);
        const res = await sbPostJson('/api/queue', body);
        const err = form.querySelector('[data-sb-err]');
        if (res.ok) {
            if (err) err.textContent = '';
            form.reset();
            sbLoadQueue();
        } else if (err) {
            err.textContent = res.error || 'failed';
        }
    });
}

function sbWireQueueResolve() {
    document.addEventListener('click', async (e) => {
        const t = e.target.closest('[data-sb-action="queue-resolve"]');
        if (!t) return;
        t.disabled = true;
        const res = await sbPostJson('/api/queue/resolve', { id: t.dataset.id });
        if (res.ok) {
            sbLoadAll();
        } else {
            t.disabled = false;
        }
    });
    // Queue asks: tapping an option resolves the item with that choice; the
    // server writes "Aern MM/DD: <choice>" as the note.
    document.addEventListener('click', async (e) => {
        const t = e.target.closest('[data-sb-action="queue-choose"]');
        if (!t) return;
        const group = t.closest('.nx-opts');
        group.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        const res = await sbPostJson('/api/queue/resolve', { id: t.dataset.id, choice: t.dataset.choice });
        if (res.ok) {
            sbLoadAll();
        } else {
            group.querySelectorAll('button').forEach((b) => { b.disabled = false; });
            alert(res.error || 'Could not record that choice.');
        }
    });
    // Todoist tasks in the /nexus/aern Today group close through the same
    // endpoint the home Today card uses.
    document.addEventListener('click', async (e) => {
        const t = e.target.closest('[data-sb-action="todoist-close"]');
        if (!t) return;
        t.disabled = true;
        const res = await sbPostJson(`/api/nexus/todoist/${encodeURIComponent(t.dataset.id)}/close`, {});
        if (res.ok) {
            sbLoadAll();
        } else {
            t.disabled = false;
        }
    });
}

// ── /nexus/aern ──────────────────────────────────────────────────────────────
const SB_AERN_BORDER = { 1: 'border-l-4 border-l-red-500', 2: 'border-l-4 border-l-amber-500', 3: '' };
const SB_AERN_KIND_ICON = { queue: '📥', fleet: '🛰️', tcg_held: '🃏', seat: '📋', todoist: '📅' };

function sbRenderAernItem(item) {
    const border = SB_AERN_BORDER[item.priority] != null ? SB_AERN_BORDER[item.priority] : SB_AERN_BORDER[3];
    const icon = SB_AERN_KIND_ICON[item.source_kind] || '•';
    const div = document.createElement('div');
    div.className = `bg-dark-card border border-dark-border rounded-lg p-4 sm:p-5 ${border}`;
    // Queue items get a tap-to-clear (resolve) button; other sources keep their
    // ref link (those clear at the source, not from this lane).
    const opts = (item.source_kind === 'queue' && item.id && Array.isArray(item.options)) ? item.options : [];
    const action = opts.length
        // Queue asks: one button per option, the first is the recommendation.
        ? `<div class="nx-opts">${opts.map((o, i) => `<button data-sb-action="queue-choose" data-id="${sbEscapeHtml(item.id)}" data-choice="${sbEscapeHtml(o)}" class="nx-opt${i === 0 ? ' nx-opt-rec' : ''}">${i === 0 ? '★ ' : ''}${sbEscapeHtml(o)}</button>`).join('')}</div>`
        : (item.source_kind === 'queue' && item.id)
        ? `<button data-sb-action="queue-resolve" data-id="${sbEscapeHtml(item.id)}" class="self-start shrink-0 px-3 py-2 rounded-md bg-blue-600 hover:bg-blue-500 text-white text-xs">${item.effort === 'read' ? '✓ Read' : 'Done'}</button>`
        : (item.source_kind === 'todoist' && item.todoist_id)
        ? `<button data-sb-action="todoist-close" data-id="${sbEscapeHtml(item.todoist_id)}" class="self-start shrink-0 px-3 py-2 rounded-md bg-emerald-600 hover:bg-emerald-500 text-white text-xs">Done</button>`
        : (item.ref ? `<div class="shrink-0 min-w-0 break-words">${sbRenderSource(item.ref, 'text-blue-400 hover:underline text-sm')}</div>` : '');
    // Stack on mobile so the text uses the FULL card width (the side-by-side row
    // reserved right-hand space and squeezed the text into a hard-wrapping column);
    // side-by-side again at sm+.
    div.innerHTML = `
        <div class="flex flex-col gap-2 ${opts.length ? '' : 'sm:flex-row sm:items-start sm:justify-between sm:gap-3'}">
            <div class="min-w-0">
                <div class="text-[11px] uppercase tracking-wide text-gray-400 mb-1">${icon} ${sbEscapeHtml(item.source_kind || '')} · P${sbEscapeHtml(item.priority)}${item.id ? ` · <span class="font-mono normal-case text-gray-400">#${sbEscapeHtml(item.id)}</span>` : ''}${item.repeat_count > 1 ? ` <span class="nx-tag">×${sbEscapeHtml(item.repeat_count)}</span>` : ''}${item.ask_missing ? ` <span class="nx-tag nx-tag-warn">no ask · ${sbEscapeHtml(item.created_by || 'unknown')}</span>` : ''}</div>
                <div class="text-base font-semibold text-white break-words">${sbEscapeHtml(item.title)}</div>
                ${item.detail ? `<div class="nx-clamp text-sm text-gray-300 mt-1 break-words">${sbEscapeHtml(item.detail)}</div>` : ''}
            </div>
            ${action}
        </div>
    `;
    return div;
}

function sbAernSection(title, items) {
    const sec = document.createElement('section');
    sec.className = 'mb-6';
    sec.innerHTML = `<h3 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">${title} <span class="font-normal">(${items.length})</span></h3><div class="space-y-3" data-aern-group></div>`;
    const g = sec.querySelector('[data-aern-group]');
    items.forEach((item) => g.appendChild(sbRenderAernItem(item)));
    return sec;
}

function sbRenderAern(data) {
    const el = document.getElementById('aern-list');
    if (!el) return;
    const all = Array.isArray(data.items) ? data.items.slice() : [];
    // Today's dated Todoist tasks anchor the top (the personal-GTD half of the
    // morning view); pure-FYI queue items (effort==="read") follow as a quick
    // skim; everything else is the priority-ordered "needs you" lane below.
    const today = all.filter((i) => i.source_kind === 'todoist')
        .sort((a, b) => (a.priority || 3) - (b.priority || 3));
    const reads = all.filter((i) => i.source_kind === 'queue' && i.effort === 'read');
    const needs = all.filter((i) => i.source_kind !== 'todoist' && !(i.source_kind === 'queue' && i.effort === 'read'))
        .sort((a, b) => (a.priority || 3) - (b.priority || 3));

    el.innerHTML = '';
    if (!all.length) {
        el.innerHTML = '<p class="text-center text-lg text-gray-400 py-6">Nothing needs you. Go live your life.</p>';
    } else {
        if (today.length) el.appendChild(sbAernSection('📅 Today', today));
        if (reads.length) el.appendChild(sbAernSection('📖 Read &amp; done', reads));
        if (needs.length) {
            el.appendChild(sbAernSection('🎯 Needs you', needs));
        } else if (reads.length) {
            const done = document.createElement('p');
            done.className = 'text-sm text-gray-400';
            done.textContent = 'Nothing else waiting — just the reads above.';
            el.appendChild(done);
        }
    }

    const genEl = document.getElementById('aern-generated-at');
    if (genEl) genEl.textContent = sbRelativeTime(data.generated_at);
}

async function sbLoadAern() {
    try {
        const data = await sbFetchJson('/api/needs-aern');
        sbRenderAern(data || {});
        sbWireClamps();
    } catch (err) {
        console.error('Failed to load needs-aern:', err);
        const el = document.getElementById('aern-list');
        if (el) el.innerHTML = '<div class="text-center text-red-400 text-sm">Failed to load.</div>';
    }
}

// ── /nexus/aern live agenda ───────────────────────────────────────────────
// Each {p:<id>} line carries its project's LIVE board status (the board is
// canon). A done project shows its unchecked line struck through; the stored
// agenda text is untouched until an agent ticks it.
const SB_PROJECT_BADGE = {
    active: ['text-emerald-400', 'active'],
    blocked: ['text-amber-400', 'blocked'],
    parked: ['text-gray-400', 'parked'],
    done: ['text-emerald-400', 'done ✓'],
    unknown: ['text-red-400', 'not on board'],
};

/** Escape, then allow **bold** and `code` - the only markdown the agenda uses. */
function sbInlineMd(s) {
    return sbEscapeHtml(s)
        .replace(/\*\*([^*]+)\*\*/g, '<strong class="text-white">$1</strong>')
        .replace(/`([^`]+)`/g, '<code class="font-mono text-[11px] bg-dark-bg border border-dark-border rounded px-1">$1</code>');
}

function sbRenderAgendaLine(l) {
    if (l.kind === 'heading') {
        return l.level <= 1
            ? `<h3 class="text-base font-semibold text-white mb-1">${sbInlineMd(l.text)}</h3>`
            : `<h4 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mt-3 mb-1">${sbInlineMd(l.text)}</h4>`;
    }
    if (l.kind === 'text') {
        // "> note" lines are the agenda's summary blockquote: drop the marker.
        return `<p class="text-sm text-gray-400 mb-1">${sbInlineMd(l.text.replace(/^>\s?/, ''))}</p>`;
    }
    const done = l.checked === true || l.auto_done;
    const box = l.kind === 'item' ? (done ? '✅' : '⬜') : '•';
    const badges = (l.projects || []).map((p) => {
        const [cls, label] = SB_PROJECT_BADGE[p.status] || SB_PROJECT_BADGE.unknown;
        const next = p.next_step
            ? `<div class="text-[11px] text-gray-300 mt-1 whitespace-pre-wrap break-words">${p.blocked_on ? `<span class="text-amber-400">blocked on ${sbEscapeHtml(p.blocked_on)}</span> · ` : ''}${sbEscapeHtml(p.next_step)}</div>`
            : '';
        return `<details class="inline-block"><summary class="cursor-pointer text-[11px] ${cls}">● ${sbEscapeHtml(label)}</summary><div class="text-[11px] text-gray-400 mt-1"><span class="font-mono">${sbEscapeHtml(p.id)}</span>${p.title ? ` · ${sbEscapeHtml(p.title)}` : ''}</div>${next}</details>`;
    }).join(' ');
    const auto = l.auto_done ? ' <span class="text-[11px] text-emerald-400">(project done)</span>' : '';
    return `<div class="flex gap-2 text-sm mb-1" style="margin-left:${Math.min(l.indent || 0, 8) * 6}px">
        <span class="shrink-0">${box}</span>
        <div class="min-w-0 break-words ${done ? 'line-through opacity-60 text-gray-400' : 'text-gray-200'}">${sbInlineMd(l.text)}${auto} ${badges}</div>
    </div>`;
}

function sbRenderAgendaBlock(elId, entry, emptyText) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!entry || !Array.isArray(entry.lines) || !entry.lines.length) {
        el.innerHTML = `<p class="text-sm text-gray-400">${emptyText}</p>`;
        return;
    }
    const items = entry.lines.filter((l) => l.kind === 'item');
    const doneCount = items.filter((l) => l.checked === true || l.auto_done).length;
    el.innerHTML = entry.lines.map(sbRenderAgendaLine).join('')
        + `<p class="text-[11px] text-gray-400 mt-3">${doneCount}/${items.length} done · updated ${sbRelativeTime(entry.updated_at)} by ${sbEscapeHtml(entry.updated_by || 'unknown')}</p>`;
}

async function sbLoadAgenda() {
    try {
        const data = await sbFetchJson('/api/agenda/view');
        sbRenderAgendaBlock('agenda-daily', data.daily, 'No daily agenda yet.');
        sbRenderAgendaBlock('agenda-weekly', data.weekly, 'No weekly agenda yet.');
    } catch (err) {
        console.error('Failed to load agenda:', err);
        const el = document.getElementById('agenda-daily');
        if (el) el.innerHTML = '<div class="text-center text-red-400 text-sm">Failed to load agenda.</div>';
    }
}

// ── Shared boot / auto-refresh ────────────────────────────────────────────
// Long queue text (10/10: a multi-KB digest filled /nexus/queue): clamp to a few
// lines and add a "more" toggle only where the text actually overflows.
function sbWireClamps(root) {
    (root || document).querySelectorAll('.nx-clamp:not([data-clamp-done])').forEach((el) => {
        if (el.closest('details:not([open])') || el.offsetParent === null) return; // hidden: measured when its <details> opens
        el.dataset.clampDone = '1';
        if (el.scrollHeight <= el.clientHeight + 2) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'nx-clamp-btn';
        btn.textContent = 'more';
        btn.addEventListener('click', () => {
            const open = el.classList.toggle('open');
            btn.textContent = open ? 'less' : 'more';
        });
        el.after(btn);
    });
}

document.addEventListener('toggle', (e) => { if (e.target.open) sbWireClamps(e.target); }, true);

function sbLoadAll() {
    if (document.getElementById('seat-groups')) sbLoadSeat();
    if (document.getElementById('queue-to-aern-open')) sbLoadQueue();
    if (document.getElementById('aern-list')) sbLoadAern();
    if (document.getElementById('agenda-daily')) sbLoadAgenda();
}

function sbStartAutoRefresh() {
    if (sbRefreshTimer) clearInterval(sbRefreshTimer);
    sbRefreshTimer = setInterval(sbLoadAll, SB_REFRESH_INTERVAL);
}

function sbStopAutoRefresh() {
    if (sbRefreshTimer) {
        clearInterval(sbRefreshTimer);
        sbRefreshTimer = null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    sbWireQueueForm();
    sbWireQueueResolve();
    sbLoadAll();
    sbStartAutoRefresh();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            sbStopAutoRefresh();
        } else {
            sbLoadAll();
            sbStartAutoRefresh();
        }
    });
});
