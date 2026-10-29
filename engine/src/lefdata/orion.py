"""Orion BMS Connect 로그 → 공통 포맷.

Connect 원본(.odl)은 독자 바이너리라 **Orion BMS 2 Utility** 로 CSV 로 바꾼 것을 읽는다.
    raw_orion_connect/            .odl 원본 (차량 폴더, 여러 시즌 섞임)
    raw_orion_connect/csv_orion_utility/<UTC시각>_bmsdata.csv   ← 이 모듈의 입력

CSV 1개 = Connect 로그 1개 = 레코딩 1개. 열 135개, `Time` 은 'Fri Aug 28 10:09:40 KST 2026' (1 초 해상도,
Utility 를 돌린 PC 의 시간대로 찍힌다). 파일명은 UTC.

신호 테이블에는 분석에 쓰는 열만 물리 이름으로 옮기고, 135열 전체는 frames.parquet 에 그대로 둔다.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

from . import __version__
from .writer import TZ, dir_bytes, fresh_dir, t_abs_array, write_index, write_json, write_parquet

KST = dt.timezone(dt.timedelta(hours=9))
T_QUALITY = "orion_clock"
NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})_bmsdata\.csv$")

# CSV 열 → 물리 이름. 전류는 Orion 규약대로 방전 +, 충전 −
COLUMNS = {
    "Pack Amperage (Current)": "current_a",
    "Pack Voltage": "voltage_v",
    "Pack Open-Circuit Voltage": "ocv_v",
    "Pack State of Charge (SOC)": "soc_pct",
    "Pack Discharge Current Limit (DCL)": "dcl_a",
    "Pack Charge Current Limit (CCL)": "ccl_a",
    "Pack Resistance": "pack_resistance_ohm",
    "Lowest Cell Voltage": "cell_min_v",
    "Highest Cell Voltage": "cell_max_v",
    "Average Cell Voltage": "cell_avg_v",
    "Lowest Cell Voltage ID": "cell_min_id",
    "Highest Cell Voltage ID": "cell_max_id",
    "Highest battery temperature": "temp_max_c",
    "Lowest battery temperature": "temp_min_c",
    "Average battery temperature": "temp_avg_c",
    "Highest Temperature ID": "temp_max_id",
    "Lowest Temperature ID": "temp_min_id",
    "Internal Heatsink Temperature": "bms_heatsink_c",
    "DCL Reduced Due To SOC": "dcl_limited_soc",
    "DCL Reduced Due To Temperature": "dcl_limited_temp",
    "DCL Reduced Due To Cell Voltage": "dcl_limited_cell_v",
    "DCL Reduced Due To Pack Voltage": "dcl_limited_pack_v",
    "DCL Reduced Due To Cell Resistance": "dcl_limited_cell_r",
    "Errors Present": "errors_present",
    "Discharge-Enable Output Active": "discharge_enable",
}


def parse_time(s: pd.Series) -> pd.Series:
    """'Fri Aug 28 10:09:40 KST 2026' → tz-aware (Asia/Seoul)."""
    t = pd.to_datetime(s.str.replace(" KST", "", regex=False), format="%a %b %d %H:%M:%S %Y")
    return t.dt.tz_localize(TZ)


def name_time_utc(path: Path) -> dt.datetime | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    return dt.datetime(*map(int, m.groups()), tzinfo=dt.timezone.utc)


def convert_file(src: Path, out_root: Path) -> dict:
    t0 = time.perf_counter()
    df = pd.read_csv(src, low_memory=False)
    t = parse_time(df["Time"])
    start = t.iloc[0].to_pydatetime()
    rec_id = start.strftime("%Y-%m-%d_%H%M%S") + "_orion"
    out = fresh_dir(out_root / rec_id)
    n = len(df)
    # 문자열에서 읽은 시각은 pandas 가 초 단위로 들고 있을 수 있다 - 단위에 기대지 않고 빼기로 구한다
    epoch_ms = ((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).to_numpy(np.int64)
    t_rel = epoch_ms - epoch_ms[0]
    base = {"t_rel_ms": pa.array(t_rel), "t_abs": t_abs_array(epoch_ms),
            "t_quality": pa.array([T_QUALITY] * n, type=pa.string())}

    frames = pa.Table.from_pandas(df, preserve_index=False).add_column(0, "t_rel_ms", pa.array(t_rel))
    write_parquet(frames, out / "frames.parquet")

    missing = [c for c in COLUMNS if c not in df]
    bms = pa.table({**base, **{new: pa.array(pd.to_numeric(df[old], errors="coerce").to_numpy(float))
                               for old, new in COLUMNS.items() if old in df}})
    write_parquet(bms, out / "bms.parquet")

    cur = pd.to_numeric(df["Pack Amperage (Current)"], errors="coerce")
    soc = pd.to_numeric(df["Pack State of Charge (SOC)"], errors="coerce")
    nt = name_time_utc(src)
    duration_s = float(t_rel[-1] / 1000) if n else 0.0
    meta = {
        "car": "LEF-25evo",
        "event": out_root.parent.name,
        "recording_id": rec_id,
        "source": {"logger": "Orion BMS Connect (Orion BMS 2 Utility 로 CSV 변환)",
                   "file": src.name, "file_bytes": src.stat().st_size, "columns": len(df.columns),
                   "missing_mapped_columns": missing},
        "time": {"basis": "CSV Time 열 (Connect 시계, 1 s 해상도)", "tz": TZ,
                 "t_abs_start": start.isoformat(), "duration_s": duration_s,
                 # 파일명(UTC, 파일 생성 순간)과 첫 행 시각의 차이. 1 s 정도 어긋나는 파일이 있다
                 "filename_utc_diff_s": (start - nt).total_seconds() if nt else None},
        "counts": {"rows": n},
        "summary": {"current_max_a": float(cur.max()), "soc_start_pct": float(soc.iloc[0]),
                    "soc_end_pct": float(soc.iloc[-1]),
                    "temp_max_c": float(pd.to_numeric(df["Highest battery temperature"], errors="coerce").max()),
                    "cell_min_v": float(pd.to_numeric(df["Lowest Cell Voltage"], errors="coerce").min())},
        "caveats": ["첫 행 SOC 가 0 인 로그가 있다 (부팅 직후 값). 분석 시 첫 몇 행 주의",
                    "전류는 약 1 Hz 평균값이라 에너지미터(100 Hz) 순간 최대보다 작게 나온다"],
        "perf": {"convert_sec": round(time.perf_counter() - t0, 3), "output_bytes": dir_bytes(out)},
        "converter_version": __version__,
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    write_json(meta, out / "meta.json")
    return {"recording_id": rec_id, "logger": "orion", "t_abs_start": start.isoformat(),
            "duration_s": round(duration_s, 1), "rows": n, **meta["summary"], "source_file": src.name}


def convert_event(event_dir: Path, src_dir: Path, first_day: str, last_day: str) -> list[dict]:
    """src_dir 의 CSV 중 시작일(KST)이 [first_day, last_day] 인 것만 이 이벤트로 변환."""
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    lo, hi = dt.date.fromisoformat(first_day), dt.date.fromisoformat(last_day)
    rows = []
    for src in sorted(src_dir.glob("*_bmsdata.csv")):
        nt = name_time_utc(src)
        if nt is None or not (lo <= nt.astimezone(KST).date() <= hi):
            continue
        rows.append(convert_file(src, out_root))
    write_index(rows, out_root / "recordings_orion.csv")
    return rows


# ---------------------------------------------------------------- 올린 파일 판별 (E4)

def detect(path: Path) -> dict | None:
    """올린 파일이 Orion BMS 2 Utility 로 바꾼 CSV 인가 (계약 §7 `detect`). 아니면 None. 올리기(ingest)는 아직 (판별만)."""
    path = Path(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline()
    if sum(c in header for c in COLUMNS) < 5:
        return None
    t = name_time_utc(path)
    return {"kind": "orion_csv", "car_guess": None, "start": t.astimezone(KST).isoformat(timespec="milliseconds") if t else None,
            "duration_s": None, "ok": True, "recording_id": None,
            "notes": ["Orion BMS Connect 로그를 Utility 로 바꾼 CSV — 올리기(ingest)는 아직 판별만 한다 (lefdata orion 으로 넣는다)"]
            + ([] if t else ["파일 이름에 시작 시각이 없다 (<UTC 시각>_bmsdata.csv)"])}
