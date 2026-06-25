# Nexus — deployment & remaining work

The nexus (Phases 0–3) is built, verified locally, and committed. It runs inside the
existing `aernhome` Flask app, **Tailscale-only**, reached at
`http://ashaman.tail125d67.ts.net:5555/nexus`. This file is the runbook for getting it
live on Ashaman and the work that still needs you.

## Deploy to Ashaman (when you're back at it)
1. On Ashaman: `git pull` in `C:\projects\aernhome`.
2. `docker compose build` then `docker compose up -d` — **locally on Ashaman**, not over
   SSH (BuildKit/cred + DERP-drop issues; see CLAUDE.md).
3. `nexus.db` auto-creates in the existing `/data` volume on first run — nothing to seed
   for goals/maintenance/capture.

## Required env (aernhome service)
- **`TODOIST_TOKEN`** — the Today widget needs the token in the container; a headless
  worker can't DPAPI-unlock Bitwarden. Put `TODOIST_TOKEN=<token>` in the aernhome
  service env (or pass `BW_SESSION`). Everything else reads local files/DBs.

## ⚠️ Open decision — books data lives on NenTera, not Ashaman
`tcg_alerts` already works on Ashaman (it reads the hourly inventory.db **mirror** mounted
at `/tcg`). But the **books pipeline reads NenTera-local paths** the Ashaman container can't
see: `C:\Users\matth\Calibre Library\metadata.db` and `C:\Users\matth\Obivault\Books`.
Pick one (this is your call):
- **(a)** Sync Obsidian `Books/` + Calibre `metadata.db` + cover files to a NAS path and
  mount it read-only into the container (set `OBSIDIAN_BOOKS` / `CALIBRE_DB` /
  `_COVER_ROOTS`). Cleanest if you want covers served from Ashaman.
- **(b)** Run `py nexus_books_import.py` on **NenTera** (where the data lives) against a
  NenTera-mounted copy of `nexus.db`, then let `nexus.db` sync — but the live DB is on
  Ashaman, so this needs a sync story (avoid clobbering app status changes).
- **(c)** Run the **nexus on NenTera** instead of Ashaman (all your personal data —
  Calibre, Obsidian, inventory canonical — already lives here; Tailscale reaches NenTera
  too). Splits nexus off from the Ashaman aernhome deploy.
Until resolved, the books shelf on Ashaman shows the "not seeded yet" state and the home
"reading" widget falls back to empty — no errors, just no books.

## Backups (Phase 5)
`nexus_backup.py` does a safe online `.backup` of `nexus.db` → `NEXUS_BACKUP_DIR`
(default `H:/aernhome/backups`), integrity-checked, keeps last 14. Schedule it daily on
the host that runs the nexus (Task Scheduler), e.g. `py C:\projects\aernhome\nexus_backup.py`.

## Phase 4 — Notion retirement (needs you + n8n, after deploy)
The write APIs n8n needs already exist (`/api/nexus/capture`, `/api/nexus/maintenance`).
Once the nexus is live on the mesh:
1. Re-point the n8n **"Maintenance Log → Inbox"** + weather/capture flows to POST the
   aernhome `/api/nexus/*` endpoints instead of Notion.
2. Keep the **Media-Tracker enrichment** (IGDB/TMDB/OpenLibrary) feeding `book_status`
   until the local book feed is proven, then retire it.
3. Retire each Notion DB (Inbox → capture, Maintenance Log → maintenance) once its local
   replacement is trusted. Decide the cutover per-DB.
