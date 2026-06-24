@echo off
setlocal
cd /d "%~dp0"

REM Check if server is already running on port 8080
powershell.exe -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }"

if %errorlevel% equ 0 (
	REM Server already running, just open browser
	start "" "http://127.0.0.1:8080/ninth"
	endlocal
	exit /b 0
)

REM Server not running - start it
if exist "%~dp0runtime\python\python.exe" goto start_server

REM No bundled runtime - run bootstrap to set up venv
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0atlas_bootstrap.ps1" -Quiet
if errorlevel 1 (
	echo [ERROR] Atlas bootstrap failed. Python 3.10+ may be required.
	pause
	exit /b 1
)

:start_server
REM Start server in a new (visible) window - user can close it when done
start "Atlas Law Server" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_atlas_server.ps1"

REM Wait until Atlas actually responds before opening the browser
powershell.exe -NoProfile -Command "$ready = $false; for ($i = 0; $i -lt 60; $i++) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/ninth' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { $ready = $true; break } } catch {}; Start-Sleep -Milliseconds 500 }; if (-not $ready) { exit 1 }"
if errorlevel 1 (
	echo [ERROR] Atlas server did not become ready within 30 seconds.
	pause
	exit /b 1
)

start "" "http://127.0.0.1:8080/ninth"
endlocal
