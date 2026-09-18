@echo off
cd /d "%~dp0"
if exist bridge.pid (
  for /f %%p in (bridge.pid) do taskkill /PID %%p /F >nul 2>&1
  del bridge.pid >nul 2>&1
  echo stopped.
) else (
  echo not running (no bridge.pid).
)
pause
