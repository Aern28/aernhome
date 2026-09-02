# created-by: fable
# created: 2026-09-02
# purpose: register the Ashaman Task Scheduler job that runs keep_sync.py inside aernhome-dashboard every 15 min
# lifespan: helper
# project: household-restock
#
# Run ON Ashaman (console or `ssh ashaman`, lands in PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\projects\aernhome\register-keep-sync-task.ps1
# Idempotent - re-running replaces the task. Same principal shape as 'TCG Delivery Check'
# (interactive user Matt, limited). Output of every run is appended to keep_sync_last.log;
# the Fleet board check `keep_sync` reads the JSON stamp the script writes in /data.
$ErrorActionPreference = "Stop"
$name = "Keep Sync"
$log  = "C:\projects\aernhome\keep_sync_last.log"
$cmd  = "docker exec aernhome-dashboard python /app/keep_sync.py >> `"$log`" 2>&1"

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
           -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Google Keep 'Grocery List' <-> Nexus restock two-way sync (keep_sync.py in aernhome-dashboard)" | Out-Null
Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo | Format-List TaskName,NextRunTime,LastRunTime,LastTaskResult
