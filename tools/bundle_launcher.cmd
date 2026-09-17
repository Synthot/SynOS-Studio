@echo off
rem Builds this configuration bundle into an installable image. Double-click, or run from a terminal.
rem Add -Yes to install Docker Desktop without asking.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
echo.
pause
