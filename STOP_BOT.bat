@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0START_BOT.bat" --stop-internal
exit /b %ERRORLEVEL%
