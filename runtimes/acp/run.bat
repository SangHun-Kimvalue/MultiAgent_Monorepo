@echo off
setlocal
REM ===========================================================
REM  Agent Control Plane - dashboard launcher
REM  Usage:
REM    run.bat        : poll real Codex/Claude/Cursor sessions
REM    run.bat fake   : demo data (FakeCollector) for UI check
REM  Stop: press Ctrl+C in this window
REM  (ASCII-only on purpose: Korean text in .bat breaks cmd parsing)
REM ===========================================================

REM Move to repo root (folder of this script) so config/paths.yaml and .acp resolve
cd /d "%~dp0"

REM Activate venv if present, otherwise use system python
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

REM Demo mode:  run.bat fake
set "ACP_MODE="
if /i "%~1"=="fake" set "ACP_MODE=--fake"

REM Dependency check - auto-install on first run only
python -c "import acp, fastapi, uvicorn" 1>nul 2>nul
if not errorlevel 1 goto run
echo [ACP] Installing dependencies (first run, may take a minute)...
python -m pip install -e .
if errorlevel 1 (
  echo [ACP] Install failed. Make sure Python 3.12 and pip are installed.
  pause
  exit /b 1
)

:run
echo.
echo ============================================
echo   Agent Control Plane - dashboard
echo   URL  : http://127.0.0.1:8900
echo   mode : %ACP_MODE%   [demo: run.bat fake]
echo   stop : press Ctrl+C
echo ============================================
echo.

REM Open the dashboard in the default browser after a 3s delay (background, no quote nesting)
start "" /min cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8900"

REM Run polling loop + web server in foreground. Ctrl+C to stop.
python -m acp web %ACP_MODE%

echo.
echo [ACP] Server stopped.
pause
endlocal
