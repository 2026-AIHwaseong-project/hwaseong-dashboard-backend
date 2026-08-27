# -*- coding: utf-8 -*-
"""관리자 콘솔 API — 파라미터 오버라이드 + 데이터 최신화

설계 원칙 (schema_ops.sql 의 소유권 분리를 파일로 이식):
  · 오버라이드 정본은 var/admin/params_override.json 하나 — server/static(배치 소유)에는
    절대 쓰지 않는다. 배치(05_load)를 몇 번 돌려도 관리자 값이 살아남아야 한다.
  · 이력은 var/admin/params_history.jsonl append-only. 삭제 없음 — 되돌리기도 새 줄.
    reason 필수(admin_grid_override 의 reason NOT NULL 과 같은 이유 — "왜 사람이
    고쳤는가"가 기록에 남아야 한다).
  · 값 반영은 apply_runtime_params() 한 함수 — 서버 상수(COST_KRW)·시뮬 엔진 모듈
    속성·메모리 meta 를 **한 번에** 갱신한다. 나눠서 하면 같은 화면에 클라 합계와
    서버 breakdown 이 다른 비용으로 표시된다(기존 테스트가 못 잡는 사고).
  · 재적재(reload)·재계산(refresh) 직후에도 반드시 apply_runtime_params() 를 다시
    부른다 — 새로 import 된 시뮬 모듈은 기본값으로 돌아가 있기 때문이다.

인증: ADMIN_TOKEN 환경변수 + Authorization: Bearer.
  **읽기(GET)와 쓰기(POST)를 나눈다.**
    · GET  — 토큰 없이도 열린다. 파라미터 대장·상태·이력을 둘러보는 것은 공개
             정보이고, "회원가입 없이 열어볼 수 있다"가 이 제품의 강점이다.
    · POST — 토큰이 설정돼 있으면 반드시 요구한다(저장·재계산). 미설정이면
             통과하되 기동 로그·화면 배너로 경고한다.
  ⚠️ 공개 인터넷에 노출된 서버라면 ADMIN_TOKEN 을 설정할 것. 없으면 누구나
     단가·모델 상수를 바꾸고 재계산을 트리거할 수 있고, workers=1 이라
     재계산 반복만으로 전 API 가 멎는다(시연 중이면 그대로 사고다).
  실패 잠금은 전역 카운터다. IP별 잠금은 행사장 NAT 오차단으로 롤백된 전례가 있다
  (main.py 의 레이트리밋 이력 참고).

편집 범위: 런타임(A계급)만 — 시뮬·추천이 요청 시점에 읽는 값이라 재계산이 필요 없다.
  기준선에 구워지는 상수(가중치·컷·감쇠)는 읽기 전용으로만 노출한다. 런타임에 바꾸면
  05_simulate 의 기준선과 화면(grid JSON)이 즉시 갈라지고, 파이프라인 쪽만 바꾸면
  기준선 assert 로 서버가 아예 안 뜬다.
"""
import asyncio
import base64
import collections
import copy
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

# ─── 시각 정본 ────────────────────────────────────────────────────────────────
# 컨테이너는 UTC 로 돈다. datetime.now() 를 그대로 쓰면 화면 이력·AI 보고서·백업
# 폴더명이 전부 9시간 이르게 찍힌다 — 실제로 KST 00:11 에 한 반영이 "15:11" 로
# 남아 있었다. 공문 초안에 나가는 시각이라 오차가 아니라 결함이다.
#
# TZ=Asia/Seoul 이나 zoneinfo.ZoneInfo("Asia/Seoul") 을 쓰지 않는 이유:
# 베이스가 python:3.11-slim 이라 /usr/share/zoneinfo 가 없다. 둘 다 예외를 던지지
# 않고 **조용히 UTC 로 되돌아가서**, 고쳤다고 믿는 상태로 같은 값이 계속 나온다.
# 한국은 1988년 이후 서머타임이 없어 고정 오프셋 +09:00 이 항상 정확하다.
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    """이 저장소에서 '지금'은 언제나 이 함수다. main.py 도 이것을 쓴다."""
    return datetime.now(KST)


ROOT = Path(__file__).resolve().parent.parent
VAR = Path(os.environ.get("HW_VAR_DIR", str(ROOT / "var")))
ADMIN_DIR = VAR / "admin"
OVERRIDE_PATH = ADMIN_DIR / "params_override.json"
HISTORY_PATH = ADMIN_DIR / "params_history.jsonl"

# main.py 가 lifespan 에서 주입한다 (순환 import 방지).
_ctx: dict = {"DATA": None, "COST_KRW": None, "PERIODS": None, "build_snapshot": None,
              "QUAD_LABEL": None, "ACTION_LABEL": None}


def init(DATA, COST_KRW, PERIODS, build_snapshot, QUAD_LABEL=None, ACTION_LABEL=None):
    _ctx.update(DATA=DATA, COST_KRW=COST_KRW, PERIODS=PERIODS, build_snapshot=build_snapshot,
                QUAD_LABEL=QUAD_LABEL, ACTION_LABEL=ACTION_LABEL)


def _load_params_module():
    """analysis/params.py 를 별도 모듈 객체로 로드 — 상수 정본.
    (여기서 로드한 사본은 오버라이드를 굽기 전 기본값 참조용으로도 쓰므로
    시뮬 엔진의 params 인스턴스와 별개다.)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hw_params_admin", ROOT / "analysis" / "params.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PARAMS = _load_params_module()


# ─── 파라미터 레지스트리 ────────────────────────────────────────────────────────
# scope: runtime  = 요청 시점에 읽혀 즉시 반영 (편집 가능)
#        pipeline = 계약 JSON 에 구워짐 — 재계산 경로가 오버라이드를 읽는 P3 에서 개방
# 기본값은 코드 정본과 같아야 한다. 어긋나면 기동 시 드리프트 검사가 잡는다.
# 범위(min/max)는 두지 않는다 — 극단값 실험은 운영자의 자유다(2026-08-26 제거).
# 저장 검증에는 형·유한성 검사와, 서버를 실제로 죽이는 값 하나(비용 ≤ 0 —
# 추천의 비용효율 ΔB̂/비용 나눗셈)만 남긴다.
SPECS = {
    "sim.headwayMult": dict(
        label="배차 증편 배수", unit="×", type="float", default=1.43,
        scope="runtime", group="effect",
        note="증편 1회 = 배차간격 ×0.70 (운행횟수 ×1.43). 마을버스 실측 배차 근거.",
        applies="시뮬레이션·추천의 증편 효과 (요청 시점 반영)"),
    "sim.fstar.am": dict(label="신설 주입량 f* — 출근", unit="회/창", type="float",
                         default=4.8, scope="runtime", group="effect",
                         note="마을버스 120노선 실측 배차 중앙값(출퇴근 25분)에서 환산.",
                         applies="정류장 신설 배치의 출근 시간대 효과"),
    "sim.fstar.day": dict(label="신설 주입량 f* — 낮", unit="회/창", type="float",
                          default=8.0, scope="runtime", group="effect",
                          note="평일 60분 배차 → 8시간 창 환산.", applies="정류장 신설 배치의 낮 효과"),
    "sim.fstar.pm": dict(label="신설 주입량 f* — 퇴근", unit="회/창", type="float",
                         default=4.8, scope="runtime", group="effect",
                         note="출퇴근 25분 배차 환산.", applies="정류장 신설 배치의 퇴근 효과"),
    "sim.fstar.night": dict(label="신설 주입량 f* — 심야", unit="회/창", type="float",
                            default=0.0, scope="runtime", group="effect",
                            note="0 이 설계 의도 — 심야 신설이 즉시 효과를 낸다는 실측 근거가 없다.",
                            applies="정류장 신설 배치의 심야 효과"),
    "sim.phi.am": dict(label="똑버스 주입량 φ — 출근", unit="회/창", type="float",
                       default=2.4, scope="runtime", group="effect",
                       note="차량 1대 1.2회/h → 2시간 창 환산.", applies="똑버스 배치의 출근 효과"),
    "sim.phi.day": dict(label="똑버스 주입량 φ — 낮", unit="회/창", type="float",
                        default=9.6, scope="runtime", group="effect",
                        note="1.2회/h × 8시간 창.", applies="똑버스 배치의 낮 효과"),
    "sim.phi.pm": dict(label="똑버스 주입량 φ — 퇴근", unit="회/창", type="float",
                       default=2.4, scope="runtime", group="effect",
                       note="1.2회/h × 2시간 창.", applies="똑버스 배치의 퇴근 효과"),
    "sim.phi.night": dict(label="똑버스 주입량 φ — 심야", unit="회/창", type="float",
                          default=2.4, scope="runtime", group="effect",
                          note="1.2회/h × 2시간 창.", applies="똑버스 배치의 심야 효과"),
    "cost.stop.krw": dict(label="정류장 신설 단가", unit="원", type="int",
                          default=42_000_000,
                          scope="runtime", group="cost",
                          note="1회성 시설비(내용연수 10년). 공개단가 미확인 — 가정값.",
                          applies="시뮬레이션 비용·추천 순위(비용효율)·화면 단가 표기"),
    "cost.drt.krw": dict(label="똑버스 단가", unit="원/년", type="int",
                         default=180_000_000,
                         scope="runtime", group="cost",
                         note="경기교통공사 표준운송원가 대비 6~12% 보수적.",
                         applies="시뮬레이션 비용·추천 순위·화면 단가 표기"),
    "cost.freq.krw": dict(label="배차 증편 단가", unit="원/년", type="int",
                          default=95_000_000,
                          scope="runtime", group="cost",
                          note="화성시 마을버스 연간 운송원가 대비 1.8% 이내 대조.",
                          applies="시뮬레이션 비용·추천 순위·화면 단가 표기"),
    "cost.defaultBudget": dict(label="기본 예산 한도", unit="원", type="int",
                               default=3_000_000_000,
                               scope="runtime", group="cost",
                               note="시연용 기본값. 화면에서 건별로 바꿀 수 있다.",
                               applies="예산 미지정 요청의 기본값·시뮬 화면 초기 예산"),
    "rec.maxPlacements": dict(label="추천 최대 배치 건수", unit="건", type="int",
                              default=10, scope="runtime", group="cost",
                              note="화면은 자체 기본 10건을 명시 전송한다 — 이 값은 API 직접 호출과 "
                                   "향후 화면 연동에 적용.",
                              applies="maxPlacements 미지정 추천 요청의 기본값"),
    # ── pipeline 계급 (model.*·baseline.*) — 저장 즉시가 아니라 [지표 재계산]
    #    (약 20초, 스테이징 → 04_model → 시뮬 게이트 → 원자 스왑) 후 반영된다.
    #
    #    한때 model.* 둘은 잠겨 있었다(locked) — params.py 가 import 시점에
    #    오버라이드를 적용하는데 시뮬 엔진이 params 에서 상수를 직접 읽어서,
    #    저장 후 재계산 전에 재기동이 끼면 옛 산출물과 새 상수가 어긋나 기준선
    #    assert 로 서버가 못 뜨는 크래시 루프가 있었다. 2026-08-26 부터
    #    05_simulate 가 기준선 상수를 **norm_stats.json(산출물)에서** 읽으므로
    #    그 창이 사라졌다 — 재계산 전에는 옛 자, 후에는 새 자로 항상 정합하다.
    "model.busTripRate": dict(label="인구→통행 환산계수", unit="통행/인·일", type="float",
                              default=0.25,
                              scope="pipeline", group="model",
                              note="전수단 원단위 2.5 × 버스 분담률 0.10 — 가정값. "
                                   "격자 JSON 에 구워지는 값이라 [지표 재계산] 후 반영됩니다.",
                              applies="잠재수요 KPI(flowTripsPerDay)"),
    "model.minFreqPerHour": dict(label="적정 판정 절대 하한", unit="회/h", type="float",
                                 default=2.0,
                                 scope="pipeline", group="model",
                                 note="야간 상대평가 오라벨 방지 가드. 격자 산출물에 구워지는 "
                                      "값이라 [지표 재계산] 후 반영됩니다.",
                                 applies="사분면 적정/과잉 판정"),
    # ── 기준선 상수 — 수식의 자(尺). 전부 786격자 실측에서 정한 값이라(README §4)
    #    바꾸면 need 밴드(2~25%) assert 가 재계산을 막을 수 있다 — 그게 안전장치다.
    #    wCov 는 따로 받지 않는다: 두 가중은 합이 1 이라 wFreq 가 정하면 따라온다.
    "baseline.wFreq": dict(label="공급지수 운행빈도 가중", unit="", type="float",
                           default=0.78, scope="pipeline", group="baseline",
                           note="접근성 가중(wCov)은 1 − 이 값으로 자동 결정됩니다.",
                           applies="공급지수 S = wFreq·정규화(운행빈도) + wCov·커버리지"),
    "baseline.dampExp": dict(label="빈 땅 MI 감쇠 지수", unit="", type="float",
                             default=0.65, scope="pipeline", group="baseline",
                             note="인구 50명 미만 임야 183칸의 가짜 사각지대를 걷어낸 실측 근거.",
                             applies="MI = (zD−zS) × (D/dRef)^이 값"),
    "baseline.miClamp": dict(label="MI 절대값 상한", unit="±", type="float",
                             default=2.6, scope="pipeline", group="baseline",
                             note="극단값 하나가 색 스케일을 무너뜨리지 않게 자릅니다.",
                             applies="MI 클램프 · 지도 색 스케일"),
    "baseline.eldCoef": dict(label="우선순위 고령 가중", unit="", type="float",
                             default=1.6, scope="pipeline", group="baseline",
                             note="회귀 학습값이 아니라 정책 결정값입니다. 근거는 README §4.",
                             applies="우선순위 = MI × (0.35+인구가중) × (1 + 이 값 × 고령비)"),
    "baseline.covThresholdM": dict(label="커버리지 임계(m)", unit="m", type="float",
                                   default=600.0, scope="pipeline", group="baseline",
                                   note="최근접 정류장 거리 중앙값 392m 실측에서 결정. "
                                        "수단 배지 컷(0.15/0.50)의 미터 환산이 함께 바뀝니다.",
                                   applies="커버리지 = clip(1 − 거리/이 값, 0.05, 1)"),
}

# 기본값 동기화 — SPECS 의 리터럴이 params.py 와 갈라지지 않게 정본에서 덮는다.
_DEFAULT_SRC = {
    "sim.headwayMult": PARAMS.HEADWAY_MULT,
    **{f"sim.fstar.{p}": PARAMS.FSTAR[p] for p in PARAMS.PERIODS},
    **{f"sim.phi.{p}": PARAMS.PHI[p] for p in PARAMS.PERIODS},
    **{f"cost.{t}.krw": PARAMS.COST_TOTAL[t] for t in ("stop", "drt", "freq")},
    "cost.defaultBudget": PARAMS.DEFAULT_BUDGET,
    "model.busTripRate": PARAMS.BASE_VALUES["model.busTripRate"],
    "model.minFreqPerHour": PARAMS.BASE_VALUES["model.minFreqPerHour"],
    **{k: v for k, v in PARAMS.BASE_VALUES.items() if k.startswith("baseline.")},
}
for _k, _v in _DEFAULT_SRC.items():
    SPECS[_k]["default"] = _v

# C계급 — 기준선에 구워진 상수. 표시 전용(살아있는 시뮬 모듈에서 읽는다).
#   (attr, label, note)
# 값은 화면이 왼쪽에 이미 보여준다. 여기에는 **왜 그 값인지**만 적는다 —
# 앞에 숫자를 또 쓰면 한 행에 같은 수가 두 번 나온다.
# 편집 가능한 기준선 상수는 SPECS(baseline.*)로 승격됐다(2026-08-26). 여기 남는
# 것은 파생값(wCov = 1−wFreq)과 다른 단계에 결합돼 열 수 없는 값뿐이다.
BASELINE_DISPLAY = [
    ("W_COV", "공급지수 접근성 가중", "따로 정하지 않습니다 — 운행빈도 가중과 합이 1 이라 자동 결정."),
    ("WALK", "승하차 안분 반경(m)", "03_join 설계와 맞물려 있어 바꾸지 않습니다."),
    ("HEADWAY_MULT", "(현재 적용) 증편 배수", "배차 영향력의 증편 배수가 실제로 주입된 값입니다."),
]


# ─── 업로드 가능한 원본 데이터 ──────────────────────────────────────────────────
#
# **여기 없는 파일은 업로드로 도달할 수 없다.** 저장 경로는 아래 name 에서만 나오고
# 사용자가 보낸 파일명은 화면 표시용으로만 쓴다 — `../` 경로 탈출이 문법적으로 불가능.
#
# 목록에 넣지 않은 이유(조사 결과):
#   rail_stations.csv       04_model.py 에 'rail' 이 0회 — 올려도 격자 판정이 안 바뀐다
#   routes.csv/route_stops  03_join 의 read_plus() 가 _plus 보강본을 우선해 읽지도 않는다
#   od_quarter_wd/stop_emd  ARS 키 정규화가 양쪽 비대칭 — 엑셀 왕복 한 번에 매칭률이
#                           0% 가 되면서도 모든 검증을 통과한다(조용히 틀리는 종류)
#   grid_hwaseong/geojson   재생성에 SGIS 372MB 원본이 필요한데 서버에 없다
#   파이프라인 산출물 전부   덮으면 즉시 세대 불일치 → 기준선 assert 로 서버가 못 뜬다
DATASETS = {
    "stopsNational": {
        "name": "stops_national_hwaseong.csv",
        "label": "정류장 대장(국가표준)",
        "header": ["정류장번호", "정류장명", "위도", "경도", "정보수집일",
                   "모바일단축번호", "도시코드", "도시명", "관리도시명"],
        "keyCol": "정류장번호", "numCols": ["위도", "경도"], "dateCol": "정보수집일",
        "minRows": 500, "maxBytes": 8 * 1024 * 1024,
        "note": "정류장 수·좌표가 공급지수와 화면 KPI에 직접 반영됩니다.",
    },
    "flowHourly": {
        "name": "flow_hourly.csv",
        "label": "유동인구 시간대별",
        "header": ["시군구코드", "시군구명", "시간코드", "외국인구분"]
                  + [f"{g}{a}수" for g in ("남자", "여자")
                     for a in ("0009", "1014", "1519", "2024", "2529", "3034",
                               "3539", "4044", "4549", "5054", "5559", "6064", "6569")]
                  + ["연도_월_일"],
        "keyCol": "시군구코드", "numCols": ["시간코드"], "dateCol": "연도_월_일",
        "minRows": 100, "maxBytes": 8 * 1024 * 1024,
        "note": "잠재수요의 시간축 신호입니다.",
    },
    "boarding": {
        "name": "boarding_hwaseong.csv",
        "label": "정류소별 승하차 집계",
        "header": ["승하차일자", "관할관청", "정류소ID", "정류소번호", "정류소명",
                   "승차합계", "초승", "환승", "하차"],
        "keyCol": "정류소번호", "numCols": ["초승", "승차합계"], "dateCol": "승하차일자",
        "minRows": 10000, "maxBytes": 20 * 1024 * 1024,
        "note": "대용량(약 19MB)입니다 — 회선이 느린 곳에서는 시간이 걸립니다.",
    },
}
UPLOAD_ID_RE = re.compile(r"^upload-\d{8}-\d{6}-[0-9a-f]{8}$")
UPLOAD_LOCK = asyncio.Lock()
_LAST_UPLOAD_AT = {"t": 0.0}


def upload_enabled() -> bool:
    """기본 꺼짐. 매 요청 환경변수를 읽는다 — import 시점 상수로 굳히면
    끄는 데 컨테이너 재기동(60~90초 다운)이 필요해지고 테스트도 못 한다."""
    return os.environ.get("HW_UPLOAD_ENABLED", "0") == "1"


def upload_apply_enabled() -> bool:
    """올린 파일을 **라이브에 실제로 반영**하는 것까지 허용할지. 이것도 기본 꺼짐.
    꺼져 있으면 검증(예행)까지만 되고 라이브 데이터는 한 바이트도 안 바뀐다."""
    return os.environ.get("HW_UPLOAD_APPLY", "0") == "1"


def _decode_upload(raw: bytes) -> tuple:
    """업로드 바이트 → (텍스트, 인코딩 변환 여부).

    한국 사용자가 엑셀에서 'CSV(쉼표로 분리)'로 저장하면 십중팔구 cp949 가 나오고,
    그게 03_join 의 `encoding='utf-8-sig'` 에서 죽는 진짜 함정이다. 접수 시점에
    utf-8-sig 로 정규화해 파이프라인 계약을 맞춘다.
    """
    if raw[:4] == b"PK\x03\x04":
        raise HTTPException(400, "엑셀 파일(.xlsx)은 아직 지원하지 않습니다 — "
                                 "엑셀에서 [다른 이름으로 저장] → CSV UTF-8 로 저장해 올려 주세요.")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise HTTPException(400, "UTF-16(엑셀 '유니코드 텍스트') 파일입니다 — "
                                 "[다른 이름으로 저장] → CSV UTF-8 로 저장해 주세요.")
    if b"\x00" in raw[:8192]:
        raise HTTPException(400, "텍스트 파일이 아닙니다(이진 데이터).")
    try:
        return raw.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp949"), True
    except UnicodeDecodeError:
        raise HTTPException(400, "문자 인코딩을 알 수 없습니다 — CSV UTF-8 로 저장해 주세요.")


def _scan_csv(text: str, spec: dict) -> dict:
    """헤더 정확 일치 + 행수 + 표본 검사. **전량 파싱 금지** —
    19MB 파일을 list(csv.reader(...)) 로 읽으면 메모리가 400MB 넘게 뛴다.
    헤더만 읽고 이후로는 행 수만 세며 흘려보낸다."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [c.strip() for c in next(reader)]
    except StopIteration:
        raise HTTPException(400, "빈 파일입니다.")

    want = spec["header"]
    if header != want:
        missing = [c for c in want if c not in header]
        extra = [c for c in header if c not in want]
        detail = f"컬럼이 다릅니다 — 기대 {len(want)}개 / 받음 {len(header)}개."
        if missing:
            detail += f" 없는 컬럼: {', '.join(missing[:6])}."
        if extra:
            detail += f" 모르는 컬럼: {', '.join(extra[:6])}."
        if not missing and not extra:
            detail += " 컬럼 순서가 다릅니다."
        raise HTTPException(400, detail)

    idx = {c: i for i, c in enumerate(header)}
    rows = 0
    bad_num = 0
    dmin = dmax = None
    di = idx.get(spec.get("dateCol") or "")
    nums = [idx[c] for c in spec.get("numCols", []) if c in idx]
    for row in reader:
        if not row or len(row) != len(header):
            continue
        rows += 1
        if rows <= 200:
            for n in nums:
                try:
                    float(row[n])
                except (ValueError, IndexError):
                    bad_num += 1
        if di is not None and row[di]:
            v = row[di].strip()
            dmin = v if dmin is None or v < dmin else dmin
            dmax = v if dmax is None or v > dmax else dmax

    if rows < spec["minRows"]:
        raise HTTPException(400, f"행이 너무 적습니다 — {rows:,}행(최소 {spec['minRows']:,}행). "
                                 "파일이 잘렸는지 확인해 주세요.")
    if nums and bad_num > len(nums) * min(rows, 200) * 0.1:
        raise HTTPException(400, f"숫자여야 할 칸에 숫자가 아닌 값이 많습니다"
                                 f"({spec['numCols']} 표본 {bad_num}건). 컬럼이 밀렸는지 확인해 주세요.")
    return {"rows": rows, "dateFrom": dmin, "dateTo": dmax}


def _live_rows(spec: dict) -> Optional[int]:
    """라이브 파일 행수 — 비교용. 큰 파일이라 폴링마다 세지 않도록 mtime 으로 캐시."""
    p = ROOT / "dataset_hwaseong" / spec["name"]
    if not p.exists():
        return None
    # 파일별로 한 칸씩 — 전체를 비우면 상태 폴링(1.2초)마다 30만 행짜리
    # 승하차 파일을 다시 세게 된다.
    key, stamp = str(p), p.stat().st_mtime_ns
    hit = _LIVE_ROWS_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    with open(p, encoding="utf-8-sig", newline="") as fh:
        n = max(sum(1 for _ in fh) - 1, 0)
    _LIVE_ROWS_CACHE[key] = (stamp, n)
    return n


_LIVE_ROWS_CACHE: dict = {}


def _prune_uploads() -> None:
    """upload-* 정리 — **나이 기준**(2시간)이 먼저, 개수 상한은 보조.

    개수 기준만 쓰면 더미 몇 개를 연달아 올려 발표자가 확인창을 띄워 둔 사이
    그 uploadId 를 밀어낼 수 있다. 접두어는 반드시 upload-* — stage-* 를 쓰면
    _prune_dirs("stage-*", keep=0) 이 갱신마다 통째로 지운다.
    """
    now = time.time()
    for d in sorted(VAR.glob("upload-*")):
        try:
            if now - d.stat().st_mtime > 7200:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    for d in sorted(VAR.glob("upload-*"))[:-10]:
        shutil.rmtree(d, ignore_errors=True)


def _accept_upload(req, spec: dict) -> dict:
    """디코드 → 검증 → var/upload-*/ 격리 저장. 스레드에서 돈다(CPU 작업).
    **라이브 데이터는 이 함수 어디에서도 건드리지 않는다.**"""
    try:
        raw = base64.b64decode(req.contentB64, validate=True)
    except Exception:
        raise HTTPException(400, "파일을 읽지 못했습니다(전송이 손상됐을 수 있습니다).")
    if len(raw) > spec["maxBytes"]:
        raise HTTPException(413, f"파일이 너무 큽니다 — 최대 {spec['maxBytes'] // 1048576}MB 까지입니다.")

    text, converted = _decode_upload(raw)
    scan = _scan_csv(text, spec)

    live_rows = _live_rows(spec)
    warnings = []
    if live_rows:
        delta = (scan["rows"] - live_rows) / live_rows
        if abs(delta) > 0.5:
            warnings.append(f"행 수가 기존 대비 {delta * 100:+.0f}% 변했습니다 "
                            f"({live_rows:,} → {scan['rows']:,}행) — 파일이 맞는지 확인해 주세요.")

    body = text.encode("utf-8-sig")     # 03_join 이 utf-8-sig 로 여는 계약을 여기서 맞춘다
    _prune_uploads()
    uid = f"upload-{now_kst().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
    d = VAR / uid
    d.mkdir(parents=True, exist_ok=True)
    (d / spec["name"]).write_bytes(body)
    meta = {"uploadId": uid, "datasetId": req.datasetId, "name": spec["name"],
            "originalFilename": (req.filename or "")[:200], "label": spec["label"],
            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            "rows": scan["rows"], "dateFrom": scan["dateFrom"], "dateTo": scan["dateTo"],
            "encodingConverted": converted, "reason": req.reason.strip(),
            "actor": req.actor, "at": _now()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    return {**meta, "liveRows": live_rows, "warnings": warnings,
            "note": spec.get("note", ""), "applyEnabled": upload_apply_enabled()}


def _overlay_upload(stage: Path, upload_id: str) -> dict:
    """올린 원본을 **스테이지에만** 얹는다.

    var/ 는 호스트 바인드 마운트라 밖에서 바꿀 수 있으므로 접수 때 기록한 해시를
    다시 대조한다. 그리고 **조용한 스킵은 절대 금지** — 못 얹었는데 파이프라인이
    그대로 돌면 "올렸는데 아무것도 안 바뀐다"가 되어 원인을 찾을 수 없다.
    """
    d = VAR / upload_id
    mp = d / "meta.json"
    if not mp.exists():
        raise RuntimeError(f"업로드 {upload_id} 를 찾을 수 없습니다(보관 기간이 지났을 수 있습니다)")
    meta = json.loads(mp.read_text("utf-8"))
    spec = DATASETS.get(meta.get("datasetId"))
    if spec is None or meta.get("name") != spec["name"]:
        raise RuntimeError("업로드 정보가 올바르지 않습니다")
    src = d / spec["name"]
    if not src.exists():
        raise RuntimeError(f"업로드 파일이 없습니다: {spec['name']}")
    if hashlib.sha256(src.read_bytes()).hexdigest() != meta.get("sha256"):
        raise RuntimeError("업로드 파일이 접수 이후 변경됐습니다 — 다시 올려 주세요")
    shutil.copy2(src, stage / "dataset_hwaseong" / spec["name"])
    _log(f"[업로드] {spec['label']} — {spec['name']} {meta.get('rows', 0):,}행을 스테이지에 반영")
    return meta


def _grid_rows(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig", newline="") as fh:
        return {(r["grid_id"], r["period"]): r for r in csv.DictReader(fh)}


def _dry_run_diff(stage: Path, upload_meta: Optional[dict]) -> dict:
    """스테이지 산출물과 라이브를 비교한다 — 스테이지가 지워지기 전에 뽑아야 한다.

    **사각지대 칸 수는 쓰지 않는다.** norm_stats 가 매 실행 재발행돼 z 가 다시
    중심화되므로 데이터를 크게 흔들어도 칸 수는 거의 안 움직인다(측정: 승차량을
    전부 1.5배로 올려도 출근 30 → 29). 실제로 움직이는 것은 **어느 격자가 어떤
    판정을 받았는가**와 우선순위 순서다.
    """
    out: dict = {"quadrantChanged": None, "topRegions": None, "cvLogR2": None}
    try:
        live = _grid_rows(ROOT / "dataset_hwaseong" / "grid_metrics.csv")
        new = _grid_rows(stage / "dataset_hwaseong" / "grid_metrics.csv")
        common = set(live) & set(new)
        out["comparedCells"] = len(common)
        out["quadrantChanged"] = sum(
            1 for k in common if live[k].get("quadrant") != new[k].get("quadrant"))

        def _top(rows: dict, period: str = "am", n: int = 5) -> list:
            cand = [r for k, r in rows.items() if k[1] == period]
            cand.sort(key=lambda r: float(r.get("priority") or 0), reverse=True)
            seen, top = set(), []
            for r in cand:
                reg = r.get("region") or "-"
                if reg not in seen:
                    seen.add(reg)
                    top.append(reg)
                if len(top) >= n:
                    break
            return top

        out["topRegions"] = {"before": _top(live), "after": _top(new)}
    except Exception as e:
        _log(f"비교 산출 일부 실패(무시): {type(e).__name__}: {e}")

    try:
        vj = json.loads((stage / "dataset_hwaseong" / "validation.json").read_text("utf-8"))
        lj = json.loads((ROOT / "dataset_hwaseong" / "validation.json").read_text("utf-8"))
        out["cvLogR2"] = {"before": (lj.get("r2") or {}).get("cv_log"),
                          "after": (vj.get("r2") or {}).get("cv_log")}
    except Exception:
        pass

    if upload_meta:
        out["upload"] = {k: upload_meta.get(k) for k in
                         ("uploadId", "label", "name", "rows", "dateFrom", "dateTo",
                          "sha256", "encodingConverted", "originalFilename")}
    return out


# ─── 오버라이드 저장소 ──────────────────────────────────────────────────────────
def _read_override() -> dict:
    try:
        raw = json.loads(OVERRIDE_PATH.read_text("utf-8"))
        params = raw.get("params", {})
        ok = {}
        for k, v in (params.items() if isinstance(params, dict) else []):
            # 사람 소유 파일이라 수기 편집을 전제한다 — 형식이 어긋난 항목은
            # 서버를 죽이는 대신 버리고 크게 알린다.
            if (isinstance(v, dict) and isinstance(v.get("value"), (int, float))
                    and not isinstance(v.get("value"), bool)):
                ok[k] = v
            else:
                print(f"[admin] 오버라이드 항목 형식 오류로 무시: {k}={v!r}",
                      file=sys.stderr, flush=True)
        return ok
    except FileNotFoundError:
        return {}
    except Exception as e:  # 손상 — 조용히 넘어가지 않는다 (db.py 의 문화)
        print(f"[admin] 오버라이드 파일 손상, 기본값으로 동작: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return {}


def _write_override(params: dict) -> None:
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "updatedAt": _now(), "params": params}
    tmp = OVERRIDE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, OVERRIDE_PATH)  # 같은 디렉토리 → 원자 교체


def _append_history(event: dict) -> None:
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": _now(), **event}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _now() -> str:
    """이력·잡·보고서에 찍히는 모든 시각의 정본. 반드시 KST 다."""
    return now_kst().isoformat(timespec="seconds")


def effective(key: str):
    """지금 계산에 쓰여야 할 값 — 오버라이드 우선, 없으면 코드 기본값."""
    ov = _read_override().get(key)
    if ov is not None:
        return ov["value"]
    return SPECS[key]["default"]


def _pipeline_effective(key: str):
    """pipeline 계급의 '실제 적용값' — 산출물(메모리 meta)에 구워진 값을 읽는다.
    오버라이드를 저장해도 재계산 전에는 여기 값이 안 바뀐다(= 재계산 대기)."""
    meta = (_ctx["DATA"] or {}).get("meta") or {}
    a = meta.get("assumptions") or {}
    if key == "model.busTripRate":
        return (a.get("busTripRate") or {}).get("value")
    # 나머지는 시뮬 엔진 속성에서 읽는다 — 05_simulate 가 norm_stats(산출물)의
    # 상수를 쓰므로 "산출물에 실제로 구워진 값"과 항상 같다.
    sim = (_ctx["DATA"] or {}).get("sim")
    attr = {"model.minFreqPerHour": "MIN_FREQ_PER_H", "baseline.wFreq": "W_FREQ",
            "baseline.dampExp": "DAMP_EXP", "baseline.miClamp": "MI_CLAMP",
            "baseline.eldCoef": "ELD", "baseline.covThresholdM": "COVM"}.get(key)
    if sim is not None and attr:
        v = getattr(sim, attr, None)
        return float(v) if v is not None else None
    return None


# ─── 값 주입 — 서버 상수 + 시뮬 엔진 + meta 를 한 번에 ─────────────────────────
def apply_runtime_params() -> dict:
    """오버라이드를 서빙 상태에 주입한다. lifespan 직후 · 저장 직후 · 재적재 직후
    세 곳 모두에서 불려야 한다 — 하나라도 빼먹으면 값이 소리 없이 초기화되거나
    한 화면에 두 단가가 표시된다."""
    DATA, COST_KRW, PERIODS = _ctx["DATA"], _ctx["COST_KRW"], _ctx["PERIODS"]
    raw = _read_override()
    ov = {k: v["value"] for k, v in raw.items()
          if k in SPECS and SPECS[k]["scope"] == "runtime"}
    eff = {k: ov.get(k, s["default"]) for k, s in SPECS.items()}

    # ① 서버 계산 정본
    for t in ("stop", "drt", "freq"):
        COST_KRW[t] = eff[f"cost.{t}.krw"]

    # ② 시뮬 엔진 — 요청 시점에 sim.* 속성으로 읽힌다 (기준선과 무관한 배치 물리량만)
    sim = DATA.get("sim")
    if sim is not None:
        sim.HEADWAY_MULT = eff["sim.headwayMult"]
        for p in PERIODS:
            sim.FSTAR[p] = eff[f"sim.fstar.{p}"]
            sim.PHI[p] = eff[f"sim.phi.{p}"]

    # ③ 화면·챗봇·보고서가 보는 meta. 제자리 수정이 아니라 사본을 완성한 뒤
    #    단일 키 재바인딩으로 교체한다 — 다른 스레드가 같은 dict 를 json.dumps 로
    #    순회하는 도중 키를 꽂으면 RuntimeError(500)가 난다.
    meta = copy.deepcopy(DATA.get("meta")) if DATA.get("meta") else None
    if meta and "cost" in meta:
        for t in ("stop", "drt", "freq"):
            c = meta["cost"].get(t)
            if not c:
                continue
            c["krw"] = eff[f"cost.{t}.krw"]
            life = c.get("lifeYears") or 1
            c["annualKrw"] = round(c["krw"] / life)
            c["overridden"] = f"cost.{t}.krw" in ov
        meta["cost"]["defaultBudget"] = eff["cost.defaultBudget"]
        for e in meta.get("effects", []):
            t = e.get("type")
            if t in ("stop", "drt", "freq") and t in meta["cost"]:
                e["unitKrw"] = meta["cost"][t]["krw"]
                e["annualKrw"] = meta["cost"][t]["annualKrw"]
                e["overridden"] = meta["cost"][t].get("overridden", False)
        meta["paramsOverride"] = {"count": len(ov),
                                  "updatedAt": max((v.get("at", "") for v in raw.values()), default=None)}
        DATA["meta"] = meta
    return eff


# ─── 격자 판정 오버라이드 ──────────────────────────────────────────────────────
#
# schema_ops.sql 의 admin_grid_override 가 설계한 기능의 파일 기반 구현이다.
# DB 없이도(기본 배포 = 계약 JSON 모드) 동작해야 해서 params_override 와 같은
# var/admin 파일 방식을 쓴다 — var/ 는 compose 에서 바인드 마운트라 재배포를
# 넘어 살아남고, 재계산(batch)이 이 파일을 건드리지 않으므로 "배치를 몇 번
# 돌려도 사람이 고친 값이 살아남는다"는 스키마의 요점이 그대로 성립한다.
#
# 1단계에서 여는 필드는 판정 계층 셋뿐이다 — quadrant(사분면) · action(수단
# 배지) · priorityScore(우선순위 점수). 숫자 지표(mi·demand·supply)는 색 bin·
# 산점도·시뮬 기준선과 얽혀 있어 반쪽 수정이 된다(값만 바꾸면 지도 색은 그대로,
# bin 까지 다시 매기려면 norm_stats 를 통째로 끌어와야 한다). 스키마는 여섯
# 필드를 정의하지만 나머지 셋은 그 얽힘을 풀 때 연다.
#
# ⚠️ 오버라이드는 **표시·판단 계층**이다. 시뮬레이션·추천의 기준선은
# grid_metrics.csv 에서 온 sim 모듈 상태라 여기 영향을 받지 않는다 — 관리자가
# 격자를 need 로 고쳐도 추천 후보 선정은 모델 값을 따른다. KPI(needCells 등)는
# 셀에서 재집계하므로 함께 움직인다(db._kpi — "지도만 붉어지고 KPI 는 그대로"
# 사고 방지).
GRID_OVERRIDE_PATH = ADMIN_DIR / "grid_override.json"

GRID_FIELDS = {
    "quadrant":      dict(kind="text", allowed=("need", "drt", "over", "ok", "mid")),
    "action":        dict(kind="text", allowed=("DRT", "NEW_STOP", "ADD_FREQ")),
    "priorityScore": dict(kind="num"),
}


def _read_grid_overrides() -> list:
    """전체 기록(취소분 포함)을 돌려준다 — 되돌리기는 삭제가 아니라 revokedAt."""
    try:
        doc = json.loads(GRID_OVERRIDE_PATH.read_text("utf-8"))
        recs = doc.get("overrides", [])
        return recs if isinstance(recs, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[admin] grid_override.json 을 읽지 못했습니다 — 무시하고 계속: {e}",
              file=sys.stderr, flush=True)
        return []


def _write_grid_overrides(recs: list) -> None:
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "updatedAt": _now(), "overrides": recs}
    tmp = GRID_OVERRIDE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, GRID_OVERRIDE_PATH)


def _grid_payload(period: str, daytype: str):
    DATA = _ctx["DATA"] or {}
    key = f"grid_{period}" if daytype == "wd" else f"grid_{period}_we"
    return DATA.get(key)


def _find_cell(payload: dict, grid_id: str):
    for c in payload.get("cells", []):
        if c["id"] == grid_id:
            return c
    return None


def _set_cell_field(cell: dict, field: str, value) -> None:
    """필드와 그 파생 라벨을 함께 바꾼다 — 라벨을 빼먹으면 지도 툴팁·표가
    옛 한글 라벨로 남아 값과 라벨이 서로 다른 말을 한다."""
    cell[field] = value
    if field == "quadrant":
        cell["quadrantLabel"] = (_ctx.get("QUAD_LABEL") or {}).get(value, value)
    elif field == "action":
        cell["actionLabel"] = (_ctx.get("ACTION_LABEL") or {}).get(value, value)


def _recount_kpi(payload: dict) -> None:
    from server import db          # 순수 함수만 쓴다 — psycopg2 는 함수 안 지연 import
    payload["kpi"] = db._kpi(payload["cells"])


def _live_grid_overrides(recs: list):
    return [r for r in recs if not r.get("revokedAt")]


def _refresh_cell_override_flag(cell: dict, recs: list, period: str, daytype: str) -> None:
    cell["overridden"] = any(
        r["gridId"] == cell["id"] and r["period"] == period and r["daytype"] == daytype
        for r in _live_grid_overrides(recs))


def apply_grid_overrides() -> int:
    """살아 있는 오버라이드를 DATA 에 주입한다.

    **스냅샷 재적재 직후에만** 부른다(lifespan · /refresh 의 DATA.update 뒤) —
    이때 셀은 산출물 원값이라, 재계산으로 원값이 바뀌었으면 prev 를 그 값으로
    따라잡아 두어야 나중의 되돌리기가 낡은 원값을 되살리지 않는다.
    저장/취소 엔드포인트는 이 함수를 쓰지 않고 제자리 수정을 한다 — 이미
    오버라이드가 적용된 셀 위에서 이 함수를 다시 돌리면 prev 따라잡기가
    오버라이드된 값을 원값으로 착각한다.
    """
    recs = _read_grid_overrides()
    live = _live_grid_overrides(recs)
    if not live:
        return 0
    touched, dirty, applied = set(), False, 0
    for r in live:
        payload = _grid_payload(r["period"], r["daytype"])
        if payload is None:                       # _we 산출물이 없는 배포본
            continue
        cell = _find_cell(payload, r["gridId"])
        if cell is None:                          # 격자 개편으로 사라진 칸
            continue
        cur = cell.get(r["field"])
        if cur != r["value"]:
            if r.get("prev") != cur:
                r["prev"] = cur
                dirty = True
            _set_cell_field(cell, r["field"], r["value"])
            applied += 1
        cell["overridden"] = True
        touched.add((r["period"], r["daytype"]))
    for period, daytype in touched:
        _recount_kpi(_grid_payload(period, daytype))
    if dirty:
        _write_grid_overrides(recs)
    return applied


# ─── 인증 ──────────────────────────────────────────────────────────────────────
_AUTH = {"fail": 0, "locked_until": 0.0, "logged": 0}


def auth_required() -> bool:
    return bool(os.environ.get("ADMIN_TOKEN", ""))


def warn_if_open() -> None:
    """기동 시 1회 — 무인증 상태를 조용히 넘기지 않는다(db.py 의 문화)."""
    if not auth_required():
        print("[admin] ⚠ ADMIN_TOKEN 미설정 — 관리자 콘솔이 인증 없이 열려 있습니다. "
              "공개 서버라면 .env 에 ADMIN_TOKEN 을 설정하세요.", file=sys.stderr, flush=True)
    else:
        print("[admin] 관리자 콘솔 토큰 인증 활성", flush=True)


async def require_read(authorization: str = Header(default="")):
    """조회는 **토큰이 있든 없든 연다.**

    "보는 데는 잠금이 없고, 바꾸는 데만 잠금이 있습니다" 의 앞쪽 절이다.
    파라미터 대장(값·근거·기본값)과 데이터 상태는 심사위원·시민이 그대로
    열어봐도 되는 정보이고, 오히려 열어 두는 것이 이 제품의 강점이다.
    (반환값은 '이 요청이 쓰기 권한까지 가졌는가' — 이력 필터가 쓴다.)"""
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        return True
    given = authorization[7:].strip() if authorization.startswith("Bearer ") else authorization.strip()
    return bool(given and hmac.compare_digest(given, token))


async def require_write(authorization: str = Header(default=""),
                        x_forwarded_for: str = Header(default="")):
    """저장·재계산 — 토큰이 설정돼 있으면 반드시 요구한다.
    미설정 서버(내부망 전제)에서는 통과하되 경고는 이미 기동 시 찍혔다.
    workers=1 이라 재계산 트리거 하나가 전 API 를 멈출 수 있어, 공개 서버에서
    이 문이 열려 있으면 시연 중 사고로 이어진다."""
    if not os.environ.get("ADMIN_TOKEN", ""):
        return True
    return await _check_token(authorization, x_forwarded_for)


async def _check_token(authorization: str, x_forwarded_for: str):
    token = os.environ.get("ADMIN_TOKEN", "")
    given = authorization[7:].strip() if authorization.startswith("Bearer ") else authorization.strip()
    # 정답 토큰은 잠금 중에도 통과한다 — 잠금은 브루트포스 지연 장치이지 정당한
    # 관리자를 봉쇄하는 장치가 아니다. (구버전 토큰을 든 탭의 폴링이 잠금을
    # 재점화해 새 토큰까지 429 로 막는 역공을 함께 차단한다.)
    if given and hmac.compare_digest(given, token):
        _AUTH["fail"] = 0
        _AUTH["logged"] = 0  # 성공 시 리셋 — 누적 10회 뒤 감사 기록이 무음화되는 것 방지
        return True
    if time.monotonic() < _AUTH["locked_until"]:
        raise HTTPException(429, "인증 실패가 반복되어 잠겼습니다. 10분 뒤 다시 시도하세요.")
    _AUTH["fail"] += 1
    if _AUTH["logged"] < 10:  # 이력 파일을 무한히 키우지 못하게 창당 10줄 상한
        _AUTH["logged"] += 1
        # XFF 최우측 = 신뢰 프록시(Caddy)가 덧붙인 실제 접속 IP. 최좌측은 위조 가능.
        _append_history({"kind": "auth.fail",
                         "ip": (x_forwarded_for.split(",")[-1].strip() or "-")})
    if _AUTH["fail"] >= 10:
        _AUTH["fail"] = 0
        _AUTH["logged"] = 0
        _AUTH["locked_until"] = time.monotonic() + 600
    await asyncio.sleep(1.0)  # async 지연 — 스레드풀을 잡지 않는다
    raise HTTPException(401, "관리자 토큰이 올바르지 않습니다.")


# 라우터 공통 의존성은 **읽기** 기준. 쓰기 엔드포인트는 아래에서 각자
# Depends(require_write) 를 하나 더 단다.
router = APIRouter(prefix="/api/v1/admin", tags=["admin"],
                   dependencies=[Depends(require_read)])


# ─── 최신화 잡 ──────────────────────────────────────────────────────────────────
JOB = {"id": None, "status": "idle",  # idle | running | done | failed
       "steps": [], "step": None, "log": collections.deque(maxlen=400),
       "startedAt": None, "finishedAt": None, "error": None, "result": None}
RELOAD_LOCK = asyncio.Lock()
# save_params 는 동기 핸들러(스레드풀 병렬)라 read-modify-write 에 락이 필요하다 —
# 없으면 동시 저장 중 나중 쓰기가 먼저 쓴 키를 조용히 되돌린다(lost update).
SAVE_LOCK = threading.Lock()

_STEP_SCRIPT = {"join": "03_join.py", "model": "04_model.py",
                "validate": "07_validate.py", "load": "05_load.py"}
# 각 단계가 갱신하는 라이브 파일 — 스테이징 성공 후 이것만 원자 교체한다.
_STEP_OUT = {
    "join": ["dataset_hwaseong/stops_hwaseong.csv", "dataset_hwaseong/grid_join.csv"],
    # grid_metrics_we.csv 를 빼면 라이브가 "새 기준선 + 옛 주말 지표" 혼합이 되고
    # 05_simulate.py 의 `[we] 기준선 불일치` assert 로 서버가 못 뜬다(크래시 루프).
    # 04_model.py 가 쓰는 산출물과 이 목록이 어긋나지 않는지 tests 가 검사한다.
    "model": ["dataset_hwaseong/grid_metrics.csv", "dataset_hwaseong/grid_metrics_we.csv",
              "dataset_hwaseong/norm_stats.json"],
    "validate": ["dataset_hwaseong/validation.json"],
}


def _log(line: str) -> None:
    JOB["log"].append(line)
    print(f"[admin.refresh] {line}", flush=True)


async def _run_script(script: Path, cwd: Path, label: str) -> None:
    JOB["step"] = label
    _log(f"── {label} 시작")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
             # 스테이징에서 돌아도 params.py 가 실제 오버라이드를 읽게 한다
             "HW_VAR_DIR": str(VAR)})
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        _log(f"[{label}] {line.decode('utf-8', 'replace').rstrip()}")
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"{label} 실패 (종료코드 {rc}) — 스테이징만 오염, 라이브 무변경")


async def _reload_data() -> dict:
    """디스크/DB → 메모리 재적재. 실패하면 예외 — DATA 는 손대지 않았으므로
    기존 데이터로 서빙이 계속된다(자동 롤백)."""
    async with RELOAD_LOCK:
        new = await asyncio.to_thread(_ctx["build_snapshot"])   # 기준선 assert 포함 ~5초
        _ctx["DATA"].update(new)                                 # lifespan 과 같은 교체 방식
        apply_runtime_params()                                   # 새 sim 모듈에 오버라이드 재주입!
        apply_grid_overrides()                                   # 새 셀(원값)에 격자 오버라이드 재주입
    kpi = new.get("grid_am", {}).get("kpi", {})
    return {"updatedAt": new.get("meta", {}).get("updatedAt"),
            "cells": len(new.get("cells", {}).get("am", {})),
            "needCellsAm": kpi.get("needCells")}


def _prune_dirs(pattern: str, keep: int) -> None:
    """stage-*/backup-* 이 무한히 쌓여 (도커에선 호스트의) 디스크를 채우지 않게
    최근 keep 개만 남긴다. 이름이 타임스탬프라 정렬 = 시간순이다."""
    dirs = sorted(VAR.glob(pattern))
    for d in dirs[:-keep] if keep else dirs:
        shutil.rmtree(d, ignore_errors=True)


async def _run_refresh(steps: list, reason: str, actor: str,
                       upload_id: Optional[str] = None, apply: bool = True) -> None:
    ts = now_kst().strftime("%Y%m%d-%H%M%S")   # 백업 폴더명도 KST
    pipeline_steps = [s for s in ("join", "model", "validate", "load") if s in steps]
    stage = None
    upload_meta: Optional[dict] = None
    backup_path: Optional[Path] = None
    try:
        before_kpi = (_ctx["DATA"].get("grid_am") or {}).get("kpi", {}).get("needCells")

        if pipeline_steps:
            # 1) 스테이징 — 제자리 덮어쓰기 금지. 04_model 은 저장 후 검증이라
            #    실패 시 반쪽 산출물이 남고, 그 상태로 재시작하면 서버가 못 뜬다.
            stage = VAR / f"stage-{ts}"
            _prune_dirs("stage-*", keep=0)      # 이전 실행 잔재 정리 (실패분 포함)
            _prune_dirs("backup-*", keep=5)
            JOB["step"] = "stage"
            _log(f"스테이징 준비: {stage}")
            (stage / "server").mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copytree, ROOT / "dataset_hwaseong", stage / "dataset_hwaseong")
            (stage / "analysis").mkdir(parents=True, exist_ok=True)
            for py in (ROOT / "analysis").glob("*.py"):
                shutil.copy2(py, stage / "analysis" / py.name)
            (stage / "server" / "static").mkdir(parents=True, exist_ok=True)
            spec_file = ROOT / "dataset_hwaseong" / "grid_spec.json"
            if spec_file.exists():
                shutil.copy2(spec_file, stage / "dataset_hwaseong" / "grid_spec.json")

            # 1-b) 올린 원본을 스테이지에만 얹는다 — 라이브는 그대로다.
            if upload_id:
                upload_meta = await asyncio.to_thread(_overlay_upload, stage, upload_id)

            # 2) 파이프라인 실행 (각 스크립트 말미 assert 가 검증 게이트)
            for s in pipeline_steps:
                await _run_script(stage / "analysis" / _STEP_SCRIPT[s], stage, s)

            # 3) 시뮬 import 게이트 — "스왑하면 서버가 못 뜰 산출물"을 여기서 차단
            gate = ("import importlib.util,sys;"
                    "p=sys.argv[1];"
                    "spec=importlib.util.spec_from_file_location('sim_gate',p);"
                    "m=importlib.util.module_from_spec(spec);"
                    "spec.loader.exec_module(m);"
                    "print('기준선 게이트 통과')")
            JOB["step"] = "gate"
            _log("── 시뮬 기준선 게이트")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", gate, str(stage / "analysis" / "05_simulate.py"),
                cwd=str(stage), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
            out, _ = await proc.communicate()
            for ln in out.decode("utf-8", "replace").splitlines()[-5:]:
                _log(f"[gate] {ln}")
            if proc.returncode != 0:
                raise RuntimeError("시뮬 기준선 게이트 실패 — 산출물을 반영하지 않습니다")

            # 4) 예행이면 여기서 끝낸다 — 백업·교체·재적재를 전부 건너뛴다.
            #    스테이지는 finally 에서 지워지므로 비교는 **지금** 뽑아야 한다.
            if not apply:
                JOB["step"] = "diff"
                _log("── 예행 — 라이브는 바꾸지 않고 결과만 비교합니다")
                dry = await asyncio.to_thread(_dry_run_diff, stage, upload_meta)
                dry["dryRun"] = True
                JOB.update(status="done", result=dry, finishedAt=_now(), step=None)
                _log(f"예행 완료 — 판정이 바뀐 격자 {dry.get('quadrantChanged')}개 "
                     f"/ {dry.get('comparedCells')}개 · 라이브 데이터는 변경되지 않았습니다")
                _append_history({"kind": "upload.dryrun", "jobId": JOB["id"], "ok": True,
                                 "uploadId": upload_id, "reason": reason,
                                 "result": dry, "actor": actor})
                return

            # 5) 백업 → 6) 파일별 원자 교체 (dataset 과 static 을 한 쌍으로)
            JOB["step"] = "swap"
            swap: list = []
            for s in pipeline_steps:
                swap += _STEP_OUT.get(s, [])
            if upload_meta:
                # 원본도 함께 교체하지 않으면 파생물만 새것이 되고, 다음 갱신에서
                # 옛 원본으로 조용히 되돌아간다 — 재현이 안 되는 종류의 사고다.
                swap += [f"dataset_hwaseong/{upload_meta['name']}"]
            if "load" in pipeline_steps:
                stage_static = {p.name for p in (stage / "server" / "static").glob("*.json")}
                live_static = {p.name for p in (ROOT / "server" / "static").glob("*.json")}
                # 05_load 는 주말 입력이 없으면 *_we.json 을 print 만 하고 건너뛴 채
                # 종료코드 0 으로 끝난다. 그대로 교체하면 화면이 평일=새것 / 주말=옛것으로
                # 조용히 갈린다 — 빠진 게 있으면 아예 교체하지 않는다.
                missing = live_static - stage_static
                if missing:
                    raise RuntimeError(
                        f"스테이지에 계약 JSON {len(missing)}개가 없습니다"
                        f"({', '.join(sorted(missing)[:5])}) — 세대가 갈리므로 교체하지 않습니다")
                swap += [f"server/static/{n}" for n in sorted(stage_static)]

            # 교체 도중 디스크가 차면 앞쪽만 새것이 되어 세대 불일치로 서버가 못 뜬다.
            # 백업본과 .new 임시본까지 감안해 넉넉히 잡고, 모자라면 **교체를 시작하기
            # 전에** 멈춘다(라이브 무변경).
            need = sum((stage / rel).stat().st_size for rel in swap if (stage / rel).exists())
            free = shutil.disk_usage(ROOT).free
            if free < need * 3:
                raise RuntimeError(
                    f"디스크 여유 부족 — 약 {need * 3 // 1048576}MB 필요, 현재 "
                    f"{free // 1048576}MB. 교체를 시작하지 않았습니다(라이브 무변경)")

            # 이전 실패가 남긴 .new 고아 정리 — 남겨두면 이후 copytree 마다 얹힌다.
            for _d in (ROOT / "dataset_hwaseong", ROOT / "server" / "static"):
                for _orphan in _d.glob("*.new"):
                    _orphan.unlink(missing_ok=True)

            backup = VAR / f"backup-{ts}"
            backup_path = backup
            backup.mkdir(parents=True, exist_ok=True)
            for rel in swap:
                live = ROOT / rel
                if live.exists():
                    dst = backup / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, dst)
            _log(f"백업 {len(swap)}개 → {backup}")

            # 파일 하나하나는 원자적이지만 **집합 전체는 아니다.** 중간에 죽으면 앞쪽만
            # 새것이 되어 05_simulate 의 기준선 assert 로 서버가 못 뜬다 — 이미 바꾼
            # 것만 백업에서 되돌려 라이브를 한 세대로 유지한다.
            done_rel: list = []
            try:
                for rel in swap:
                    # stage(var/, 도커에선 바인드 마운트)와 ROOT 는 다른 파일시스템일 수
                    # 있어 os.replace 직행은 EXDEV 로 죽는다. 목적지와 같은 디렉토리에
                    # .new 로 복사한 뒤 replace — 원자성은 목적지 fs 안에서 성립한다.
                    live = ROOT / rel
                    tmp = live.with_name(live.name + ".new")
                    shutil.copy2(stage / rel, tmp)
                    os.replace(tmp, live)
                    done_rel.append(rel)
            except Exception as e:
                _log(f"⚠ 교체 실패({type(e).__name__}) — {len(done_rel)}개 되돌리는 중")
                for rel in reversed(done_rel):
                    live = ROOT / rel
                    src = backup / rel
                    try:
                        if src.exists():
                            tmp = live.with_name(live.name + ".rb")
                            shutil.copy2(src, tmp)
                            os.replace(tmp, live)
                        else:
                            live.unlink(missing_ok=True)   # 교체 전 라이브에 없던 파일
                    except Exception as re:
                        _log(f"⚠ {rel} 되돌리기 실패: {type(re).__name__}: {re}")
                _log("되돌리기 완료 — 라이브는 교체 전 상태입니다")
                raise RuntimeError(f"원자 교체 실패, 되돌렸습니다: {type(e).__name__}: {e}")
            _log(f"원자 교체 {len(swap)}개 완료")

        # DB 재적재 — 명시 요청("db")했거나, DB 모드에서 계약 JSON 을 새로
        # 구웠을 때("load"). 빼먹으면 reload 가 옛 batch_* 를 읽어 파일만 새것인
        # "성공했는데 화면은 그대로" 상태가 된다.
        if os.environ.get("DATABASE_URL") and ("db" in steps or "load" in pipeline_steps):
            await _run_script(ROOT / "analysis" / "06_load_db.py", ROOT, "db")

        # 재적재 — 파이프라인이 돌았든 아니든 마지막은 항상 메모리 반영
        JOB["step"] = "reload"
        _log("── 메모리 재적재")
        result = await _reload_data()
        result["needCellsAmBefore"] = before_kpi
        result["paramsVersion"] = ((_ctx["DATA"].get("meta") or {}).get("paramsVersion"))
        if "validate" in pipeline_steps:
            try:
                vj = json.loads((ROOT / "dataset_hwaseong" / "validation.json").read_text("utf-8"))
                usable = vj.get("qualitativeUsable")
                result["validation"] = {"qualitativeUsable": usable,
                                        "cvLogR2": (vj.get("r2") or {}).get("cv_log")}
                if isinstance(usable, (int, float)) and usable < 3:
                    _log(f"⚠ 정성 대조 usable {usable}건 < 목표 3건 — 발표 인용 수치를 재확인하세요")
            except Exception as e:
                _log(f"validation.json 요약 실패(무시): {type(e).__name__}")
        JOB.update(status="done", result=result, finishedAt=_now(), step=None)
        _log(f"완료 — needCells(am) {before_kpi} → {result.get('needCellsAm')}")
        _append_history({"kind": "refresh.done", "jobId": JOB["id"], "ok": True,
                         "steps": steps, "result": result, "actor": actor})
        if upload_meta and backup_path is not None:
            # 되돌리는 명령을 **이력에 문자열 그대로** 남긴다. 무대에서 이게 있는
            # 것과 없는 것이 3분과 30분을 가른다(backup-* 는 5회까지만 보관).
            restore = (f"docker compose exec api sh -c 'cp -f {backup_path}/dataset_hwaseong/* "
                       f"/app/dataset_hwaseong/ && cp -f {backup_path}/server/static/*.json "
                       f"/app/server/static/'")
            _log(f"되돌리려면: {restore}")
            _append_history({"kind": "upload.apply", "jobId": JOB["id"], "ok": True,
                             "uploadId": upload_id, "reason": reason, "actor": actor,
                             "file": upload_meta.get("name"), "rows": upload_meta.get("rows"),
                             "sha256": (upload_meta.get("sha256") or "")[:10],
                             "backup": str(backup_path), "restore": restore})
    except Exception as e:
        JOB.update(status="failed", error=f"{type(e).__name__}: {e}", finishedAt=_now(), step=None)
        _log(f"실패: {JOB['error']}")
        _append_history({"kind": "refresh.done", "jobId": JOB["id"], "ok": False,
                         "steps": steps, "error": JOB["error"], "actor": actor})
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


# ─── API ───────────────────────────────────────────────────────────────────────
def _param_rows() -> list:
    raw = _read_override()
    rows = []
    for key, s in SPECS.items():
        ov = raw.get(key)
        editable = s["scope"] in ("runtime", "pipeline") and not s.get("locked")
        eff = effective(key)
        pending = False
        if s["scope"] == "pipeline":
            live = _pipeline_effective(key)
            if live is not None:
                eff = live
            pending = ov is not None and eff != ov["value"]
        rows.append({
            "key": key, "label": s["label"], "unit": s["unit"], "type": s["type"],
            "min": s.get("min"), "max": s.get("max"), "scope": s["scope"], "group": s["group"],
            "editable": editable, "requiresRefresh": s["scope"] == "pipeline",
            "default": s["default"],
            "override": ov["value"] if ov else None,
            "effective": eff,
            "pending": pending,
            "overridden": ov is not None,
            "reason": ov.get("reason") if ov else None,
            "actor": ov.get("actor") if ov else None,
            "at": ov.get("at") if ov else None,
            "note": s["note"], "applies": s["applies"],
        })
    sim = (_ctx["DATA"] or {}).get("sim")
    for attr, label, note in BASELINE_DISPLAY:
        val = getattr(sim, attr, None) if sim is not None else None
        rows.append({"key": f"baseline.{attr}", "label": label, "unit": "", "type": "float",
                     "min": None, "max": None, "scope": "baseline", "group": "baseline",
                     "editable": False, "requiresRefresh": True, "pending": False,
                     "default": val, "override": None, "effective": val,
                     "overridden": False, "reason": None, "actor": None, "at": None,
                     "note": note, "applies": "기준선(D·S·MI) — 편집은 새 기준선 발행 절차로만"})
    return rows


@router.get("/params")
def get_params():
    raw = _read_override()
    return {"updatedAt": max((v["at"] for v in raw.values()), default=None),
            "overrideCount": len(raw), "params": _param_rows()}


class SaveRequest(BaseModel):
    changes: dict
    reason: str = ""
    actor: str = "admin"


@router.post("/params")
def save_params(req: SaveRequest, _w=Depends(require_write)):
    if JOB["status"] == "running":
        raise HTTPException(409, "데이터 갱신이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
    # 사유는 선택이다(2026-08-26 완화). 이전에는 5자 이상을 강제했는데, 화면이
    # '관리자 콘솔에서 수정'이라는 무의미한 문구로 자동 충족시키는 결과만 낳았다 —
    # 강제가 기록의 질을 만들지 못했다. 적으면 이력의 '왜' 칸에 남는다.
    reason = (req.reason or "").strip()[:200]
    if not req.changes:
        raise HTTPException(400, "changes 가 비어 있습니다.")

    # 전건 검증 통과 시에만 적용 (all-or-nothing — _validate_placements 의 철학)
    unknown = [k for k in req.changes if k not in SPECS]
    if unknown:
        raise HTTPException(400, f"changes 에 알 수 없는 키가 있습니다: {unknown}")
    # 화면에서 감추는 것만으로는 부족하다 — API 는 그대로 열려 있다.
    locked = [k for k in req.changes if SPECS[k].get("locked")]
    if locked:
        raise HTTPException(400, f"읽기 전용 항목이라 저장할 수 없습니다: {locked} — "
                                 "격자 산출물에 구워지는 값이라 파이프라인을 다시 돌려야 바뀝니다.")
    # pipeline 계급은 저장 가능하되 산출물 재계산([지표 재계산]) 전에는 화면에
    # 반영되지 않는다 — 응답 requiresRefresh 로 알린다. baseline 계급은 SPECS 에
    # 없어 아래 unknown 검사에서 자동 거부된다.
    normalized: dict = {}
    for k, v in req.changes.items():
        if v is None:                       # null = 기본값 복귀 (revoke)
            normalized[k] = None
            continue
        s = SPECS[k]
        # 유한성 검사가 형 검사보다 먼저다 — Python json 은 Infinity/NaN 을
        # 받아들이는데, 그대로 저장되면 응답 직렬화(_json 의 allow_nan=False)가
        # 죽어 /meta 를 포함한 전 API 가 500 이 된다. int(inf) 도 OverflowError.
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise HTTPException(400, f"{k} 는 유한한 숫자여야 합니다 (받은 값: {v!r})")
        if s["type"] == "int":
            if int(v) != v:
                raise HTTPException(400, f"{k} 는 정수여야 합니다 (받은 값: {v!r})")
            v = int(v)
        else:
            v = float(v)
        # 범위 제한은 두지 않는다(SPECS 상단 주석). 단 비용 0/음수는 추천의
        # 비용효율(ΔB̂/비용) 나눗셈에서 ZeroDivisionError 로 이어지므로 막는다.
        if k.startswith("cost.") and k.endswith(".krw") and v <= 0:
            raise HTTPException(400, f"{k} 는 0보다 커야 합니다 — 추천 순위가 비용으로 나눕니다 (받은 값: {v:,})")
        normalized[k] = v

    with SAVE_LOCK:
        return _save_locked(req, normalized, reason)


def _save_locked(req: "SaveRequest", normalized: dict, reason: str):
    current = _read_override()
    applied = []
    for k, v in normalized.items():
        old = current[k]["value"] if k in current else None
        old_eff = old if old is not None else SPECS[k]["default"]
        if v is None:
            if k in current:
                del current[k]
                applied.append({"key": k, "old": old_eff, "new": SPECS[k]["default"], "revoked": True})
        else:
            if old_eff != v:
                current[k] = {"value": v, "reason": reason, "actor": req.actor, "at": _now()}
                applied.append({"key": k, "old": old_eff, "new": v})
    if not applied:
        raise HTTPException(400, "바뀐 값이 없습니다.")

    # 이력 먼저, 상태 나중 — 상태 파일이 있으면 이력도 반드시 있다
    _append_history({"kind": "param.set", "actor": req.actor, "reason": reason,
                     "changes": applied})
    _write_override(current)
    apply_runtime_params()
    needs = sorted({a["key"] for a in applied if SPECS[a["key"]]["scope"] == "pipeline"})
    return {"ok": True, "applied": applied, "overrideCount": len(current),
            "requiresRefresh": needs, "params": _param_rows()}


class GridOverrideRequest(BaseModel):
    gridId: str
    period: str
    daytype: str = "wd"
    field: str
    value: object = None
    reason: str = ""
    actor: str = "admin"


class GridOverrideRevokeRequest(BaseModel):
    id: str
    reason: str = ""
    actor: str = "admin"


@router.get("/grid-overrides")
def list_grid_overrides(can_write: bool = Depends(require_read)):
    recs = _read_grid_overrides()
    return {"overrides": list(reversed(recs)),      # 최신이 위
            "liveCount": len(_live_grid_overrides(recs)),
            "fields": {k: dict(kind=v["kind"], allowed=list(v.get("allowed", [])))
                       for k, v in GRID_FIELDS.items()},
            "canWrite": can_write}


@router.post("/grid-overrides")
def save_grid_override(req: GridOverrideRequest, _w=Depends(require_write)):
    # 사유는 선택 — save_params 와 같은 완화(같은 자리 주석 참고). "왜 모델 값을
    # 사람이 고쳤는가"는 여전히 심사 단골 질문이라 화면이 입력을 권하지만,
    # 빈 사유로도 막지는 않는다.
    reason = (req.reason or "").strip()[:200]
    if req.field not in GRID_FIELDS:
        raise HTTPException(400, f"field 는 {list(GRID_FIELDS)} 중 하나여야 합니다 "
                                 f"(받은 값: {req.field!r}). mi·demand·supply 는 색 bin· "
                                 "산점도와 얽혀 1단계에서 열지 않았습니다.")
    if req.period not in (_ctx["PERIODS"] or []):
        raise HTTPException(400, f"period 는 {_ctx['PERIODS']} 중 하나여야 합니다.")
    if req.daytype not in ("wd", "we"):
        raise HTTPException(400, "daytype 은 wd 또는 we 여야 합니다.")
    payload = _grid_payload(req.period, req.daytype)
    if payload is None:
        raise HTTPException(404, f"grid_{req.period}{'_we' if req.daytype == 'we' else ''} "
                                 "데이터가 없습니다.")
    cell = _find_cell(payload, req.gridId)
    if cell is None:
        raise HTTPException(400, f"gridId 를 찾을 수 없습니다: {req.gridId!r}")

    spec = GRID_FIELDS[req.field]
    v = req.value
    if spec["kind"] == "text":
        if v not in spec["allowed"]:
            raise HTTPException(400, f"{req.field} 값은 {list(spec['allowed'])} 중 하나여야 "
                                     f"합니다 (받은 값: {v!r})")
    else:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise HTTPException(400, f"{req.field} 값은 유한한 숫자여야 합니다 (받은 값: {v!r})")
        v = float(v)
        if v < 0:
            raise HTTPException(400, f"{req.field} 값은 0 이상이어야 합니다 — 음수 점수는 "
                                     "정렬을 뒤집는 게 아니라 뜻이 없습니다.")

    with SAVE_LOCK:
        recs = _read_grid_overrides()
        # 같은 (격자·시간대·요일·필드)에 살아 있는 오버라이드는 하나뿐이어야 한다
        # (schema_ops.sql 의 유니크 인덱스와 같은 규칙). 새 저장은 옛것을 자동
        # 취소하고 **옛것의 prev(진짜 원값)** 를 물려받는다 — 지금 셀 값은 이미
        # 오버라이드된 값이라 그걸 prev 로 삼으면 되돌리기가 원값으로 못 돌아간다.
        old = next((r for r in _live_grid_overrides(recs)
                    if r["gridId"] == req.gridId and r["period"] == req.period
                    and r["daytype"] == req.daytype and r["field"] == req.field), None)
        prev = old["prev"] if old else cell.get(req.field)
        if old:
            old["revokedAt"] = _now()
            old["revokeReason"] = "새 값으로 교체"
        rec = {"id": f"g-{secrets.token_urlsafe(6)}", "gridId": req.gridId,
               "period": req.period, "daytype": req.daytype, "field": req.field,
               "value": v, "prev": prev, "reason": reason, "actor": req.actor,
               "at": _now(), "revokedAt": None, "revokeReason": None}
        recs.append(rec)
        _append_history({"kind": "grid.set", "actor": req.actor, "reason": reason,
                         "changes": [{"key": f"{req.gridId}·{req.period}·{req.daytype}·{req.field}",
                                      "old": prev, "new": v}]})
        _write_grid_overrides(recs)
        _set_cell_field(cell, req.field, v)
        cell["overridden"] = True
        _recount_kpi(payload)
    return {"ok": True, "override": rec, "kpi": payload["kpi"]}


@router.post("/grid-overrides/revoke")
def revoke_grid_override(req: GridOverrideRevokeRequest, _w=Depends(require_write)):
    with SAVE_LOCK:
        recs = _read_grid_overrides()
        rec = next((r for r in recs if r["id"] == req.id), None)
        if rec is None:
            raise HTTPException(404, f"오버라이드를 찾을 수 없습니다: {req.id!r}")
        if rec.get("revokedAt"):
            raise HTTPException(400, "이미 취소된 오버라이드입니다.")
        rec["revokedAt"] = _now()
        rec["revokeReason"] = (req.reason or "").strip() or "관리자 취소"
        payload = _grid_payload(rec["period"], rec["daytype"])
        cell = _find_cell(payload, rec["gridId"]) if payload else None
        if cell is not None:
            # 현재 값이 오버라이드 값일 때만 원값을 되살린다 — 그사이 재계산으로
            # 셀이 새로 태어났다면(값이 이미 다르면) 덮어쓸 이유가 없다.
            if cell.get(rec["field"]) == rec["value"]:
                _set_cell_field(cell, rec["field"], rec["prev"])
            _refresh_cell_override_flag(cell, recs, rec["period"], rec["daytype"])
            _recount_kpi(payload)
        _append_history({"kind": "grid.revoke", "actor": req.actor,
                         "reason": rec["revokeReason"],
                         "changes": [{"key": f"{rec['gridId']}·{rec['period']}·{rec['daytype']}·{rec['field']}",
                                      "old": rec["value"], "new": rec["prev"]}]})
        _write_grid_overrides(recs)
    return {"ok": True, "override": rec,
            "kpi": payload["kpi"] if payload else None}


class RefreshRequest(BaseModel):
    # extra="allow" — 모르는 필드를 보존한다. 기본값(ignore)이면 dryRun 같은 오해
    # 필드가 조용히 사라져 "무시됐다"는 인상만 남는다. 받아 두고 명시적으로 막는다.
    model_config = {"extra": "allow"}

    steps: list = ["reload"]
    reason: str = ""
    actor: str = "admin"
    # 아래 둘은 업로드 경로 전용이다. uploadId 가 없으면 이 엔드포인트는
    # **종전과 100% 동일하게** 동작해야 한다 — 배포된 프론트는 {steps, reason,
    # actor} 만 보내므로, apply 기본값을 그대로 분기에 쓰면 [모델 재계산]이
    # 결과를 버리고 [화면 반영]이 무동작이 된다(완료 토스트는 그대로 떠서
    # 실패로 보이지도 않는다).
    uploadId: Optional[str] = None
    apply: bool = False


@router.post("/refresh")
async def start_refresh(req: RefreshRequest, _w=Depends(require_write)):
    if JOB["status"] == "running":
        raise HTTPException(409, "이미 갱신이 진행 중입니다.")

    steps = list(req.steps)
    do_apply = True                      # uploadId 없는 기존 경로는 항상 실제 반영
    if req.uploadId:
        if not upload_enabled():
            raise HTTPException(404, "업로드가 켜져 있지 않습니다.")
        if not UPLOAD_ID_RE.match(req.uploadId):
            raise HTTPException(400, "uploadId 형식이 올바르지 않습니다.")
        updir = VAR / req.uploadId
        if updir.parent != VAR or not (updir / "meta.json").exists():
            raise HTTPException(400, "업로드를 찾을 수 없습니다 — 다시 올려 주세요.")
        do_apply = bool(req.apply)
        if do_apply and not upload_apply_enabled():
            raise HTTPException(409, "이 서버는 검증(예행)까지만 열려 있습니다 — "
                                     "라이브 반영은 따로 켜야 합니다.")
        # 단계는 **서버가 정한다.** 클라이언트가 일부만 보내면 옛 세대끼리 정합해
        # 게이트를 통과한 뒤 라이브가 '새 원본 + 옛 지표' 자기모순이 되는데,
        # 그래도 서버는 정상 기동해 아무도 눈치채지 못한다.
        steps = (["join", "model", "validate", "load", "reload"] if do_apply
                 else ["join", "model", "validate"])

    valid = {"join", "model", "validate", "load", "db", "reload"}
    bad = [s for s in steps if s not in valid]
    if bad:
        raise HTTPException(400, f"steps 에 알 수 없는 단계가 있습니다: {bad} (허용: {sorted(valid)})")
    if not steps:
        raise HTTPException(400, "steps 가 비어 있습니다.")

    # ── 단계 의존성은 **서버가 보정한다** (2026-08-27) ────────────────────────
    # 업로드 경로에는 이 방어가 있었는데(위 주석) 일반 경로는 클라이언트가 보낸
    # 그대로 돌렸다. 그래서 `model` 만 돌리고 `load` 를 빼면 이런 일이 났다:
    #   04_model 이 grid_metrics.csv 를 새 상수로 다시 굽는다 → 그런데
    #   server/static/*.json 은 옛 세대 그대로 → reload 가 그 옛 JSON 을 읽는다
    #   → **저장·이력·잡은 전부 성공인데 화면 수치만 안 바뀐다.**
    # 독립 검증에서 baseline.eldCoef 를 1.6→5.0 으로 올리고도 우선순위가 소수점
    # 4자리까지 동일했던 것이 이 경로였다. db 단계도 static JSON 을 적재하므로
    # load 없이 돌리면 옛 세대를 DB 에 넣어 기준일이 되레 후퇴한다(같은 검증의
    # '주의 2'). 어느 쪽도 사람이 눈치채기 어려우니 조합 자체를 막는다.
    auto = []
    # 업로드 경로는 위에서 서버가 이미 스텝을 정했다 — 특히 **예행(dry-run)은
    # load 를 일부러 뺀다**(계약 JSON 을 만들지 않아야 라이브가 무변경이다).
    # 여기서 보정하면 그 설계를 되레 깨뜨린다.
    if not req.uploadId:
        if ("join" in steps or "model" in steps or "db" in steps) and "load" not in steps:
            steps.append("load")
            auto.append("load")
        # 실행 순서도 서버가 정한다 — 뒤섞어 보내도 파이프라인 순서는 하나다.
        order = ["join", "model", "validate", "load", "db", "reload"]
        steps = [x for x in order if x in set(steps)]

    # dryRun 은 업로드 검증 전용이다. 일반 재계산에 얹어 보내면 예행인 줄 알고
    # 눌렀는데 라이브가 도는 형태가 된다 — 조용히 무시하지 말고 막는다.
    extra = getattr(req, "model_extra", None) or {}
    if extra.get("dryRun") and not req.uploadId:
        raise HTTPException(400, "dryRun 은 업로드 검증(uploadId + apply:false) 전용입니다 — "
                                 "일반 재계산에는 예행이 없습니다. 라이브가 그대로 돕니다.")
    if "db" in steps and not os.environ.get("DATABASE_URL"):
        raise HTTPException(400, "DATABASE_URL 이 없어 db 단계를 실행할 수 없습니다.")

    JOB.update(id=f"RF-{int(time.time())}", status="running", steps=steps,
               step="queued", startedAt=_now(), finishedAt=None, error=None, result=None)
    JOB["log"].clear()
    _append_history({"kind": "refresh.start", "jobId": JOB["id"], "steps": steps,
                     "reason": req.reason, "actor": req.actor,
                     "uploadId": req.uploadId, "apply": do_apply,
                     "autoAdded": auto or None})
    asyncio.get_running_loop().create_task(
        _run_refresh(steps, req.reason, req.actor, upload_id=req.uploadId, apply=do_apply))
    return {"ok": True, "jobId": JOB["id"], "steps": steps, "dryRun": not do_apply,
            # 서버가 채워 넣은 단계 — 부른 쪽이 "내가 안 보낸 게 돌았다"를 알 수 있게.
            "autoAddedSteps": auto}


class UploadRequest(BaseModel):
    datasetId: str
    filename: str = ""
    contentB64: str
    reason: str = ""
    actor: str = "admin"


TEMPLATE_SAMPLE_ROWS = 3


@router.get("/upload/template")
def upload_template(datasetId: str = Query(..., description="DATASETS 의 키")):
    """예시 형식 CSV — 헤더 한 줄 + 라이브 파일 앞 3행.

    **헤더 정본을 서버가 준다.** 프론트가 컬럼 이름을 따로 들고 있으면 두 값이
    갈리는데, 그때 나는 증상이 하필 "예시대로 만들었는데 400 컬럼이 다릅니다"라
    사용자가 원인을 찾을 방법이 없다. 이 저장소가 반복해 겪은 '같은 개념이 두 값을
    갖는' 사고를 여기서는 애초에 안 만든다.

    **BOM(utf-8-sig)을 붙인다.** 이 파일의 가장 흔한 사용 경로가 "내려받아 엑셀로
    열고 고쳐서 다시 올리기"인데, BOM 이 없으면 엑셀이 cp949 로 열어 한글이 깨진
    채로 편집된다. _decode_upload 가 접수 시점에 cp949 를 받아 주기는 하지만,
    깨진 글자를 되살려 주지는 못한다 — 깨지지 않게 하는 편이 낫다.

    표본 3행을 싣는 이유: 실제로 반려를 만드는 것은 컬럼 이름보다 **값의 생김새**
    (날짜가 20251201 인지 2025-12-01 인지, 코드에 접두어가 붙는지)다. 다만 라이브
    파일의 헤더가 계약과 다르면 표본을 싣지 않는다 — 틀린 예시를 주느니 없는 게 낫다.
    """
    if not upload_enabled():
        raise HTTPException(404, "업로드가 켜져 있지 않습니다.")
    spec = DATASETS.get(datasetId)
    if spec is None:
        raise HTTPException(400, f"올릴 수 없는 대상입니다 (허용: {', '.join(DATASETS)})")

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")   # 엑셀이 기대하는 줄바꿈
    w.writerow(spec["header"])

    src = ROOT / "dataset_hwaseong" / spec["name"]
    try:
        # 승하차 원본은 30만 행·19MB 다. 앞 몇 줄만 읽고 빠져나온다.
        with open(src, encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader(fh)
            head = next(rd, None)
            if head and [c.strip() for c in head] == spec["header"]:
                for i, row in enumerate(rd):
                    if i >= TEMPLATE_SAMPLE_ROWS:
                        break
                    w.writerow(row)
    except OSError:
        pass    # 표본 없이 헤더만 — 그래도 계약은 전달된다

    fname = spec["name"][:-4] if spec["name"].endswith(".csv") else spec["name"]
    fname += "_예시양식.csv"
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        # 한글 파일명은 RFC 5987 로만 안전하게 나간다. filename= 은 옛 클라이언트용 폴백.
        headers={"Content-Disposition":
                 "attachment; filename=upload_template.csv; "
                 "filename*=UTF-8''" + quote(fname)},
    )


@router.post("/upload")
async def upload_dataset(req: UploadRequest, _w=Depends(require_write)):
    """원본 CSV 접수 — 받아서 검증하고 격리 보관한 뒤 리포트만 돌려준다.

    **이 단계에서 라이브 데이터는 한 바이트도 바뀌지 않는다.** 실제 반영은
    /refresh 가 uploadId 를 들고 왔을 때만, 그것도 예행이 기본이다.
    """
    if not upload_enabled():
        raise HTTPException(404, "업로드가 켜져 있지 않습니다.")
    if JOB["status"] == "running":
        raise HTTPException(409, "데이터 갱신이 진행 중입니다 — 끝난 뒤 올려 주세요.")

    spec = DATASETS.get(req.datasetId)
    if spec is None:
        raise HTTPException(400, f"올릴 수 없는 대상입니다 (허용: {', '.join(DATASETS)})")
    if len(req.reason.strip()) < 5:
        raise HTTPException(400, "왜 올리는지 5자 이상 적어 주세요. 이력에 남습니다.")
    # base64 는 원본의 약 4/3 배. 디코드 전에 먼저 자른다.
    if len(req.contentB64) > (spec["maxBytes"] * 4 // 3) + 4096:
        raise HTTPException(413, f"파일이 너무 큽니다 — 최대 {spec['maxBytes'] // 1048576}MB 까지입니다.")

    # 연타 방지 — **첫 await 이전에** 동기적으로 갱신한다. await 사이에 창이 열리면
    # 동시 요청이 둘 다 통과한다(save_params 가 같은 이유로 같은 형태를 쓴다).
    now = time.monotonic()
    if now - _LAST_UPLOAD_AT["t"] < 10:
        raise HTTPException(429, "잠시 후 다시 시도해 주세요.")
    _LAST_UPLOAD_AT["t"] = now

    async with UPLOAD_LOCK:
        info = await asyncio.to_thread(_accept_upload, req, spec)
    _append_history({"kind": "upload.accept", "uploadId": info["uploadId"],
                     "datasetId": req.datasetId, "file": info["name"],
                     "rows": info["rows"], "bytes": info["bytes"],
                     "sha256": info["sha256"][:10], "reason": info["reason"],
                     "actor": req.actor})
    return {"ok": True, **info}


@router.get("/status")
def get_status():
    DATA = _ctx["DATA"] or {}
    raw = _read_override()
    meta = DATA.get("meta") or {}
    try:
        ADMIN_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(ADMIN_DIR, os.W_OK)
    except OSError:
        writable = False
    return {
        "data": {"source": DATA.get("_source"), "loadedAt": DATA.get("_loadedAt"),
                 "metaUpdatedAt": meta.get("updatedAt"),
                 "gridCells": len((DATA.get("cells") or {}).get("am", {})),
                 "kpiAm": (DATA.get("grid_am") or {}).get("kpi")},
        "overrides": {"count": len(raw), "keys": sorted(raw),
                      "updatedAt": max((v["at"] for v in raw.values()), default=None)},
        "job": {"id": JOB["id"], "status": JOB["status"], "steps": JOB["steps"],
                "step": JOB["step"], "startedAt": JOB["startedAt"],
                "finishedAt": JOB["finishedAt"], "error": JOB["error"],
                "result": JOB["result"], "logTail": list(JOB["log"])[-40:]},
        "env": {"dbConfigured": bool(os.environ.get("DATABASE_URL")),
                "varDirWritable": writable,
                "authRequired": auth_required(),
                "writeLocked": auth_required()},
        # 새 GET 을 만들지 않는다 — 상태는 이미 화면이 읽고 있어 배선이 공짜다.
        "upload": {
            "enabled": upload_enabled(),
            "applyEnabled": upload_apply_enabled(),
            "targets": _upload_targets() if upload_enabled() else [],
            "last": _last_upload(),
        },
        # 지금은 화면에도 로그에도 없어서, 디스크가 찼다는 사실이 교체 도중에야
        # 드러난다. 시연 전에 눈으로 확인할 수 있게 함께 싣는다.
        "disk": _disk_info(),
    }


def _upload_targets() -> list:
    out = []
    for k, v in DATASETS.items():
        try:
            live = _live_rows(v)
        except OSError:
            live = None
        out.append({"id": k, "label": v["label"], "name": v["name"],
                    "columns": len(v["header"]), "minRows": v["minRows"],
                    "maxBytes": v["maxBytes"], "note": v.get("note", ""),
                    "liveRows": live})
    return out


def _last_upload() -> Optional[dict]:
    dirs = sorted(VAR.glob("upload-*"))
    for d in reversed(dirs):
        try:
            m = json.loads((d / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        return {"uploadId": m.get("uploadId"), "label": m.get("label"),
                "file": m.get("originalFilename") or m.get("name"),
                "rows": m.get("rows"), "at": m.get("at"),
                "sha256": (m.get("sha256") or "")[:10]}
    return None


def _disk_info() -> dict:
    try:
        du = shutil.disk_usage(ROOT)
        var_used = sum(f.stat().st_size for f in VAR.rglob("*") if f.is_file())
        return {"freeMb": du.free // 1048576, "totalMb": du.total // 1048576,
                "varUsedMb": var_used // 1048576}
    except OSError:
        return {}


@router.get("/history")
def get_history(limit: int = Query(50, ge=1, le=500), kind: Optional[str] = None,
                can_write: bool = Depends(require_read)):
    items = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if kind and ev.get("kind", "").split(".")[0] != kind:
                    continue
                # 인증 실패 기록에는 접속 IP 가 들어간다 — 쓰기 권한이 있는
                # 운영자에게만 보인다(누가 두드렸는지는 운영 정보다).
                if ev.get("kind") == "auth.fail" and not can_write:
                    continue
                items.append(ev)
    except FileNotFoundError:
        pass
    return {"total": len(items), "items": items[-limit:][::-1]}
