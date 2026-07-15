# WIKI 퇴직연금 관리 시스템 — 회사 서버(Windows) 배포 안내

회사 서버(Windows)에 올려 직원들이 사내에서 접속해 테스트하는 방법입니다.
**도커 있는 경우 / 없는 경우** 두 가지로 나눠 정리했습니다. 데이터(SQLite DB·업로드
파일)는 모두 **서버 안에만** 저장되어 회사 밖으로 나가지 않습니다.

접속 주소는 공통으로 `http://서버IP:8501` 입니다. (서버IP는 서버에서 `ipconfig`의 IPv4)

---

## A. 도커가 없는 경우 (Windows 네이티브) — 대부분 이 경우

### 1) 준비 (최초 1회)
1. 서버에 **Python 3.11 이상** 설치 — https://www.python.org/downloads/
   설치 시 **“Add python.exe to PATH” 체크**.
2. 이 폴더(`dbo-engine`)를 서버에 복사.
3. 명령프롬프트에서 의존성 설치:
   ```
   cd C:\경로\dbo-engine
   pip install -r app\requirements.txt
   ```
4. **방화벽 열기**: `deploy\firewall_open.bat` 를 **관리자 권한으로 실행** (한 번만).

### 2) 실행
- **간단 실행(테스트용)**: `deploy\run_server.bat` 더블클릭 → 이 PC의 브라우저가
  자동으로 열립니다(안 열리면 `http://localhost:8501` 직접 입력). 다른 직원은 창에 뜬
  `http://서버IP:8501` 주소로 접속. (창을 닫으면 서버도 꺼짐)
  ※ 서버는 `headless`(브라우저 자동열기 끔) 모드라 `streamlit`이 스스로 창을 띄우지
  않습니다 — 대신 위 배치파일이 이 PC 브라우저를 열어줍니다.
- **24시간 상시(권장)**: 서비스로 등록하면 로그아웃·재부팅해도 자동 시작되고
  프로세스가 죽어도 자동 재시작됩니다.
  1. NSSM(무료) 다운로드 → https://nssm.cc/download → `win64\nssm.exe` 를
     `deploy` 폴더에 복사.
  2. `deploy\install_service_nssm.bat` 를 **관리자 권한으로 실행**.
  - 중지: `nssm stop WikiPension` / 제거: `nssm remove WikiPension confirm`

### 3) 절전 끄기 (상시 구동 시 필수)
설정 › 시스템 › 전원 › **화면/절전 “안 함”**. (서버는 보통 이미 설정됨)

---

## B. 도커가 있는 경우 (Docker Desktop / Docker Engine)

가장 깔끔합니다. 파이썬·라이브러리가 컨테이너에 포장돼 서버 환경과 무관하게 동일 동작하고,
자동 재시작·DB 영속이 기본입니다.

### 1) 실행 (최초/업데이트 공통)
```
cd C:\경로\dbo-engine
docker compose up -d --build
```
- 접속: `http://서버IP:8501`
- 로그: `docker compose logs -f`
- 중지: `docker compose down`  (데이터는 `data\` 폴더에 남습니다)

### 2) 방화벽
사내 다른 PC에서 접속하려면 8501 포트를 열어야 합니다 →
`deploy\firewall_open.bat` 관리자 권한 실행(또는 방화벽에서 8501 인바운드 허용).

### 3) 특징
- `restart: unless-stopped` → 서버 부팅/장애 시 컨테이너 자동 재시작.
- `./data` 볼륨 마운트 → 재시작·재빌드해도 등록 데이터·업로드 파일 유지.

---

## 외부(집·외근)에서도 접속하려면
- 회사 **VPN**이 있으면 접속자가 VPN 연결 후 `http://서버IP:8501` 그대로 접속.
- VPN이 없으면 **Tailscale**(무료) 를 서버와 접속자 PC에 설치 → 안전하게 원격 접속
  (포트 개방·공인 IP 불필요). 데이터는 여전히 서버 안에만 있습니다.

## 백업
- 데이터는 전부 `dbo-engine\data\platform\` 폴더(및 도커의 `data\`)에 있습니다.
  이 폴더만 주기적으로 복사해두면 백업됩니다.

## 참고 (현재 단계)
- 로그인은 프로토타입이라 비밀번호가 없습니다(목록에서 계정 선택). 정식 운영 전
  비밀번호 로그인·개인정보 암호화를 붙이는 것을 권장합니다.
