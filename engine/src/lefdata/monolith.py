"""Monolith TMA-1 바이너리 로그(.log) → 공통 포맷.

포맷 원본: https://github.com/luftaquila/monolith  web/src/service/protocol.js

레코드 = 24바이트 고정, 전 필드 little endian
    0  u8   magic 0xAE
    1  u8   type (LOG_TYPE 인덱스)
    2  u16  checksum
    4  u32  timestamp (부팅 기준 ms)
    8  16B  payload

Monolith는 파일 1개 = 로거 전원 1회 = 레코딩 1개 (파일당 BOOT 레코드 1개).
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

from . import __version__
from .writer import (
    TZ, dir_bytes, fresh_dir, t_abs_array, write_index, write_json, write_parquet,
)

REC_SIZE = 24
MAGIC = 0xAE
LOG_TYPE = ["INVALID", "BOOT", "CAN", "GPS", "ANALOG", "DIGITAL", "GYROSCOPE", "SYSTEM", "USER_EVENT"]
T_BOOT, T_CAN, T_GPS, T_ANALOG, T_DIGITAL, T_GYRO, T_SYS, T_USER = range(1, 9)

HEADER = np.dtype([("magic", "u1"), ("type", "u1"), ("checksum", "<u2"), ("ts", "<u4")])
GPS_PL = np.dtype([("lat", "<u4"), ("lon", "<u4"), ("lat_dir", "u1"), ("lon_dir", "u1"),
                   ("_r", "<u2"), ("speed", "<u2"), ("course", "<u2")])
BOOT_PL = np.dtype([("protocol_version", "u1"), ("_r", "u1"), ("mac", "u1", 6), ("boot_time", "<u8")])
CAN_PL = np.dtype([("id", "<u4"), ("extended", "u1"), ("remote", "u1"), ("len", "u1"), ("_r", "u1"),
                   ("data", "u1", 8)])

# 공식 변환식 (protocol.js convert)
ADC_FSR_V = 4.096
ACCEL_FSR_G = 8
GYRO_FSR_DPS = 500

KST = dt.timezone(dt.timedelta(hours=9))

# 절대시각 출처. boot_time 은 SNTP 동기화 전 RTC 값이라 독립 검증은 안 됨 (파일명도 같은 RTC 기반)
T_QUALITY = "boot_rtc"


# ---------------------------------------------------------------- 파싱

def checksum_ok(raw: np.ndarray) -> np.ndarray:
    """uint32 6개 XOR (체크섬 자리 0으로) → 상위 16bit 를 하위에 접어 더함."""
    words = raw.view("<u4").reshape(-1, 6).copy()
    words[:, 0] &= 0x0000FFFF
    x = np.bitwise_xor.reduce(words, axis=1)
    calc = ((x & 0xFFFF) + (x >> 16)) & 0xFFFF
    return calc == raw[:, 2:4].copy().view("<u2").ravel()


def _payload(raw: np.ndarray, mask: np.ndarray, dtype) -> np.ndarray:
    return np.frombuffer(raw[mask, 8:24].tobytes(), dtype=dtype)


def nmea_to_deg(v: np.ndarray) -> np.ndarray:
    """ddmm.mmmmm ×1e5 정수 → 십진도."""
    x = v / 1e7
    d = np.floor(x)
    return d + (x - d) * 100 / 60


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", errors="replace")


# ---------------------------------------------------------------- 변환

def convert_file(src: Path, out_root: Path) -> dict:
    """.log 1개를 레코딩 1개로 변환하고 인덱스용 요약을 돌려준다."""
    t0 = time.perf_counter()
    data = src.read_bytes()
    n = len(data) // REC_SIZE
    tail = len(data) % REC_SIZE
    raw = np.frombuffer(data, dtype="u1", count=n * REC_SIZE).reshape(n, REC_SIZE)
    hdr = raw[:, :8].copy().view(HEADER).ravel()

    rtype = hdr["type"]
    ts = hdr["ts"].astype(np.int64)
    valid = (hdr["magic"] == MAGIC) & (rtype >= 1) & (rtype < len(LOG_TYPE)) & checksum_ok(raw)

    # --- BOOT: 레코딩 기준 시각
    is_boot = rtype == T_BOOT
    if is_boot.sum() != 1:
        raise ValueError(f"{src.name}: BOOT 레코드가 {is_boot.sum()}개 (1개여야 함)")
    boot = _payload(raw, is_boot, BOOT_PL)[0]
    boot_time_s = int(boot["boot_time"])
    boot_ts = int(ts[is_boot][0])

    t_rel = ts - boot_ts
    t_abs_ms = boot_time_s * 1000 + t_rel

    start_kst = dt.datetime.fromtimestamp(boot_time_s, KST)
    rec_id = start_kst.strftime("%Y-%m-%d_%H%M%S")
    stem_parts = src.stem.split("_", 2)
    label = stem_parts[2] if len(stem_parts) == 3 else None
    filename_match = src.stem.startswith(rec_id)

    out = fresh_dir(out_root / rec_id)

    # --- frames: 원본 전부
    safe_type = np.where((rtype < len(LOG_TYPE)), rtype, 0).astype(np.int8)
    is_can = rtype == T_CAN
    can_id = np.zeros(n, dtype=np.int64)
    if is_can.any():
        can_id[is_can] = _payload(raw, is_can, CAN_PL)["id"]
    payload = raw[:, 8:24].tobytes()
    frames = pa.table({
        "row_no": pa.array(np.arange(n, dtype=np.int64)),
        "t_rel_ms": pa.array(t_rel),
        "t_abs": t_abs_array(t_abs_ms),
        "t_quality": pa.array(np.full(n, T_QUALITY), type=pa.string()).dictionary_encode(),
        "t_raw": pa.nulls(n, pa.string()),
        "kind": pa.DictionaryArray.from_arrays(pa.array(safe_type), pa.array(LOG_TYPE)),
        "can_id": pa.array(can_id, type=pa.int64(), mask=~is_can),
        "payload": pa.FixedSizeBinaryArray.from_buffers(pa.binary(16), n, [None, pa.py_buffer(payload)]).cast(pa.binary()),
        "valid": pa.array(valid),
        "dup": pa.array(np.zeros(n, dtype=bool)),
    })
    write_parquet(frames, out / "frames.parquet")

    def base(mask):
        return {
            "t_rel_ms": pa.array(t_rel[mask]),
            "t_abs": t_abs_array(t_abs_ms[mask]),
            "t_quality": pa.array(np.full(int(mask.sum()), T_QUALITY), type=pa.string()).dictionary_encode(),
            "valid": pa.array(valid[mask]),
        }

    # --- GPS
    m = rtype == T_GPS
    g = _payload(raw, m, GPS_PL)
    fix = g["lat"] != 0
    lat = np.where(fix, nmea_to_deg(g["lat"]) * np.where(g["lat_dir"] == ord("S"), -1, 1), np.nan)
    lon = np.where(fix, nmea_to_deg(g["lon"]) * np.where(g["lon_dir"] == ord("W"), -1, 1), np.nan)
    gps = pa.table({**base(m),
                    "fix": pa.array(fix),
                    "lat": pa.array(lat), "lon": pa.array(lon),
                    "speed_kmh": pa.array(g["speed"] / 100),   # 펌웨어가 knot→km/h 환산해 ×100 저장
                    "course_deg": pa.array(g["course"] / 100)})
    write_parquet(gps, out / "gps.parquet")

    # --- ANALOG (AIN은 전압까지만. 무엇이 연결됐는지 모르므로 ℃ 변환하지 않음)
    m = rtype == T_ANALOG
    a = _payload(raw, m, "<i2").reshape(-1, 8)
    analog = pa.table({**base(m),
                       **{f"ain{i+1}_v": pa.array(a[:, i] / 32768 * ADC_FSR_V) for i in range(6)},
                       "supply_raw": pa.array(a[:, 6]),
                       "board_temp_raw": pa.array(a[:, 7])})
    write_parquet(analog, out / "analog.parquet")

    # --- GYROSCOPE (MPU-6500)
    m = rtype == T_GYRO
    y = _payload(raw, m, "<i2").reshape(-1, 8)
    imu = pa.table({**base(m),
                    "ax_g": pa.array(y[:, 0] / 32768 * ACCEL_FSR_G),
                    "ay_g": pa.array(y[:, 1] / 32768 * ACCEL_FSR_G),
                    "az_g": pa.array(y[:, 2] / 32768 * ACCEL_FSR_G),
                    "gx_dps": pa.array(y[:, 4] / 32768 * GYRO_FSR_DPS),
                    "gy_dps": pa.array(y[:, 5] / 32768 * GYRO_FSR_DPS),
                    "gz_dps": pa.array(y[:, 6] / 32768 * GYRO_FSR_DPS),
                    "temp_raw": pa.array(y[:, 3])})
    write_parquet(imu, out / "imu.parquet")

    # --- SYSTEM / USER_EVENT
    m = (rtype == T_SYS) | (rtype == T_USER)
    event_rows = np.flatnonzero(m)
    msgs = [_cstr(raw[i, 8:24].tobytes()) for i in event_rows]
    events = pa.table({**base(m),
                       "kind": pa.array([LOG_TYPE[t] for t in rtype[m]]),
                       "message": pa.array(msgs, type=pa.string())})
    write_parquet(events, out / "events.parquet")

    # --- 시간 품질: 단조증가, 최대 간격, SNTP 동기화 전후 간격 (시계 점프가 ts에 섞였는지)
    d = np.diff(ts)
    sntp_rows = [int(i) for i, msg in zip(event_rows, msgs) if msg == "RTC_SNTP_SYNC"]
    sntp_gaps = [[int(d[i - 1]) if i > 0 else None, int(d[i]) if i < n - 1 else None] for i in sntp_rows]
    gaps_over_1s = [
        {"at": dt.datetime.fromtimestamp(t_abs_ms[i] / 1000, KST).isoformat(timespec="milliseconds"),
         "row_no": int(i), "gap_ms": int(d[i])}
        for i in np.flatnonzero(d > 1000)
    ]

    by_kind = {LOG_TYPE[t]: int((rtype == t).sum()) for t in range(len(LOG_TYPE)) if (rtype == t).any()}
    invalid = int((~valid).sum())
    duration_s = float((t_rel.max() - t_rel.min()) / 1000)

    meta = {
        "car": "LEF-25evo",
        "event": out_root.parent.name,
        "recording_id": rec_id,
        "source": {
            "logger": "Monolith TMA-1",
            "file": str(src.relative_to(out_root.parent)).replace("\\", "/"),
            "file_bytes": len(data),
            "tail_bytes_ignored": tail,
            "boot_mac": ":".join(f"{x:02X}" for x in boot["mac"]),
            "protocol_version": int(boot["protocol_version"]),
        },
        "label": label,
        "time": {
            "basis": "BOOT.boot_time (Unix epoch s, UTC) + (ts_ms - ts_ms@BOOT)",
            "abs_precision": "±1 s (boot_time 초 단위)",
            "abs_caveat": "boot_time 은 SNTP 동기화 전 RTC 값. 파일명도 같은 RTC 기반이라 독립 검증 아님",
            "tz": TZ,
            "t_abs_start": start_kst.isoformat(),
            "duration_s": duration_s,
            "boot_ts_ms": boot_ts,                  # 전원이 들어온 뒤 BOOT 레코드까지 (ms). t_rel_ms = ts - boot_ts
            "last_ts_ms": int(ts.max()),            # 전원이 들어온 뒤 마지막 레코드까지 → 같은 전원을 쓰는 장비와 짝지을 때 쓴다
            "filename_time_match": filename_match,
            "ts_monotonic": bool((d >= 0).all()),
            "max_gap_ms": int(d.max()) if len(d) else 0,
            "sntp_sync_events": len(sntp_rows),
            "gaps_around_sntp_ms": sntp_gaps,
            "gaps_over_1s": gaps_over_1s,
        },
        "counts": {
            "frames": n,
            "invalid": invalid,
            "dup": 0,
            "by_kind": by_kind,
            "gps_fix": int(fix.sum()),
        },
        "quality": {
            "checksum_fail_rate": invalid / n if n else 0.0,
            "dup_rate": 0.0,
            "t_abs_null_rate": 0.0,
        },
        "perf": {
            "convert_sec": round(time.perf_counter() - t0, 3),
            "input_bytes": len(data),
            "output_bytes": dir_bytes(out),   # parquet 합계 (meta.json 제외)
        },
        "converter_version": __version__,
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    write_json(meta, out / "meta.json")

    return {
        "recording_id": rec_id,
        "label": label,
        "t_abs_start": start_kst.isoformat(),
        "duration_s": round(duration_s, 1),
        "frames": n,
        "invalid": invalid,
        "gps_fix": int(fix.sum()),
        "gaps_over_1s": len(gaps_over_1s),
        "imu_rows": int((rtype == T_GYRO).sum()),
        "analog_rows": int((rtype == T_ANALOG).sum()),
        "can_rows": int(is_can.sum()),
        "input_bytes": len(data),
        "output_bytes": meta["perf"]["output_bytes"],
        "convert_sec": meta["perf"]["convert_sec"],
        "source_file": meta["source"]["file"],
    }


def convert_event(event_dir: Path) -> list[dict]:
    """이벤트 폴더의 raw_logger/*.log 전부 변환 → normalized/ + recordings.csv."""
    src_dir = event_dir / "raw_logger"
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    rows = []
    for src in sorted(src_dir.glob("*.log")):
        row = convert_file(src, out_root)
        rows.append(row)
        print(f"  {row['recording_id']}  {row['label'] or '-':<16} frames={row['frames']:>8,}  "
              f"invalid={row['invalid']}  gps_fix={row['gps_fix']:>6,}  "
              f"{row['input_bytes']/1e6:6.1f}MB → {row['output_bytes']/1e6:5.1f}MB  {row['convert_sec']:.2f}s")
    write_index(rows, out_root / "recordings.csv")
    return rows
