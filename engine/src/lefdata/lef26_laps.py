"""LEF-26: 영상 GPS 로 나눈 구간(랩 · 가속 · 스키드패드)에 로거 데이터를 붙인다.

LEF-26 은 로거 GPS 가 죽어서 랩을 온보드 영상의 GPS 로 나눴다 (`laps.py` · `events.py`). 그런데 전류 · 배터리 ·
서스펜션은 로거에만 있다. 두 장비의 시계는 `lef26_clock` 이 요레이트 파형으로 맞춰 놓았으므로,
영상에서 잡은 구간을 로거 시간으로 옮겨 구간마다 로거 값을 요약한다.

    영상 경과시간 ──(영상 GPS 시각)──▶ 참 시각(UTC) ──(ClockMap.to_rel)──▶ 로거 경과시간 t_rel

먼저 `lefdata lef26` · `lefdata gopro --laps` · `lefdata events` · `lefdata lef26-clock` 이 끝나 있어야 한다.
결과는 해석이므로 derived/ 에 둔다 — `lap_logger.csv` · `accel_logger.csv` · `skidpad_logger.csv`.
앞쪽 열은 원래 표(laps.csv …)의 열 그대로라 그 표와 합칠 수 있고, `logger_recording` · `log_t_start_s` · `log_t_end_s` 로
normalized/ 의 어느 표든 같은 구간을 잘라낼 수 있다.

붙였는지 믿을 수 있나 — 구간마다 같이 적는다
    cover_share    구간 중 로거가 기록하고 있던 비율. MIN_COVER 에 못 미치면 값을 비운다 (반쪽짜리 최댓값을 내지 않는다)
    clock_dist_s   구간의 양 끝에서 가장 가까운 '맞은 창' 까지의 시간. 창은 30 초짜리라 15 초 안이면 맞춘 파형 안이다
    gps_lag_s · align_corr
                   **시계 맞추기와 독립인 검증.** 시계는 카메라 자이로 ↔ 로거 자이로로 맞췄다. 여기서는
                   로거 가속도계(종 · 횡) ↔ 영상 GPS 의 속도 변화 · 선회로 다시 대 본다.
                   gps_lag_s 는 GPS 가 로거보다 늦은 정도다 (GPS 수신기의 필터 지연, 0.1 ~ 0.2 s 가 정상).
                   1 초쯤 벗어나 있으면 그 영상 조각의 IMU 시각 보정이 틀린 것이다
    imu_long_off_g · imu_lat_off_g
                   로거 가속도 − GPS 가 본 가속도의 평균. 트랙 랩에서는 센서 장착 기울기만큼의 치우침이다
                   (수백분의 일 g, 날마다 다르다). 가속 · 스키드패드에서는 차체 자세
                   (스쿼트 · 슬립각)가 더해진다. 아래 가속도 값에서 **빼지 않았다**

값을 읽을 때
    - CAN 신호(모터 · BMS · 써미스터)는 **띄엄띄엄** 들어와 있다. 그래서 최댓값 · 최솟값은 '기록된 것 중' 이고
      실제 피크는 더 클 수 있다. 평균(`iq_mean_a`)은 1 초 칸 평균의 평균이며, 표본이 있던 칸의 비율을
      `iq_cover_share` 에 같이 적는다
    - IMU · 서스펜션(LIN)은 로거가 직접 읽어 약 100 Hz 로 고르게 있다
    - 구간 경계는 GPS 위치로 잡은 것이라 로거 시간으로 ±0.3 s 쯤 불확실하다. 구간 통계에는 영향이 없다

횡가속도 부호 (2026-10-01 실측): 로거의 `ax_lat_g` 는 **우회전에서 양수**다. 여기서는 좌회전을 양수로 뒤집어
`lat_g_mean` 에 쓰고, 최댓값은 방향별로 나눠 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import lef26, lef26_clock as lc, sync

G = 9.80665
MIN_COVER = 0.95            # 로거가 구간의 이만큼을 기록하지 않았으면 값을 내지 않는다
IMU_STEP_S = 0.1            # IMU 를 이 간격 칸 평균으로 줄인 뒤
IMU_SMOOTH_N = 5            #   이만큼 이동평균 (0.5 s) — 노면 진동이 아니라 차의 가감속 · 선회를 본다
LIN_CLIP_MM = 74.9          # 리니어 센서 상한(75 mm)에 붙은 표본
HV_ON_V = 150.0             # 팩 전압이 이보다 낮으면 고전압이 꺼진 것 (motor_runs 와 같은 기준)
SOC_EDGE_ROWS = 50          # 구간 처음 · 끝 이만큼의 BMS 행 중앙값을 시작 · 끝 SOC 로 본다
BIN_S = 1.0                 # CAN 신호 평균을 낼 때의 칸
ALIGN_STEP_S = 0.05
ALIGN_PAD_S = 3.0           # 짧은 구간(가속 4.5 s)도 파형이 잡히도록 앞뒤로 더 본다
ALIGN_SMOOTH_S = 1.0
ALIGN_LAGS_S = np.arange(-1.5, 1.5001, 0.05)
ALIGN_LAT_MIN_KMH = 15.0    # 이보다 느리면 GPS 진행 방위가 흔들려 선회 가속도를 못 믿는다
SPEED_GPS_LAG_S = 0.2       # 모터 속도와 대 볼 때 GPS 속도를 이만큼 당긴다 (speed_calibration 으로 잰 값)
SPEED_AGREE_KMH = 5.0
SPEED_MOVING_KMH = 10.0

# 종류 → (구간 표, 결과 표, 그대로 가져올 열, (시작, 끝) 을 영상 경과시간으로 주는 함수)
SEGMENTS = {
    "lap": ("laps.csv", "lap_logger.csv", ["recording_id", "run", "lap", "lap_time_s", "vmax_kmh"],
            lambda d: (d["t_start_s"], d["t_end_s"])),
    "accel": ("accel_runs.csv", "accel_logger.csv", ["recording_id", "t_start_s", "direction", "t75_s", "v_max_kmh"],
              lambda d: (d["t_start_s"], d["t_start_s"] + d["t75_s"])),
    "skidpad": ("skidpad_laps.csv", "skidpad_logger.csv",
                ["recording_id", "run", "arc", "turn", "lap", "lap_time_s", "lat_acc_g"],
                lambda d: (d["t_end_s"] - d["lap_time_s"], d["t_end_s"])),
}

JOIN_COLS = ["t_abs_start", "logger_recording", "log_t_start_s", "log_t_end_s", "cover_share", "clock_dist_s",
             "gps_lag_s", "align_corr", "imu_hz", "imu_gap_max_s", "imu_long_off_g", "imu_lat_off_g"]
VALUE_COLS = ["long_g_mean", "long_g_max", "long_g_min", "lat_g_mean", "lat_g_left_max", "lat_g_right_max",
              "lin_rl_p01_mm", "lin_rl_p50_mm", "lin_rl_p99_mm", "lin_rl_clip_share",
              "lin_rr_p01_mm", "lin_rr_p50_mm", "lin_rr_p99_mm", "lin_rr_clip_share",
              "soc_start_pct", "soc_end_pct", "cell_min_v", "bms_rows",
              "batt_high_c", "batt_low_c", "batt_avg_c", "batt_spread_c", "batt_hottest_id",
              "iq_peak_a", "iq_cmd_peak_a", "iq_mean_a", "iq_cover_share", "vdc_min_v", "vdc_max_v",
              "t_motor_max_c", "t_igbt_max_c", "n_act_max_pct", "speed_samples", "speed_agree_share"]
INT_COLS = ["bms_rows", "batt_high_c", "batt_low_c", "batt_spread_c", "batt_hottest_id", "iq_peak_a", "iq_cmd_peak_a",
            "speed_samples"]


# ---------------------------------------------------------------- 읽기

@dataclass
class Video:
    """영상 한 개의 GPS: 영상 경과시간 ↔ 참 시각, 그리고 GPS 가 본 종 · 횡 가속도 (검증용)."""
    t_rel: np.ndarray
    t_true: np.ndarray
    speed_kmh: np.ndarray
    grid: np.ndarray            # 참 시각의 고정 간격 격자
    v_ms: np.ndarray
    long_g: np.ndarray          # 속도 변화율
    lat_left_g: np.ndarray      # 속도 × 진행 방위 변화율. 좌회전 양수

    def to_true(self, t_video_s) -> np.ndarray:
        return np.interp(np.asarray(t_video_s, float), self.t_rel, self.t_true)


@dataclass
class Logger:
    """로거 레코딩 하나: 시계 지도와, 구간을 잘라 요약할 표들. 시각은 전부 로거 경과시간(초)."""
    rec: str
    cm: lc.ClockMap | None      # 영상과 겹치지 않은 레코딩은 None
    win_true: np.ndarray        # 맞은 창들의 참 시각
    imu_t: np.ndarray
    long_g: np.ndarray
    lat_left_g: np.ndarray
    lin: pd.DataFrame | None
    bms: pd.DataFrame
    therm: pd.DataFrame
    motor: dict[str, pd.DataFrame]
    yaw_dps: np.ndarray | None = None   # 요레이트 (좌회전 +) — 영상이 없는 세션에서 로거로 차량 거동을 낼 때


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    """가운데 맞춘 이동평균. 빈 칸(NaN)은 건너뛰고, 창의 절반도 못 채우면 NaN."""
    return pd.Series(x).rolling(n, center=True, min_periods=n // 2 + 1).mean().to_numpy()


def gps_accel(t_s, speed_kmh, course_deg, step: float = ALIGN_STEP_S, smooth_s: float = ALIGN_SMOOTH_S):
    """GPS 속도 · 진행 방위 → 고정 간격 격자 위의 (시각, 속도 m/s, 종가속도 g, 횡가속도 g).

    종 = 속도 변화율, 횡 = 속도 × 진행 방위 변화율. 진행 방위는 북 = 0 · 시계방향이 증가이므로
    부호를 뒤집어 **좌회전을 양수**로 낸다.
    """
    t_s = np.asarray(t_s, float)
    grid = np.arange(t_s[0], t_s[-1], step)
    n = int(round(smooth_s / step))
    v = np.interp(grid, t_s, np.asarray(speed_kmh, float)) / 3.6
    course = np.interp(grid, t_s, np.unwrap(np.radians(np.asarray(course_deg, float))))
    return grid, v, _smooth(np.gradient(v, step) / G, n), _smooth(-np.gradient(course, step) * v / G, n)


def read_video(rec_dir: Path) -> Video:
    g = pq.read_table(rec_dir / "gps.parquet", columns=["t_rel_ms", "t_abs", "speed_kmh", "course_deg"]).to_pandas()
    t_true, speed = lc._epoch_s(g["t_abs"]), g["speed_kmh"].to_numpy()
    return Video(g["t_rel_ms"].to_numpy() / 1000.0, t_true, speed, *gps_accel(t_true, speed, g["course_deg"].to_numpy()))


def read_logger(rec_dir: Path, cm: lc.ClockMap | None, win_true: np.ndarray) -> Logger:
    imu = pq.read_table(rec_dir / "imu.parquet", columns=["t_rel_ms", "ax_lat_g", "ay_long_g", "yaw_dps"]).to_pandas()
    lin = pq.read_table(rec_dir / "lin.parquet", columns=["t_rel_ms", "lin_rl_mm", "lin_rr_mm"]).to_pandas()
    bms = pq.read_table(rec_dir / "bms.parquet", columns=["t_rel_ms", "valid", "cell_max_v", "cell_min_v", "soc_pct"]).to_pandas()
    bms = bms[bms["valid"] & (bms["cell_min_v"] >= 2.0) & (bms["cell_max_v"] <= 4.3)].copy()      # 약한 체크섬 보완
    therm = pq.read_table(rec_dir / "thermistor.parquet",
                          columns=["t_rel_ms", "source", "lowest_c", "highest_c", "average_c", "hottest_id"]).to_pandas()
    m = pq.read_table(rec_dir / "motor.parquet", columns=["t_rel_ms", "signal", "value", "raw"]).to_pandas()
    for df in (lin, bms, therm, m):
        df["t_s"] = df["t_rel_ms"] / 1000.0
    return Logger(rec_dir.name, cm, win_true, imu["t_rel_ms"].to_numpy() / 1000.0, imu["ay_long_g"].to_numpy(),
                  -imu["ax_lat_g"].to_numpy(),              # 로거의 X 는 우회전에서 양수다 → 좌회전 양수로 뒤집는다
                  lin if len(lin) else None, bms, therm, {k: v for k, v in m.groupby("signal")}, imu["yaw_dps"].to_numpy())


def _cut(df: pd.DataFrame | None, r0: float, r1: float) -> pd.DataFrame | None:
    if df is None:
        return None
    t = df["t_s"].to_numpy()
    return df[(t >= r0) & (t < r1)]


# ---------------------------------------------------------------- 구간 하나를 요약한다

def _fresh(raw: np.ndarray) -> np.ndarray:
    """직전과 값이 다른 표본. 이 로그는 같은 CAN 프레임이 여러 번(유독 16 번) 다시 찍혀 있다."""
    return np.concatenate([[True], raw[1:] != raw[:-1]]) if len(raw) else np.array([], bool)


def binned_mean(t_s, values, r0: float, r1: float, bin_s: float = BIN_S) -> tuple[float | None, float]:
    """띄엄띄엄 들어온 신호의 구간 평균 → (칸 평균의 평균, 표본이 있던 칸의 비율).

    표본이 몰려서 들어오는 로그라 그냥 평균 내면 몰린 순간에 끌려간다. 칸마다 먼저 평균을 내고 그 칸들을 평균 낸다.
    표본이 없는 칸은 모르는 것이므로 비율로 드러낸다.
    """
    t, v = np.asarray(t_s, float), np.asarray(values, float)
    n_bins = max(int(np.ceil((r1 - r0) / bin_s)), 1)
    if not len(t):
        return None, 0.0
    k = np.clip(np.floor((t - r0) / bin_s).astype(int), 0, n_bins - 1)
    count = np.bincount(k, minlength=n_bins)
    total = np.bincount(k, weights=v, minlength=n_bins)
    has = count > 0
    return float((total[has] / count[has]).mean()), float(has.mean())


def imu_stats(log: Logger, r0: float, r1: float) -> dict:
    m = (log.imu_t >= r0) & (log.imu_t < r1)
    t = log.imu_t[m]
    if len(t) < 2:
        return {}
    _, lg = sync.grid(t - r0, log.long_g[m], IMU_STEP_S)
    _, lt = sync.grid(t - r0, log.lat_left_g[m], IMU_STEP_S)
    mean_long, mean_lat = float(np.nanmean(lg)), float(np.nanmean(lt))      # 칸 평균의 평균 = 시간 평균
    lg, lt = _smooth(lg, IMU_SMOOTH_N), _smooth(lt, IMU_SMOOTH_N)
    if not np.isfinite(lg).any():
        return {}
    return {"long_g_mean": round(mean_long, 3), "long_g_max": round(float(np.nanmax(lg)), 3),
            "long_g_min": round(float(np.nanmin(lg)), 3), "lat_g_mean": round(mean_lat, 3),
            "lat_g_left_max": round(float(np.nanmax(lt)), 3), "lat_g_right_max": round(float(np.nanmax(-lt)), 3)}


def lin_stats(log: Logger, r0: float, r1: float) -> dict:
    """리어 서스펜션 스트로크. 센서가 0 · 75 mm 로 튀는 표본이 있어 최소 · 최대 대신 1 · 99 백분위를 쓴다."""
    d = _cut(log.lin, r0, r1)
    if d is None or d.empty:
        return {}
    out = {}
    for side in ("rl", "rr"):
        x = d[f"lin_{side}_mm"].to_numpy()
        p = np.percentile(x, [1, 50, 99])
        out.update({f"lin_{side}_p01_mm": round(float(p[0]), 1), f"lin_{side}_p50_mm": round(float(p[1]), 1),
                    f"lin_{side}_p99_mm": round(float(p[2]), 1),
                    f"lin_{side}_clip_share": round(float((x >= LIN_CLIP_MM).mean()), 3)})
    return out


def battery_stats(log: Logger, r0: float, r1: float) -> dict:
    out = {}
    b = _cut(log.bms, r0, r1)
    if len(b):
        out.update({"soc_start_pct": float(b["soc_pct"].iloc[:SOC_EDGE_ROWS].median()),
                    "soc_end_pct": float(b["soc_pct"].iloc[-SOC_EDGE_ROWS:].median()),
                    "cell_min_v": round(float(b["cell_min_v"].min()), 4), "bms_rows": len(b)})
    th = _cut(log.therm, r0, r1)
    if len(th):
        both = th.dropna(subset=["lowest_c", "highest_c"])
        avg, hot = th["average_c"].dropna(), th["hottest_id"].dropna()
        out.update({"batt_high_c": None if th["highest_c"].isna().all() else int(th["highest_c"].max()),
                    "batt_low_c": None if th["lowest_c"].isna().all() else int(th["lowest_c"].min()),
                    "batt_avg_c": round(float(avg.mean()), 1) if len(avg) else None,
                    "batt_spread_c": int((both["highest_c"] - both["lowest_c"]).max()) if len(both) else None,
                    "batt_hottest_id": int(hot.mode().iloc[0]) if len(hot) else None})
    return out


def motor_stats(log: Logger, vid: Video | None, r0: float, r1: float) -> dict:
    """vid 가 없으면(영상과 겹치지 않는 구간) GPS 속도와 대 보는 `speed_agree_share` 만 빠진다."""
    sig = {k: _cut(v, r0, r1) for k, v in log.motor.items()}
    have = lambda k: k in sig and len(sig[k]) > 0
    out = {}
    if have("iq_act_a"):
        v, new = sig["iq_act_a"]["value"].abs().to_numpy(), _fresh(sig["iq_act_a"]["raw"].to_numpy())
        mean, share = binned_mean(sig["iq_act_a"]["t_s"].to_numpy()[new], v[new], r0, r1)
        out.update({"iq_peak_a": round(float(v.max())), "iq_mean_a": round(mean, 1), "iq_cover_share": round(share, 2)})
    if have("iq_cmd_a"):
        out["iq_cmd_peak_a"] = round(float(sig["iq_cmd_a"]["value"].abs().max()))
    if have("vdc_v"):
        hv = sig["vdc_v"]["value"].to_numpy()
        hv = hv[hv > HV_ON_V]
        if len(hv):
            out.update({"vdc_min_v": round(float(hv.min()), 1), "vdc_max_v": round(float(hv.max()), 1)})
    for k, col in (("t_motor_c", "t_motor_max_c"), ("t_igbt_c", "t_igbt_max_c")):
        if have(k) and np.isfinite(sig[k]["value"]).any():
            out[col] = round(float(np.nanmax(sig[k]["value"])), 1)
    if have("n_act_pct"):
        n = sig["n_act_pct"]
        pct, fresh = n["value"].abs().to_numpy(), _fresh(n["raw"].to_numpy())
        out.update({"n_act_max_pct": round(float(pct.max()), 1), "speed_samples": int(fresh.sum())})
        if vid is None or log.cm is None:
            return out
        v_gps = np.interp(log.cm.to_true(n["t_s"].to_numpy()[fresh]) + SPEED_GPS_LAG_S, vid.t_true, vid.speed_kmh)
        moving = v_gps > SPEED_MOVING_KMH
        if moving.any():
            off = np.abs(pct[fresh][moving] * lef26.KMH_PER_PCT - v_gps[moving])
            out["speed_agree_share"] = round(float((off < SPEED_AGREE_KMH).mean()), 2)
    return out


def alignment(log: Logger, vid: Video, t0_true: float, t1_true: float) -> dict:
    """로거 가속도계 ↔ 영상 GPS 가 본 가속도. 잴 수 없으면 빈 dict.

    종(속도 변화율)과 횡(속도 × 진행 방위 변화율)을 한 줄로 이어 붙여 상관을 본다 — 직선 가속에는 횡이 없고
    스키드패드에는 종이 없어서, 하나만 쓰면 어느 한쪽 종목에서 잴 것이 없다.

        gps_lag_s        상관이 가장 높은 어긋남. 양수 = GPS 가 로거보다 늦다
        align_corr       그때의 상관
        imu_long_off_g   로거 종가속도 − GPS 종가속도의 평균. 센서 장착 기울기만큼의 치우침이다
        imu_lat_off_g    횡도 같다 (좌회전 양수 기준)
    """
    grid = np.arange(t0_true - ALIGN_PAD_S, t1_true + ALIGN_PAD_S, ALIGN_STEP_S)
    r_lo, r_hi = log.cm.to_rel([grid[0] - 1.0, grid[-1] + 1.0])
    m = (log.imu_t >= r_lo) & (log.imu_t <= r_hi)
    if m.sum() < 20:
        return {}
    t_true = log.cm.to_true(log.imu_t[m])
    k = np.floor((t_true - grid[0]) / ALIGN_STEP_S).astype(int)
    ok = (k >= 0) & (k < len(grid))
    n = int(round(ALIGN_SMOOTH_S / ALIGN_STEP_S))
    count = np.bincount(k[ok], minlength=len(grid))
    with np.errstate(invalid="ignore", divide="ignore"):
        lg = _smooth(np.bincount(k[ok], weights=log.long_g[m][ok], minlength=len(grid)) / count, n)
        lt = _smooth(np.bincount(k[ok], weights=log.lat_left_g[m][ok], minlength=len(grid)) / count, n)
    best = {}
    for lag in ALIGN_LAGS_S:
        at = grid + ALIGN_STEP_S / 2 + lag                       # 칸의 가운데 시각
        g_long, g_lat = np.interp(at, vid.grid, vid.long_g), np.interp(at, vid.grid, vid.lat_left_g)
        fast = np.interp(at, vid.grid, vid.v_ms) * 3.6 > ALIGN_LAT_MIN_KMH
        d_long, d_lat = lg - g_long, (lt - g_lat)[fast]
        a = np.concatenate([lg, lt[fast]])
        b = np.concatenate([g_long, g_lat[fast]])
        both = np.isfinite(a) & np.isfinite(b)
        if both.sum() < 20 or a[both].std() < 1e-6 or b[both].std() < 1e-6:
            continue
        c = float(np.corrcoef(a[both], b[both])[0, 1])
        if not best or c > best["align_corr"]:
            best = {"gps_lag_s": round(float(lag), 2), "align_corr": round(c, 3),
                    "imu_long_off_g": round(float(np.nanmean(d_long)), 3),
                    "imu_lat_off_g": round(float(np.nanmean(d_lat)), 3) if np.isfinite(d_lat).sum() >= 20 else None}
    return best


def locate(loggers: list[Logger], t0_true: float, t1_true: float) -> tuple[Logger | None, float, float, float]:
    """구간을 가장 많이 덮는 로거 레코딩 → (레코딩, 로거 시작, 로거 끝, 덮은 비율)."""
    best = (None, np.nan, np.nan, 0.0)
    for log in loggers:
        r0, r1 = (float(x) for x in log.cm.to_rel([t0_true, t1_true]))
        if r1 <= r0 or not len(log.imu_t):
            continue
        cover = max(0.0, min(r1, float(log.imu_t[-1])) - max(r0, float(log.imu_t[0]))) / (r1 - r0)
        if cover > best[3]:
            best = (log, r0, r1, cover)
    return best


def summarize_segment(loggers: list[Logger], vid: Video, t0_video: float, t1_video: float) -> dict:
    t0, t1 = (float(x) for x in vid.to_true([t0_video, t1_video]))
    log, r0, r1, cover = locate(loggers, t0, t1)
    row = {"t_abs_start": lc._iso(t0), "cover_share": round(cover, 3)}
    if log is None:
        return row
    row.update({"logger_recording": log.rec, "log_t_start_s": round(r0, 3), "log_t_end_s": round(r1, 3),
                "clock_dist_s": round(float(max(np.abs(log.win_true - t).min() for t in (t0, t1))), 1)})
    m = (log.imu_t >= r0) & (log.imu_t < r1)
    if m.sum() > 1:
        row.update({"imu_hz": round(float(m.sum() / (t1 - t0)), 1),
                    "imu_gap_max_s": round(float(np.diff(np.sort(log.imu_t[m])).max()), 2)})
    if cover < MIN_COVER:
        return row
    row.update({**alignment(log, vid, t0, t1), **imu_stats(log, r0, r1), **lin_stats(log, r0, r1),
                **battery_stats(log, r0, r1), **motor_stats(log, vid, r0, r1)})
    return row


# ---------------------------------------------------------------- 이벤트 전체

def run_event(event_dir: Path, kinds=tuple(SEGMENTS)) -> dict[str, pd.DataFrame]:
    """derived/ 의 구간 표마다 로거 값을 붙여 `<종류>_logger.csv` 로 쓴다. → {종류: 표}"""
    norm, out = event_dir / "normalized", event_dir / "derived"
    for need in ("logger_clock_windows.csv", "logger_clock_pairs.csv"):
        if not (out / need).exists():
            raise FileNotFoundError(f"{need} 가 없다 — 먼저 lefdata lef26-clock 을 돌릴 것")
    windows = pd.read_csv(out / "logger_clock_windows.csv", encoding="utf-8-sig")
    pairs = pd.read_csv(out / "logger_clock_pairs.csv", encoding="utf-8-sig")
    pairs = pairs[pairs["accepted"]]

    videos: dict[str, Video] = {}
    logs: dict[str, Logger | None] = {}

    def loggers_of(video: str) -> list[Logger]:
        found = []
        for rid in pairs.loc[pairs["video"] == video, "recording_id"]:
            if rid not in logs:
                cm = lc.clock_map(windows, pairs, rid)
                logs[rid] = None if cm is None else read_logger(
                    norm / rid, cm, windows.loc[windows["recording_id"] == rid, "true_s"].to_numpy())
            if logs[rid] is not None:
                found.append(logs[rid])
        return found

    result = {}
    for kind in kinds:
        src, dst, keep, bounds = SEGMENTS[kind]
        if not (out / src).exists():
            continue
        try:
            seg = pd.read_csv(out / src, encoding="utf-8-sig")
        except pd.errors.EmptyDataError:                # 그 종목을 안 한 이벤트 (열도 없는 빈 표)
            continue
        t0s, t1s = bounds(seg)
        rows = []
        for i, s in seg.iterrows():
            v = s["recording_id"]
            if v not in videos:
                videos[v] = read_video(norm / v)
            rows.append({**{c: s[c] for c in keep},
                         **summarize_segment(loggers_of(v), videos[v], float(t0s[i]), float(t1s[i]))})
        df = pd.DataFrame(rows).reindex(columns=keep + JOIN_COLS + VALUE_COLS)
        df[INT_COLS] = df[INT_COLS].astype("Int64")
        df.to_csv(out / dst, index=False, encoding="utf-8-sig")
        result[kind] = df
    return result
