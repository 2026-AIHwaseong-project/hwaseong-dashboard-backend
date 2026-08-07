"""
전국 원본 → 화성시만 추출한 경량 데이터셋 생성

    python analysis/00_extract_hwaseong.py

전국 데이터를 그대로 저장소에 올리면 클론이 느려지므로, 화성시분만 뽑아
dataset_hwaseong/ 에 저장한다. 팀원은 이 폴더만 있으면 바로 작업할 수 있다.

원본(dataset/)은 로컬에만 두고 .gitignore 로 제외한다.
"""
import sys, io, gzip, tarfile
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dataset"
OUT = ROOT / "dataset_hwaseong"
OUT.mkdir(exist_ok=True)

TARGET = "화성"


def read_csv(path_or_buf, **kw):
    """공공데이터 CSV는 cp949가 대부분이라 폴백을 둔다."""
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            if hasattr(path_or_buf, "seek"):
                path_or_buf.seek(0)
            return pd.read_csv(path_or_buf, encoding=enc, low_memory=False, **kw)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise RuntimeError(f"인코딩 판별 실패: {path_or_buf}")


def save(df, name):
    p = OUT / name
    df.to_csv(p, index=False, encoding="utf-8-sig")
    print(f"  -> {name}  {len(df):,}행  {p.stat().st_size/1e6:.2f} MB")


print("=" * 62)
print("[1] 승하차 — 전국 tar에서 화성시만 (일자별 gz 병합)")
frames = []
for tar_path in sorted(SRC.glob("*승하차*.tar")):
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            if not m.name.endswith(".gz"):
                continue
            raw = gzip.decompress(tf.extractfile(m).read())
            df = read_csv(io.BytesIO(raw))
            col = next((c for c in df.columns if "관할" in c or "시군" in c), None)
            if col:
                df = df[df[col].astype(str).str.contains(TARGET, na=False)]
            frames.append(df)
    print(f"  {tar_path.name} 처리 완료")
if frames:
    save(pd.concat(frames, ignore_index=True), "boarding_hwaseong.csv")

print("=" * 62)
print("[2] 전국 버스정류장 → 화성시")
f = SRC / "국토교통부_전국 버스정류장 위치정보_20251031.csv"
if f.exists():
    df = read_csv(f)
    col = next((c for c in df.columns if "도시명" in c), None)
    save(df[df[col].astype(str).str.contains(TARGET, na=False)], "stops_national_hwaseong.csv")

print("=" * 62)
print("[3] 이미 화성시 전용인 파일 — 그대로 복사")
passthrough = {
    "분석시스템_분석갤러리_유동인구_화성시.csv": "flow_hourly.csv",
    "버스정류소현황.csv": "stops_gg.csv",
    "경기도 화성시_공동주택 현황_20260701.csv": "apartments.csv",
    "경기도 화성시_마을버스노선현황_20260201.csv": "routes_village.csv",
    "경기도 화성시_산업단지 입주기업 현황_20231231.csv": "industrial_complex.csv",
    "경기도 화성시_인구_20260701.csv": "population_dong.csv",
    "경기도 화성시_제조업 현황_20260526.csv": "manufacturing.csv",
}
for src_name, out_name in passthrough.items():
    p = SRC / src_name
    if p.exists():
        save(read_csv(p), out_name)
    else:
        print(f"  !! 없음: {src_name}")

print("=" * 62)
print("[4] 철도역 — 화성시 인근 + 동탄역 보강")
f = SRC / "전체_도시철도역사정보_20260630.xlsx"
if f.exists():
    df = pd.read_excel(f)
    m = df.astype(str).apply(
        lambda r: r.str.contains("화성|동탄|병점|어천|야목", na=False)
    ).any(axis=1)
    rail = df[m][["역사명", "노선명", "역위도", "역경도"]].copy()
    # 동탄역(SRT/GTX-A)은 도시철도 표준데이터에 없어 TAGO 정류장 실측으로 보강
    if not (rail["역사명"] == "동탄역").any():
        rail = pd.concat([rail, pd.DataFrame([{
            "역사명": "동탄역", "노선명": "SRT/GTX-A",
            "역위도": 37.1997, "역경도": 127.0962,
        }])], ignore_index=True)
    save(rail, "rail_stations.csv")

print("=" * 62)
total = sum(p.stat().st_size for p in OUT.glob("*.csv"))
print(f"완료 — dataset_hwaseong/ 총 {total/1e6:.1f} MB")
print("※ SGIS 격자(전국 279MB)는 공간조인이 필요해 제외. dataset/README.md 참조")
