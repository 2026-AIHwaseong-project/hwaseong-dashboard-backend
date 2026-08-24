FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 전체 복사 (dataset_hwaseong/ + server/static/ + analysis/ 포함)
COPY . .

EXPOSE 8000

# workers=1 고정. 이유가 셋이고, 셋 다 늘리면 조용히 틀리는 쪽으로 깨진다.
#   ① 05_simulate.py 모듈이 프로세스 공유가 안 된다.
#   ② 관리자 재계산·업로드 반영의 마지막 단계인 reload 가 **요청을 받은 그 프로세스의**
#      메모리만 갈아끼운다(server/admin.py:913-915 → _reload_data). 워커가 N개면 1개만
#      새 세대를 들고 나머지 N−1 개가 옛 지도를 계속 서빙한다 — 같은 URL 이 새로고침할
#      때마다 다른 답을 준다. HW_UPLOAD_APPLY 를 켠 지금 이 경로가 실제로 돈다.
#   ③ JOB 상태도 프로세스 지역이라, 진행 폴링이 엉뚱한 워커에 붙으면 "잡이 사라졌다"가
#      되고 "이미 실행 중" 가드가 풀려 재계산 두 개가 같은 stage-* 에 동시에 쓴다.
# 성능이 목적이라면 워커가 아니라 압축이다 — 배포 대상 t2.small 은 vCPU 가 1개라
# 프로세스를 늘려도 처리량이 안 는다. server/main.py 의 compresslevel=6 참조.
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
