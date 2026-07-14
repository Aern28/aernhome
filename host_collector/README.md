# Fleet Host Stats Collector

`fleet_host_stats.ps1` runs on the Windows Docker host **Ashaman** and collects a
handful of host-level health signals that can't be observed from inside a
container (Task Scheduler state, mapped drives, the TCGplayer automation
browser session, host disk space, Tailscale, and the interactive login used
for printing). It writes them as a single JSON file:

```
C:\projects\aernhome\data\host_stats.json
```

A Flask app running in a container on Ashaman reads that file (via a bind
mount of the `data` directory) to show fleet status.

## Output schema

```json
{
  "generated_at": "2026-07-05T22:15:13-05:00",
  "checks": [
    {"id": "tcg_autoprocess",    "label": "TCG AutoProcess Task",              "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "nexus_backup",       "label": "Nexus Backup (H:)",                 "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "supersaiyan_backup", "label": "Supersaiyan Backup (F:)",           "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "chrome_cdp",         "label": "Chrome CDP (TCGplayer automation)", "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "disk_c",             "label": "Disk Space (C:)",                   "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "tailscale",          "label": "Tailscale",                        "status": "up|warn|down|unknown", "detail": "..."},
    {"id": "matt_session",       "label": "Matt Interactive Session",          "status": "up|warn|down|unknown", "detail": "..."}
  ]
}
```

`generated_at` is ISO 8601 with a numeric UTC offset (`yyyy-MM-ddTHH:mm:sszzz`),
matching the host's local time zone.

## Checks

1. **tcg_autoprocess** - queries the Task Scheduler task `TCG-AutoProcess` via
   `schtasks /query /tn TCG-AutoProcess /v /fo csv`. `up` if it last ran under
   40 minutes ago with result `0`; `warn` if the last result was nonzero;
   `down` if it's been more than 40 minutes since the last run, or the task
   is disabled or missing.
2. **nexus_backup** - newest file under `\\192.168.1.118\home\aernhome\backups`
   (UNC, matching `nexus_backup.py`). `up` if under 26h old, `warn` if under
   50h, `down` if older. Before the reachability test the check drops any
   cached SMB connection to the share (`net use \\...\home /delete`) so each
   poll opens a **fresh authenticated session** against the cmdkey-stored
   credential — a rotated/stale NAS password therefore goes `down` within one
   poll cycle instead of staying green on a cached session (added after the
   7/14 NAS rotation, where the board stayed green for hours on a dead cred).
   `down` with `"not reachable on a FRESH SMB session"` means stored
   credential stale/rotated or share down.
3. **supersaiyan_backup** - newest `backup_log_*.txt` directly on `F:\`
   (monthly job). `up` if under 35 days old, `warn` if under 45, `down` if
   older or `F:\` is missing.
4. **chrome_cdp** - hits `http://127.0.0.1:9222/json/version` (the Chrome
   DevTools Protocol port used by the TCGplayer automation session). `up` if
   it returns JSON (detail includes the browser version string), `down`
   otherwise.
5. **disk_c** - free space on `C:`. `warn` under 30GB, `down` under 10GB,
   otherwise `up`.
6. **tailscale** - `tailscale status --json`. `up` if `Self.Online` is true
   (detail includes the online/total peer count); `down` if not online;
   `unknown` if the `tailscale` CLI can't be found.
7. **matt_session** - `quser` output for an active session belonging to
   `matt`/`Matt` (this account drives label printing in the TCG pipeline).
   `up` if Active, `warn` if Disconnected, `down` if no session is found.

## Reliability

- Every check runs inside its own `try/catch`, both internally and again at
  the call site (`Invoke-CheckSafely`). Anything unexpected that a check
  doesn't already handle degrades to `status: "unknown"` with the exception
  message in `detail` - it never aborts the whole script.
- If JSON serialization itself somehow fails, the script falls back to a
  minimal hand-built JSON string so a well-formed file is still produced.
- Output is written atomically: the JSON is written to
  `host_stats.json.tmp` next to the destination, then moved into place with
  `Move-Item -Force`. The Flask app never sees a partially-written file.
- Written as UTF-8 without a BOM (matches Python's `json.load`).
- The script is Windows PowerShell 5.1-safe (no PS7-only syntax), since
  Task Scheduler on Ashaman invokes `powershell.exe`, not `pwsh.exe`.
- The scheduled task is configured to run whether or not a user is logged
  on. Only `matt_session` (by design) and `nexus_backup`'s H: drive check
  depend on session/drive-mapping state - that's intentional, since a
  missing interactive session or missing mapped drive *is* the condition
  being monitored, not a script bug.

## Registering the scheduled task

Run this once, from an elevated PowerShell/cmd prompt on Ashaman, logged in
as (or specifying credentials for) the `Matt` account, so the task runs in
that user's context every 10 minutes starting immediately:

```
schtasks /create /tn "Fleet Host Stats" /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\projects\aernhome\host_collector\fleet_host_stats.ps1" /sc minute /mo 10 /ru Matt /rp * /st %time:~0,5% /rl LIMITED /f
```

Notes:
- `/rp *` prompts for the `Matt` account password interactively at creation
  time (needed so the task can run whether or not `Matt` is logged in).
- `/st %time:~0,5%` starts the schedule at (approximately) the current time,
  so the first run kicks off immediately for a `/sc minute /mo 10` recurrence
  starting "now"; adjust to a literal `HH:mm` if running this from
  PowerShell (`%time%` is a cmd.exe-ism - in PowerShell substitute the
  current time manually, e.g. `/st 14:05`).
- `/f` forces overwrite if a task of the same name already exists.

## Uninstalling

```
schtasks /delete /tn "Fleet Host Stats" /f
```

## Manual test

Run it directly to confirm it still writes valid JSON to the real path (or
pass `-OutPath` to write somewhere else for testing without touching the
live file the Flask app reads):

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\projects\aernhome\host_collector\fleet_host_stats.ps1
```

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\projects\aernhome\host_collector\fleet_host_stats.ps1 -OutPath C:\Temp\host_stats_test.json
```

The script also prints the JSON it wrote to stdout, so running it manually
(e.g. from Task Scheduler's "Run" button, or interactively) is
self-verifying without needing to separately open the output file.
