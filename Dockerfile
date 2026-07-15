# WIKI 퇴직연금 관리 시스템 — 컨테이너 이미지 (도커 있는 경우)
FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시)
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt

# 앱 소스 복사 (data/ 등은 .dockerignore로 제외 → 실데이터는 볼륨으로 마운트)
COPY . /app/

EXPOSE 8501
ENV STREAMLIT_SERVER_HEADLESS=true

# DB·업로드 파일은 /app/data 에 저장 → 볼륨 마운트로 영속화
CMD ["streamlit", "run", "app/platform_app.py", \
     "--server.address=0.0.0.0", "--server.port=8501"]
