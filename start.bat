@echo off
setlocal
set "PROJECT_WSL=/mnt/d/Users/sunqi39/Desktop/tmp_link_manager"
start "TMP Link Manager" wsl.exe -d Ubuntu bash -lc "cd '%PROJECT_WSL%' && bash scripts/start.sh"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\wait-and-open.ps1"
endlocal
