@echo off
REM ============================================================
REM  DBO Platform (3-role login) - one-click launcher [RECOMMENDED]
REM  Client / Actuary / Admin login. Census validation,
REM  calculation, and actuarial report (business-report format).
REM  Double-click after installing Python. First run takes 2-3 min.
REM  ASCII-only on purpose to avoid Korean cmd encoding issues.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Pick a stable Python (avoid pre-release like 3.14) ---
set "PY="
for %%V in (3.13 3.12 3.11) do (
  if not defined PY ( py -%%V --version >nul 2>nul && set "PY=py -%%V" )
)
if not defined PY ( py --version >nul 2>nul && set "PY=py" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [ERROR] Python not found.
  echo   Install Python 3.12 from python.org, then run this again.
  pause & exit /b 1
)
echo Using Python: %PY%
%PY% --version

set "VPY=.venv\Scripts\python.exe"

REM --- Virtualenv: recreate if missing or streamlit not installed ---
if exist "%VPY%" (
  "%VPY%" -c "import streamlit" >nul 2>nul || ( echo Recreating virtualenv... & rmdir /s /q .venv )
)
if not exist "%VPY%" (
  echo [1/3] Creating virtualenv...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [ERROR] Failed to create virtualenv & pause & exit /b 1 )
)

REM --- Install dependencies (use the venv python directly) ---
echo [2/3] Installing/checking dependencies... (first run takes a few minutes)
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -e ".[app]"
"%VPY%" -c "import streamlit" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] streamlit is not installed.
  echo   Usually a Python version issue. Install Python 3.12 and try again.
  pause & exit /b 1
)

echo.
echo ============================================================
echo  Demo accounts (created automatically on first run)
echo    Admin    : admin    / admin123
echo    Actuary  : actuary  / act123
echo    Client   : clientA  / ca123    (Gana Electronics)
echo    Client   : clientB  / cb123    (Dara Corp)
echo ============================================================
echo.

REM --- Launch (browser opens automatically; DB auto-creates/migrates) ---
echo [3/3] Launching platform - your browser will open. Press Ctrl+C here to stop.
"%VPY%" -m streamlit run app\platform_app.py

pause
