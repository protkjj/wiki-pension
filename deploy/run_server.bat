@echo off
chcp 65001 >nul
REM ============================================================
REM  WIKI 퇴직연금 관리 시스템 — 서버 실행 (도커 없는 경우)
REM  이 파일을 더블클릭하면 앱이 켜지고, 사내 다른 PC에서 접속할 수 있습니다.
REM  종료하려면 이 창을 닫으세요.
REM ============================================================
cd /d "%~dp0.."

echo.
echo [1/2] 접속 주소(이 PC의 사내 IP)를 확인합니다...
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo    http://%%a:8501
echo    (직원들은 위 주소 중 하나를 브라우저에 입력해 접속합니다.)
echo.
echo [2/2] 서버를 시작합니다. 이 창을 닫으면 서버가 꺼집니다.
echo    잠시 후 이 PC의 브라우저가 자동으로 열립니다 (안 열리면 http://localhost:8501 직접 입력).
echo.

REM 서버가 뜬 뒤(약 5초) 이 PC 브라우저를 자동으로 연다 (explorer로 기본 브라우저 실행)
start "" /min cmd /c "timeout /t 5 >nul & explorer http://localhost:8501"

python -m streamlit run app\platform_app.py
pause
