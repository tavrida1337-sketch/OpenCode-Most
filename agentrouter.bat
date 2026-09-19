@echo off
chcp 65001 >nul
python "%~dp0switch.py" agentrouter
pause
