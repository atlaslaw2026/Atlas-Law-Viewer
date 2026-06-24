@echo off
cd /d "%~dp0"
echo ============================================
echo  Atlas Law Viewer - Diagnostics
echo ============================================
echo.

echo [1] Checking bundled Python...
if exist "%~dp0runtime\python\python.exe" (
    echo     FOUND: runtime\python\python.exe
    "%~dp0runtime\python\python.exe" -c "import sys; print('    Python version:', sys.version)"
) else (
    echo     MISSING: runtime\python\python.exe
)
echo.

echo [2] Checking required files...
for %%f in (atlas_law_server.py opinions_index.html opinions_data.json start_atlas_server.ps1) do (
    if exist "%~dp0%%f" (echo     OK: %%f) else (echo     MISSING: %%f)
)
echo.

echo [3] Checking port 8080...
netstat -ano | findstr :8080 | findstr LISTEN
if errorlevel 1 (echo     Port 8080 not in use.) else (echo     Port 8080 already in use.)
echo.

echo [4] Starting server in this window (Ctrl+C to stop)...
echo.
if exist "%~dp0runtime\python\python.exe" (
    "%~dp0runtime\python\python.exe" "%~dp0atlas_law_server.py"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_atlas_server.ps1"
)
pause
