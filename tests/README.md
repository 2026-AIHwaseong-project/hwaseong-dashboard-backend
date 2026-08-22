# 테스트

```bash
pip install -r requirements.txt
pip install pytest httpx playwright && playwright install chromium

python -m pytest tests/ -q -m "not slow"      # 152개 (E2E 포함, 약 110초)
python -m pytest tests/test_pipeline.py -q    # ① 파이프라인 단위 (0.4초)
python -m pytest tests/test_api.py -q         # ② API 통합 (14초)
python -m pytest tests/test_e2e.py -q -m e2e  # ③ 브라우저 E2E (98초)
python -m pytest tests/ -q -m slow            # 산출물 재현성 (04_model 재실행)
```

## 계층

| 계층 | 파일 | 개수 | 무엇을 지키나 |
|---|---|---:|---|
| ① 파이프라인 단위 | `test_pipeline.py` | 47 | 수식·컷·격자 공간조인·norm_stats 계약 |
| ② API 통합 | `test_api.py` | 88 | 10개 엔드포인트 계약·불변식·입력검증·응답시간 |
| ③ 브라우저 E2E | `test_e2e.py` | 18 | 실제로 그려지고 클릭이 먹는가 |

`07_validate.py` 와의 차이 — 그쪽은 실데이터 전체로 **회귀 성능을 재는** 검증기이고,
①은 픽스처로 **수식 자체를 확인하는** 단위 테스트다. 수식이 틀려도 R² 는 그럴듯하게
나올 수 있으므로 둘 다 필요하다.

## ③ 실행 전제

프론트 저장소가 백엔드와 **같은 부모 폴더**에 있어야 한다. 백엔드가 `/app/` 으로
직접 서빙하므로 같은 원점이 되고 CORS 설정이 필요 없다.

```
어느폴더/
├── hwaseong-dashboard-backend/   ← pytest 실행
└── hwaseong-dashboard/           ← 자동으로 /app/ 에 붙음
```

없으면 E2E 는 skip 된다. 서버는 `conftest.py` 의 `live_server` 픽스처가 빈 포트를
잡아 자동으로 띄우고 끈다.

외부 CDN(카카오맵 SDK·Pretendard)은 오프라인 CI 에서 실패하지만 SVG 지도는 정상
렌더링된다. 그래서 테스트는 **같은 원점 요청 실패와 자체 JS 예외만** 잡는다.

## 현재 실패하는 9개

전부 `test_api.py` 의 D(입력검증)·E(의미 일관성) 구간이다. 실패가 곧 미해결 결함
목록이므로 **지우지 말고 고쳐서 통과시킬 것.**

| 테스트 | 결함 |
|---|---|
| `test_priorities_negative_limit_rejected` | `limit=-1` 이 슬라이스로 새어 101건 반환 |
| `test_simulation_placement_list_capped` | placements 배열 길이 상한 없음 |
| `test_simulation_rejects_negative_budget` | 음수 예산 수용 |
| `test_simulation_flags_over_budget` | 예산 1억에 36억 배치해도 초과 표시 없음 |
| `test_report_model_allowlist` | 임의 모델 문자열이 SDK 로 그대로 전달 |
| `test_report_context_size_capped` | context 가 프롬프트에 무제한 증폭 (1MB 확인) |
| `test_resolved_trips_one_meaning` | 같은 응답 안에서 '해소 통행량'이 3가지 값 |
| `test_freq_placement_is_associative` | `count:2` 와 `1건+1건` 의 결과가 18.8% 다름 |
| `test_documented_stop_count_matches_api` | 문서 3,158개 vs API 2,866개 |
