# Nexus — deployment & remaining work

The nexus (Phases 0–3) is built, verified locally, and committed. It runs inside the
existing `aernhome` Flask app, **Tailscale-only**, reached at
`http://ashaman.tail125d67.ts.net:5555/nexus`. This file is the runbook for getting it
live on Ashaman and the work that still needs you.

## Deploy to Ashaman — the 3-step finish (the build MUST be local, not SSH)
Everything below the build is already prepped from NenTera (code pushed, `data/` staged).
The one thing that can't be done remotely is `docker compose build` (BuildKit/cred + DERP
drop kills it over SSH — see CLAUDE.md). So this finishes in a short **RDP-to-Ashaman**
session:

1. **`git pull`** in `C:\projects\aernhome`.
2. **Add the connector tokens to `C:\projects\aernhome\.env`** (gitignored; sits next to
   the compose alongside the existing `AERNHOME_UNLOCK_TOKEN` / `CLOUDFLARE_TUNNEL_TOKEN`):
   ```
   TODOIST_TOKEN=<bw: "Todoist API Token" (notes)>
   TMDB_TOKEN=<bw: "TMDB API Key" (notes)>
   IGDB_CLIENT_ID=<bw: "Twitch IGDB" (username)>
   IGDB_CLIENT_SECRET=<bw: "Twitch IGDB" (password)>
   ```
   Any left blank → that connector just degrades (Todoist widget empty / posters
   title-only); nothing errors.
3. **`docker compose build aernhome ; docker compose up -d`** — **locally on Ashaman**.
   Verify on the mesh: `http://ashaman.tail125d67.ts.net:5555/nexus`.

`nexus.db` + `book_covers/` are shipped into the `/data` volume (staged via scp from
NenTera), so books/TV/games/notes/goals are all there on first boot. After this, **Ashaman
is canonical** — write to the live site, not the NenTera dev box.

## Books locality — RESOLVED (covers are portable)
Books are seeded/maintained on **NenTera** (where Calibre + Obsidian live) and the covers
are **materialized into `data/book_covers/<id>.jpg`** by `nexus_books_import.py`, with
`cover_ref` rewritten to a `DATA_DIR`-relative path. So the whole `data/` dir (db + covers)
ships to any host and covers serve with no Calibre/Obsidian mount and no re-import on
Ashaman. TV/game posters are remote TMDB/IGDB URLs — portable by nature.
**To refresh the shelf later:** run `py nexus_books_import.py` on NenTera, then re-ship
`nexus.db` + `book_covers/` to Ashaman (same scp as the initial deploy). A two-way sync
(like TCG's `db_sync.py`) is the eventual upgrade if book edits on the live site need to
survive a reseed; for now reseeds are NenTera→Ashaman one-way.

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
