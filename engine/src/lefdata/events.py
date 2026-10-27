"""종목별 구간 분석 — 가속(75 m)과 스키드패드(8자).

랩 분할(`laps.py`)은 "계측선을 몇 번 지났나"를 세는 방식이라 트랙을 도는 종목에만 맞는다.
가속은 출발선에서 결승선까지 한 번 지나는 종목이고, 스키드패드는 두 원을 번갈아 도는 8자 코스라
각각 다른 방법이 필요하다.

가속 (FSAE: 정지 출발 후 75 m 직선)
    - 출발점: `laps.find_runs` 가 준 구간은 이미 10 km/h 를 넘은 뒤라 정지 구간이 잘려 있다.
      구간 시작에서 **뒤로 거슬러 올라가** 속도가 STOP_KMH 아래인 마지막 지점을 출발로 잡는다
    - 75 m: 출발점부터 누적 이동거리가 75 m 가 되는 시점 (앞뒤 샘플 사이 선형 보간)
    - 거리는 두 가지로 재서 둘 다 남긴다 — GPS 위치 적분, GPS 속도 적분
    - 직진성으로 가속 주행만 걸러낸다 (스키드패드는 곡선이라 제외됨)
    - 역방향 시험은 평균 진행 방위로 갈라낸다

스키드패드 (FSAE: 두 원을 번갈아 도는 8자, 각 원 2바퀴)
    - 8자 전체를 원 하나로 맞추면 안 된다 (해 보면 반지름 · 잔차가 엉망이다)
    - 진행 방위의 변화율(각속도) 부호로 **좌회전 구간과 우회전 구간을 먼저 나눈다**
    - 구간마다 원을 맞추고, 중심 기준 회전각이 360° 될 때마다 한 바퀴로 센다
    - 횡가속도는 v² / r 로 계산한다 (IMU 와 교차검증 가능)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import laps as lapmod

# --- 가속
STOP_KMH = 2.0           # 이 아래면 정지로 본다 (출발점 탐색용)
BACKTRACK_S = 20.0       # 구간 시작에서 최대 이 시간만큼 거슬러 올라가며 정지 지점을 찾는다
ACCEL_DIST_M = 75.0      # FSAE 가속 구간 길이
ACCEL_MIN_TOP_KMH = 30.0
STRAIGHTNESS_MIN = 0.97  # 75 m 구간의 (직선거리 / 경로거리). 곡선 주행을 걸러낸다

# --- 스키드패드
SKID_MIN_SPEED = 12.0
YAW_DEADBAND = 0.15      # rad/s. 이보다 작으면 직진으로 본다
YAW_SMOOTH_N = 5         # 각속도 이동평균 샘플 수 (GPS 방위 노이즈 완화)
ARC_MIN_S = 4.0          # 한 원 구간으로 인정할 최소 길이
SKID_MIN_R, SKID_MAX_R = 4.0, 15.0
TURN_NAME = {1: "right", -1: "left"}      # 진행 방위 변화율의 부호 → 선회 방향 (방위는 시계방향이 증가)
SKID_MAX_RMS_M = 1.5     # 원에 이보다 안 맞는 호는 세지 않는다 (원을 빠져나가는 구간이 한 바퀴로 잡힌 사례: 잔차 2.6 m)
G = 9.80665


def _cumdist(x, y) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])


def _cumdist_from_speed(t_s, v_kmh) -> np.ndarray:
    """속도 적분 거리. GPS 속도는 도플러 기반이라 위치보다 노이즈가 적다."""
    dt = np.diff(t_s)
    step = (v_kmh[:-1] + v_kmh[1:]) / 2 / 3.6 * dt
    return np.concatenate([[0.0], np.cumsum(step)])


def _interp_at(dist: np.ndarray, t: np.ndarray, target: float):
    """누적거리가 target 이 되는 시각을 선형 보간. 못 닿으면 None."""
    k = int(np.searchsorted(dist, target))
    if k == 0 or k >= len(dist):
        return None
    f = (target - dist[k - 1]) / (dist[k] - dist[k - 1])
    return float(t[k - 1] + f * (t[k] - t[k - 1]))


def _mean_heading(course_deg: np.ndarray) -> float:
    """각도 평균은 단위벡터로 구한다 (359° 와 1° 의 평균은 180° 가 아니라 0°)."""
    r = np.radians(np.asarray(course_deg, dtype=float))
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360)


def _prep(gps: pd.DataFrame, lat0: float, lon0: float):
    g = gps[gps["fix"]].reset_index(drop=True)
    t = g["t_rel_ms"].to_numpy() / 1000.0
    x, y = lapmod.to_local_m(g["lat"], g["lon"], lat0, lon0)
    return g, t, np.asarray(x), np.asarray(y), g["speed_kmh"].to_numpy(), g["course_deg"].to_numpy()


# ---------------------------------------------------------------- 가속

def _launch_index(t, v, a: int) -> int:
    """구간 시작 a 에서 뒤로 거슬러 올라가 정지 상태의 마지막 샘플을 찾는다."""
    lo = int(np.searchsorted(t, t[a] - BACKTRACK_S))
    stopped = np.flatnonzero(v[lo:a + 1] <= STOP_KMH)
    return lo + int(stopped[-1]) if len(stopped) else a


def accel_runs(gps: pd.DataFrame, lat0: float, lon0: float) -> pd.DataFrame:
    """가속 주행을 찾아 75 m 통과 시간을 잰다."""
    g, t, x, y, v, c = _prep(gps, lat0, lon0)

    rows = []
    for a, b in lapmod.find_runs(t, v):
        if v[a:b + 1].max() < ACCEL_MIN_TOP_KMH:
            continue
        s = _launch_index(t, v, a)
        sl = slice(s, b + 1)
        d_pos = _cumdist(x[sl], y[sl])
        d_spd = _cumdist_from_speed(t[sl], v[sl])
        if d_pos[-1] < ACCEL_DIST_M:
            continue
        t75 = _interp_at(d_pos, t[sl], ACCEL_DIST_M)
        t75_spd = _interp_at(d_spd, t[sl], ACCEL_DIST_M)
        if t75 is None:
            continue
        # 75 m 구간이 직선인가 — 스키드패드 같은 곡선 주행을 걸러낸다
        k75 = int(np.searchsorted(d_pos, ACCEL_DIST_M))
        straight = float(np.hypot(x[s + k75] - x[s], y[s + k75] - y[s]) / d_pos[k75])
        if straight < STRAIGHTNESS_MIN:
            continue
        moving = v[sl] > 15
        rows.append({
            "t_abs_start": g["t_abs"].iloc[s],
            "t_start_s": round(float(t[s]), 2),
            "v_start_kmh": round(float(v[s]), 2),
            "t75_s": round(t75 - float(t[s]), 3),
            "t75_speed_int_s": None if t75_spd is None else round(t75_spd - float(t[s]), 3),
            "v_at_75_kmh": round(float(np.interp(t75, t[sl], v[sl])), 2),
            "v_max_kmh": round(float(v[a:b + 1].max()), 2),
            "straightness": round(straight, 3),
            "heading_deg": round(_mean_heading(c[sl][moving]), 1),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    ref = _mean_heading(df["heading_deg"].to_numpy())
    diff = (df["heading_deg"] - ref + 180) % 360 - 180
    df["direction"] = np.where(diff.abs() < 90, "forward", "reverse")
    return df


# ---------------------------------------------------------------- 스키드패드

def fit_circle(x: np.ndarray, y: np.ndarray):
    """최소제곱 원 맞춤 → (중심x, 중심y, 반지름)."""
    A = np.column_stack([x, y, np.ones(len(x))])
    sol, *_ = np.linalg.lstsq(A, x ** 2 + y ** 2, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    return float(cx), float(cy), float(np.sqrt(sol[2] + cx ** 2 + cy ** 2))


def _yaw_arcs(t, c, v):
    """진행 방위 변화율의 부호로 좌회전/우회전 구간을 나눈다 → [(시작, 끝, 부호)].

    진행 방위는 북=0 · **시계방향이 증가**다. 그래서 부호 +1 이 **우회전**, −1 이 좌회전이다.
    (2026-10-01 까지 반대로 라벨을 붙였다. 위치 기하 · 온보드 영상으로 확인하고 고쳤다)
    """
    ang = np.unwrap(np.radians(c))
    yaw = np.gradient(ang, t)
    if YAW_SMOOTH_N > 1:                       # 이동평균으로 GPS 방위 노이즈를 줄인다
        k = np.ones(YAW_SMOOTH_N) / YAW_SMOOTH_N
        yaw = np.convolve(yaw, k, mode="same")
    sign = np.sign(yaw)
    sign[(np.abs(yaw) < YAW_DEADBAND) | (v < SKID_MIN_SPEED)] = 0

    arcs, i = [], 0
    while i < len(sign):
        j = i
        while j + 1 < len(sign) and sign[j + 1] == sign[i]:
            j += 1
        if sign[i] != 0 and t[j] - t[i] >= ARC_MIN_S:
            arcs.append((i, j, int(sign[i])))
        i = j + 1
    return arcs


def skidpad_laps(gps: pd.DataFrame, lat0: float, lon0: float,
                 t_start_s: float, t_end_s: float):
    """한 스키드패드 주행을 좌/우 원 구간으로 나눠 바퀴를 센다."""
    g, t, x, y, v, c = _prep(gps, lat0, lon0)
    w = np.flatnonzero((t >= t_start_s) & (t <= t_end_s))
    if len(w) < 50:
        return pd.DataFrame(), pd.DataFrame()
    t, x, y, v, c = t[w], x[w], y[w], v[w], c[w]

    laps_rows, arc_rows = [], []
    for n, (i, j, s) in enumerate(_yaw_arcs(t, c, v), 1):
        cx, cy, r = fit_circle(x[i:j + 1], y[i:j + 1])
        resid = np.hypot(x[i:j + 1] - cx, y[i:j + 1] - cy) - r
        rms = float(np.sqrt((resid ** 2).mean()))
        ok = SKID_MIN_R <= r <= SKID_MAX_R and rms <= SKID_MAX_RMS_M
        arc_rows.append({
            "arc": n, "turn": TURN_NAME[s],
            "t_start_s": round(float(t[i]), 2), "duration_s": round(float(t[j] - t[i]), 2),
            "circle_r_m": round(r, 2), "fit_rms_m": round(rms, 2),
            "v_avg_kmh": round(float(v[i:j + 1].mean()), 2), "used": bool(ok),
        })
        if not ok:
            continue
        # GPS 방위(북=0, 시계방향)와 수학 각도(동=0, 반시계방향)는 회전 부호가 반대다.
        # 좌표계 부호를 따지지 말고, 실제로 증가한 방향을 보고 양수로 뒤집는다.
        ang = np.unwrap(np.arctan2(y[i:j + 1] - cy, x[i:j + 1] - cx))
        turns = (ang - ang[0]) / (2 * np.pi)
        if turns[-1] < 0:
            turns = -turns
        ta, tt = t[i:j + 1], turns
        for k in range(1, int(np.floor(tt.max())) + 1):
            idx = int(np.searchsorted(tt, k))
            if idx == 0 or idx >= len(tt):
                continue
            f = (k - tt[idx - 1]) / (tt[idx] - tt[idx - 1])
            t_end = float(ta[idx - 1] + f * (ta[idx] - ta[idx - 1]))
            t_beg = laps_rows[-1]["t_end_s"] if laps_rows and laps_rows[-1]["arc"] == n else float(ta[0])
            mask = (ta >= t_beg) & (ta <= t_end)
            vm = float(v[i:j + 1][mask].mean())
            laps_rows.append({
                "arc": n, "turn": TURN_NAME[s], "lap": k,
                "t_end_s": round(t_end, 3), "lap_time_s": round(t_end - t_beg, 3),
                "v_avg_kmh": round(vm, 2), "circle_r_m": round(r, 2),
                "lat_acc_g": round((vm / 3.6) ** 2 / r / G, 3),
            })
    lp, ar = pd.DataFrame(laps_rows), pd.DataFrame(arc_rows)
    # 호 하나가 몇 바퀴를 채웠는지 표에 적어둔다. 0 이면 스키드패드가 아니라
    # 반지름만 맞은 일반 코너다 (오토크로스 트랙 코너가 여기 걸린다).
    n = lp.groupby("arc").size() if not lp.empty else pd.Series(dtype=int)
    ar["laps"] = ar["arc"].map(n).fillna(0).astype(int) if not ar.empty else []
    return lp, ar


# ---------------------------------------------------------------- 묶음

def analyze_recording(event_dir: Path, rec_id: str, lat0: float, lon0: float):
    """한 레코딩에서 가속·스키드패드를 분석해 (accel, skid_laps, skid_arcs) 반환."""
    import pyarrow.parquet as pq

    gps = pq.read_table(event_dir / "normalized" / rec_id / "gps.parquet").to_pandas()
    accel = accel_runs(gps, lat0, lon0)
    if not accel.empty:
        accel.insert(0, "recording_id", rec_id)

    _, t, _, _, v, _ = _prep(gps, lat0, lon0)
    accel_starts = set(accel["t_start_s"]) if not accel.empty else set()
    laps_all, arcs_all = [], []
    for i, (a, b) in enumerate(lapmod.find_runs(t, v), 1):
        if any(abs(s - t[a]) < 30 for s in accel_starts):
            continue                                   # 가속으로 잡힌 구간은 건너뛴다
        lp, ar = skidpad_laps(gps, lat0, lon0, t[a], t[b])
        for df, acc in ((lp, laps_all), (ar, arcs_all)):
            if not df.empty:
                df.insert(0, "run", i)
                df.insert(0, "recording_id", rec_id)
                acc.append(df)
    return (accel,
            pd.concat(laps_all, ignore_index=True) if laps_all else pd.DataFrame(),
            pd.concat(arcs_all, ignore_index=True) if arcs_all else pd.DataFrame())


def run_event(event_dir: Path, rec_ids: list[str] | None = None):
    """이벤트의 가속·스키드패드를 분석해 derived/ 에 표로 저장한다."""
    import json

    import pyarrow.parquet as pq

    norm = event_dir / "normalized"
    gate = json.loads((event_dir / "derived" / "gate.json").read_text(encoding="utf-8"))
    lat0, lon0 = gate["origin"]["lat0"], gate["origin"]["lon0"]
    if rec_ids is None:
        rec_ids = [p.name for p in lapmod.gps_recordings(norm)
                   if pq.ParquetFile(p / "gps.parquet").metadata.num_rows > 0]

    accels, laps_all, arcs_all = [], [], []
    for rid in rec_ids:
        a, l, ar = analyze_recording(event_dir, rid, lat0, lon0)
        for df, acc in ((a, accels), (l, laps_all), (ar, arcs_all)):
            if not df.empty:
                acc.append(df)

    out = event_dir / "derived"
    out.mkdir(exist_ok=True)
    res = {}
    for name, parts in (("accel_runs", accels), ("skidpad_laps", laps_all), ("skidpad_arcs", arcs_all)):
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
        res[name] = df
    return res["accel_runs"], res["skidpad_laps"], res["skidpad_arcs"]
