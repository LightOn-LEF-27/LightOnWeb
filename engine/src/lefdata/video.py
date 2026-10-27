"""온보드 영상(GoPro) → 공통 포맷.

영상을 **또 하나의 데이터 소스**로 취급한다. 영상에서 뽑은 GPS 를 다른 로거와 같은 스키마로
`normalized/` 에 넣으면, 랩 분할·종목 분석이 이미 있는 코드 그대로 돌아간다.
(로거 GPS 가 없던 차의 랩을 실제로 이렇게 셌다.)

    raw_video/*.MP4   →   normalized/<recording_id>/{gps,imu,frames}.parquet
                      →   lefdata laps / events 가 그대로 처리

왜 필요한가: LEF-26 은 로거 GPS 가 사실상 죽어서(안테나 단선) **영상이 유일한 위치 데이터원**이다.

파싱 자체는 `gopro.py` (로거·폴더 구조를 모르는 순수 계층). 이 파일은 우리 폴더 구조를 안다.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

from . import __version__, gopro
from .writer import T_ABS_TYPE, TZ, dir_bytes, fresh_dir, write_index, write_json

# GoPro 는 4 GB 마다 파일을 쪼갠다. GOPR3331 → GP013331 → GP023331 … 이 하나의 연속 녹화다.
#   GOPR<번호>.MP4 = 첫 조각, GP<조각번호><번호>.MP4 = 이어지는 조각
CHUNK_RE = re.compile(r"^(?:GOPR(?P<a>\d{4})|GP(?P<seq>\d{2})(?P<b>\d{4}))$", re.I)
VIDEO_EXT = (".MP4", ".LRV")      # LRV(저해상도 프록시)에도 같은 텔레메트리 트랙이 들어 있다

_BASE = [("t_rel_ms", pa.int64()), ("t_abs", T_ABS_TYPE), ("t_quality", pa.string())]
SCHEMAS = {
    "gps": pa.schema(_BASE + [("fix", pa.bool_()), ("lat", pa.float64()), ("lon", pa.float64()),
                              ("speed_kmh", pa.float64()), ("course_deg", pa.float64()),
                              ("alt_m", pa.float64()), ("dop", pa.float64())]),
    # ⚠ 축은 **카메라 기준**이다. 차량 축이 아니다 (장착 방향에 따라 달라진다).
    "imu": pa.schema(_BASE + [(c, pa.float64()) for c in
                              ("a0_g", "a1_g", "a2_g", "g0_dps", "g1_dps", "g2_dps")]),
}


def group_chunks(video_dir: Path) -> dict[str, list[Path]]:
    """녹화 번호별로 조각을 모은다. 같은 번호 = 같은 녹화."""
    groups: dict[str, list[tuple[int, Path]]] = {}
    for p in sorted(video_dir.iterdir()):
        if p.suffix.upper() not in VIDEO_EXT:
            continue
        m = CHUNK_RE.match(p.stem)
        if not m:
            continue
        num = m.group("a") or m.group("b")
        seq = 0 if m.group("a") else int(m.group("seq")) + 1
        groups.setdefault(num, []).append((seq, p))
    out = {}
    for num, items in groups.items():
        # 같은 조각의 MP4 와 LRV 가 둘 다 있으면 MP4 를 쓴다 (해상도는 무관하나 원본 우선)
        best: dict[int, Path] = {}
        for seq, p in sorted(items):
            if seq not in best or p.suffix.upper() == ".MP4":
                best[seq] = p
        out[num] = [best[k] for k in sorted(best)]
    return out


INDEX_NAME = "recordings_video.csv"      # 로거 색인(recordings.csv)과 섞이지 않게 따로 둔다


def recording_id(num: str, first_fix_utc) -> str:
    """`<GPS 날짜(KST)>_<녹화 번호>`. 이벤트가 여러 날이면 폴더 이름의 날짜로는 구분이 안 된다.

    카메라 시계(파일의 촬영 시각)는 쓰지 않는다 — 설정이 안 된 카메라는 2015년으로 찍힌다.
    """
    return f"{first_fix_utc.tz_convert(TZ):%Y-%m-%d}_{num}"


def convert_event(event_dir: Path, video_dir: Path | None = None) -> list[dict]:
    """raw_video/ 의 영상들 → normalized/. 녹화 하나가 레코딩 하나다.

    GPS 3D fix 가 한 번도 없는 녹화(실내 등)는 건너뛴다 — 위치도 믿을 시각도 없다.
    """
    video_dir = video_dir or (event_dir / "raw_video")
    out_root = event_dir / "normalized"
    out_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for num, files in sorted(group_chunks(video_dir).items()):
        t_start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            bins = []
            for f in files:
                b = Path(tmp) / f"{f.stem}.bin"
                try:
                    bins.append(gopro.extract_stream(f, b))
                except RuntimeError:
                    continue                      # 텔레메트리가 없는 조각은 건너뛴다
            gps, imu, imu_time = gopro.read_recording(bins)

        if gps.empty:
            print(f"  [{num}] GPS fix 없음 — 건너뜀 ({', '.join(f.name for f in files)})")
            continue

        t0 = gps["t_utc"].iloc[0]
        rid = recording_id(num, t0)
        out = fresh_dir(out_root / rid)
        gps["t_rel_ms"] = ((gps["t_utc"] - t0).dt.total_seconds() * 1000).round().astype("int64")
        gps["course_deg"] = gopro.course_from_track(gps["lat"], gps["lon"], gps["t_rel_ms"] / 1000.0)
        gps["fix"] = True                          # read_streams 가 3D fix 만 남긴다
        n_imu = 0
        if not imu.empty:
            imu = imu[imu["t_utc"] >= t0].copy()
            imu["t_rel_ms"] = ((imu["t_utc"] - t0).dt.total_seconds() * 1000).round().astype("int64")
            n_imu = len(imu)

        for name, df in (("gps", gps), ("imu", imu)):
            _write(out / f"{name}.parquet", df, SCHEMAS[name])

        secs = float(gps["t_rel_ms"].iloc[-1]) / 1000.0
        meta = {
            "car": event_dir.parent.name,
            "event": event_dir.name,
            "recording_id": rid,
            "source": {"logger": "GoPro 온보드 영상 (GPMF)",
                       "files": [f.name for f in files],
                       "note": "LRV(저해상도 프록시)에도 같은 텔레메트리 트랙이 들어 있다"},
            "label": None,
            "time": {
                "tz": TZ,
                "t_quality": "gps_utc",
                "tz_basis": "GPMF 의 GPSU(UTC) 를 KST 로 변환. GPS 시각이라 가장 신뢰도가 높다",
                "t_abs_start": gps["t_utc"].iloc[0].tz_convert(TZ).isoformat(timespec="milliseconds"),
                "t_abs_end": gps["t_utc"].iloc[-1].tz_convert(TZ).isoformat(timespec="milliseconds"),
                "duration_s": round(secs, 1),
            },
            "counts": {"gps_rows": len(gps), "imu_rows": n_imu},
            "quality": {
                "gps_hz": round(len(gps) / secs, 1) if secs else None,
                "dop_median": round(float(gps["dop"].median()), 1),
                "dropped_no_3d_fix": "read_streams 가 3D fix 가 아닌 블록을 버린다 (위성 잡기 전 구간)",
            },
            # 같은 블록의 IMU 는 GPS 보다 0~2 초 늦게 들어 있고 파일마다 다르다. 잰 값과 당긴 값을 남긴다 (gopro.read_recording)
            "imu_time": {
                "files": imu_time,
                "t_quality": {"gps_utc": "GPS 시각에 맞춤", "gps_utc_uncal": "못 맞춤 — 최대 2 s 늦을 수 있다"},
            },
            "imu_axes": {
                "basis": "GPMF 스트림 이름 (HERO5): 가속도 '(up/down, right/left, forward/back)', 자이로 '(z,x,y)'",
                "a0_g": "카메라 위아래", "a1_g": "카메라 좌우", "a2_g": "카메라 앞뒤",
                "g0_dps": "위아래 축 둘레", "g1_dps": "좌우 축 둘레", "g2_dps": "앞뒤 축 둘레",
            },
            "caveats": [
                "course_deg 는 GoPro 가 주지 않아 위치 변화로 계산한 파생값이다",
                "IMU 축은 카메라 기준이다. 차량 축이 아니다 — 카메라가 기울어 달려 있으면 그만큼 섞인다. "
                "정지 구간의 가속도 평균이 '위' 방향이므로, 자이로를 그 방향에 투영하면 장착각과 무관한 요레이트가 된다",
            ],
            "converter": {"version": __version__, "elapsed_s": round(time.perf_counter() - t_start, 1)},
        }
        write_json(meta, out / "meta.json")
        # 조각마다 IMU 를 몇 초 당겼는지. '?' = 재지 못함
        shifts = "/".join(f"{c['imu_shift_applied_s']:.0f}" if c["calibrated"] else "?" for c in imu_time)
        rows.append({"recording_id": rid, "t_abs_start": meta["time"]["t_abs_start"],
                     "t_abs_end": meta["time"]["t_abs_end"], "duration_s": meta["time"]["duration_s"],
                     "gps_rows": len(gps), "imu_rows": n_imu,
                     "gps_hz": meta["quality"]["gps_hz"], "imu_shift_s": shifts, "output_bytes": dir_bytes(out)})
        print(f"  [{rid}] {len(files)}조각 · GPS {len(gps):,}행 ({meta['quality']['gps_hz']} Hz) · "
              f"IMU {n_imu:,}행 (GPS 보다 {shifts} s 늦어 당김) · {secs / 60:.1f}분")

    write_index(rows, out_root / INDEX_NAME)
    return rows


def _write(path: Path, df: pd.DataFrame, schema: pa.Schema) -> None:
    import pyarrow.parquet as pq
    if df.empty:
        pq.write_table(schema.empty_table(), path, compression="zstd")
        return
    cols = {}
    for f in schema:
        if f.name == "t_abs":
            cols[f.name] = pa.array(df["t_utc"].dt.tz_convert(TZ).dt.floor("ms")).cast(T_ABS_TYPE)
        elif f.name == "t_quality":
            ok = df["t_ok"].to_numpy() if "t_ok" in df else np.ones(len(df), bool)
            cols[f.name] = pa.array(np.where(ok, "gps_utc", "gps_utc_uncal"), pa.string())
        else:
            cols[f.name] = pa.array(df[f.name].to_numpy(), f.type)
    pq.write_table(pa.table(cols, schema=schema), path, compression="zstd")
