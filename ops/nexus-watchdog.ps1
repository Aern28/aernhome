<#
  nexus-watchdog.ps1  --  self-heals the Docker Desktop port-proxy break that
  silently takes the Nexus board (aernhome-dashboard :5555) offline.

  Runs LOCALLY on Ashaman (the Docker host) every 5 min via scheduled task.
  Runs here, not on Phoenix, because Ashaman is always-on and can check the app
  from INSIDE the container to tell a proxy-break apart from a real app failure.

  Logic:
    1. Probe the PUBLISHED port (what the fleet actually consumes).
       - reachable  -> healthy; log only on a state change (no 288 lines/day).
    2. unreachable -> ask the app from INSIDE the container:
       - inside OK   -> published-port proxy break (the known bug). Restart the
                        container (rebuilds the proxy), then re-verify.
       - inside DOWN -> real app/container fault. Restart ONCE if not in cooldown,
                        but log it LOUD as needing a human look -- don't mask it.
       - can't exec / not running -> real outage; log ALERT, do NOT loop-restart.
    3. Cooldown: never restart more than once per COOLDOWN_MIN, so a restart that
       doesn't fix it can't turn into a 5-minute restart storm.

  Non-destructive: `docker restart` only. No data touched (Nexus DB is bind-mounted).
#>

$ErrorActionPreference = 'Stop'
$Published   = 'http://127.0.0.1:5555/api/fleet'   # host->container via docker-proxy (breaks with the bug)
$Container   = 'aernhome-dashboard'
$InsideUrl   = 'http://127.0.0.1:5555/api/fleet'   # app-local, inside the container (stays up during the bug)
$LogDir      = 'C:\tools\logs'
$Log         = Join-Path $LogDir 'nexus-watchdog.log'
$StateFile   = Join-Path $LogDir 'nexus-watchdog-state.json'
$CooldownMin = 15
$ProbeSec    = 8

# docker.exe resolved EXPLICITLY: Task Scheduler's PATH lacks Docker's bin dir, so a
# bare "docker" throws CommandNotFoundException under ErrorActionPreference='Stop' and
# killed this watchdog silently from 2026-07-27 to 2026-08-03. Never trust PATH here.
$Docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $Docker) { $Docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log([string]$msg) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Add-Content -Path $Log -Value $line
}

function Get-State {
  if (Test-Path $StateFile) { try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
  return [pscustomobject]@{ status = 'unknown'; last_restart = $null }
}
function Set-State($status, $lastRestart) {
  [pscustomobject]@{ status = $status; last_restart = $lastRestart } |
    ConvertTo-Json | Set-Content -Path $StateFile
}

function Test-Published {
  try { Invoke-RestMethod -Uri $Published -TimeoutSec $ProbeSec | Out-Null; return $true }
  catch { return $false }
}

# A terminating error MUST reach the log. This watchdog died invisibly for a week
# because an unhandled throw exits 1 while writing nothing anywhere, so the log read
# "PROBE FAILED" then silence and the state file still said healthy.
trap { Write-Log ("FATAL: " + $PSItem.Exception.Message); exit 9 }

$state = Get-State

# --- 1. published-port probe ---
if (Test-Published) {
  if ($state.status -ne 'healthy') { Write-Log "OK: Nexus reachable (recovered from '$($state.status)')" }
  Set-State 'healthy' $state.last_restart
  exit 0
}

Write-Log "PROBE FAILED: $Published unreachable"

# --- cooldown guard ---
if ($state.last_restart) {
  $since = (Get-Date) - [datetime]$state.last_restart
  if ($since.TotalMinutes -lt $CooldownMin) {
    Write-Log ("SKIP restart: last restart {0:N1} min ago (< {1} min cooldown)" -f $since.TotalMinutes, $CooldownMin)
    Set-State 'unhealthy-cooldown' $state.last_restart
    exit 1
  }
}

# --- 2. is the container even running? ---
$running = (& $Docker inspect -f '{{.State.Running}}' $Container 2>$null)
if ($LASTEXITCODE -ne 0 -or $running -notmatch 'true') {
  Write-Log "ALERT: container '$Container' not running (state='$running'). Real outage -- NOT auto-restarting; needs a look."
  Set-State 'container-down' $state.last_restart
  exit 2
}

# --- ask the app from inside the container ---
$py = 'import urllib.request,sys' + "`n" +
      'try:' + "`n" +
      '  sys.exit(0 if urllib.request.urlopen(''' + $InsideUrl + ''',timeout=5).status==200 else 1)' + "`n" +
      'except Exception: sys.exit(1)'
# SINGLE quotes in the Python above are load-bearing: PowerShell strips embedded double
# quotes when passing an argument to a native exe, so the double-quoted form reached
# python as urlopen(http://...) -> SyntaxError -> exit 1 on EVERY run. $insideOk was
# therefore permanently false, and every proxy break would have been misreported as
# "app is DOWN inside the container too" instead of self-healing quietly. Fixed 2026-08-03.
& $Docker exec $Container python -c $py 2>$null
$insideOk = ($LASTEXITCODE -eq 0)

if ($insideOk) {
  Write-Log "DIAGNOSIS: app healthy INSIDE container but published port dead = Docker port-proxy break. Restarting $Container..."
} else {
  Write-Log "ALERT: app is DOWN inside the container too (not just the proxy). Restarting once, but this needs a human look."
}

# --- 3. restart (rebuilds the port-proxy) ---
& $Docker restart $Container 2>$null | Out-Null
$now = (Get-Date).ToString('o')
Start-Sleep -Seconds 10

if (Test-Published) {
  Write-Log "RECOVERED: Nexus reachable after restart of $Container."
  Set-State 'healthy' $now
  exit 0
} else {
  Write-Log "STILL DOWN after restart -- escalate (manual look needed)."
  Set-State 'still-down' $now
  exit 3
}
