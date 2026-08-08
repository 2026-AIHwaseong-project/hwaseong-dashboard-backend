FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 전체 복사 (dataset_hwaseong/ + server/static/ + analysis/ 포함)
COPY . .

EXPOSE 8000

# workers=1 고정 — 05_simulate.py 모듈이 프로세스 공유 안 됨
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
