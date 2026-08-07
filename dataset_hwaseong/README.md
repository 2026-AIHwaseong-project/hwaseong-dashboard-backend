# 화성시 데이터셋

전국 원본에서 **화성시분만 추출**한 경량 데이터입니다. 전부 무료 공공데이터이며
이용허락범위 제한이 없습니다. 재생성: `python analysis/00_extract_hwaseong.py`

| 파일 | 행수 | 용도 | 원본 |
|---|---|---|---|
| `boarding_hwaseong.csv` | 301,455 | ★★ **실현수요 B** · 모델 검증 | [경기데이터드림 정류소별 승하차](https://data.gg.go.kr/portal/data/service/selectServicePage.do?infId=MZCREO5CKHZM6PJEA55P37391662&infSeq=1) |
| `flow_hourly.csv` | 2,976 | ★★ **시간배율 · 총량보정 · 외국인** | [15135464](https://www.data.go.kr/data/15135464/fileData.do) |
| `stops_national_hwaseong.csv` | 3,158 | ★★ **정류장 좌표 (주 소스)** | [15067528](https://www.data.go.kr/data/15067528/fileData.do) |
| `stops_gg.csv` | 2,632 | 정류장 보조 (2018년 스냅샷) | [경기데이터드림](https://data.gg.go.kr/portal/data/service/selectServicePage.do?infId=GDKWAGWYRKJYIRVX110226832213&infSeq=1) |
| `industrial_complex.csv` | 1,735 | ★ 산단 출근수요 | [15126632](https://www.data.go.kr/data/15126632/fileData.do) |
| `manufacturing.csv` | 25,624 | 산단 외 제조업체 | [15112420](https://www.data.go.kr/data/15112420/fileData.do) |
| `apartments.csv` | 476 | 주거 밀도 가중 | [15117209](https://www.data.go.kr/data/15117209/fileData.do) |
| `population_dong.csv` | 29 | 읍면동 인구 (격자 안분 검증) | [15118049](https://www.data.go.kr/data/15118049/fileData.do) |
| `routes_village.csv` | 155 | 마을버스 (배차·인가대수 포함) | [15052048](https://www.data.go.kr/data/15052048/fileData.do) |
| `rail_stations.csv` | 5 | 철도역 (통행 유인) | [15013205](https://www.data.go.kr/data/15013205/standard.do) |

**총 24.8 MB** · 인코딩 `utf-8-sig` (원본은 대부분 cp949였음)

### 파이프라인 산출물 (원본 아님)

| 파일 | 행수 | 생성 | 내용 |
|---|---|---|---|
| `hwaseong_dong.geojson` | 29 | 프론트 `tools/build-boundary.py` | 읍면동 실경계 (4읍 9면 16동) · EPSG:4326 · 25m 단순화 |
| `grid_hwaseong.csv` | **786** | `python analysis/02_grid.py` | ★ **격자 뼈대.** 좌표·읍면동·인구·고령·가구·주택·종사자 |

`grid_hwaseong.csv` 컬럼:

```
grid_id  lon lat  x_5179 y_5179  region_code region region_kind
pop  elderly  elderly_ratio  households  houses  workers
```

**검증 결과 (실제 화성시와 대조)**

| 항목 | 산출 | 실제 | |
|---|---|---|---|
| 총인구 | 1,026,706명 | 약 100만 | ✅ |
| 총가구 | 396,589가구 | 약 40만 | ✅ |
| 고령비 | 10.7% | 약 11% (전국 평균보다 낮은 젊은 도시) | ✅ |
| 종사자 1위 격자 | 반월동 59,383명 (37.229, 127.066) | **삼성전자 화성캠퍼스** | ✅ |
| 종사자 3위 격자 | 남양읍 15,749명 · 인구 198명 | **현대차 남양연구소** | ✅ |

종사자만 몰리고 인구는 없는 격자가 실제 대형 사업장 위치로 떨어졌습니다.
**공간조인이 맞다는 가장 강한 증거입니다.**

---

## 별도로 받아야 하는 것

### SGIS 격자 통계 및 경계 (전국 279MB)

격자 뼈대와 인구·가구·주택·**사업체/종사자**가 들어있습니다. 전국 데이터인데
격자코드로 화성시를 거를 수 없어(코드집에 시도 단위 매핑만 존재) 공간조인이
필요하므로 추출본을 만들지 못했습니다.

✅ **`02_grid.py` 가 처리 완료.** 위 `grid_hwaseong.csv` 가 그 산출물입니다.
격자만 필요하면 이 원본을 안 받아도 됩니다. **재생성할 때만 필요합니다.**

[공공데이터포털 15141768](https://www.data.go.kr/data/15141768/fileData.do) 에서
받아 `dataset/` 에 압축 해제하세요.

```
국가데이터처_SGIS 격자 통계 및 경계/
├── 1. 통계/  (인구 · 가구 · 주택 · 사업체·종사자)   long format
├── 2. 경계/  grid_XX_1K.shp                       ← 공간조인 대상
└── 3. 코드집/ statistics_code.xlsx                ← 통계항목 코드 해설
```

### API 수집분

`analysis/01_fetch.py` 가 받아옵니다. `.env` 에 키 필요.
TAGO 정류소·노선, 경기도 배차간격, 똑버스 경유정류소.

---

## 받았지만 안 쓰는 것

### 화성시 공간정보시스템 데이터 ([15118266](https://www.data.go.kr/data/15118266/fileData.do)) ❌

기획서 §6.2에 **"행정경계(격자 클리핑)"** 용도로 적어 받았는데, **열어보니 못 씁니다.**

CSV 13개가 전부 **속성 테이블뿐이고 지오메트리가 없습니다.** `도형번호` 컬럼으로
별도 도형 파일을 참조하는 구조인데 그 도형 파일이 배포본에 없습니다. shp·geojson 어떤
형식도 들어있지 않습니다.

| 파일 | 행 | 왜 못 쓰나 |
|---|---|---|
| `BML_ADMB_AS` 행정경계 | 29 | **좌표 없음.** 행정구역명·코드만. 받은 목적이 여기였는데 실패 |
| `RDL_STAT_PS` 정류장 | 995 | 좌표 없음 · `정류장명` 전부 공백 → 우리 정류소 3,158개와 조인 불가 |
| `RDL_CCTV/STLT/TFSN` 등 | 44,069~375 | CCTV·가로등·교통표지. 모델과 무관 |
| `화성시 VDS 자료.xlsx` | 707 | 좌표는 있음(EPSG:5186). 단 **검지기 위치만이고 교통량 값이 없음** |

→ **행정경계는 SGIS 읍면동(`hwaseong_dong.geojson`)으로 해결했습니다.**
→ 이 폴더는 다시 받을 필요 없습니다.

---

## ⚠️ 알려진 제약 (설계에 직접 영향)

### 1. 승하차에 시간대가 없습니다 🔴

```
승하차일자 | 관할관청 | 정류소ID | 정류소번호 | 정류소명 | 승차합계 | 초승 | 환승 | 하차
```

**일자별 집계**입니다. 프론트 `GET /stops/{id}/profile` 이 요구하는 24시간
프로파일을 실측으로 만들 수 없습니다.

→ `flow_hourly.csv` 의 시간배율로 일 총량을 안분하고, 응답에 `isEstimated: true` 를 넣습니다.

### 2. 정류소 ID 체계가 두 가지

| 매칭 방식 | 매칭률 |
|---|---|
| `stops_gg.csv` 정류소id ↔ 승하차 정류소ID | 79.2% |
| **`stops_national_hwaseong.csv` 모바일단축번호 ↔ 승하차 정류소번호** | **99.5%** ✅ |

**ARS번호(모바일단축번호)로 조인하세요.**

```python
board.merge(stops_nat[["모바일단축번호","위도","경도"]],
            left_on="정류소번호", right_on="모바일단축번호", how="left")
```

### 3. 유동인구는 동절기 60일치

2023-12-01 ~ 2024-01-29. 승하차(2025-12~2026-03)와 **2년 시차**가 있습니다.
치명적이진 않으나 검증 파트에서 지적될 수 있어 발표 시 명시가 필요합니다.

### 4. 동탄역이 원본에 없습니다

SRT는 운영사가 SR이라 코레일 데이터에 없고, GTX-A는 도시철도 표준데이터에
미반영입니다. `rail_stations.csv` 에는 TAGO 정류장 실측 좌표
(37.1997, 127.0962)로 보강해 넣었습니다.

### 5. SGIS 격자는 1km입니다

공공데이터포털 배포판은 `_1K` 만 제공합니다(500m는 SGIS 포털 별도 신청).
화성시 격자는 **786개**입니다(`02_grid.py` 실측). 프론트 목 데이터의 250m·353개 가정과 다릅니다.

읍면동 경계는 프론트 저장소 `tools/build-boundary.py` 가 SGIS 읍면동 SHP
(`bnd_dong_00_2025_2Q`)에서 화성시 29개(4읍 9면 16동)를 뽑아 EPSG:4326 으로
변환해 둔 것이 있으니 재사용하세요. SGIS 시군구 코드는 `31240` 입니다
(행정표준코드 `41590` 과 다릅니다).
