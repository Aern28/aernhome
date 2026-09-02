# Nexus — deployment status & remaining work

The nexus (Phases 0–5) is built and **live** on Ashaman, Tailscale-only, at
`http://ashaman.tail125d67.ts.net:5555/nexus`. **Ashaman is canonical** —
write to the live site, not a dev checkout. This file now doubles as the
runbook for shipping *changes* to it, plus the remaining Phase 4 follow-up.

## Deploying changes (current workflow)

`docker-compose.yml` bind-mounts the repo checkout straight into `/app`
(`C:/projects/aernhome:/app`), so day-to-day changes need **no rebuild**:

```powershell
cd C:\projects\aernhome
git pull
docker restart aernhome-dashboard
```

Rebuild only when `requirements.txt` (Python deps) changes:

```powershell
docker compose build aernhome
docker compose up -d
```

**Bridge when a rebuild isn't possible right now** (2026-09-02, BYOS + Keep sync
deps): the container's image layer is writable, so packages can be hot-installed
and they survive `docker restart` — but NOT a recreate (`docker compose up -d`
after a compose change) and NOT a rebuild-less `--force-recreate`. Order matters:

```powershell
cd C:\projects\aernhome
git pull
docker compose up -d                      # recreate first (picks up compose changes, e.g. the :5556 port)
docker exec aernhome-dashboard pip install --no-cache-dir -r /app/requirements.txt
docker restart aernhome-dashboard
```

The fleet check `byos_deps` goes **down** the moment the bridge is lost, so a
silent regression is impossible; run the real `docker compose build aernhome`
from the console whenever convenient and the bridge stops mattering.

**Dev host**: NenTera (laptop) is currently down for an SSD replacement
(expected back ~2026-07-08) — Trainer (desktop) is the dev host in the
meantime. Once NenTera is restored, either machine can develop from; either
way, a full `docker compose build` still needs to run **locally on Ashaman**
(RDP or console session), not over SSH — BuildKit's credential helper plus
the Tailscale DERP relay reliably kill long builds over SSH (see the
Trainer/NenTera CLAUDE.md "Docker" lessons). `git pull` + `docker restart`
work fine over SSH.

## Connector tokens (`.env` on Ashaman)

Nexus connectors read their tokens from `C:\projects\aernhome\.env`
(gitignored), alongside the existing `AERNHOME_UNLOCK_TOKEN` /
`CLOUDFLARE_TUNNEL_TOKEN` / `SIGNAL_*` vars (see `README.md`):

```
TODOIST_TOKEN=<bw: "Todoist API Token" (notes)>
TMDB_TOKEN=<bw: "TMDB API Key" (notes)>
IGDB_CLIENT_ID=<bw: "Twitch IGDB" (username)>
IGDB_CLIENT_SECRET=<bw: "Twitch IGDB" (password)>
```

Any left blank → that connector just degrades (Todoist widget empty / posters
title-only); nothing errors. Only relevant again if Ashaman is reprovisioned
from scratch — these are already set on the live host.

## Data directory

Everything the nexus (and the rest of the dashboard) owns lives under
`C:\projects\aernhome\data` on Ashaman, bind-mounted to `/data` in the
container: `dashboard.db`, `nexus.db`, `book_covers/`, `services.json` (see
`README.md` → Configuration), `host_stats.json`, `nas_stats.json`. This is a
local disk path on Ashaman, **not** a NAS/`H:` path — the NAS is only used
for the H:/I: disk-usage cards and as the nexus backup destination below.

## Books locality — covers are portable

Books are seeded/maintained wherever Calibre + Obsidian live (historically
NenTera; while NenTera is down, this pauses — Trainer isn't set up with
those yet). Covers are **materialized into `data/book_covers/<id>.jpg`** by
`nexus_books_import.py`, with `cover_ref` rewritten to a `DATA_DIR`-relative
path, so the whole `data/` dir (db + covers) is portable across hosts and
covers serve with no Calibre/Obsidian mount needed on Ashaman. TV/game
posters are remote TMDB/IGDB URLs — portable by nature.

**To refresh the shelf later:** run `py nexus_books_import.py` on whichever
machine has Calibre + Obsidian, then ship `nexus.db` + `book_covers/` to
Ashaman's `data/` dir. A two-way sync (like TCG's `db_sync.py`) is the
eventual upgrade if book edits made on the live site need to survive a
reseed; for now reseeds are one-way (dev host → Ashaman).

## Backups (Phase 5)

`nexus_backup.py` does a safe online `.backup` of `nexus.db` →
`NEXUS_BACKUP_DIR` (default `H:/aernhome/backups`), integrity-checked, keeps
last 14. Scheduled daily on Ashaman via Task Scheduler:
`py C:\projects\aernhome\nexus_backup.py`.

**Known issue**: this is currently **failing** — the `H:` mapped drive isn't
reliably visible from the Task Scheduler service context that runs the
backup job, even though it shows up fine in an interactive session. The
Fleet board's `nexus_backup` check (see `README.md` → Fleet Board & Host
Stats, and `host_collector/README.md`) exists specifically to surface this
silently-failing job — it currently reports `down` with detail `"H: not
accessible from this session"`. Not yet fixed; tracked on `/nexus/fleet`.

## Phase 4 — Notion retirement (needs you + n8n)

The write APIs n8n needs already exist (`/api/nexus/capture`,
`/api/nexus/maintenance`). With the nexus live on the mesh:

1. Re-point the n8n **"Maintenance Log → Inbox"** + weather/capture flows to
   POST the aernhome `/api/nexus/*` endpoints instead of Notion.
2. Keep the **Media-Tracker enrichment** (IGDB/TMDB/OpenLibrary) feeding
   `book_status` until the local book feed is proven, then retire it.
3. Retire each Notion DB (Inbox → capture, Maintenance Log → maintenance)
   once its local replacement is trusted. Decide the cutover per-DB.

Note: n8n itself (the automation running these flows) is flagged
`"deprecated": true` in `services.json` — it's barely used day-to-day and is
a longer-term retirement candidate once Phase 4 completes and any other
n8n-only flows are migrated or dropped.

---

## The seat-board write contract (`/api/seat`)

*Added 2026-08-06 at Ashaman's request (queue `aa1feb37`): it had to reverse-engineer all of this
from `second_brain.py` because none of it was written down. The seat is the fleet's cross-machine
state — every seat reads it first — so a caller that gets this wrong can silently delete another
machine's work.*

### Read

```
GET http://100.110.245.37:5555/api/seat     # tailnet; on the home LAN 192.168.1.141:5555 also works
```
Returns `{updated_at, updated_at_local, updated_by, projects:[...]}`. **Every UTC timestamp ships a
`_local` companion** (`2026-08-03 15:17 CDT`) — read those; both a seat and Aern once misread `20:17`
as 8pm when it was 15:17.

### Write — two branches, and only one is safe by default

```jsonc
POST /api/seat   Content-Type: application/json
{ "updated_by": "<seat>-claude",          // YOUR seat, never a copy-pasted one
  "base_updated_at": "<the updated_at you READ>",
  "project":  { ... } }                   // ✅ singular = UPSERT by id
```

| branch | behaviour | when |
|---|---|---|
| `project` (singular) | **Upsert by `id`.** Merges into the existing record; cannot touch sibling entries. | **Default. Use this.** |
| `projects` (plural) | **FULL REPLACE — anything absent from your payload is DELETED.** | Only for a deliberate whole-board rewrite, and only with `base_updated_at`. |

**Optimistic concurrency:** send `base_updated_at` (the `updated_at` from your GET). If the board
moved since you read it you get **409** plus `current_updated_at` / `current_updated_by` /
`current_projects` — merge and retry rather than clobbering. Omit it and you silently win, which is
how two seats overwrote each other on 2026-08-03.

**Upserts are PATCH-style.** `_clean_project()` fills any field you omit from the existing record, so
`{"id":"foo","status":"done"}` flips status and leaves title/detail/next_step/links intact. You do not
need to round-trip the whole object.

### Project schema

`id` · `title` · `status` · `detail` · `next_step` · `blocked_on` · `links[]`

- `status` ∈ **`active` | `parked` | `done`** — an invalid value silently falls back to the existing
  status (or `active`), so a typo looks like it worked. Spell it right.
- `id` — omit it and one is generated from the title plus a random suffix, which creates a duplicate
  lane instead of updating yours. **Always send the id you mean.**
- `blocked_on` — `"aern"` when it needs the human; `null` otherwise.
- `links` — `[{label, ref}]`, ref being a path/commit/URL.

### Naming convention

`updated_by` is `<host>-claude`: `trainer-claude`, `phoenix-claude`. Non-seat writers use their own
identity (`claude-code@ashaman`, `phoenix-mover-scan`, `fable@trainer`) — useful for telling a human
seat's write from a scanner's.

### Housekeeping

```
POST /api/seat/prune   {"updated_by":"<seat>-claude"}    # physically removes every status:"done" entry
```
Closing a lane is `status:"done"`; **prune** is the separate, deliberate step that removes them. Keep
recently-closed entries visible for a while — they are the record of what just happened — then prune.

### Two rules that are not in the code

1. **Write back on closure.** When Aern completes something an entry was waiting on, updating that
   entry is *part of* the task, same turn. Otherwise another seat reads a stale board and confidently
   reports finished work as blocked (this happened 2026-08-05).
2. **Never fossilise a volatile number** in a `next_step` ("expect $27.11"). Write "whatever the source
   says, with a date" — a stale expectation becomes a false instruction to the next reader.
