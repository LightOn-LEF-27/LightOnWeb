"""LEF-25Evo 자작 STM32 백업 로거(NUCLEO-L432KC) CSV → 공통 포맷, 그리고 다른 장비와 시간 맞추기.

파일 `1.1 DATA_Log<N>.csv` 1개 = 로거 전원 1회 = 레코딩 1개. 번호는 SD 카드에 쌓인 순서다.
`raw_stm32/` 바로 밑의 파일은 `stm32_logNNN`, 하위 폴더에 든 파일은 `stm32_<폴더>_logNNN` 이 된다
(카드를 비우면 번호가 1 부터 다시 시작하므로 묶음마다 하위 폴더에 둔다).

    # CAL SPEED_RPMMAX_INT(0xCE)=0 CURRENT_DEVICE(0xC6)=0 CURRENT_200PC(0xD9)=0     ← 새 형식에만 있다
    TIME,BT_LCV,BT_HTP,...           ← 헤더. 옛 형식 16 열 · 새 형식 29 열
    00:00:01,39347,25,...            ← TIME 은 부팅 기준, **1초 해상도**, 2 ~ 10 행/초

시계 — **TIME 은 참 시간보다 2 % 가까이 느리게 간다**
    참 시간 = f × TIME.
    레코딩마다 조금 다르다 (보드 온도를 타는 RC 발진기의 모습이다 — 펌웨어 소스가 없어 원인은 추정).
    그래서 시간 열이 둘이다.
        t_log_ms   로거가 찍은 TIME 그대로 (frames 에만)
        t_rel_ms   바로잡은 경과 시간 = f × (TIME + 같은 초 안에서의 순서). 같은 초의 행은 그 초 안에 고르게 편다
    f 는 `align_event` 가 다른 장비와 파형을 맞춰 레코딩마다 재고, 못 잰 레코딩은 CLOCK_F_PRIOR 를 쓴다
    (meta.json 의 time.clock). 절대시각 t_abs 도 그때 채운다:
        t_quality = "aligned_em"    DC 버스 전압 ↔ 에너지미터 전압. 에너지미터 시계
        t_quality = "aligned_gps"   모터 회전수 ↔ Monolith GPS 속도. Monolith 시계
        t_quality = "none"          못 맞춤. t_abs 없음

로거 함정
    - 새 형식 첫 줄 CAL 값이 전부 0 이라 로거가 계산한 환산 열(_C, _RPM, _A, _V)이 비어 있다 → raw 로 우리가 환산
    - BT_VOL · BT_CRT 는 **하위 1바이트만** 남아 있다 (0~255, 원래는 0.1 V · 0.1 A 단위 16비트).
      윗자리는 다른 열로 되살린다: 전압은 DC 버스 전압에 가장 가까운 값, 전류는 BT_POW ÷ 전압에 가장 가까운 값
      → bms 표의 pack_v · current_a (에너지미터와 대조해 확인)
    - BT_POW 는 부호 있는 16비트, 0.1 kW 단위의 팩 전력이다 (65526 = −1.0 kW). 에너지미터 전력과 같은 모양으로
      오르내린다 → bms 표의 power_kw
    - HV_RPM_MAX 는 로거에 박힌 상수다 (옛 형식 6000, 새 형식 6500). 컨트롤러 설정값(N-100)을 읽은 것이 아니다
      → `rpm` 의 절댓값은 그 가정만큼 틀릴 수 있다. 차속으로 바꿀 때는 GPS 로 구한 환산값을 쓴다
    - 첫 행 다음에 SD 카드에 쌓인 파일 수에 따라 1 ~ 14 초 공백이 있다 (이슈 DAQ-2)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__, sync
from .lef26 import BAMOCAR_VDC_V_PER_NUM, igbt_temp_c
from .writer import TZ, dir_bytes, fresh_dir, t_abs_array, write_index, write_json, write_parquet

NAME_RE = re.compile(r"DATA_Log(\d+)\.csv$")
CELL_V_PER_NUM = 1e-4       # BT_LCV 40655 → 4.0655 V
RPM_FULL_SCALE = 32767      # HV_RPM 는 N-100 % = 32767 (LEF-26 0x30 과 같다)
KMH_PER_RPM = 0.0197        # 파형을 맞출 때 '달렸나' 를 가리는 용도로만 쓴다 (GPS 로 구한 어림값)
POWER_KW_PER_NUM = 0.1      # BT_POW (부호 있는 16비트)
PACK_V_PER_NUM = 0.1        # BT_VOL (하위 바이트)
PACK_A_PER_NUM = 0.1        # BT_CRT (하위 바이트)
BMS_MINUS_DCBUS_V = -1.27   # BMS 팩 전압 − BAMOCAR DC 버스 전압. 윗자리를 고를 때의 기준 (±12.8 V 안이면 되므로 대강이면 된다)
HV_LIVE_V = 50.0            # DC 버스가 이 전압을 넘으면 HV 가 걸린 것으로 본다

# 모터(고정자 권선) 온도 센서 KTY81/210 — BAMOCAR 0x49 의 원시 Num ↔ ℃.
#   출처: UniTek "BAMOCAR - Temperature and VdcBus Tables" 4/4 쪽 KTY81-2xx 열. 25Evo 계기판 펌웨어
#   (GitHub LIGHT-ON-APPS/LIGTH-ON-DISPLAY, src/ap/telemetry/telemetry.c) 에 옮겨 적힌 값을 가져왔다 — 원 매뉴얼은 직접 못 봤다.
#   같은 소스의 IGBT 표가 우리가 26팀에게 받은 표(lef26.IGBT_TEMP_RAW)와 한 점(−10 ℃, 2 Num)만 빼고 같다.
MOTOR_TEMP_RAW = np.array([
    7414, 7687, 7962, 8240, 8520, 8802, 9085, 9369, 9654, 9939, 10225, 10510, 10795,
    11080, 11364, 11646, 11927, 12207, 12485, 12762, 13036, 13308, 13578, 13846, 14111, 14373,
    14633, 14890, 15144, 15391, 15628, 15852, 16061, 16251, 16421, 16569, 16692, 16789, 16857,
], dtype=float)
MOTOR_TEMP_C = np.arange(-35.0, 155.1, 5.0)

# 참 시간 = 이 값 × 로거 TIME. 파형으로 잰 레코딩들의 가운데. 못 잰 레코딩에 쓴다
CLOCK_F_PRIOR = 1.0185
CLOCK_F_RANGE = (1.012, 1.024)     # 이 밖의 값이 나오면 맞춤이 틀린 것으로 본다

KST = dt.timezone(dt.timedelta(hours=9))


# ---------------------------------------------------------------- 파싱

def read_csv(path: Path) -> tuple[str, pd.DataFrame]:
    """(CAL 주석 줄, 표). 주석 줄이 없으면 빈 문자열."""
    first = path.open(encoding="utf-8", errors="replace").readline().strip()
    cal = first[1:].strip() if first.startswith("#") else ""
    df = pd.read_csv(path, comment="#")
    return cal, df


def time_to_ms(s: pd.Series) -> np.ndarray:
    """'HH:MM:SS' → ms. 로거가 찍은 그대로다 — 1초 해상도라 같은 초의 행은 같은 값이다."""
    p = s.astype(str).str.split(":", expand=True).astype(int)
    return ((p[0] * 3600 + p[1] * 60 + p[2]) * 1000).to_numpy(np.int64)


def spread_within_second(t_log_ms: np.ndarray) -> np.ndarray:
    """같은 초에 찍힌 행을 그 초 안에 고르게 편다 → 초 단위 실수.

    n 개가 같은 초면 k 번째 행은 (초 + (k + 0.5) / n). 행은 적힌 순서대로이므로 순서는 믿을 수 있고,
    초 안에서의 정확한 위치만 모른다 (오차 ±0.5 s 안쪽).
    """
    sec = np.asarray(t_log_ms, np.int64) // 1000
    if not len(sec):
        return np.zeros(0)
    start = np.flatnonzero(np.r_[True, sec[1:] != sec[:-1]])          # 같은 초가 이어진 묶음의 첫 행
    size = np.diff(np.r_[start, len(sec)])
    k = np.arange(len(sec)) - np.repeat(start, size)
    return sec + (k + 0.5) / np.repeat(size, size)


def true_rel_ms(t_log_ms: np.ndarray, f: float) -> np.ndarray:
    """로거 TIME → 바로잡은 경과 시간(ms). 참 시간 = f × 로거 시간."""
    return np.round(spread_within_second(t_log_ms) * f * 1000).astype(np.int64)


def motor_temp_c(raw: np.ndarray) -> np.ndarray:
    """BAMOCAR 모터 온도 원시값 → ℃ (KTY81/210 표를 직선 보간). 0 이하(값 없음)와 표 밖은 NaN."""
    raw = np.asarray(raw, float)
    out = np.interp(raw, MOTOR_TEMP_RAW, MOTOR_TEMP_C, left=np.nan, right=np.nan)
    return np.where(raw > 0, out, np.nan)


def signed16(x: np.ndarray) -> np.ndarray:
    """u16 으로 적힌 값을 부호 있는 16비트로 (65526 → −10). NaN 은 그대로."""
    x = np.asarray(x, float)
    return np.where(x > 32767, x - 65536, x)


def restore_pack(bt_vol, bt_crt, bt_pow, vdc_v) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """하위 바이트만 남은 BT_VOL · BT_CRT 의 윗자리를 되살린다 → (팩 전압 V, 팩 전류 A, 팩 전력 kW).

    전압: (BT_VOL + 256·k) × 0.1 V 중 DC 버스 전압 + BMS_MINUS_DCBUS_V 에 가장 가까운 k. HV 가 꺼져 있으면(기준이 없으면) NaN
    전류: (BT_CRT + 256·k) × 0.1 A 중 전력 ÷ 전압에 가장 가까운 k. 전압을 모르면, 전력이 0 일 때만 0 근처로 본다
    후보 사이 간격이 25.6 V · 25.6 A 라 기준이 그 절반(12.8)보다 정확하기만 하면 된다 — 전력 분해능 0.1 kW 는 전류 0.4 A 쯤이다.

    급가속 때는 전압이 0.3 초에 30 V 씩 처져서, 조금 다른 순간에 읽힌 DC 버스 전압이 12.8 V 넘게 다를 수 있다 (드물다).
    세 값은 BMS 가 같이 낸 것이라 전압 × 전류 = 전력이 맞아야 하므로, 이웃 후보(k ± 1)가 이 조건을 뚜렷이(0.15 kW 넘게)
    더 잘 맞추면 그쪽을 고른다.
    """
    bt_vol, bt_crt, vdc_v = (np.asarray(a, float) for a in (bt_vol, bt_crt, vdc_v))
    power = signed16(bt_pow) * POWER_KW_PER_NUM

    def current_for(v):
        ref_a = np.where(np.isfinite(v), power * 1000 / v, np.where(power == 0, 0.0, np.nan))
        k = np.round((ref_a / PACK_A_PER_NUM - bt_crt) / 256)
        return (bt_crt + 256 * k) * PACK_A_PER_NUM

    with np.errstate(invalid="ignore", divide="ignore"):
        ref_v = np.where(vdc_v > HV_LIVE_V, vdc_v + BMS_MINUS_DCBUS_V, np.nan)
        k0 = np.round((ref_v / PACK_V_PER_NUM - bt_vol) / 256)
        pack_v = (bt_vol + 256 * k0) * PACK_V_PER_NUM
        current = current_for(pack_v)
        miss = np.abs(pack_v * current / 1000 - power)
        for dk in (-1, 1):
            v = (bt_vol + 256 * (k0 + dk)) * PACK_V_PER_NUM
            i = current_for(v)
            m = np.abs(v * i / 1000 - power)
            better = m < miss - 0.15
            pack_v, current, miss = np.where(better, v, pack_v), np.where(better, i, current), np.where(better, m, miss)
    return pack_v, current, power


def log_no(path: Path) -> int:
    m = NAME_RE.search(path.name)
    if not m:
        raise ValueError(f"STM32 로그 파일명이 아니다: {path.name}")
    return int(m.group(1))


def recording_id(n: int, group: str = "") -> str:
    return f"stm32_{group}_log{n:03d}" if group else f"stm32_log{n:03d}"


def parse_recording_id(rec_id: str) -> tuple[str, int]:
    """'stm32_day3_log008' → ('day3', 8).  'stm32_log044' → ('', 44)."""
    m = re.fullmatch(r"stm32_(?:(.+)_)?log(\d+)", rec_id)
    if not m:
        raise ValueError(f"STM32 레코딩 이름이 아니다: {rec_id}")
    return m.group(1) or "", int(m.group(2))


def source_files(event_dir: Path) -> list[tuple[str, Path]]:
    """(묶음 이름, 파일) 목록. raw_stm32/ 바로 밑은 묶음 이름이 빈 문자열이다."""
    root = event_dir / "raw_stm32"
    out = [("", p) for p in sorted(root.glob("*.csv"), key=log_no)]
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        out += [(sub.name, p) for p in sorted(sub.glob("*.csv"), key=log_no)]
    return out


# ---------------------------------------------------------------- 변환

def _nanmax(x: np.ndarray) -> float | None:
    x = x[np.isfinite(x)]
    return float(x.max()) if len(x) else None


def convert_file(src: Path, out_root: Path, group: str = "") -> dict:
    t0 = time.perf_counter()
    n_log = log_no(src)
    rec_id = recording_id(n_log, group)
    out = fresh_dir(out_root / rec_id)
    cal, df = read_csv(src)
    n = len(df)
    t_log = time_to_ms(df["TIME"]) if n else np.zeros(0, np.int64)
    t_rel = true_rel_ms(t_log, CLOCK_F_PRIOR)
    null_abs = t_abs_array(np.zeros(n, np.int64), mask=np.ones(n, bool))
    base = {"t_rel_ms": pa.array(t_rel), "t_abs": null_abs,
            "t_quality": pa.array(["none"] * n, type=pa.string())}

    # 원본 전 열 보관 (해석 전)
    frames = pa.Table.from_pandas(df, preserve_index=False)
    frames = frames.add_column(0, "t_rel_ms", pa.array(t_rel))
    frames = frames.add_column(1, "t_log_ms", pa.array(t_log))
    write_parquet(frames, out / "frames.parquet")

    col = lambda c: df[c].to_numpy(dtype=float) if c in df else np.full(n, np.nan)
    soc = col("BT_SOC")
    bms_live = (soc > 0) | (col("BT_LCV") > 0)          # BMS 미연결 시 전부 0
    dcbus = col("HV_DCBUS_RAW")
    pack_v, pack_a, pack_kw = restore_pack(col("BT_VOL"), col("BT_CRT"), col("BT_POW"), dcbus * BAMOCAR_VDC_V_PER_NUM)
    bms = pa.table({**base,
                    "cell_min_v": pa.array(np.where(bms_live, col("BT_LCV") * CELL_V_PER_NUM, np.nan)),
                    "temp_max_c": pa.array(np.where(bms_live, col("BT_HTP"), np.nan)),
                    "temp_min_c": pa.array(np.where(bms_live, col("BT_LTP"), np.nan)),
                    "soc_pct": pa.array(np.where(bms_live, soc, np.nan)),
                    "pack_v": pa.array(np.where(bms_live, pack_v, np.nan)),
                    "current_a": pa.array(np.where(bms_live, pack_a, np.nan)),
                    "power_kw": pa.array(np.where(bms_live, pack_kw, np.nan))})
    write_parquet(bms, out / "bms.parquet")

    n_max = col("HV_RPM_MAX")
    igbt_raw = col("HV_IGBT_TEMP")
    rpm = col("HV_RPM") / RPM_FULL_SCALE * n_max
    motor = pa.table({**base,
                      "rpm": pa.array(rpm),
                      "vdc_v": pa.array(dcbus * BAMOCAR_VDC_V_PER_NUM),
                      "t_igbt_c": pa.array(igbt_temp_c(igbt_raw)),
                      "t_motor_c": pa.array(motor_temp_c(col("HV_TEMP"))),
                      "rpm_raw": pa.array(col("HV_RPM")),
                      "t_motor_raw": pa.array(col("HV_TEMP")),
                      "t_igbt_raw": pa.array(igbt_raw),
                      "vdc_raw": pa.array(dcbus)})
    write_parquet(motor, out / "motor.parquet")

    duration_s = float((t_rel[-1] - t_rel[0]) / 1000) if n else 0.0
    soc_live = soc[bms_live]
    is_new = "HV_DCBUS_RAW" in df
    meta = {
        "car": "LEF-25evo",
        "event": out_root.parent.name,
        "recording_id": rec_id,
        "source": {
            "logger": "자작 STM32 백업 로거 (NUCLEO-L432KC)",
            "file": str(src.relative_to(out_root.parent)).replace("\\", "/"),
            "file_bytes": src.stat().st_size,
            "group": group,
            "format": "new (29 열)" if is_new else "old (16 열)",
            "cal_line": cal,
        },
        "time": {
            "basis": "t_rel_ms = clock.f × (TIME + 같은 초 안의 순서). TIME 은 부팅 기준 1 s 해상도. 로거 그대로는 frames.t_log_ms",
            "tz": TZ,
            "resolution_ms": 1000,
            "duration_s": duration_s,
            "duration_log_s": float((t_log[-1] - t_log[0]) / 1000) if n else 0.0,
            "clock": {"f": CLOCK_F_PRIOR, "basis": "prior",
                      "note": "참 시간 = f × 로거 TIME. prior = 잰 레코딩들의 가운데값 (레코딩마다 조금 다르다)"},
            "t_abs_start": None,
            "alignment": None,
        },
        "counts": {"rows": n, "bms_live_rows": int(bms_live.sum())},
        "summary": {
            "soc_start_pct": float(soc_live[0]) if len(soc_live) else None,
            "soc_end_pct": float(soc_live[-1]) if len(soc_live) else None,
            "rpm_abs_max": _nanmax(np.abs(rpm)),
            "vdc_max_v": _nanmax(dcbus * BAMOCAR_VDC_V_PER_NUM),
        },
        "caveats": [
            "로거 시계가 2 % 가까이 느리다. t_rel_ms 는 바로잡은 값이고, 맞춤(align)이 안 된 레코딩은 사전값이라 1000 s 에 ±1.5 s 틀릴 수 있다",
            "로거가 계산한 환산 열(_C/_RPM/_A/_V)은 CAL 값이 0 이라 비어 있다. raw 로 다시 환산했다",
            "pack_v · current_a 는 하위 바이트만 남은 BT_VOL · BT_CRT 의 윗자리를 DC 버스 전압 · BT_POW 로 되살린 값이다. "
            "HV 가 꺼져 있으면 pack_v 는 null",
            "power_kw (BT_POW) 는 BMS 가 낸 값이고 에너지미터보다 조금 크다. 행이 초당 2 ~ 6 개라 급한 변화는 뭉개진다",
            "t_igbt_c 는 BAMOCAR NTC 표 (LEF-26 과 같은 표. 25Evo 계기판 펌웨어의 표와도 같다)",
            "t_motor_c 는 KTY81/210 표로 환산 (EMRAX 228 권선 센서). 표 출처는 25Evo 계기판 펌웨어 — 원 매뉴얼은 직접 못 봤다",
            "rpm 부호는 회전 방향. 전진이 음수. 크기는 로거 상수 HV_RPM_MAX(옛 형식 6000 · 새 형식 6500)로 환산한 것이다",
        ] + ([] if is_new else ["옛 형식이라 DC 버스 전압·IGBT 온도 열이 없다 (vdc_v · t_igbt_c 는 전부 null)"]),
        "perf": {"convert_sec": round(time.perf_counter() - t0, 3), "input_bytes": src.stat().st_size,
                 "output_bytes": dir_bytes(out)},
        "converter_version": __version__,
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    write_json(meta, out / "meta.json")
    return {"recording_id": rec_id, "logger": "stm32", "group": group, "log_no": n_log, "rows": n,
            "format": "new" if is_new else "old",
            "duration_s": round(duration_s, 1), "soc_start_pct": meta["summary"]["soc_start_pct"],
            "soc_end_pct": meta["summary"]["soc_end_pct"], "rpm_abs_max": meta["summary"]["rpm_abs_max"],
            "vdc_max_v": meta["summary"]["vdc_max_v"],
            "clock_f": CLOCK_F_PRIOR, "clock_basis": "prior", "t_abs_start": None,
            "source_file": meta["source"]["file"]}


def convert_event(event_dir: Path) -> list[dict]:
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    rows = [convert_file(src, out_root, group) for group, src in source_files(event_dir)]
    write_index(rows, out_root / "recordings_stm32.csv")
    return rows


# ---------------------------------------------------------------- 시간 맞추기: 거친 단계 (전압 절댓값)

def per_second(t_ms: np.ndarray, x: np.ndarray, t0_ms: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """초 단위 평균. (초 인덱스, 평균값)."""
    sec = (t_ms - t0_ms) // 1000
    ok = np.isfinite(x)
    s = pd.Series(x[ok]).groupby(sec[ok]).mean()
    return s.index.to_numpy(np.int64), s.to_numpy(float)


def lag_scan(ref_s: np.ndarray, ref: np.ndarray, sig_s: np.ndarray, sig: np.ndarray,
             min_overlap: int = 60, min_frac: float = 0.8):
    """sig 를 ref 위로 1 초씩 옮기며 (lag, 겹친 초 수, 상관, 차이 중앙값, |차이| 중앙값 함수) 를 낸다.

    lag = sig 의 0 초가 ref 의 몇 초에 해당하는지.
    sig 의 min_frac 이상이 겹쳐야 후보로 본다 - 짧게 겹친 우연한 일치를 막는다.
    """
    if len(ref) < min_overlap or len(sig) < min_overlap:
        return
    r0 = int(ref_s.min())
    dense = np.full(int(ref_s.max()) - r0 + 1, np.nan)
    dense[ref_s - r0] = ref
    need = max(min_overlap, int(min_frac * len(sig)))
    for lag in range(r0 - int(sig_s.min()) - 30, int(ref_s.max()) - int(sig_s.min()) + 1):
        idx = sig_s + lag - r0
        inside = (idx >= 0) & (idx < len(dense))
        r = np.full(len(sig), np.nan)
        r[inside] = dense[idx[inside]]
        ok = np.isfinite(r)
        k = int(ok.sum())
        if k < need:
            continue
        a, b = r[ok], sig[ok]
        c = float(np.corrcoef(a, b)[0, 1]) if a.std() > 1e-6 and b.std() > 1e-6 else float("nan")
        d = b - a
        yield lag, k, c, float(np.median(d)), d


def best_match(ref_s, ref, sig_s, sig, offset: float, window_s: int = 60):
    """두 단계로 맞춘다. 반환 (lag, 겹침, 상관, 오차 중앙값).

    1. |sig - offset - ref| 중앙값이 가장 작은 lag - 전압 **절댓값**(= SOC 수준)으로 어느 구간인지 고른다
    2. 그 lag ± window_s 안에서 상관이 가장 큰 lag - 전압이 처지는 **순간**으로 시점을 다듬는다

    절댓값은 천천히 변해서 수십 초 어긋나도 오차가 거의 같고, 상관은 평평한 팩 전압끼리 다 비슷해
    엉뚱한 구간을 고른다. 각자 잘하는 것만 시킨다.
    """
    scan = [(lag, k, c, float(np.median(np.abs(d - offset)))) for lag, k, c, _, d in
            lag_scan(ref_s, ref, sig_s, sig)]
    if not scan:
        return None
    coarse = min(scan, key=lambda x: x[3])
    near = [x for x in scan if abs(x[0] - coarse[0]) <= window_s and np.isfinite(x[2])]
    return max(near, key=lambda x: x[2]) if near else coarse


def estimate_offset(pairs) -> float:
    """두 장비의 전압 차이(STM32 - 에너지미터). 길게 겹친 확실한 쌍(상관 최대 기준)의 중앙값.

    같은 팩을 재므로 차이는 일정해야 한다.
    """
    meds = []
    for ref_s, ref, sig_s, sig in pairs:
        best = None
        for lag, k, c, med, _ in lag_scan(ref_s, ref, sig_s, sig):
            if k >= 600 and np.isfinite(c) and (best is None or c > best[0]):
                best = (c, med)
        if best and best[0] >= 0.8:
            meds.append(best[1])
    return float(np.median(meds)) if meds else 0.0


# ---------------------------------------------------------------- 시간 맞추기: 고운 단계 (창 맞추기 → 시계 직선)

STEP_S = 0.5
MIN_WINDOWS = 5


def _filled(k0: int, g: np.ndarray) -> tuple[int, np.ndarray]:
    """격자의 짧은 빈칸(5 칸까지)을 이어 붙인다. STM32 는 행이 성겨 0.5 s 칸이 자주 빈다."""
    return k0, pd.Series(g).interpolate(limit=5, limit_area="inside").to_numpy()


def fit_clock_line(ref_t_s, ref_x, sig_t_s, sig_x, *, lag0: float | None, slack_s: float = 30.0,
                   win_s: float = 40.0, hop_s: float = 10.0, min_std: float = 1.0, min_corr: float = 0.8,
                   tol_s: float = 1.0) -> dict | None:
    """sig(STM32, 이미 사전값으로 편 시간) 의 조각을 ref(기준 장비)에서 찾아 직선을 맞춘다: ref 시각 = a + g × sig 시각.

    lag0 = sig 의 0 초가 ref 의 몇 초쯤인지 (그 앞뒤 slack_s 만 본다). None 이면 ref 전체에서 찾는다.
    반환: a, g, n(직선에 모인 창 수), n_windows(상관이 min_corr 넘은 창 수), rms_s, span_s. 못 맞추면 None.
    """
    ks, gs = _filled(*sync.grid(sig_t_s, sig_x, STEP_S))
    kr, gr = _filled(*sync.grid(ref_t_s, ref_x, STEP_S))
    if len(gs) < win_s / STEP_S or len(gr) < win_s / STEP_S:
        return None
    expect = slack = None
    if lag0 is not None:
        expect, slack = (lambda t: t + lag0), (lambda t: np.full(len(t), slack_s))
    m = sync.match_windows(ks, gs, kr, gr, step=STEP_S, win_s=win_s, hop_s=hop_s, min_std=min_std,
                           expect=expect, slack=slack)
    good = m[m["corr"] >= min_corr]
    if len(good) < MIN_WINDOWS:
        return None
    fit = sync.fit_clock(good["ref_s"], good["sig_s"], slopes=np.arange(0.990, 1.0101, 0.0002),
                         tol_s=tol_s, min_inliers=MIN_WINDOWS)
    if fit is None:
        return None
    return {"a": fit["a"], "g": fit["f"], "n": fit["n"], "n_windows": int(len(good)),
            "rms_s": fit["rms_s"], "span_s": fit["span_s"]}


def _clock_f(g: float, span_s: float) -> tuple[float, bool]:
    """창 맞추기가 준 기울기 g 를 쓸지 정한다. (f, 잰 값인가)

    맞은 창들이 덮은 시간이 짧으면 기울기를 믿을 수 없다 (창 위치 오차 0.3 s ÷ 200 s = 0.15 %) → 사전값을 쓴다.
    """
    f = CLOCK_F_PRIOR * g
    if span_s >= 300 and CLOCK_F_RANGE[0] <= f <= CLOCK_F_RANGE[1]:
        return f, True
    return CLOCK_F_PRIOR, False


# ---------------------------------------------------------------- 시간 맞추기: 이벤트 단위

def _stm32_tracks(norm: Path) -> list[dict]:
    out = []
    for p in sorted(norm.glob("stm32_*log*")):
        group, n = parse_recording_id(p.name)
        mt = pq.read_table(p / "motor.parquet", columns=["t_rel_ms", "vdc_v", "rpm"]).to_pandas()
        t = mt["t_rel_ms"].to_numpy(np.int64)
        out.append({"path": p, "group": group, "log_no": n, "t_s": t / 1000.0,
                    "vdc": mt["vdc_v"].to_numpy(float), "kmh": np.abs(mt["rpm"].to_numpy(float)) * KMH_PER_RPM,
                    "duration_s": float((t[-1] - t[0]) / 1000) if len(t) else 0.0})
    return out


def _em_tracks(norm: Path) -> list[dict]:
    out = []
    for p in sorted(norm.glob("*_em")):
        m = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        if not m["summary"]["hv_on"]:
            continue
        e = pq.read_table(p / "energy.parquet", columns=["t_rel_ms", "voltage_v", "valid"]).to_pandas()
        e = e[e["valid"] & (e["voltage_v"] > 50)]
        s, v = per_second(e["t_rel_ms"].to_numpy(np.int64), e["voltage_v"].to_numpy(float))
        out.append({"name": p.name, "start": dt.datetime.fromisoformat(m["time"]["t_abs_start"]),
                    "sec": s, "v_sec": v, "t_s": e["t_rel_ms"].to_numpy(np.int64) / 1000.0,
                    "v": e["voltage_v"].to_numpy(float)})
    return out


def _gps_tracks(norm: Path, min_kmh: float = 20.0, min_fix_s: float = 60.0) -> list[dict]:
    """달린 흔적이 있는 Monolith 레코딩의 GPS 속도."""
    out = []
    for p in sorted(q for q in norm.iterdir() if q.is_dir() and (q / "gps.parquet").exists()
                    and not q.name.startswith("stm32_")):
        m = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        if "Monolith" not in m.get("source", {}).get("logger", ""):
            continue
        g = pq.read_table(p / "gps.parquet", columns=["t_rel_ms", "speed_kmh", "fix"]).to_pandas()
        g = g[g["fix"]]
        if len(g) < min_fix_s * 5 or g["speed_kmh"].max() < min_kmh:
            continue
        out.append({"name": p.name, "start": dt.datetime.fromisoformat(m["time"]["t_abs_start"]),
                    "t_s": g["t_rel_ms"].to_numpy(np.int64) / 1000.0, "kmh": g["speed_kmh"].to_numpy(float)})
    return out


def _align_em(tr: dict, ems: list[dict], offset: float, max_err_v: float, min_margin_v: float,
              min_vdc_std: float) -> dict:
    """STM32 레코딩 하나를 에너지미터 전압에 맞춘다. 반환 dict 의 ok 가 True 면 쓸 수 있다.

    에너지미터 레코딩마다 (1) 전압 절댓값으로 자리를 잡고 (2) 그 근처에서 창을 맞춰 직선을 구한다.
    창 맞춤까지 된 것만 후보로 남긴다 — 평평한 팩 전압은 절댓값만으로는 아무 데나 맞아 보인다.
    (1 초 평균끼리의 상관은 이제 기준으로 쓰지 않는다. 전압이 처지는 순간이 1 ~ 2 초라 시각이 0.5 초만 어긋나도
    상관이 0.95 → 0.3 으로 떨어진다. 정수 초 단위로 옮겨 보는 거친 단계에서는 믿을 수 없는 값이다.)
    """
    row = {"em_recording": None, "lag_s": None, "overlap_s": None, "corr": None, "err_v": None,
           "runner_up_err_v": None, "em_a_s": None, "em_f": None, "em_windows": None, "em_rms_s": None,
           "em_span_s": None, "ok": False, "note": ""}
    live = tr["vdc"] > 50
    s, v = per_second((tr["t_s"][live] * 1000).astype(np.int64), tr["vdc"][live])
    if len(v) < 60 or np.std(v) < min_vdc_std:
        row["note"] = "전압 파형 없음 (HV 꺼짐·정지)"
        return row
    cands, nearest = [], None
    for em in ems:
        b = best_match(em["sec"], em["v_sec"], s, v, offset)
        if b is None:
            continue
        if nearest is None or b[3] < nearest[0]:
            nearest = (b[3], em, b)
        if b[3] > 3 * max_err_v:
            continue
        fit = fit_clock_line(em["t_s"], em["v"], tr["t_s"][live], tr["vdc"][live], lag0=float(b[0]),
                             slack_s=15.0 + 0.004 * tr["duration_s"], min_std=1.0, min_corr=0.8)
        if fit is not None:
            cands.append((b[3], em, b, fit))
    if not cands:
        if nearest is not None and nearest[0] <= 3 * max_err_v:        # 전압 수준은 맞는 후보가 있었다 → 진단용으로 남긴다
            err, em, (lag, k, c, _) = nearest
            row.update(em_recording=em["name"], lag_s=lag, overlap_s=k, corr=round(c, 3), err_v=round(err, 2))
            row["note"] = "전압이 처지는 구간이 부족해 창 맞춤 실패"
        else:
            row["note"] = "전압이 맞는 에너지미터 레코딩 없음"
        return row
    cands.sort(key=lambda x: x[0])
    err, em, (lag, k, c, _), fit = cands[0]
    runner = cands[1][0] if len(cands) > 1 else None
    f, measured = _clock_f(fit["g"], fit["span_s"])
    a = fit["a"] if measured else fit["a"] + (fit["g"] - 1.0) * float(np.median(tr["t_s"][live]))
    row.update(em_recording=em["name"], lag_s=lag, overlap_s=k, corr=round(c, 3), err_v=round(err, 2),
               runner_up_err_v=round(runner, 2) if runner is not None else None,
               em_a_s=round(a, 2), em_f=round(f, 5), em_windows=fit["n"], em_rms_s=round(fit["rms_s"], 2),
               em_span_s=round(fit["span_s"]))
    margin_ok = runner is None or runner - err >= min_margin_v
    if err > max_err_v or not margin_ok:
        row["note"] = ("전압 오차 큼" if err > max_err_v else "") + (" 2위와 차이 작음" if not margin_ok else "")
        return row
    row.update(ok=True, _start=em["start"], _measured=measured)
    return row


def _align_gps(tr: dict, gps: list[dict], min_kmh: float = 20.0) -> dict:
    """STM32 레코딩 하나를 Monolith GPS 속도에 맞춘다 (모터 회전수 ↔ 차속)."""
    row = {"gps_recording": None, "gps_a_s": None, "gps_f": None, "gps_windows": None,
           "gps_runner_up_windows": None, "gps_rms_s": None, "gps_span_s": None, "ok": False, "note": ""}
    ok = np.isfinite(tr["kmh"])
    if tr["duration_s"] < 60 or not ok.any() or np.nanmax(tr["kmh"]) < min_kmh:
        row["note"] = "달린 구간 없음"
        return row
    fits = []
    for g in gps:
        # 1) 같은 전원 주기라고 보고 (두 로거가 같이 켜진다) 좁게  2) 안 되면 레코딩 전체에서
        fit = fit_clock_line(g["t_s"], g["kmh"], tr["t_s"][ok], tr["kmh"][ok], lag0=0.0, slack_s=60.0,
                             min_std=4.0, min_corr=0.9, tol_s=0.7)
        if fit is None:
            fit = fit_clock_line(g["t_s"], g["kmh"], tr["t_s"][ok], tr["kmh"][ok], lag0=None,
                                 min_std=4.0, min_corr=0.9, tol_s=0.7)
            if fit is not None and fit["n"] < 2 * MIN_WINDOWS:
                fit = None                          # 넓게 찾은 것은 근거가 더 많아야 한다
        if fit is not None:
            fits.append((fit["n"], g, fit))
    if not fits:
        row["note"] = "속도 파형이 맞는 Monolith 레코딩 없음"
        return row
    fits.sort(key=lambda x: -x[0])
    n, g, fit = fits[0]
    runner = fits[1][0] if len(fits) > 1 else 0
    f, measured = _clock_f(fit["g"], fit["span_s"])
    a = fit["a"] if measured else fit["a"] + (fit["g"] - 1.0) * float(np.median(tr["t_s"][ok]))
    row.update(gps_recording=g["name"], gps_a_s=round(a, 2), gps_f=round(f, 5), gps_windows=n,
               gps_runner_up_windows=runner, gps_rms_s=round(fit["rms_s"], 2), gps_span_s=round(fit["span_s"]))
    if n < 2 * runner or fit["n"] < 0.5 * fit["n_windows"]:
        row["note"] = "속도 파형이 다른 레코딩에도 비슷하게 맞음"
        return row
    row.update(ok=True, _start=g["start"], _measured=measured)
    return row


def align_event(event_dir: Path, clock: str = "em", max_err_v: float = 2.0, min_margin_v: float = 0.5,
                min_vdc_std: float = 1.0) -> pd.DataFrame:
    """STM32 레코딩마다 다른 장비와 파형을 맞춰 시계 속도(f)와 시작 시각을 찾는다. `convert_event` 직후에 부른다.

    기준 장비 둘
      em   DC 버스 전압 ↔ 에너지미터 전압.  거친 단계(전압 절댓값으로 어느 구간인지) → 고운 단계(창 맞추기로 직선)
      gps  모터 회전수 ↔ Monolith GPS 속도.  창 맞추기로 직선
    clock = 절대시각을 어느 장비 시계로 채울지 ("em" 또는 "gps"). 한 이벤트 안에서는 하나로 통일해야 섞이지 않는다.
    그 장비로 못 맞춘 레코딩은 다른 장비로 잰 f 만 쓰고 t_abs 는 비워 둔다.

    em 쪽 채택 조건: 창 맞춤 성공, 전압 오차 중앙값 ≤ max_err_v, (창 맞춤까지 된) 2위 레코딩보다 min_margin_v 이상 나음.
    채택된 것끼리 같은 묶음 안에서 로그 번호 순서·시간 겹침을 검사해 충돌하면 둘 다 보류한다.
    결과는 derived/stm32_alignment.csv. em_recording · gps_recording 은 em_ok · gps_ok 가 참일 때만 짝이다 —
    거짓인데 이름이 적혀 있으면 조건을 넘지 못한 후보다 (왜 안 됐는지 보려고 남긴다).
    """
    if clock not in ("em", "gps"):
        raise ValueError("clock 은 'em' 또는 'gps'")
    norm = event_dir / "normalized"
    ems, gps, sigs = _em_tracks(norm), _gps_tracks(norm), _stm32_tracks(norm)

    def volts(tr):
        live = tr["vdc"] > 50
        return per_second((tr["t_s"][live] * 1000).astype(np.int64), tr["vdc"][live])

    long_v = [volts(tr) for tr in sigs if (tr["vdc"] > 50).sum() >= 600]
    offset = estimate_offset([(em["sec"], em["v_sec"], s, v) for em in ems for s, v in long_v
                              if len(s) >= 600 and np.std(v) >= min_vdc_std]) if ems else 0.0

    rows, chosen = [], []
    for tr in sigs:
        e = _align_em(tr, ems, offset, max_err_v, min_margin_v, min_vdc_std) if ems else \
            {"ok": False, "note": "에너지미터 레코딩 없음"}
        g = _align_gps(tr, gps) if gps else {"ok": False, "note": "GPS 레코딩 없음"}
        main, other = (e, g) if clock == "em" else (g, e)
        row = {"recording_id": tr["path"].name, "group": tr["group"], "log_no": tr["log_no"],
               "duration_s": round(tr["duration_s"]), "clock": clock,
               **{k: v for k, v in e.items() if not k.startswith("_") and k not in ("ok", "note")},
               "em_ok": bool(e["ok"]), "offset_v": round(offset, 2),
               **{k: v for k, v in g.items() if not k.startswith("_") and k not in ("ok", "note")},
               "gps_ok": bool(g["ok"]),
               "clock_f": CLOCK_F_PRIOR, "clock_basis": "prior", "t_abs_start": None, "applied": False,
               "note": main.get("note", "")}
        rows.append(row)
        kind = {"em": "em_voltage", "gps": "gps_speed"}
        if main["ok"]:
            key = "em" if clock == "em" else "gps"
            f = main[f"{key}_f"]
            t_start = main["_start"] + dt.timedelta(seconds=main[f"{key}_a_s"])
            row.update(clock_f=f, clock_basis=kind[key] if main["_measured"] else "prior",
                       t_abs_start=t_start.isoformat(timespec="milliseconds"))
            chosen.append((row, tr, t_start, f))
        elif other["ok"] and other["_measured"]:
            key = "gps" if clock == "em" else "em"                # 시계 속도만 빌려 쓴다. t_abs 는 비운다
            row.update(clock_f=other[f"{key}_f"], clock_basis=kind[key])
            _apply(tr["path"], None, row["clock_f"], row)

    # 로거는 하나다. 같은 묶음 안에서 로그 번호 순서대로 시간이 가야 하고 서로 겹치면 안 된다 (5 s 여유)
    bad = set()
    for group in sorted({r["group"] for r, *_ in chosen}):
        seq = sorted((c for c in chosen if c[0]["group"] == group), key=lambda c: c[0]["log_no"])
        for (r1, tr1, t1, f1), (r2, _, t2, _) in zip(seq, seq[1:]):
            dur1 = tr1["duration_s"] * f1 / CLOCK_F_PRIOR
            if t1 + dt.timedelta(seconds=dur1 - 5) > t2:
                bad.update({r1["recording_id"], r2["recording_id"]})
    for row, tr, t_start, f in chosen:
        if row["recording_id"] in bad:
            row.update(note="다른 로그와 시간 순서·겹침 충돌", t_abs_start=None)
            _apply(tr["path"], None, f, row)
            continue
        _apply(tr["path"], t_start, f, row)
        row["applied"] = True

    out = pd.DataFrame(rows)
    (event_dir / "derived").mkdir(exist_ok=True)
    out.to_csv(event_dir / "derived" / "stm32_alignment.csv", index=False, encoding="utf-8-sig")

    idx_path = norm / "recordings_stm32.csv"
    if idx_path.exists() and len(out):
        idx = pd.read_csv(idx_path, encoding="utf-8-sig").drop(columns=["clock_f", "clock_basis", "t_abs_start",
                                                                        "duration_s"], errors="ignore")
        dur = {r["recording_id"]: json.loads((norm / r["recording_id"] / "meta.json").read_text(
            encoding="utf-8"))["time"]["duration_s"] for r in rows}
        add = out[["recording_id", "clock_f", "clock_basis", "t_abs_start"]].assign(
            duration_s=[round(dur[r], 1) for r in out["recording_id"]])
        idx.merge(add, on="recording_id", how="left").to_csv(idx_path, index=False, encoding="utf-8-sig")
    return out


def _apply(rec: Path, t_start: dt.datetime | None, f: float, row: dict) -> None:
    """맞춘 결과를 레코딩에 반영한다: frames·bms·motor 의 t_rel_ms (시계 속도 f), bms·motor 의 t_abs, meta.json.

    t_rel_ms 는 frames.t_log_ms 에서 다시 계산하므로 여러 번 불러도 결과가 같다.
    t_start = STM32 의 0 초가 기준 장비 시계로 언제인가. None 이면 시계 속도만 반영하고 t_abs 는 비운다.
    절대시각 정밀도는 ±0.5 s 안쪽 (TIME 1 s 해상도를 초 안에서 고르게 편 값).
    """
    frames = pq.read_table(rec / "frames.parquet")
    t_rel = true_rel_ms(frames["t_log_ms"].to_numpy(), f)
    write_parquet(frames.set_column(frames.schema.get_field_index("t_rel_ms"), "t_rel_ms", pa.array(t_rel)),
                  rec / "frames.parquet")
    quality = "none" if t_start is None else {"em": "aligned_em", "gps": "aligned_gps"}[row["clock"]]
    for name in ("bms", "motor"):
        path = rec / f"{name}.parquet"
        t = pq.read_table(path)
        t = t.set_column(t.schema.get_field_index("t_rel_ms"), "t_rel_ms", pa.array(t_rel))
        if t_start is not None:
            start_ms = int(round(t_start.timestamp() * 1000))
            t = t.set_column(t.schema.get_field_index("t_abs"), "t_abs", t_abs_array(start_ms + t_rel))
        t = t.set_column(t.schema.get_field_index("t_quality"), "t_quality",
                         pa.array([quality] * len(t_rel), type=pa.string()))
        write_parquet(t, path)
    meta_path = rec / "meta.json"
    m = json.loads(meta_path.read_text(encoding="utf-8"))
    m["time"]["duration_s"] = float((t_rel[-1] - t_rel[0]) / 1000) if len(t_rel) else 0.0
    m["time"]["clock"] = {"f": f, "basis": row["clock_basis"],
                          "note": "참 시간 = f × 로거 TIME. em_voltage · gps_speed = 다른 장비와 파형을 맞춰 잰 값, prior = 사전값"}
    if t_start is not None:
        m["time"]["t_abs_start"] = t_start.isoformat(timespec="milliseconds")
        keys = ("em_recording", "lag_s", "overlap_s", "corr", "err_v", "runner_up_err_v", "offset_v", "em_a_s",
                "em_f", "em_windows", "em_rms_s", "em_span_s") if row["clock"] == "em" else \
               ("gps_recording", "gps_a_s", "gps_f", "gps_windows", "gps_runner_up_windows", "gps_rms_s", "gps_span_s")
        method = ("DC 버스 전압 ↔ 에너지미터 전압: 전압 절댓값으로 구간을 고르고, 40 s 창을 맞춰 직선(시작 시각·시계 속도)을 구함"
                  if row["clock"] == "em" else
                  "모터 회전수 ↔ Monolith GPS 속도: 40 s 창을 맞춰 직선(시작 시각·시계 속도)을 구함")
        m["time"]["alignment"] = {"method": method, "clock_of": "에너지미터" if row["clock"] == "em" else "Monolith",
                                  "precision": "±0.5 s", **{k: row[k] for k in keys}}
    write_json(m, meta_path)
