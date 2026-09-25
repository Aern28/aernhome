@echo off
REM Nexus Maint Todoist task target (daily 08:00).
REM 2026-09-25: it exited rc=1 at 08:00 daily while running clean by hand and from
REM schtasks at 13:52 - with no output kept anywhere. So: log every run, absolute docker
REM path (never trust a task's PATH), one retry after 60s. The log names the real cause.
set "LOG=C:\tools\logs\maint-todoist.log"
set "DOCKER=C:\Program Files\Docker\Docker\resources\bin\docker.exe"
echo ==== %DATE% %TIME% >> "%LOG%"
"%DOCKER%" exec aernhome-dashboard python /data/maint_todoist_push.py >> "%LOG%" 2>&1
if not errorlevel 1 exit /b 0
echo ---- rc=%ERRORLEVEL%, retrying in 60s >> "%LOG%"
ping -n 61 127.0.0.1 >nul
"%DOCKER%" exec aernhome-dashboard python /data/maint_todoist_push.py >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ---- retry rc=%RC% >> "%LOG%"
exit /b %RC%
