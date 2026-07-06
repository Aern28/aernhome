<#
.SYNOPSIS
    Fleet Host Stats collector for the "Ashaman" Windows Docker host.

.DESCRIPTION
    Runs a series of health checks that only make sense from the Windows host
    (as opposed to from inside a container) and writes the results as a single
    JSON file that a Flask app running in a container reads.

    Designed to run unattended via Windows Task Scheduler every 10 minutes,
    whether or not a user is logged on. Every check is individually wrapped in
    try/catch (both inside the check function and at the call site) so a
    single failing check can never prevent the script from producing valid
    JSON output. On any unexpected failure a check reports status "unknown"
    with the error message in "detail".

    Output is written atomically: the JSON is written to a ".tmp" file next
    to the destination, then moved into place with Move-Item -Force, so the
    Flask app never observes a partially-written file.

.NOTES
    PowerShell 5.1 compatible (no PS7-only syntax). Do not use ternary
    operators, null-coalescing (??), or other PS7-only features here.
#>

[CmdletBinding()]
param(
    [string]$OutPath = "C:\projects\aernhome\data\host_stats.json"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function New-CheckResult {
    param(
        [string]$Id,
        [string]$Label,
        [string]$Status = "unknown",
        [string]$Detail = ""
    )
    $result = New-Object System.Collections.Specialized.OrderedDictionary
    $result.id = $Id
    $result.label = $Label
    $result.status = $Status
    $result.detail = $Detail
    return $result
}

# Runs a check scriptblock and guarantees a well-formed result even if the
# scriptblock throws something the check's own internal try/catch didn't
# anticipate. Defense in depth per the "every check must always emit valid
# JSON" requirement.
function Invoke-CheckSafely {
    param(
        [scriptblock]$Block,
        [string]$Id,
        [string]$Label
    )
    try {
        $checkResult = & $Block
        if (-not $checkResult) {
            return New-CheckResult -Id $Id -Label $Label -Status "unknown" -Detail "Check returned no result"
        }
        return $checkResult
    }
    catch {
        return New-CheckResult -Id $Id -Label $Label -Status "unknown" -Detail "Unhandled error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 1: tcg_autoprocess - Task Scheduler task "TCG-AutoProcess"
# ---------------------------------------------------------------------------

function Test-TcgAutoprocess {
    $id = "tcg_autoprocess"
    $label = "TCG AutoProcess Task"
    try {
        # Run via cmd.exe so a missing task's stderr text comes back as plain
        # strings instead of PowerShell NativeCommandError records (which
        # would otherwise dump a multi-line formatted error into "detail").
        $raw = cmd /c "schtasks /query /tn `"TCG-AutoProcess`" /v /fo csv 2>&1"
        if ($LASTEXITCODE -ne 0) {
            $msg = (($raw | Out-String).Trim() -replace '\s+', ' ')
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Task not found or query failed: $msg"
        }

        $csv = $raw | ConvertFrom-Csv
        $row = $csv | Select-Object -First 1
        if (-not $row) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "schtasks returned no rows"
        }

        $taskStatus = $row.'Status'
        $lastResultRaw = $row.'Last Result'
        $lastRunRaw = $row.'Last Run Time'

        if ($taskStatus -eq 'Disabled') {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Task is disabled"
        }

        $lastRun = $null
        if ($lastRunRaw -and $lastRunRaw -ne 'N/A' -and $lastRunRaw -ne 'Never') {
            # Defensive locale parsing: try current culture first, then a
            # couple of common fallbacks, since schtasks formats dates using
            # the OS locale (which may differ from what .NET expects).
            try {
                $lastRun = [DateTime]::Parse($lastRunRaw, [System.Globalization.CultureInfo]::CurrentCulture, [System.Globalization.DateTimeStyles]::None)
            }
            catch {
                try {
                    $lastRun = [DateTime]::Parse($lastRunRaw, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::None)
                }
                catch {
                    try {
                        $lastRun = Get-Date $lastRunRaw
                    }
                    catch {
                        $lastRun = $null
                    }
                }
            }
        }

        if (-not $lastRun) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Task has never run or last run time unparsable ('$lastRunRaw')"
        }

        $minutesSince = (New-TimeSpan -Start $lastRun -End (Get-Date)).TotalMinutes
        $resultCode = "$lastResultRaw".Trim()
        $roundedMinutes = [math]::Round($minutesSince, 1)

        if ($minutesSince -gt 40) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Last run $roundedMinutes min ago (>40 min threshold), last result $resultCode"
        }
        elseif ($resultCode -ne '0') {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Last run $roundedMinutes min ago, last result $resultCode (nonzero)"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Last run $roundedMinutes min ago, result 0"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 2: nexus_backup - newest file in H:\aernhome\backups
# ---------------------------------------------------------------------------

function Test-NexusBackup {
    $id = "nexus_backup"
    $label = "Nexus Backup (H:)"
    try {
        if (-not (Test-Path "H:\")) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "H: not accessible from this session - daily backups likely failing"
        }

        $dir = "H:\aernhome\backups"
        if (-not (Test-Path $dir)) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Backup directory not found: $dir"
        }

        $newest = Get-ChildItem -Path $dir -File -ErrorAction Stop | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $newest) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "No backup files found in $dir"
        }

        $ageHours = (New-TimeSpan -Start $newest.LastWriteTime -End (Get-Date)).TotalHours
        $roundedHours = [math]::Round($ageHours, 1)

        if ($ageHours -lt 26) {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Newest: $($newest.Name), age $roundedHours h"
        }
        elseif ($ageHours -lt 50) {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Newest: $($newest.Name), age $roundedHours h"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Newest: $($newest.Name), age $roundedHours h"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 3: supersaiyan_backup - newest backup_log_*.txt on F:\
# ---------------------------------------------------------------------------

function Test-SupersaiyanBackup {
    $id = "supersaiyan_backup"
    $label = "Supersaiyan Backup (F:)"
    try {
        if (-not (Test-Path "F:\")) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "F: not accessible from this session"
        }

        $files = Get-ChildItem -Path "F:\" -Filter "backup_log_*.txt" -File -ErrorAction Stop
        if (-not $files) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "No backup_log_*.txt files found on F:\"
        }

        $newest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $ageDays = (New-TimeSpan -Start $newest.LastWriteTime -End (Get-Date)).TotalDays
        $roundedDays = [math]::Round($ageDays, 1)

        if ($ageDays -lt 35) {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Newest: $($newest.Name), age $roundedDays d"
        }
        elseif ($ageDays -lt 45) {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Newest: $($newest.Name), age $roundedDays d"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Newest: $($newest.Name), age $roundedDays d"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 4: chrome_cdp - Chrome DevTools Protocol on 127.0.0.1:9222
#           (the TCGplayer automation browser session)
# ---------------------------------------------------------------------------

function Test-ChromeCdp {
    $id = "chrome_cdp"
    $label = "Chrome CDP (TCGplayer automation)"
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        $json = $resp.Content | ConvertFrom-Json
        $browser = $json.Browser
        if (-not $browser) { $browser = "(version endpoint responded, no Browser field)" }
        return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Browser: $browser"
    }
    catch {
        # Per spec this check is binary: up if the JSON endpoint answers,
        # down otherwise (not "unknown" - an unreachable CDP port is a
        # meaningful down signal for the TCGplayer automation pipeline).
        return New-CheckResult -Id $id -Label $label -Status "down" -Detail "CDP endpoint not reachable: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 5: disk_c - free space on C:
# ---------------------------------------------------------------------------

function Test-DiskC {
    $id = "disk_c"
    $label = "Disk Space (C:)"
    try {
        $drive = Get-PSDrive -Name C -ErrorAction Stop
        $freeGB = $drive.Free / 1GB
        $roundedGB = [math]::Round($freeGB, 1)

        if ($freeGB -lt 10) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Free: $roundedGB GB"
        }
        elseif ($freeGB -lt 30) {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Free: $roundedGB GB"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Free: $roundedGB GB"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 6: tailscale - tailscale status --json
# ---------------------------------------------------------------------------

function Test-Tailscale {
    $id = "tailscale"
    $label = "Tailscale"
    try {
        $tsExe = $null
        $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
        if ($cmd) {
            $tsExe = $cmd.Source
        }
        else {
            $candidates = @(
                "C:\Program Files\Tailscale\tailscale.exe",
                "C:\Program Files (x86)\Tailscale\tailscale.exe"
            )
            foreach ($candidate in $candidates) {
                if (Test-Path $candidate) {
                    $tsExe = $candidate
                    break
                }
            }
        }

        if (-not $tsExe) {
            return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "tailscale CLI not found"
        }

        $raw = & $tsExe status --json 2>$null
        if (-not $raw) {
            return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "tailscale status returned no output"
        }

        $json = $raw | Out-String | ConvertFrom-Json

        $onlinePeers = 0
        $totalPeers = 0
        if ($json.Peer) {
            $peerVals = @($json.Peer.PSObject.Properties | ForEach-Object { $_.Value })
            $totalPeers = $peerVals.Count
            $onlinePeers = @($peerVals | Where-Object { $_.Online }).Count
        }

        $selfOnline = $false
        if ($json.Self -and $json.Self.Online) {
            $selfOnline = $true
        }

        if ($selfOnline) {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Self online: true; peers online: $onlinePeers/$totalPeers"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "Self online: false; peers online: $onlinePeers/$totalPeers"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Check 7: matt_session - quser shows an active session for matt/Matt
# ---------------------------------------------------------------------------

function Test-MattSession {
    $id = "matt_session"
    $label = "Matt Interactive Session"
    try {
        $raw = quser 2>$null
        if (-not $raw) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "No sessions returned by quser (or quser unavailable)"
        }

        $mattLines = @($raw | Where-Object { $_ -match '(?i)\bmatt\b' })
        if ($mattLines.Count -eq 0) {
            return New-CheckResult -Id $id -Label $label -Status "down" -Detail "No session found for user matt"
        }

        $line = $mattLines[0]
        if ($line -match '(?i)\bActive\b') {
            return New-CheckResult -Id $id -Label $label -Status "up" -Detail "Matt session Active"
        }
        elseif ($line -match '(?i)\bDisc') {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Matt session Disconnected"
        }
        else {
            return New-CheckResult -Id $id -Label $label -Status "warn" -Detail "Matt session found, state unclear: $($line.Trim())"
        }
    }
    catch {
        return New-CheckResult -Id $id -Label $label -Status "unknown" -Detail "Error: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$checks = @()
$checks += Invoke-CheckSafely -Block { Test-TcgAutoprocess } -Id "tcg_autoprocess" -Label "TCG AutoProcess Task"
$checks += Invoke-CheckSafely -Block { Test-NexusBackup } -Id "nexus_backup" -Label "Nexus Backup (H:)"
$checks += Invoke-CheckSafely -Block { Test-SupersaiyanBackup } -Id "supersaiyan_backup" -Label "Supersaiyan Backup (F:)"
$checks += Invoke-CheckSafely -Block { Test-ChromeCdp } -Id "chrome_cdp" -Label "Chrome CDP (TCGplayer automation)"
$checks += Invoke-CheckSafely -Block { Test-DiskC } -Id "disk_c" -Label "Disk Space (C:)"
$checks += Invoke-CheckSafely -Block { Test-Tailscale } -Id "tailscale" -Label "Tailscale"
$checks += Invoke-CheckSafely -Block { Test-MattSession } -Id "matt_session" -Label "Matt Interactive Session"

$output = New-Object System.Collections.Specialized.OrderedDictionary
$output.generated_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
$output.checks = $checks

try {
    $json = $output | ConvertTo-Json -Depth 5 -Compress
}
catch {
    # Last-resort fallback: if serialization itself somehow fails, still
    # emit valid, schema-shaped JSON rather than nothing at all.
    $escapedMsg = $_.Exception.Message -replace '\\', '\\\\' -replace '"', '\"'
    $nowStr = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    $json = '{"generated_at":"' + $nowStr + '","checks":[{"id":"collector","label":"Fleet Host Stats Collector","status":"unknown","detail":"JSON serialization failed: ' + $escapedMsg + '"}]}'
}

$outDir = Split-Path -Path $OutPath -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$tempPath = "$OutPath.tmp"

# UTF8 without BOM, matching what json.load() in the Flask app expects.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tempPath, $json, $utf8NoBom)

Move-Item -Path $tempPath -Destination $OutPath -Force

Write-Output $json
