# 테스트

```bash
pip install -r requirements.txt
pip install pytest httpx playwright && playwright install chromium

python -m pytest tests/ -q -m "not slow"      # 166개 (E2E 포함, 약 110초)
python -m pytest tests/test_pipeline.py -q    # ① 파이프라인 단위 (0.4초)
python -m pytest tests/test_api.py -q         # ② API 통합 (14초)
python -m pytest tests/test_e2e.py -q -m e2e  # ③ 브라우저 E2E (98초)
python -m pytest tests/ -q -m slow            # 산출물 재현성 (04_model 재실행)
```

## 계층

| 계층 | 파일 | 개수 | 무엇을 지키나 |
|---|---|---:|---|
| ① 파이프라인 단위 | `test_pipeline.py` | 50 | 수식·컷·격자 공간조인·norm_stats 계약·산출물 교체 목록 |
| ② API 통합 | `test_api.py` | 99 | 10개 엔드포인트 계약·불변식·입력검증·응답시간·관리자 |
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

## 실패를 결함 목록으로 쓰는 방식

이 스위트는 **테스트를 먼저 쓰고 그 실패 목록을 결함 목록으로 삼아** 만들어졌다.
도입 시점에 9건이 빨간불이었고 — `limit=-1` 이 슬라이스로 새던 것, 배치 배열에
상한이 없던 것, 음수 예산 수용, 예산 초과 표시 누락, 임의 모델 문자열이 SDK 로
그대로 가던 것, context 프롬프트 무제한 증폭, '해소 통행량'이 한 응답에서 세 값,
증편이 표현 방식에 따라 다른 결과, 문서와 API 의 정류장 수 불일치 — **지금은
전부 통과한다.** 앞의 6건은 `aa114f88` 이, 뒤의 3건은 `f192b3e` 가 고쳤다.

이 방식은 계속 쓴다. 새 결함을 찾으면 **고치기 전에 테스트부터 빨간불로 만들고**,
고친 뒤 초록불이 되는지 확인한다. 통과하는 테스트는 아무것도 증명하지 않는다 —
결함을 넣었을 때 실패해야 그물이다.

### 지금 알려진 한계

- 이 컨테이너에서는 `test_recommendation_under_500ms` 12건이 700~900ms 로 실패한다.
  러너 성능 문제이고 CI(GitHub Actions)에서는 통과한다.
- `test_pipeline.py` 의 수식 테스트 일부는 `04_model.py` 의 함수를 부르지 않고
  수식을 테스트 파일 안에 다시 적어 둔다. 실제로 검증되는 것은 `params.py` 의
  상수값이라, 모델 수식 자체를 고치면 이 계층이 못 잡는다.
- `-m slow` 재현성 테스트는 `grid_metrics.csv` 만 되돌린다. 돌린 뒤
  `git status` 에 `norm_stats.json` 이 남으면 되돌리고 커밋할 것.
