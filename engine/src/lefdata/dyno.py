"""섀시다이나모(차대 동력계) 기록 → 공통 포맷, 그리고 절대 시각이 없는 차 로거 레코딩을 다이나모에 맞추기.

    lefdata dyno --event <이벤트 폴더>
        → normalized/<YYYY-MM-DD_HHMMSS>_dyno/dyno.parquet · meta.json      다이나모 기록 하나 = 레코딩 하나
        → normalized/recordings_dyno.csv
        → derived/dyno_alignment.csv                                        시각 없는 로거 레코딩 ↔ 다이나모 기록 짝

파일 (raw_dyno/*_dyno.xls) — 다이나모 운영 프로그램(MagellanCD)이 내보낸 기록이다. 확장자는 .xls 지만 엑셀이 아니라 **CP949 탭 구분 텍스트**다.
    1줄 = 열 이름 · 2줄 = 단위 · 그 뒤 한 줄에 표본 하나 (Time 열, 0.1 s 간격). Inf · -Inf 는 값 없음으로 읽는다.
    파일 하나 = 프로그램에서 기록을 켜고 끈 한 번. **시작 시각은 파일 이름에만 있다** — 프로그램이 지은 이름은 `test-YY-MM-DD HH MM SS.xls`,
    우리 폴더에서는 `YYYY-MM-DD_HHMMSS_dyno.xls` 로 바꿔 둔다 (둘 다 읽는다). 이 시각은 **다이나모 PC 시계**라 다른 장비와 수 분 어긋날 수 있다
    → 세션에서는 catalog_notes.toml 의 [clocks.dyno] offset_s 로 기준 시계에 옮긴다.

열 — 원래 이름을 아래 ASCII 이름으로 바꿔 전부 남긴다 (표에 없는 열은 col_<번호> 로 남기고 meta 의 unmapped 에 적는다).
    speed_kmh = 차량속도 (롤러 표면 속도) · force_n = F (롤러 토크 ÷ 롤러 반지름 — 바퀴가 롤러에 준 힘) · roller_power_w = 동력흡수량 (F × 속도)
    dc_voltage_v · dc_current_a · dc_power_w = Udc4 · Idc4 · P4 — 다이나모 쪽 **전력 분석기가 잰 차의 직류 버스** (방전 +)
    장비가 계산한 효율 · 에너지 소비율 열도 남기지만 세션 채널로는 내지 않는다 (가속 중에는 관성 몫이 섞인다)

시각 없는 로거 레코딩 맞추기 — 전력 분석기의 직류 전압과 차 로거가 기록한 DC 버스 전압(모터 컨트롤러가 잰 값)은 같은 물리량이다.
    대상: 같은 이벤트의 25Evo STM32 백업 로거 · LEF-26 로거 레코딩 중 절대 시각이 없는 것 (다른 장비에 못 맞춘 것 · RTC 를 믿을 수 없는 것)
    1. 직선: 레코딩을 40 s 창으로 잘라 다이나모 기록 전체(파일들을 다이나모 시계로 이은 것)에서 찾고, 맞은 창들에 직선을 맞춘다
       (다이나모 시각 = a + g × 로거 경과시간, `evo_stm32.fit_clock_line`). 창이 모자란 짧은 레코딩은 직선이 없다
    2. 짝: 직선이 가리킨 자리 ±LOCAL_S 안에서 다이나모 파일마다 전체 파형을 다시 대 본다 → 「그 파일 시작 = 로거 경과 몇 초」 짝 하나 (method = line)
    3. 짧은 레코딩은 엔진이 짝을 고르지 않는다 — HV 를 켜고 끄는 모양 · 같은 시험을 되풀이한 모양이 여러 곳에서 비슷하게 맞기 때문이다.
       사람이 catalog_notes.toml 의 [[dyno_pair]] 에 짝을 적으면 (recording · dyno · t_rel_s = 그 파일 시작이 로거 경과 몇 초쯤인가)
       그 ±LOCAL_S 안에서 다듬고 상관이 PAIR_MIN_CORR 를 넘을 때만 쓴다 (method = notes)
    결과 derived/dyno_alignment.csv — 한 줄 = 짝 하나 (짝이 없는 레코딩 · 파일도 이유와 함께 한 줄씩). catalog_build 가 읽어
    그 레코딩을 다이나모 시계로 시각 구간 세션에 붙인다 (`load_alignment` — 짝들을 잇는 꺾은선, 끝 밖은 직선의 기울기로 연장).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__, evo_stm32
from .writer import TZ, dir_bytes, fresh_dir, t_abs_array, write_index, write_json, write_parquet

KST = dt.timezone(dt.timedelta(hours=9))
LOGGER = "섀시다이나모 (MagellanCD 기록)"     # meta source.logger — catalog_build 가 이 앞머리로 장비를 알아본다
T_QUALITY = "dyno_pc"                         # 다이나모 PC 시계 (파일 이름의 시작 시각 + Time). 다른 장비와 독립 검증 안 됨
ENCODING = "cp949"
NAME_RES = (re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})"),            # 우리 이름 2027-01-02_101500_dyno.xls
            re.compile(r"(\d{2})-(\d{2})-(\d{2}) (\d{2}) (\d{2}) (\d{2})"))           # 프로그램 이름 test-26-08-13 15 14 26.xls

# 원래 열 이름 → ASCII 이름. 단위는 2줄에서 읽어 meta 에 그대로 적는다
COLUMNS = {
    "Time": "t_s", "차량속도": "speed_kmh", "롤러 회전수": "roller_rpm", "Torque_Sensor": "roller_torque_nm", "TPS": "tps_pct",
    "Slope_SV": "slope_pct", "차량가속도": "accel_ms2", "주행거리_구간": "distance_km", "주행거리_총구간": "distance_total_km",
    "주행시간_구간": "drive_time_s", "주행시간_총구간": "drive_time_total_s", "F": "force_n", "동력흡수량": "roller_power_w",
    "Load_Total": "load_total_n", "Log_Check": "log_check", "Load_Driver_N": "load_driver_n", "Load_Torque Sensor_N": "load_torque_sensor_n",
    "Torque_Driver": "torque_driver_pct", "Torque_Driver_Nm": "torque_driver_nm", "Road_Load": "road_load_n",
    "베어링온도1": "bearing1_c", "베어링온도2": "bearing2_c",
    "Udc4": "dc_voltage_v", "Idc4": "dc_current_a", "WP4": "dc_energy_wh", "P4": "dc_power_w", "S4": "dc_apparent_va",
    "적산시간_구간": "acc_time_s", "적산시간_총구간": "acc_time_total_s", "적산동력흡수량_구간": "roller_energy_wh",
    "적산동력흡수량_총구간": "roller_energy_total_wh", "배터리 적산전력량4_구간": "dc_energy_seg_wh", "배터리 적산전력량4_총구간": "dc_energy_total_wh",
    "실시간효율": "efficiency_pct", "효율_구간": "efficiency_seg_pct", "효율_총구간": "efficiency_total_pct", "직류에너지소비율": "dc_wh_per_km",
    "최대등판": "max_grade_pct", "WS1.T": "ws1_temp_c", "WS1.H": "ws1_rh_pct", "챔버온도": "chamber_c", "챔버습도": "chamber_rh_pct",
    "대기압": "pressure_mbar",
}
NEED = ("t_s", "speed_kmh", "force_n", "dc_voltage_v", "dc_current_a", "dc_power_w")   # 이것이 없으면 다이나모 기록으로 보지 않는다
MOVING_KMH = 3.0            # 롤러가 이보다 빠르면 '돌았다'
DC_LIVE_A = 20.0            # 직류 전류가 한 번이라도 이보다 크면 전력 분석기가 물려 있던 것으로 본다 (요약 · 확인용)
DC_CHECK_ROLLER_W = 2000.0  # 직류 값 검사: 롤러가 이보다 큰 출력을 받고 (정속 · 가속 중)
DC_CHECK_MIN_N = 20         #   그런 표본이 이만큼 넘게 있을 때
DC_BAD_SHARE = 0.5          #   직류 전력이 롤러 출력보다 작은 표본이 이 비율을 넘으면 전력 분석기 값을 믿지 않는다 (차가 받은 것보다 많이 낼 수 없다)

# 시각 없는 로거 레코딩 맞추기
HV_V = 50.0                 # 이 전압 위만 쓴다 (HV 를 켜고 끄는 계단은 여러 곳에서 비슷하게 맞는다)
STEP_S = 0.2                # 다시 대 볼 때의 격자
MAX_GAP_S = 2.0             # 로거 표본 사이가 이보다 비면 그 칸은 빈칸 (STM32 는 2 ~ 10 행/초, 시작 직후 공백이 있다)
LOCAL_S = 3.0               # 직선 · 사람이 적은 자리 둘레에서 찾는 폭 (±)
LINE_MIN_CORR = 0.75        # 직선이 가리킨 자리에서 파일 전체 상관이 이보다 커야 짝으로 쓴다
PAIR_MIN_CORR = 0.9         # 사람이 적은 짝은 더 엄격하게 — 비슷한 모양이 많은 짧은 레코딩이라서
MIN_TEMPLATE_STD_V = 1.0    # 다이나모 파일의 (HV 켜진) 전압이 이보다 평평하면 다시 대 볼 수 없다
MIN_OVERLAP_S = 20.0        # 다시 대 볼 때 겹친 (빈칸 아닌) 시간이 이보다 짧으면 상관을 내지 않는다
OVERLAP_SHARE = 0.8         #   그리고 파일 · 로거 중 짧은 쪽의 이 비율은 겹쳐야 한다
LINE_ARGS = {"lag0": None, "min_std": 1.0, "min_corr": 0.9, "tol_s": 0.7}
DEVICES = (("자작 STM32", "stm32"), ("LEF-26 STM32F446RE", "lef26_logger"))      # 맞출 수 있는 로거 (meta source.logger 앞머리)
ALIGN_NAME = "dyno_alignment.csv"
NOTES_NAME = "catalog_notes.toml"


# ---------------------------------------------------------------- 파일 읽기

def name_time(path: Path) -> dt.datetime | None:
    """파일 이름의 시작 시각 (다이나모 PC 시계, KST). 없으면 None."""
    m = NAME_RES[0].match(path.name)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=KST)
    m = NAME_RES[1].search(path.name)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        return dt.datetime(2000 + y, mo, d, h, mi, s, tzinfo=KST)
    return None


def read_text(path: Path) -> tuple[pd.DataFrame, dict, list[str]]:
    """다이나모 기록 → (ASCII 열 이름 표, {ASCII 이름: 원래 이름 · 단위}, 표에 없던 원래 열 이름들). 다이나모 기록이 아니면 ValueError."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError as e:
        raise ValueError(f"{ENCODING} 글자가 아니다 — {e}") from None
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) < 3 or "\t" not in lines[0]:
        raise ValueError("탭으로 나눈 머리 줄이 없다")
    names = [c.strip() for c in lines[0].split("\t")]
    units = [c.strip() for c in lines[1].split("\t")]
    if len(units) != len(names):
        raise ValueError(f"이름 줄({len(names)}칸)과 단위 줄({len(units)}칸)의 칸 수가 다르다")
    body = "\n".join(l for l in lines[2:] if l.strip())
    df = pd.read_csv(io.StringIO(body), sep="\t", header=None, names=range(len(names)), na_values=["Inf", "-Inf", "NaN", "inf", "-inf"],
                     dtype=float, engine="c")
    cols, info, unmapped = {}, {}, []
    for i, (n, u) in enumerate(zip(names, units)):
        key = COLUMNS.get(n)
        if key is None:
            key = f"col_{i:02d}"
            unmapped.append(n)
        cols[i] = key
        info[key] = {"name": n, "unit": u}
    df = df.rename(columns=cols)
    missing = [k for k in NEED if k not in df.columns]
    if missing:
        raise ValueError(f"다이나모 기록에 있어야 할 열이 없다: {', '.join(missing)}")
    return df, info, unmapped


def recording_id(start: dt.datetime) -> str:
    return start.strftime("%Y-%m-%d_%H%M%S") + "_dyno"


def dc_check(roller_w, accel_ms2, dc_w) -> tuple[float | None, int, bool]:
    """전력 분석기 값이 물리적으로 맞나 → (직류 전력 < 롤러 출력인 표본 비율, 본 표본 수, 믿지 않을 것인가).

    정속 · 가속 중(감속도가 작을 때) 롤러가 받는 출력은 차가 직류 버스에서 꺼낸 전력보다 클 수 없다 — 클 때가 많으면
    분석기를 덜 물렸거나 방향이 거꾸로다.
    """
    r, a, p = (np.asarray(x, float) for x in (roller_w, accel_ms2, dc_w))
    m = (r > DC_CHECK_ROLLER_W) & (a >= -0.05) & np.isfinite(p)
    n = int(m.sum())
    if n < DC_CHECK_MIN_N:
        return None, n, False
    share = float((p[m] < r[m]).mean())
    return share, n, share > DC_BAD_SHARE


def _energy_wh(t_s: np.ndarray, p_w: np.ndarray) -> tuple[float, float]:
    """사다리꼴 적분 → (방전 Wh, 배터리로 돌아온 Wh — 양수)."""
    ok = np.isfinite(t_s) & np.isfinite(p_w)
    t, p = t_s[ok], p_w[ok]
    if len(t) < 2:
        return 0.0, 0.0
    seg = (p[1:] + p[:-1]) / 2 * np.diff(t) / 3600
    return float(seg[seg > 0].sum()), float(-seg[seg < 0].sum())


# ---------------------------------------------------------------- 변환

def convert_file(src: Path, out_root: Path) -> dict:
    t0 = time.perf_counter()
    start = name_time(src)
    if start is None:
        raise ValueError("파일 이름에 시작 시각이 없다 (YYYY-MM-DD_HHMMSS… 또는 test-YY-MM-DD HH MM SS…)")
    df, info, unmapped = read_text(src)
    df = df[df["t_s"].notna()].reset_index(drop=True)
    rec_id = recording_id(start)
    out = fresh_dir(out_root / rec_id)
    t_rel = np.round(df["t_s"].to_numpy(float) * 1000).astype(np.int64)
    start_ms = int(start.timestamp() * 1000)
    n = len(df)
    values = {k: pa.array(df[k].to_numpy(float)) for k in df.columns if k != "t_s"}
    table = pa.table({"t_rel_ms": pa.array(t_rel), "t_abs": t_abs_array(start_ms + t_rel),
                      "t_quality": pa.array([T_QUALITY] * n, type=pa.string()), **values})
    write_parquet(table, out / "dyno.parquet")

    t_s = t_rel / 1000.0
    d = np.diff(t_rel)
    v, i, p = (df[k].to_numpy(float) for k in ("dc_voltage_v", "dc_current_a", "dc_power_w"))
    speed = df["speed_kmh"].to_numpy(float)
    dis, back = _energy_wh(t_s, p)
    moving = speed > MOVING_KMH
    dc_live = bool(np.nanmax(np.abs(i)) > DC_LIVE_A) if np.isfinite(i).any() else False
    bad_share, bad_n, dc_bad = dc_check(df["roller_power_w"].to_numpy(float), df["accel_ms2"].to_numpy(float), p)
    nanmax = lambda x: round(float(np.nanmax(x)), 2) if np.isfinite(x).any() else None      # noqa: E731
    nanmin = lambda x: round(float(np.nanmin(x)), 2) if np.isfinite(x).any() else None      # noqa: E731
    duration_s = float((t_rel[-1] - t_rel[0]) / 1000) if n else 0.0
    meta = {
        "car": out_root.parent.parent.name,
        "event": out_root.parent.name,
        "recording_id": rec_id,
        "source": {"logger": LOGGER, "format": f"{ENCODING} 탭 구분 텍스트 (1줄 이름 · 2줄 단위)",
                   "file": str(src.relative_to(out_root.parent)).replace("\\", "/"), "file_bytes": src.stat().st_size,
                   "columns": info, "unmapped": unmapped},
        "time": {"basis": "파일 이름의 시작 시각 (다이나모 PC 시계, 초 단위) + Time 열", "tz": TZ,
                 "t_abs_start": start.isoformat(timespec="milliseconds"), "duration_s": duration_s,
                 "first_t_ms": int(t_rel[0]) if n else None, "ts_monotonic": bool((d > 0).all()) if n > 1 else True,
                 "median_dt_ms": float(np.median(d)) if n > 1 else None, "max_gap_ms": int(d.max()) if n > 1 else 0},
        "counts": {"rows": n},
        "summary": {"moving": bool(moving.any()), "moving_s": round(float(moving.sum() * (np.median(d) if n > 1 else 0) / 1000), 1),
                    "speed_max_kmh": nanmax(speed), "force_max_n": nanmax(df["force_n"].to_numpy(float)),
                    "dc_connected": dc_live, "dc_suspect": dc_bad,
                    "dc_below_roller_share": round(bad_share, 3) if bad_share is not None else None, "dc_check_samples": bad_n,
                    "dc_voltage_max_v": nanmax(v), "dc_voltage_min_v": nanmin(v),
                    "dc_current_max_a": nanmax(i), "dc_current_min_a": nanmin(i),
                    "dc_power_max_kw": round(nanmax(p) / 1000, 2) if nanmax(p) is not None else None,
                    "dc_discharge_wh": round(dis, 1), "dc_regen_wh": round(back, 1)},
        "caveats": ["절대 시각은 다이나모 PC 시계다. 다른 장비와 섞을 때는 catalog_notes.toml 의 [clocks.dyno] 로 옮긴다",
                    "dc_* 는 다이나모 쪽 전력 분석기가 잰 차의 직류 버스다 (방전 +). 정속 · 가속 중 직류 전력이 롤러 출력보다 작은 표본이 많으면 "
                    "분석기를 덜 물렸거나 방향이 거꾸로인 기록이다 (summary.dc_suspect)",
                    "force_n · roller_power_w 는 롤러 쪽 값이다. 가속 중에는 다이나모의 관성 흉내 · 미끄러짐이 섞여 바퀴 힘과 같지 않다",
                    "장비가 계산한 효율 · 에너지 소비율 열(efficiency_* · dc_wh_per_km)은 그대로 옮긴 것이다 — 가속 중 값은 쓰지 말 것"],
        "perf": {"convert_sec": round(time.perf_counter() - t0, 3), "input_bytes": src.stat().st_size, "output_bytes": dir_bytes(out)},
        "converter_version": __version__,
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    write_json(meta, out / "meta.json")
    s = meta["summary"]
    return {"recording_id": rec_id, "logger": "dyno", "t_abs_start": meta["time"]["t_abs_start"], "duration_s": round(duration_s, 1),
            "rows": n, "moving": s["moving"], "speed_max_kmh": s["speed_max_kmh"], "dc_connected": dc_live, "dc_suspect": dc_bad,
            "dc_current_max_a": s["dc_current_max_a"], "dc_power_max_kw": s["dc_power_max_kw"], "dc_discharge_wh": s["dc_discharge_wh"],
            "dc_regen_wh": s["dc_regen_wh"], "unmapped_columns": len(unmapped), "source_file": meta["source"]["file"]}


def convert_event(event_dir: Path) -> tuple[list[dict], list[tuple[str, str]]]:
    """이벤트의 raw_dyno/ 전부 → (변환한 레코딩들, 건너뛴 파일과 이유). 건너뛴 파일은 화면에도 찍는다."""
    event_dir = Path(event_dir)
    raw = event_dir / "raw_dyno"
    rows, skipped = [], []
    if not raw.is_dir():
        return rows, [("raw_dyno/", "폴더가 없다")]
    out_root = event_dir / "normalized"
    out_root.mkdir(exist_ok=True)
    for src in sorted(p for p in raw.iterdir() if p.is_file()):
        if src.suffix.lower() != ".xls":
            skipped.append((src.name, "다이나모 기록 파일이 아님 (.xls 만 — 코스트다운 결과표 · 화면 캡처는 변환하지 않는다)"))
            continue
        try:
            rows.append(convert_file(src, out_root))
        except ValueError as e:
            skipped.append((src.name, str(e)))
    for name, why in skipped:
        print(f"  건너뜀 {name} — {why}")
    write_index(rows, out_root / "recordings_dyno.csv")
    return rows, skipped


# ---------------------------------------------------------------- 시각 없는 로거 레코딩 → 다이나모 시계

@dataclass
class _File:
    rec: str
    start: float            # UTC 초 (다이나모 PC 시계)
    t: np.ndarray           # 파일 시작부터의 초
    v: np.ndarray           # 직류 전압


@dataclass
class _Sig:
    rec: str
    device: str
    t: np.ndarray           # 로거 경과시간 (변환기가 바로잡은 t_rel)
    v: np.ndarray           # DC 버스 전압
    duration_s: float


def _dyno_files(norm: Path) -> list[_File]:
    out = []
    for p in sorted(norm.glob("*_dyno")):
        if not (p / "dyno.parquet").exists():
            continue
        m = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        d = pq.read_table(p / "dyno.parquet", columns=["t_rel_ms", "dc_voltage_v"]).to_pandas()
        out.append(_File(p.name, pd.Timestamp(m["time"]["t_abs_start"]).timestamp(), d["t_rel_ms"].to_numpy() / 1000.0,
                         d["dc_voltage_v"].to_numpy(float)))
    return out


def _untimed(norm: Path) -> tuple[list[_Sig], list[tuple[str, str]]]:
    """절대 시각이 없는 로거 레코딩의 DC 버스 전압 → (맞출 것, 건너뛴 것과 이유)."""
    sigs, skipped = [], []
    for p in sorted(q for q in norm.iterdir() if q.is_dir() and (q / "meta.json").exists()):
        m = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        logger = m.get("source", {}).get("logger", "")
        device = next((d for key, d in DEVICES if logger.startswith(key)), None)
        if device is None:
            continue
        tm = m.get("time", {})
        if tm.get("t_abs_start") or tm.get("since_boot_s") is not None:
            continue                                        # 이미 시각이 있다 (다른 장비에 맞췄거나 믿을 수 있는 RTC · 켠 순간부터의 초)
        mt = p / "motor.parquet"
        if not mt.exists():
            skipped.append((p.name, "motor 표 없음"))
            continue
        if device == "stm32":
            d = pq.read_table(mt, columns=["t_rel_ms", "vdc_v"]).to_pandas()
            t, v = d["t_rel_ms"].to_numpy() / 1000.0, d["vdc_v"].to_numpy(float)
        else:
            d = pq.read_table(mt, columns=["t_rel_ms", "signal", "value"]).to_pandas()
            d = d[d["signal"] == "vdc_v"]
            t, v = d["t_rel_ms"].to_numpy() / 1000.0, d["value"].to_numpy(float)
        o = np.argsort(t, kind="stable")
        t, v = t[o], v[o]
        if len(t) < 2:
            skipped.append((p.name, "DC 버스 전압 표본 없음"))
            continue
        sigs.append(_Sig(p.name, device, t, v, float(t[-1])))
    return sigs, skipped


def _grid(t: np.ndarray, v: np.ndarray, t0: float, n: int) -> np.ndarray:
    """표본 → t0 부터 STEP_S 간격 n 칸 (사이는 직선으로). HV 아래 · 표본 사이가 MAX_GAP_S 넘게 빈 칸은 NaN."""
    g = t0 + np.arange(n) * STEP_S
    ok = np.isfinite(v)
    t, v = t[ok], v[ok]
    if len(t) < 2:
        return np.full(n, np.nan)
    out = np.interp(g, t, v, left=np.nan, right=np.nan)
    j = np.clip(np.searchsorted(t, g), 1, len(t) - 1)
    out[(t[j] - t[j - 1]) > MAX_GAP_S] = np.nan
    out[out <= HV_V] = np.nan
    return out


def _refine(sig: _Sig, f: _File, guess_s: float) -> tuple[float, float, str]:
    """다이나모 파일 f 의 시작이 로거 경과 guess_s 초쯤일 때 ±LOCAL_S 안에서 다시 댄다 → (로거 경과 초, 상관, 못 했으면 이유)."""
    n = int(round((f.t[-1] - f.t[0]) / STEP_S)) + 1
    tpl = _grid(f.t, f.v, f.t[0], n)
    hv = np.isfinite(tpl)
    if hv.sum() < 30 or np.nanstd(tpl) < MIN_TEMPLATE_STD_V:
        return np.nan, np.nan, "다이나모 파일의 전압이 평평하거나 HV 가 거의 꺼져 있어 대 볼 수 없음"
    first = int(np.argmax(hv))
    last = len(hv) - int(np.argmax(hv[::-1]))
    tpl = tpl[first:last]
    tpl = pd.Series(tpl).interpolate(limit_area="inside").to_numpy()        # HV 안의 짧은 빈칸
    if not np.isfinite(tpl).all():
        return np.nan, np.nan, "다이나모 파일의 HV 구간이 끊겨 있음"
    k = int(round(LOCAL_S / STEP_S))
    s0 = guess_s + f.t[0] + first * STEP_S - k * STEP_S                     # 찾는 범위 첫 칸의 로거 경과시간
    seg = _grid(sig.t, sig.v, s0, len(tpl) + 2 * k)
    need = max(MIN_OVERLAP_S / STEP_S, OVERLAP_SHARE * min(len(tpl), np.isfinite(seg).sum() - 2 * k))
    c = _local_corr(seg, tpl, need)
    if not len(c) or not np.isfinite(c).any():
        return np.nan, np.nan, "그 자리에 로거 전압 표본이 모자람 (HV 꺼짐 · 기록 공백 · 레코딩 밖)"
    i = int(np.nanargmax(c))
    pos = float(i)
    if 0 < i < len(c) - 1 and np.isfinite(c[i - 1]) and np.isfinite(c[i + 1]):
        den = c[i - 1] - 2 * c[i] + c[i + 1]
        if den < 0:
            pos += 0.5 * (c[i - 1] - c[i + 1]) / den
    if i in (0, len(c) - 1):
        return np.nan, float(c[i]), f"가장 잘 맞는 곳이 찾는 범위(±{LOCAL_S:g} s) 끝 — 자리가 더 멀다"
    return s0 + pos * STEP_S - first * STEP_S - f.t[0], float(c[i]), ""


def _local_corr(seg: np.ndarray, tpl: np.ndarray, need: float) -> np.ndarray:
    """tpl 을 seg 위로 한 칸씩 밀며 빈칸(NaN)을 뺀 상관 — 길이 len(seg) − len(tpl) + 1. 겹친 칸이 need 개 아래면 NaN.

    빈칸을 0 으로 채우면 300 V 근처 값에서 상관이 크게 망가진다 (sync.ncc 는 평균이 0 근처인 신호용).
    """
    m = len(tpl)
    out = np.full(max(len(seg) - m + 1, 0), np.nan)
    for i in range(len(out)):
        x = seg[i:i + m]
        ok = np.isfinite(x)
        if ok.sum() < need:
            continue
        a, b = x[ok], tpl[ok]
        if a.std() > 1e-9 and b.std() > 1e-9:
            out[i] = float(np.corrcoef(a, b)[0, 1])
    return out


def _notes_pairs(event_dir: Path) -> list[dict]:
    path = event_dir / NOTES_NAME
    if not path.exists():
        return []
    return tomllib.loads(path.read_text(encoding="utf-8")).get("dyno_pair", [])


def align_event(event_dir: Path) -> pd.DataFrame:
    """절대 시각이 없는 로거 레코딩 ↔ 다이나모 기록 → derived/dyno_alignment.csv (한 줄 = 짝 하나, 못 맞춘 것도 이유와 함께)."""
    event_dir = Path(event_dir)
    norm = event_dir / "normalized"
    files = _dyno_files(norm)
    sigs, skipped = _untimed(norm)
    by_file = {f.rec: f for f in files}
    pairs = _notes_pairs(event_dir)
    rows = []
    blank = {"dyno_recording": "", "dyno_start": "", "t_rel_s": None, "corr": None, "shift_s": None, "method": "", "g": None,
             "line_windows": None, "line_rms_s": None, "accepted": False}
    for rec, why in skipped:
        rows.append({"recording_id": rec, "device": "", **blank, "rec_duration_s": None, "note": why})
    if not files:
        for s in sigs:
            rows.append({"recording_id": s.rec, "device": s.device, **blank, "rec_duration_s": round(s.duration_s, 1), "note": "다이나모 기록 없음"})
        return _write(event_dir, rows)
    base = files[0].start
    ref_t = np.concatenate([f.start - base + f.t for f in files])
    ref_v = np.concatenate([f.v for f in files])
    live = ref_v > HV_V
    for s in sigs:
        mine = [p for p in pairs if p.get("recording") == s.rec]
        ok = s.v > HV_V
        line = evo_stm32.fit_clock_line(ref_t[live], ref_v[live], s.t[ok], s.v[ok], **LINE_ARGS) if ok.sum() >= 100 else None
        got = 0
        common = {"recording_id": s.rec, "device": s.device, "rec_duration_s": round(s.duration_s, 1)}
        if line is not None:
            lw = {"g": round(line["g"], 6), "line_windows": int(line["n"]), "line_rms_s": round(line["rms_s"], 3)}
            for f in files:
                guess = (f.start - base - line["a"]) / line["g"]
                if guess + f.t[-1] < 0 or guess > s.duration_s:
                    continue                                    # 이 레코딩이 켜져 있지 않던 파일
                at, c, why = _refine(s, f, guess)
                good = not why and c >= LINE_MIN_CORR
                if not why and not good:
                    why = f"직선이 가리킨 자리의 상관 {c:.2f} < {LINE_MIN_CORR}"
                rows.append({**common, **blank, **lw, "dyno_recording": f.rec, "dyno_start": _iso(f.start),
                             "t_rel_s": round(at, 2) if np.isfinite(at) else round(guess, 2), "corr": _r(c),
                             "shift_s": round(at - guess, 2) if np.isfinite(at) else None, "method": "line", "accepted": good, "note": why})
                got += good
        for p in mine:
            f = by_file.get(p.get("dyno", ""))
            g0 = float(line["g"]) if line is not None else 1.0
            if f is None or "t_rel_s" not in p:
                rows.append({**common, **blank, "dyno_recording": p.get("dyno", ""), "method": "notes",
                             "note": f"{NOTES_NAME} [[dyno_pair]] — 다이나모 레코딩 이름 · t_rel_s 를 확인할 것"})
                continue
            at, c, why = _refine(s, f, float(p["t_rel_s"]))
            good = not why and c >= PAIR_MIN_CORR
            if not why and not good:
                why = f"적어 둔 자리의 상관 {c:.2f} < {PAIR_MIN_CORR}"
            rows.append({**common, **blank, "dyno_recording": f.rec, "dyno_start": _iso(f.start),
                         "t_rel_s": round(at, 2) if np.isfinite(at) else float(p["t_rel_s"]), "corr": _r(c),
                         "shift_s": round(at - float(p["t_rel_s"]), 2) if np.isfinite(at) else None, "method": "notes",
                         "g": round(g0, 6), "accepted": good, "note": why or str(p.get("basis", ""))})
            got += good
        if not got and line is None and not mine:
            rows.append({**common, **blank, "note": "직선을 맞추지 못함 — 다이나모 기록 때 켜져 있지 않았거나 (HV 꺼짐 포함), 짧아서 창이 모자람. "
                                                    f"짧은 레코딩의 짝은 엔진이 고르지 않는다 — 알면 {NOTES_NAME} 의 [[dyno_pair]] 에 적을 것"})
    df = pd.DataFrame(rows)
    _dedupe(df)
    return _write(event_dir, df.to_dict("records"))


def _iso(utc_s: float) -> str:
    return dt.datetime.fromtimestamp(utc_s, KST).isoformat(timespec="milliseconds")


def _r(c: float) -> float | None:
    return round(float(c), 3) if np.isfinite(c) else None


def _write(event_dir: Path, rows: list[dict]) -> pd.DataFrame:
    cols = ["recording_id", "device", "dyno_recording", "dyno_start", "t_rel_s", "corr", "shift_s", "method", "g", "line_windows",
            "line_rms_s", "rec_duration_s", "accepted", "note"]
    df = pd.DataFrame(rows, columns=cols)
    (event_dir / "derived").mkdir(exist_ok=True)
    df.to_csv(event_dir / "derived" / ALIGN_NAME, index=False, encoding="utf-8-sig")
    return df


def _dedupe(df: pd.DataFrame) -> None:
    """같은 장비의 레코딩 둘이 다이나모 파일 하나에 다 짝지어지면 둘 다 쓰지 않는다 (로거는 한 대 — 같은 순간에 두 레코딩일 수 없다)."""
    if df.empty:
        return
    ok = df[df["accepted"].astype(bool)]
    twice = ok.groupby(["device", "dyno_recording"])["recording_id"].nunique()
    for dev, f in twice[twice > 1].index:
        m = df["accepted"].astype(bool) & (df["dyno_recording"] == f) & (df["device"] == dev)
        names = ", ".join(sorted(set(df.loc[m, "recording_id"])))
        df.loc[m, "accepted"] = False
        df.loc[m, "note"] = f"같은 다이나모 파일이 레코딩 {names} 에 다 맞음 — 어느 쪽인지 몰라 쓰지 않음"


# ---------------------------------------------------------------- 세션 쪽에서 읽기

@dataclass
class Placement:
    """로거 레코딩 하나의 '경과시간 → 다이나모 PC 시계 (UTC 초)' 지도 — 짝들을 잇는 꺾은선, 끝 밖은 기울기 g 로 연장."""
    rec: str
    device: str
    t_rel: np.ndarray       # 짝의 로거 경과시간 (오름차순)
    dyno_s: np.ndarray      # 그 순간의 다이나모 시각 (UTC 초)
    g: float
    duration_s: float
    pairs: int
    corr_min: float

    def to_dyno(self, t_rel_s) -> np.ndarray:
        x = np.asarray(t_rel_s, float)
        out = np.interp(x, self.t_rel, self.dyno_s)
        out = np.where(x < self.t_rel[0], self.dyno_s[0] + (x - self.t_rel[0]) * self.g, out)
        return np.where(x > self.t_rel[-1], self.dyno_s[-1] + (x - self.t_rel[-1]) * self.g, out)

    @property
    def span(self) -> tuple[float, float]:
        a, b = self.to_dyno([0.0, self.duration_s])
        return float(a), float(b)


def load_alignment(event_dir: Path) -> dict[str, Placement]:
    """derived/dyno_alignment.csv 의 쓸 수 있는 짝 → {레코딩: 지도}. 파일이 없으면 빈 dict."""
    path = Path(event_dir) / "derived" / ALIGN_NAME
    if not path.exists():
        return {}
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df[df["accepted"].astype(bool)]
    out = {}
    for rec, g in df.groupby("recording_id"):
        g = g.sort_values("t_rel_s")
        t_rel = g["t_rel_s"].to_numpy(float)
        dyno_s = np.array([pd.Timestamp(x).timestamp() for x in g["dyno_start"]])
        slope = float(g["g"].dropna().iloc[0]) if g["g"].notna().any() else 1.0
        out[rec] = Placement(rec, str(g["device"].iloc[0]), t_rel, dyno_s, slope, float(g["rec_duration_s"].iloc[0]), len(g),
                             float(g["corr"].min()))
    return out


# ---------------------------------------------------------------- 올린 파일 판별 (E4)

def detect(path: Path) -> dict | None:
    """올린 파일이 다이나모 기록(CP949 탭 텍스트)인가 (계약 §7 `detect`). 아니면 None."""
    path = Path(path)
    if path.suffix.lower() != ".xls":
        return None
    with open(path, "rb") as fh:
        head = fh.read(4096)
    try:
        first = head.decode(ENCODING, errors="strict").split("\n")[0]
    except UnicodeDecodeError:
        return None
    names = [c.strip() for c in first.split("\t")]
    if "Time" not in names or "Udc4" not in names:
        return None
    start = name_time(path)
    df, _, unmapped = read_text(path)
    t = df["t_s"].dropna().to_numpy(float)
    notes = [f"표본 {len(t):,}개", "시작 시각 = 파일 이름 (다이나모 PC 시계)"]
    if start is None:
        notes.append("파일 이름에 시작 시각이 없다 — 변환하지 않는다")
    if unmapped:
        notes.append(f"모르는 열 {len(unmapped)}개 (그대로 남긴다)")
    return {"kind": "chassis_dyno_txt", "car_guess": None, "start": start.isoformat(timespec="milliseconds") if start else None,
            "duration_s": round(float(t[-1] - t[0]), 1) if len(t) > 1 else 0.0, "ok": start is not None,
            "recording_id": recording_id(start) if start else None, "notes": notes}
