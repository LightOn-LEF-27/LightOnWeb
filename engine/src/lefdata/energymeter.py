"""FSK 에너지미터(FSK-EEM) 로그(.log) → 공통 포맷.

검차 통과 후 차에 다는 장비. HV 음극선에 직렬로 들어가 팩 전압·전류를 100 Hz 로 잰다.
설계·펌웨어·뷰어가 공개돼 있다: https://github.com/luftaquila/fsk-energymeter (Monolith 와 같은 제작자).
아래 포맷은 그 펌웨어의 `device/firmware/Core/Inc/energymeter.h` · `energymeter_record.c` 를 그대로 옮긴 것이고,
우리가 가진 파일 전부를 이 정의로 읽어 체크섬 실패 0 을 확인했다.

파일 1개 = 미터 전원 1회 = 레코딩 1개. 전 필드 little endian.

    헤더 32 B
        0   u8    0xAA
        1   u8    0x00 (헤더 타입)
        2   u16   체크섬
        4   u32   헤더를 쓴 순간의 틱 (부팅 후 ms, 보통 400 ~ 750)
        8   12 B  장치 ID (STM32 96 비트 UID, u32 ×3. 파일명 뒤쪽과 같다)
        20  4 B   펌웨어 v1.8.0(2026-01) 앞: 프로토콜 버전 u8 (= 2) + 예약 3 B
                  v1.8.0 부터: 전압·전류 영점 보정값 i16 ×2 (0.001 V · 0.001 A)
        24  6 B   부팅 시각 YY MM DD hh mm ss (장치 RTC. 뷰어의 Sync RTC 로 PC 시각에 맞춘다)
        30  u16   부팅 시각 ms
    레코드 16 B
        0   u8    0xAA
        1   u8    0x01 (레코드 타입).  0x02 = 이벤트 (소스에 정의만 있고 우리 파일에는 0개)
        2   u16   체크섬
        4   u32   틱 (부팅 후 ms)
        8   i16   HV 전압 × 10 (V)
        10  i16   HV 전류 × 10 (A, 방전 +)
        12  i16   LV 공급 전압 × 100 (V)
        14  i16   장치(CPU) 온도 × 100 (℃)

    체크섬 = 16 비트 워드로 끊어 (체크섬 자리는 0 으로 두고) 전부 XOR. 저장된 값까지 XOR 하면 0 이 된다.

시각: 레코드 틱은 MCU 크리스털(HSE)로 가서 정확하다. 절대시각은 부팅 때 읽은 RTC + 틱이다 —
RTC 는 사람이 맞춘 값이라 다른 장비와 수십 ~ 수백 초 어긋난다.
전원을 켠 뒤 약 550 ms 동안 전압·전류 영점을 잡으므로, 그때 HV 가 이미 걸려 있었다면 값이 통째로 치우친다.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

from . import __version__
from .writer import TZ, dir_bytes, fresh_dir, t_abs_array, write_index, write_json, write_parquet

HEADER_SIZE = 32
REC_SIZE = 16
MAGIC = 0xAA
T_HEADER, T_RECORD, T_EVENT = 0x00, 0x01, 0x02
V_PER_NUM = 0.1
A_PER_NUM = 0.1
LV_V_PER_NUM = 0.01
TEMP_C_PER_NUM = 0.01
HV_ON_V = 50.0          # 이 전압을 넘으면 HV 가 켜진 레코딩으로 본다
FORMAT_SOURCE = "https://github.com/luftaquila/fsk-energymeter (device/firmware/Core/Inc/energymeter.h)"

KST = dt.timezone(dt.timedelta(hours=9))
T_QUALITY = "meter_rtc"   # 미터 내부 시계 (사람이 PC 시각에 맞춘 RTC). 다른 장비와 독립 검증은 안 됨

NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{3})")

# ch = [HV 전압, HV 전류, LV 전압, 온도]
RECORD = np.dtype([("magic", "u1"), ("type", "u1"), ("checksum", "<u2"), ("t_ms", "<u4"),
                   ("ch", "<i2", 4)])


# ---------------------------------------------------------------- 파싱

def checksum_ok(data: bytes, offset: int, size: int, count: int = 1) -> np.ndarray:
    """offset 부터 size 바이트짜리 덩어리 count 개의 체크섬 검사. 워드를 전부 XOR 하면 0 이어야 한다."""
    words = np.frombuffer(data, "<u2", count=count * size // 2, offset=offset).reshape(count, size // 2)
    return np.bitwise_xor.reduce(words, axis=1) == 0


def parse_header(head: bytes) -> dict:
    """32 B 헤더 → 장치 ID, 시작 시각(KST), 헤더 틱, 체크섬 통과 여부, 20 ~ 23 바이트 해석."""
    if len(head) < HEADER_SIZE or head[0] != MAGIC or head[1] != T_HEADER:
        raise ValueError("에너지미터 헤더가 아니다")
    ids = np.frombuffer(head[8:20], "<u4")
    y, mo, d, h, mi, s = head[24:30]
    ms = int.from_bytes(head[30:32], "little")
    start = dt.datetime(2000 + y, mo, d, h, mi, s, ms * 1000, tzinfo=KST)
    word = bytes(head[20:24])
    if word[1:] == b"\0\0\0" and 0 < word[0] < 16:
        layout = {"layout": "v1.8.0 앞 펌웨어 (프로토콜 버전)", "protocol_version": int(word[0])}
    else:
        v_cal, c_cal = np.frombuffer(word, "<i2")
        layout = {"layout": "v1.8.0 부터 (영점 보정값)", "v_cal_v": int(v_cal) / 1000, "c_cal_a": int(c_cal) / 1000}
    return {"device_id": "-".join(f"{x:08X}" for x in ids), "start": start,
            "tick_ms": int.from_bytes(head[4:8], "little"),
            "checksum_ok": bool(checksum_ok(head, 0, HEADER_SIZE)[0]),
            "bytes_20_23": {"hex": word.hex(), **layout}}


def read_records(data: bytes) -> np.ndarray:
    """헤더 뒤 16 B 레코드 전부. 끝의 자투리 바이트는 버린다 (meta 에 개수 기록)."""
    n = max(0, (len(data) - HEADER_SIZE) // REC_SIZE)
    return np.frombuffer(data, RECORD, count=n, offset=HEADER_SIZE)


def name_time(path: Path) -> dt.datetime | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = map(int, m.groups())
    return dt.datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=KST)


def energy_wh(t_ms: np.ndarray, v: np.ndarray, i: np.ndarray) -> tuple[float, float]:
    """사다리꼴 적분 → (방전 Wh, 충전·회생 Wh). 충전은 양수로 돌려준다."""
    if len(t_ms) < 2:
        return 0.0, 0.0
    p = v * i
    dt_h = np.diff(t_ms.astype(float)) / 3.6e6
    seg = (p[1:] + p[:-1]) / 2 * dt_h
    return float(seg[seg > 0].sum()), float(-seg[seg < 0].sum())


# ---------------------------------------------------------------- 변환

def recording_id(start: dt.datetime) -> str:
    return start.strftime("%Y-%m-%d_%H%M%S") + "_em"


def convert_file(src: Path, out_root: Path) -> dict:
    t0 = time.perf_counter()
    data = src.read_bytes()
    head = parse_header(data[:HEADER_SIZE])
    start = head["start"]
    rec_id = recording_id(start)
    out = fresh_dir(out_root / rec_id)

    r_all = read_records(data)
    n_all = len(r_all)
    tail = len(data) - HEADER_SIZE - n_all * REC_SIZE if len(data) >= HEADER_SIZE else len(data)
    sum_ok = checksum_ok(data, HEADER_SIZE, REC_SIZE, n_all) if n_all else np.zeros(0, bool)
    is_rec = r_all["type"] == T_RECORD
    n_event = int((r_all["type"] == T_EVENT).sum())
    r = r_all[is_rec]                      # 이벤트·알 수 없는 타입은 측정값 표에 넣지 않는다 (개수만 meta 에)
    n = len(r)
    valid = (r["magic"] == MAGIC) & sum_ok[is_rec]
    t_rel = r["t_ms"].astype(np.int64)
    v = r["ch"][:, 0] * V_PER_NUM
    i = r["ch"][:, 1] * A_PER_NUM
    lv = r["ch"][:, 2] * LV_V_PER_NUM
    temp = r["ch"][:, 3] * TEMP_C_PER_NUM
    start_ms = int(start.timestamp() * 1000)

    energy = pa.table({
        "t_rel_ms": pa.array(t_rel),
        "t_abs": t_abs_array(start_ms + t_rel),
        "t_quality": pa.array([T_QUALITY] * n, type=pa.string()),
        "valid": pa.array(valid),
        "voltage_v": pa.array(v),
        "current_a": pa.array(i),
        "lv_voltage_v": pa.array(lv),
        "temp_c": pa.array(temp),
    })
    write_parquet(energy, out / "energy.parquet")

    d = np.diff(t_rel)
    dis_wh, chg_wh = energy_wh(t_rel[valid], v[valid], i[valid])
    nt = name_time(src)
    hv_on = bool(n and v.max() > HV_ON_V)
    duration_s = float((t_rel[-1] - t_rel[0]) / 1000) if n else 0.0

    meta = {
        "car": out_root.parent.parent.name,
        "event": out_root.parent.name,
        "recording_id": rec_id,
        "source": {
            "logger": "FSK 에너지미터 (FSK-EEM)",
            "format_source": FORMAT_SOURCE,
            "device_id": head["device_id"],
            "file": str(src.relative_to(out_root.parent)).replace("\\", "/"),
            "file_bytes": len(data),
            "tail_bytes_ignored": int(tail),
            "header_checksum_ok": head["checksum_ok"],
            "header_tick_ms": head["tick_ms"],
            "header_bytes_20_23": head["bytes_20_23"],
        },
        "time": {
            "basis": "헤더 부팅 시각(미터 RTC, ms) + 레코드 틱(부팅 후 ms, MCU 크리스털)",
            "tz": TZ,
            "t_abs_start": start.isoformat(timespec="milliseconds"),
            "filename_time_match": (nt == start) if nt else None,
            "duration_s": duration_s,
            "first_tick_ms": int(t_rel[0]) if n else None,
            "last_tick_ms": int(t_rel[-1]) if n else None,
            "ts_monotonic": bool((d >= 0).all()) if n > 1 else True,
            "median_dt_ms": float(np.median(d)) if n > 1 else None,
            "max_gap_ms": int(d.max()) if n > 1 else 0,
        },
        "counts": {"records": n, "invalid": int((~valid).sum()), "checksum_fail": int((~sum_ok).sum()),
                   "events": n_event, "other_types": int(n_all - n - n_event)},
        "summary": {
            "hv_on": hv_on,
            "voltage_max_v": float(v.max()) if n else None,
            "current_max_a": float(i.max()) if n else None,
            "current_min_a": float(i.min()) if n else None,
            "discharge_wh": round(dis_wh, 1),
            "charge_wh": round(chg_wh, 1),
            "lv_voltage_median_v": round(float(np.median(lv)), 2) if n else None,
            "temp_max_c": round(float(temp.max()), 1) if n else None,
        },
        "caveats": [
            "절대시각은 사람이 맞춘 RTC 다. 다른 장비와 섞을 때는 derived/clock_offsets.csv 의 차이를 더한다",
            "전원을 켠 뒤 약 0.55 s 동안 전압·전류 영점을 잡는다. 그때 HV 가 걸려 있었으면 값이 치우친다",
            "lv_voltage_v 는 전원이 꺼지는 마지막 0.1 초쯤에 4 ~ 9 V 로 떨어진다 (정상)",
        ],
        "perf": {"convert_sec": round(time.perf_counter() - t0, 3), "input_bytes": len(data),
                 "output_bytes": dir_bytes(out)},
        "converter_version": __version__,
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    write_json(meta, out / "meta.json")

    return {
        "recording_id": rec_id,
        "logger": "energymeter",
        "device_id": head["device_id"],
        "t_abs_start": start.isoformat(timespec="milliseconds"),
        "duration_s": round(duration_s, 1),
        "last_tick_ms": meta["time"]["last_tick_ms"],
        "records": n,
        "checksum_fail": meta["counts"]["checksum_fail"],
        "hv_on": hv_on,
        "voltage_max_v": meta["summary"]["voltage_max_v"],
        "current_max_a": meta["summary"]["current_max_a"],
        "discharge_wh": meta["summary"]["discharge_wh"],
        "charge_wh": meta["summary"]["charge_wh"],
        "source_file": meta["source"]["file"],
    }


def convert_event(event_dir: Path) -> list[dict]:
    """이벤트 폴더의 raw_energymeter/*.log 전부 변환. 헤더만 있는 빈 파일은 건너뛴다."""
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    rows = []
    for src in sorted((event_dir / "raw_energymeter").glob("*.log")):
        if src.stat().st_size <= HEADER_SIZE:
            print(f"  {src.name[:23]}  빈 파일 (헤더만) - 건너뜀")
            continue
        row = convert_file(src, out_root)
        rows.append(row)
        if row["hv_on"]:
            print(f"  {row['recording_id']}  {row['duration_s']:>7.1f} s  "
                  f"V≤{row['voltage_max_v']:5.1f}  A≤{row['current_max_a']:5.1f}  "
                  f"방전 {row['discharge_wh']:7.1f} Wh  회생 {row['charge_wh']:5.1f} Wh")
    write_index(rows, out_root / "recordings_energymeter.csv")
    return rows
