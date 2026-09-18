@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist bridge.pid (
  for /f %%p in (bridge.pid) do tasklist /FI "PID eq %%p" 2>nul | findstr /I "python" >nul && (
    echo already running, opening site...
    start "" "http://127.0.0.1:8000/"
    exit /b 0
  )
)
start "opencode-bridge" /min python "%~dp0bridge.py" %*
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000/"
echo bridge started in background, site opened. stop.bat to stop.
