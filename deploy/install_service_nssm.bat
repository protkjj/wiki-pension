@echo off
chcp 65001 >nul
REM ============================================================
REM  24시간 상시 구동 — Windows 서비스로 등록 (도커 없는 경우 권장)
REM  로그아웃/재부팅해도 자동 시작, 프로세스가 죽으면 자동 재시작됩니다.
REM
REM  준비물: NSSM (무료). https://nssm.cc/download 에서 받아
REM          win64\nssm.exe 를 이 deploy 폴더에 복사해 두세요.
REM  ★ 반드시 '관리자 권한으로 실행'
REM ============================================================
setlocal
cd /d "%~dp0"
set APPDIR=%~dp0..

REM python 실행 파일 전체 경로 찾기
for /f "delims=" %%i in ('where python') do (set PY=%%i& goto :found)
:found
echo 사용 python: %PY%
echo 앱 폴더: %APPDIR%

nssm install WikiPension "%PY%" "-m streamlit run app\platform_app.py"
nssm set WikiPension AppDirectory "%APPDIR%"
nssm set WikiPension Start SERVICE_AUTO_START
nssm set WikiPension DisplayName "WIKI 퇴직연금 관리 시스템"
nssm start WikiPension

echo.
echo 서비스(WikiPension) 등록·시작 완료.
echo  - 부팅 시 자동 시작 / 죽으면 자동 재시작
echo  - 중지:  nssm stop WikiPension
echo  - 제거:  nssm remove WikiPension confirm
pause
