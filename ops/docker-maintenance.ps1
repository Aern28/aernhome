# Docker Maintenance for Ashaman -- prune + compact the WSL vhdx, then GUARANTEE recovery.
#
# HISTORY / WHY THIS IS THE WAY IT IS (2026-08-03):
#   The original version ran `docker system prune -f`, then `wsl --shutdown`, compacted
#   the vhdx -- and STOPPED. Its final line was literally "Restart Docker Desktop to
#   continue using containers." So every Sunday at 04:00 it stopped the entire fleet's
#   Docker stack and relied on Docker Desktop happening to restart the VM on its own.
#   Its intended safety net, the "Docker Startup" scheduled task, had last run
#   2026-07-27, FAILED (result 1), and had no next run scheduled -- i.e. dead.
#   It also wrote nothing to disk, so a failure would have been invisible.
#
#   This version: records what was running BEFORE, always attempts recovery in a
#   finally block (even if compaction throws), waits for the engine, restarts anything
#   that did not come back, verifies the Nexus board, and logs every step. It exits
#   non-zero on an unhealthy end state so Task Scheduler shows a real failure.
#
# Usage:
#   docker-maintenance.ps1               full run (prune + compact + recover)
#   docker-maintenance.ps1 -RecoverOnly  NON-DESTRUCTIVE: exercise recovery/verify only
#   docker-maintenance.ps1 -SkipCompact  prune + recover, no WSL stop / no compaction

param(
    [switch]$RecoverOnly,
    [switch]$SkipCompact
)

$ErrorActionPreference = 'Stop'
$LogDir = 'C:\tools\logs'
$Log    = Join-Path $LogDir 'docker-maintenance.log'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $Log -Value $line
    Write-Host $line
}

# Never trust PATH from a scheduled task -- this is exactly what killed nexus-watchdog
# silently from 2026-07-27 to 2026-08-03.
$Docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $Docker) { $Docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
if (-not (Test-Path $Docker)) { Write-Log "FATAL: docker.exe not found at '$Docker'"; exit 9 }

# A terminating error must reach the log, not vanish into a non-zero exit code.
trap { Write-Log ("FATAL: " + $PSItem.Exception.Message); exit 9 }

function Test-Engine {
    & $Docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-RunningContainers {
    if (-not (Test-Engine)) { return @() }
    $names = & $Docker ps --format '{{.Names}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $names) { return @() }
    return @($names | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Wait-Engine([int]$TimeoutSec = 300) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $nudged = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-Engine) { return $true }
        # A plain `docker` call normally wakes the engine on demand. If it hasn't come
        # back after 60s, explicitly ask Docker Desktop to start it (once).
        if (-not $nudged -and (Get-Date) -gt $deadline.AddSeconds(-($TimeoutSec - 60))) {
            $cli = 'C:\Program Files\Docker\Docker\resources\bin\com.docker.cli.exe'
            $dd  = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
            if (Test-Path $cli) {
                Write-Log "engine still down after 60s -- asking Docker Desktop to start it"
                & $Docker desktop start 2>$null | Out-Null
            } elseif ((Test-Path $dd) -and -not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
                Write-Log "engine still down after 60s -- launching Docker Desktop"
                Start-Process $dd | Out-Null
            }
            $nudged = $true
        }
        Start-Sleep -Seconds 5
    }
    return (Test-Engine)
}

Write-Log "=== Docker Maintenance start (RecoverOnly=$RecoverOnly SkipCompact=$SkipCompact) ==="

# --- capture the pre-state so recovery has a target to restore -------------------
$before = Get-RunningContainers
Write-Log ("pre-state: {0} containers running" -f $before.Count)
if ($before.Count -eq 0) { Write-Log "WARNING: no containers running before maintenance (engine down already?)" }

$compacted = $false
$sizeBefore = $null
$sizeAfter  = $null

try {
    if (-not $RecoverOnly) {
        # --- 1. prune ------------------------------------------------------------
        Write-Log "[1/4] pruning unused images/containers/networks..."
        & $Docker system prune -f 2>&1 | ForEach-Object { Write-Log ("    " + $_) }

        if (-not $SkipCompact) {
            # --- 2. locate the vhdx BEFORE stopping anything ----------------------
            # Resolved from the REGISTERED WSL distro, not a hardcoded guess list.
            # The old guess list matched 'G:\Docker\wsl\disk\docker_data.vhdx' -- a stale
            # 23.8 GB leftover last written 2026-02-24 -- while the live disk was the
            # 55.7 GB one under the CustomWslDistroDir. So every Sunday this stopped the
            # whole fleet to compact a DEAD file. Pick by "actually being written to".
            $vhdxPath = $null
            $cands = New-Object System.Collections.Generic.List[string]
            Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue | ForEach-Object {
                $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
                if ($props.DistributionName -match 'docker') {
                    $bp = $props.BasePath -replace '^\\\\\?\\', ''
                    $parent = Split-Path $bp -Parent
                    $cands.Add((Join-Path $parent 'disk\docker_data.vhdx'))
                    $cands.Add((Join-Path $bp 'docker_data.vhdx'))
                    $cands.Add((Join-Path $bp 'ext4.vhdx'))
                }
            }
            foreach ($p in @("$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx",
                             "$env:LOCALAPPDATA\Docker\wsl\data\ext4.vhdx")) { $cands.Add($p) }

            $live = $cands | Where-Object { $_ -and (Test-Path $_) } |
                    Get-Item -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1

            if ($live) {
                $ageH = ((Get-Date) - $live.LastWriteTime).TotalHours
                if ($ageH -gt 24) {
                    # Refuse to take an outage for a file nothing is writing to.
                    Write-Log ("SKIP compaction: newest candidate '{0}' was last written {1:N1}h ago -- that is not the live disk. Stack left running." -f $live.FullName, $ageH)
                } else {
                    $vhdxPath = $live.FullName
                    Write-Log ("    resolved live vhdx: {0} (written {1:N1}h ago)" -f $vhdxPath, $ageH)
                }
            }

            if (-not $vhdxPath -or -not (Test-Path $vhdxPath)) {
                # Bail BEFORE the destructive step -- never stop the stack for a compaction
                # we then can't perform.
                Write-Log "SKIP compaction: docker_data.vhdx not found. Stack left running."
            } else {
                $sizeBefore = [math]::Round((Get-Item $vhdxPath).Length / 1GB, 2)
                Write-Log ("[2/4] vhdx = {0} ({1} GB)" -f $vhdxPath, $sizeBefore)

                Write-Log "[3/4] stopping WSL for compaction (stack goes down here)..."
                & wsl.exe --shutdown
                Start-Sleep -Seconds 8

                $dp = "select vdisk file=`"$vhdxPath`"`r`ncompact vdisk`r`nexit"
                $dp | & diskpart.exe 2>&1 | ForEach-Object { if ($_ -match '\S') { Write-Log ("    " + $_.Trim()) } }
                $compacted = $true

                $sizeAfter = [math]::Round((Get-Item $vhdxPath).Length / 1GB, 2)
                Write-Log ("    compaction: {0} GB -> {1} GB (saved {2} GB)" -f $sizeBefore, $sizeAfter, [math]::Round($sizeBefore - $sizeAfter, 2))
            }
        }
    }
}
finally {
    # --- 4. ALWAYS recover, even if the above threw ------------------------------
    Write-Log "[4/4] recovery: waiting for the Docker engine..."
    if (-not (Wait-Engine -TimeoutSec 300)) {
        Write-Log "ALERT: Docker engine did NOT come back within 5 minutes. FLEET IS DOWN -- needs a human."
        exit 2
    }
    Write-Log "    engine is up."

    # restart anything that was running before but isn't now
    Start-Sleep -Seconds 10
    $after   = Get-RunningContainers
    $missing = @($before | Where-Object { $after -notcontains $_ })
    if ($missing.Count -gt 0) {
        Write-Log ("    {0} container(s) did not return: {1}" -f $missing.Count, ($missing -join ', '))
        foreach ($c in $missing) {
            & $Docker start $c 2>$null | Out-Null
            Write-Log ("      start {0} -> exit {1}" -f $c, $LASTEXITCODE)
        }
        Start-Sleep -Seconds 8
        $after = Get-RunningContainers
    }

    $stillMissing = @($before | Where-Object { $after -notcontains $_ })
    Write-Log ("    containers: {0} before -> {1} after" -f $before.Count, $after.Count)

    # --- verify the thing the fleet actually consumes ----------------------------
    $boardOk = $false
    for ($i = 0; $i -lt 6; $i++) {
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5555/api/fleet' -TimeoutSec 15 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $boardOk = $true; break }
        } catch { Start-Sleep -Seconds 10 }
    }

    if ($boardOk) { Write-Log "    Nexus board reachable on :5555." }
    else {
        # This is the known Docker-Desktop port-proxy break. nexus-watchdog heals it
        # within 5 min, but bounce it here so Sunday morning isn't degraded at all.
        Write-Log "    board NOT reachable -- likely the port-proxy break; restarting aernhome-dashboard."
        & $Docker restart aernhome-dashboard 2>$null | Out-Null
        Start-Sleep -Seconds 15
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5555/api/fleet' -TimeoutSec 15 -UseBasicParsing
            $boardOk = ($r.StatusCode -eq 200)
        } catch { $boardOk = $false }
        Write-Log ("    after restart, board reachable = {0}" -f $boardOk)
    }

    if ($stillMissing.Count -gt 0 -or -not $boardOk) {
        Write-Log ("ALERT: unhealthy end state (missing: {0}; boardOk={1})" -f ($stillMissing -join ', '), $boardOk)
        Write-Log "=== Docker Maintenance END (FAILED) ==="
        exit 3
    }

    Write-Log "=== Docker Maintenance END (healthy) ==="
}
