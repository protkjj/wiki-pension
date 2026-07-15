@echo off
chcp 65001 >nul
REM ============================================================
REM  방화벽에서 8501 포트 인바운드 허용 (한 번만 실행)
REM  ★ 반드시 '관리자 권한으로 실행' (파일 우클릭 → 관리자 권한으로 실행)
REM ============================================================
netsh advfirewall firewall add rule name="WIKI Pension 8501" dir=in action=allow protocol=TCP localport=8501
echo.
echo 방화벽 8501 포트 인바운드 허용을 추가했습니다.
echo 이제 사내 다른 PC에서 http://이서버IP:8501 로 접속됩니다.
pause
