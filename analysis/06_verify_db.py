# -*- coding: utf-8 -*-
"""
06_verify_db.py — DB 모드와 JSON 모드가 같은 것을 내놓는지 확인합니다.

    python analysis/06_verify_db.py

server/db.py 의 유일한 계약은 "계약 JSON 과 같은 것을 돌려준다" 입니다. 같은지를
눈으로 볼 방법이 없으면 그 계약은 없는 것과 같아서, 여기서 **직렬화 바이트까지**
맞춰 봅니다(server/main.py 의 _json 과 같은 옵션으로 덤프).

바이트까지 보는 이유 — 값이 같아도 배열 순서나 키 순서가 다르면 /api/v1/priorities
의 동점 구간 순위가 바뀝니다. 값만 비교하면 그게 안 잡힙니다.

종료 코드 = 다른 항목 수(0이면 통과).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not os.environ.get("DATABASE_URL"):
    sys.exit("DATABASE_URL 이 없습니다. 예:\n"
             "  DATABASE_URL=postgresql://hw:hw_pass@localhost:5432/hwaseong "
             "python analysis/06_verify_db.py")

import server.main as M   # noqa: E402 — sys.path 를 먼저 손봐야 합니다
from server import db     # noqa: E402


def dump(o) -> str:
    """server/main.py 의 _json 과 같은 옵션. 여기서 갈리면 비교가 무의미합니다."""
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                      default=lambda x: x.item() if hasattr(x, "item") else str(x))


def first_diff(a: str, b: str) -> str:
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lo = max(0, i - 60)
    return (f"    첫 차이 {i}번째 글자\n"
            f"      JSON: …{a[lo:i + 60]}…\n"
            f"      DB  : …{b[lo:i + 60]}…")


def main() -> int:
    from_json = M._load_json()
    from_db = db.load_all(M.QUAD_LABEL, M.ACTION_LABEL)
    if from_db is None:
        sys.exit("DB 에서 못 읽었습니다 — 위 경고를 보세요. "
                 "적재가 안 됐다면: python analysis/06_load_db.py")

    if set(from_json) != set(from_db):
        print(f"FAIL: 최상위 키가 다릅니다 "
              f"(JSON 만: {set(from_json) - set(from_db)}, DB 만: {set(from_db) - set(from_json)})")
        return 1

    fails = 0
    for key in sorted(from_json):
        a, b = dump(from_json[key]), dump(from_db[key])
        if a == b:
            print(f"PASS: {key:12s} {len(a):>9,} 바이트 일치")
        else:
            fails += 1
            print(f"FAIL: {key:12s} JSON {len(a):,} 바이트 vs DB {len(b):,} 바이트")
            print(first_diff(a, b))

    # 격자 배열 순서는 별도로 짚습니다. 위에서 이미 잡히지만, 깨졌을 때 무엇이
    # 문제인지(순서인지 값인지) 한 줄로 알려 주는 편이 낫습니다.
    for p in db.PERIODS:
        ja = [c["id"] for c in from_json[f"grid_{p}"]["cells"]]
        dbb = [c["id"] for c in from_db[f"grid_{p}"]["cells"]]
        if ja != dbb:
            fails += 1
            same = "값은 같고 순서만 다릅니다" if sorted(ja) == sorted(dbb) else "구성이 다릅니다"
            print(f"FAIL: grid_{p} 격자 순서 — {same} (batch_grid.ord 확인)")

    print(f"\n{'통과' if not fails else f'실패 {fails}건'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
