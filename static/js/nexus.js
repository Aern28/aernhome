// nexus.js — write-back interactions for the personal nexus (Tailscale-only).
// Plain vanilla JS + event delegation; endpoints are /api/nexus/* (POST, JSON).

async function nexusPost(url, body) {
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

document.addEventListener("DOMContentLoaded", () => {
  // ── Quick capture ──
  const capForm = document.querySelector("[data-nx-capture]");
  if (capForm) {
    const input = capForm.querySelector("input[type=text]");
    const list = document.querySelector("[data-nx-capture-list]");
    const submit = async () => {
      const text = input.value.trim();
      if (!text) return;
      const res = await nexusPost("/api/nexus/capture", { text });
      if (res.ok) {
        input.value = "";
        if (list) {
          const li = document.createElement("li");
          li.className = "flex items-center gap-2 text-sm py-1 border-t border-dark-border first:border-0";
          li.dataset.capture = res.id;
          li.innerHTML =
            '<span class="flex-1 text-gray-300"></span>' +
            `<button data-nx-action="capture-to-goal" data-id="${res.id}" class="text-[11px] text-gray-500 hover:text-blue-400">→ goal</button>` +
            `<button data-nx-action="capture-dismiss" data-id="${res.id}" class="text-[11px] text-gray-500 hover:text-red-400">✕</button>`;
          li.querySelector("span").textContent = text; // textContent = XSS-safe
          list.prepend(li);
        }
      }
    };
    capForm.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  }

  // ── Delegated click handlers ──
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-nx-action]");
    if (!t) return;
    const action = t.dataset.nxAction;

    if (action === "todoist-close") {
      t.disabled = true;
      const res = await nexusPost(`/api/nexus/todoist/${t.dataset.id}/close`, {});
      const row = t.closest("li");
      if (res.ok && row) { row.style.opacity = "0.4"; row.style.textDecoration = "line-through"; t.remove(); }
      else t.disabled = false;
    }

    else if (action === "maint-done") {
      t.disabled = true;
      const res = await nexusPost(`/api/nexus/maintenance/${t.dataset.id}/done`, {});
      if (res.ok) { const row = t.closest("[data-row]"); if (row) row.remove(); }
      else t.disabled = false;
    }

    else if (action === "goal-status") {
      const res = await nexusPost(`/api/nexus/goal/${t.dataset.id}/status`, { status: t.dataset.status });
      if (res.ok) location.reload();
    }

    else if (action === "book-status") {
      const res = await nexusPost(`/api/nexus/book/${t.dataset.id}/status`, { status: t.dataset.status });
      if (res.ok) location.reload();
    }

    else if (action === "book-delete") {
      const res = await nexusPost(`/api/nexus/book/${t.dataset.id}/delete`, {});
      if (res.ok) { const card = t.closest("[data-book]"); if (card) card.remove(); }
    }

    else if (action === "capture-to-goal") {
      const res = await nexusPost(`/api/nexus/capture/${t.dataset.id}/to-goal`, {});
      if (res.ok) location.reload(); // surface the new goal in the goals widget
    }

    else if (action === "capture-dismiss") {
      const res = await nexusPost(`/api/nexus/capture/${t.dataset.id}/process`, {});
      if (res.ok) { const li = t.closest("[data-capture]"); if (li) li.remove(); }
    }

    else if (action === "link-delete") {
      const res = await nexusPost(`/api/nexus/link/${t.dataset.id}/delete`, {});
      if (res.ok) { const chip = t.closest("[data-link]"); if (chip) chip.remove(); }
    }

    // ── Media (TV) ──
    else if (action === "media-status") {
      const res = await nexusPost(`/api/nexus/media/${t.dataset.id}/status`, { status: t.dataset.status });
      if (res.ok) location.reload();
    }

    else if (action === "media-delete") {
      const res = await nexusPost(`/api/nexus/media/${t.dataset.id}/delete`, {});
      if (res.ok) { const card = t.closest("[data-media]"); if (card) card.remove(); }
    }

    // ── Notes ──
    else if (action === "note-pin") {
      const res = await nexusPost(`/api/nexus/note/${t.dataset.id}/pin`, { pinned: t.dataset.pinned !== "1" });
      if (res.ok) location.reload(); // re-sort + recolor
    }

    else if (action === "note-delete") {
      const res = await nexusPost(`/api/nexus/note/${t.dataset.id}/delete`, {});
      if (res.ok) { const card = t.closest("[data-note]"); if (card) card.remove(); }
    }

    else if (action === "note-edit") {
      const card = t.closest("[data-note]");
      if (!card || card.querySelector("[data-note-editor]")) return; // already editing
      const bodyEl = card.querySelector("[data-note-body]");
      const ta = document.createElement("textarea");
      ta.dataset.noteEditor = "1";
      ta.rows = 4;
      ta.value = bodyEl.textContent;
      ta.className = "w-full bg-dark-bg border border-dark-border rounded px-2 py-1 text-sm text-gray-200 focus:outline-none focus:border-blue-500 flex-1";
      const save = document.createElement("button");
      save.textContent = "save";
      save.dataset.nxAction = "note-save";
      save.dataset.id = t.dataset.id;
      save.className = "mt-1 self-start px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs";
      bodyEl.style.display = "none";
      bodyEl.after(ta);
      ta.after(save);
      ta.focus();
    }

    else if (action === "note-save") {
      const card = t.closest("[data-note]");
      const ta = card && card.querySelector("[data-note-editor]");
      if (!ta) return;
      const res = await nexusPost(`/api/nexus/note/${t.dataset.id}`, { body: ta.value });
      if (res.ok) location.reload();
    }
  });

  // ── Media progress (text input -> POST on change) ──
  document.addEventListener("change", async (e) => {
    const t = e.target.closest("[data-nx-action='media-progress']");
    if (!t) return;
    await nexusPost(`/api/nexus/media/${t.dataset.id}/progress`, { progress: t.value });
  });

  // ── Goal progress (range input -> POST on change) ──
  document.querySelectorAll("[data-nx-goal-progress]").forEach((rng) => {
    const id = rng.dataset.nxGoalProgress;
    const bar = document.querySelector(`[data-nx-goal-bar="${id}"]`);
    const lbl = document.querySelector(`[data-nx-goal-pct="${id}"]`);
    rng.addEventListener("input", () => {
      if (bar) bar.style.width = rng.value + "%";
      if (lbl) lbl.textContent = rng.value + "%";
    });
    rng.addEventListener("change", async () => {
      const res = await nexusPost(`/api/nexus/goal/${id}/progress`, { progress_pct: parseInt(rng.value, 10) });
      if (res.ok && parseInt(rng.value, 10) >= 100) location.reload(); // auto-marked done
    });
  });

  // ── Create forms (goal / maintenance) -> POST then reload ──
  document.querySelectorAll("[data-nx-form]").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const kind = form.dataset.nxForm; // "goal" | "maintenance"
      const body = {};
      form.querySelectorAll("[name]").forEach((el) => {
        if (el.value !== "") body[el.name] = el.value;
      });
      const urls = { goal: "/api/nexus/goal", maintenance: "/api/nexus/maintenance", link: "/api/nexus/link", media: "/api/nexus/media", note: "/api/nexus/note" };
      const res = await nexusPost(urls[kind], body);
      if (res.ok) location.reload();
      else {
        const err = form.querySelector("[data-nx-err]");
        if (err) err.textContent = res.error || "failed";
      }
    });
  });
});
