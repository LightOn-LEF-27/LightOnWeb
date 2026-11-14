"""종목별 구간 분석 — 가속(75 m) · 스키드패드(8자) · 짐카나(정지 → 코스 → 같은 자리 정지).

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
    - 바퀴는 **8자의 두 원이 맞닿는 점(교차점 — FSAE 계측선)을 지날 때마다** 센다 (2026-11-14). 그 전에는 호의 시작에서 360° 마다 셌는데,
      마지막 원을 나가며 제동해 바깥으로 몇 m 밀린 2바퀴째가 빠졌다 (호가 2.00 바퀴 조금 앞, 원에서 벗어난 곳에서 끝났다). 짝이 되는 반대 방향 원이
      없으면(원 하나만 돈 주행) 예전처럼 호의 시작부터 360° 마다
    - 호의 양 끝은 각속도의 불감대 · 이동평균에 깎인다 → 원을 맞춘 뒤 **차가 그 원 근처(SKID_EXTEND_TOL_M)에 있는 동안으로 늘린다**.
      같은 원을 같은 방향으로 돌다 각속도가 잠깐 불감대로 떨어져 둘로 갈린 호는 합친다
    - 횡가속도는 v² / r 로 계산한다 (IMU 와 교차검증 가능)

짐카나 (2026-11-14 트랙데이 — 콘으로 만든 짧은 코스, 계측선이 없다)
    - 출발 · 끝이 정지다: 입구에서 몇 초 섰다가 출발해 코스를 돌고 **같은 자리로 돌아와 선다**
    - 그래서 '서 있던 곳(GYM_STOP_S 이상) → 주행 → 다음 정지' 중 두 정지 자리가 GYM_SAME_PLACE_M 안인 것을 찾는다
    - 기록 = 출발한 순간(정지의 마지막 표본) ~ 다시 선 순간(정지의 첫 표본). 정지는 1 초 가운데값으로 고른 속도로 가린다 (GPS 속도의 잡음)
    - 스키드패드(그 안에 원 SKID_LAPS_MIN 바퀴 이상)와 가속(같은 출발)은 짐카나로 세지 않는다. 트랙 주행(피트 → 랩 → 같은 피트)은
      이 표에 섞일 수 있다 — 종목은 세션이 정한다 (랩이 있으면 트랙)
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
SKID_EXTEND_TOL_M = 4.0  # 호의 양 끝을 원에서 이 안에 있는 동안 늘린다 (나가며 제동하면 바깥으로 3 m 쯤 밀린 채 계측선을 지난다)
SKID_PAIR_TOL = 0.25     # 두 원의 중심 거리가 반지름 합의 이 비율 안이면 8자 짝 (맞닿는 점 = 계측선)
SKID_SAME_CIRCLE_M = 2.0  # 같은 방향 이웃 호의 중심이 이 안이고 그 사이도 원 위면 같은 원 — 합친다
SKID_LAPS_MIN = 2        # 원을 이만큼 돈 주행은 스키드패드 (짐카나 안의 한 바퀴 선회와 가른다)

# --- 짐카나
GYM_STOP_S = 3.0         # 이만큼 서 있어야 출발 · 끝의 정지로 본다 (트랙데이: 입구에서 약 5 s, 끝은 더 길게)
GYM_SAME_PLACE_M = 15.0  # 출발한 자리와 다시 선 자리가 이 안이면 같은 자리로 돌아온 것
GYM_MIN_DIST_M = 150.0   # 이보다 짧은 주행은 짐카나가 아니다 (차를 옮긴 것)
GYM_MIN_S = 15.0
GYM_MIN_TOP_KMH = 20.0
GYM_SMOOTH_S = 1.0       # 정지를 가릴 속도는 이 길이의 가운데값으로 고른다
GPS_GAP_S = 1.0          # GPS 표본 사이가 이보다 벌어지면 정지를 이어 보지 않는다
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
    arcs = _circle_arcs(t, x, y, c, v)
    for n, a in enumerate(arcs, 1):
        i, j, s, cx, cy, r, rms, ok = a["i"], a["j"], a["s"], a["cx"], a["cy"], a["r"], a["rms"], a["ok"]
        arc_rows.append({
            "arc": n, "turn": TURN_NAME[s],
            "t_start_s": round(float(t[i]), 2), "duration_s": round(float(t[j] - t[i]), 2),
            "circle_r_m": round(r, 2), "fit_rms_m": round(rms, 2),
            "v_avg_kmh": round(float(v[i:j + 1].mean()), 2), "used": bool(ok),
        })
        if not ok:
            continue
        i, j = a["ei"], a["ej"]                          # 원 근처에 있던 동안 전부 (불감대 · 이동평균이 깎은 끝을 되돌림)
        ta = t[i:j + 1]
        # GPS 방위(북=0, 시계방향)와 수학 각도(동=0, 반시계방향)는 회전 부호가 반대다.
        # 좌표계 부호를 따지지 말고, 실제로 증가한 방향을 보고 양수로 뒤집는다.
        ang = np.unwrap(np.arctan2(y[i:j + 1] - cy, x[i:j + 1] - cx))
        sgn = 1.0 if ang[-1] >= ang[0] else -1.0
        mate = _mate(arcs, n - 1)
        if mate is not None:                             # 계측선 = 짝 원의 중심 쪽 (두 원이 맞닿는 점) — 지날 때마다 한 바퀴
            theta = float(np.arctan2(mate["cy"] - cy, mate["cx"] - cx))
            u = np.maximum.accumulate(sgn * (ang - theta) / (2 * np.pi))
            marks = range(int(np.ceil(u[0])), int(np.floor(u[-1])) + 1)
        else:                                            # 짝이 없는 원 — 호의 시작부터 360° 마다
            u = np.maximum.accumulate(sgn * (ang - ang[0]) / (2 * np.pi))
            marks = range(0, int(np.floor(u[-1])) + 1)
        arc_rows[-1].update(t_on_circle_s=round(float(ta[0]), 2), on_circle_s=round(float(ta[-1] - ta[0]), 2),
                            turns=round(float(u[-1] - u[0]), 3), lap_from="crossing" if mate is not None else "arc_start")
        cross = []
        for m in marks:                                  # u 가 정수 m 을 지난 시각
            idx = int(np.searchsorted(u, m))
            if idx >= len(u):
                continue
            if idx == 0:
                cross.append(float(ta[0]))
                continue
            f = (m - u[idx - 1]) / (u[idx] - u[idx - 1]) if u[idx] > u[idx - 1] else 0.0
            cross.append(float(ta[idx - 1] + f * (ta[idx] - ta[idx - 1])))
        for k in range(1, len(cross)):
            t_beg, t_end = cross[k - 1], cross[k]
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


def _fit(x, y, i: int, j: int) -> dict:
    cx, cy, r = fit_circle(x[i:j + 1], y[i:j + 1])
    rms = float(np.sqrt(((np.hypot(x[i:j + 1] - cx, y[i:j + 1] - cy) - r) ** 2).mean()))
    return {"cx": cx, "cy": cy, "r": r, "rms": rms, "ok": bool(SKID_MIN_R <= r <= SKID_MAX_R and rms <= SKID_MAX_RMS_M)}


def _mate(arcs: list[dict], k: int) -> dict | None:
    """호 k 의 8자 짝 — 바로 앞이나 뒤의 반대 방향 원 중 두 원이 맞닿는 것 (중심 거리 ≈ 반지름 합). 없으면 None."""
    a = arcs[k]
    for m in (k - 1, k + 1):
        if 0 <= m < len(arcs) and arcs[m]["ok"] and arcs[m]["s"] == -a["s"]:
            b = arcs[m]
            if abs(np.hypot(b["cx"] - a["cx"], b["cy"] - a["cy"]) - (a["r"] + b["r"])) <= SKID_PAIR_TOL * (a["r"] + b["r"]):
                return b
    return None


def _on_circle(x, y, a: dict) -> np.ndarray:
    return np.abs(np.hypot(x - a["cx"], y - a["cy"]) - a["r"]) <= SKID_EXTEND_TOL_M


def _circle_arcs(t, x, y, c, v) -> list[dict]:
    """좌 · 우 호 → 원을 맞춘 호들 {i, j (각속도로 가른 끝), ei, ej (원 위에 있던 동안), s, cx, cy, r, rms, ok}.

    같은 원을 같은 방향으로 돌다 둘로 갈린 호(중심이 SKID_SAME_CIRCLE_M 안 · 사이도 원 위)는 합친다.
    맞은 호는 양 끝을 원 근처(SKID_EXTEND_TOL_M)에 있는 동안으로 늘린다. 8자의 이웃 원 쪽으로도 늘린다 — 계측선(두 원이 맞닿는 점)이
    바로 호 경계라 거기서 멈추면 마지막 교차를 몇 도 차이로 놓친다. 두 원은 맞닿는 점을 지나면 금방 벌어지므로 거리로 멈춘다.
    """
    arcs: list[dict] = []
    for i, j, s in _yaw_arcs(t, c, v):
        a = {"i": i, "j": j, "s": s, **_fit(x, y, i, j)}
        p = arcs[-1] if arcs else None
        if p is not None and a["ok"] and p["ok"] and p["s"] == s and np.hypot(p["cx"] - a["cx"], p["cy"] - a["cy"]) <= SKID_SAME_CIRCLE_M \
                and _on_circle(x[p["j"]:i + 1], y[p["j"]:i + 1], a).all():
            both = {"i": p["i"], "j": j, "s": s, **_fit(x, y, p["i"], j)}
            if both["ok"]:
                arcs[-1] = both
                continue
        arcs.append(a)
    for k, a in enumerate(arcs):
        a["ei"], a["ej"] = a["i"], a["j"]
        if not a["ok"]:
            continue
        lo, hi = 0, len(t) - 1
        on = _on_circle(x, y, a)
        while a["ei"] > lo and on[a["ei"] - 1]:
            a["ei"] -= 1
        while a["ej"] < hi and on[a["ej"] + 1]:
            a["ej"] += 1
    return arcs


# ---------------------------------------------------------------- 짐카나

def _stops(t: np.ndarray, v: np.ndarray) -> list[tuple[int, int]]:
    """GYM_STOP_S 이상 서 있던 동안 [(첫 표본, 마지막 표본)]. 속도는 GYM_SMOOTH_S 가운데값, 표본이 GPS_GAP_S 넘게 비면 끊는다."""
    if len(t) < 2:
        return []
    half = max(int(round(GYM_SMOOTH_S / 2 / max(float(np.median(np.diff(t))), 1e-3))), 0)
    vs = pd.Series(v).rolling(2 * half + 1, center=True, min_periods=1).median().to_numpy()
    still = vs <= STOP_KMH
    out, i, n = [], 0, len(t)
    while i < n:
        if not still[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and still[j + 1] and t[j + 1] - t[j] <= GPS_GAP_S:
            j += 1
        if t[j] - t[i] >= GYM_STOP_S:
            out.append((i, j))
        i = j + 1
    return out


def gymkhana_runs(gps: pd.DataFrame, lat0: float, lon0: float) -> pd.DataFrame:
    """정지 → 주행 → 같은 자리 정지 (짐카나). 한 줄 = 한 번 달린 것: 출발 · 끝 시각, 기록, 거리, 최고 속도, 돌아온 자리 차이."""
    g, t, x, y, v, _ = _prep(gps, lat0, lon0)
    stops = _stops(t, v)
    rows = []
    for (a0, a1), (b0, b1) in zip(stops, stops[1:]):
        sl = slice(a1, b0 + 1)
        dist = float(_cumdist(x[sl], y[sl])[-1]) if b0 > a1 else 0.0
        gap = float(np.hypot(np.median(x[b0:b1 + 1]) - np.median(x[a0:a1 + 1]), np.median(y[b0:b1 + 1]) - np.median(y[a0:a1 + 1])))
        dur = float(t[b0] - t[a1])
        vmax = float(v[sl].max())
        if dur < GYM_MIN_S or dist < GYM_MIN_DIST_M or vmax < GYM_MIN_TOP_KMH or gap > GYM_SAME_PLACE_M:
            continue
        rows.append({
            "t_abs_start": g["t_abs"].iloc[a1],
            "t_start_s": round(float(t[a1]), 3), "t_end_s": round(float(t[b0]), 3), "time_s": round(dur, 3),
            "distance_m": round(dist, 1), "v_max_kmh": round(vmax, 2), "v_avg_kmh": round(dist / dur * 3.6, 2),
            "return_gap_m": round(gap, 1), "stop_before_s": round(float(t[a1] - t[a0]), 1), "stop_after_s": round(float(t[b1] - t[b0]), 1),
            "gps_gap_max_s": round(float(np.diff(t[sl]).max()), 2) if b0 > a1 else 0.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 묶음

def analyze_recording(event_dir: Path, rec_id: str, lat0: float, lon0: float):
    """한 레코딩에서 가속 · 스키드패드 · 짐카나를 분석해 (accel, skid_laps, skid_arcs, gymkhana) 반환."""
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
    skid = pd.concat(laps_all, ignore_index=True) if laps_all else pd.DataFrame()
    gym = gymkhana_runs(gps, lat0, lon0)
    if not gym.empty:                                  # 원을 SKID_LAPS_MIN 바퀴 넘게 돈 주행 · 가속은 짐카나가 아니다
        ends = skid["t_end_s"].to_numpy() if not skid.empty else np.zeros(0)
        circles = np.array([int(((ends > a) & (ends <= b)).sum()) for a, b in zip(gym["t_start_s"], gym["t_end_s"])])
        launch = np.array([any(abs(s - a) < 5 for s in accel_starts) for a in gym["t_start_s"]], dtype=bool)
        gym = gym[(circles < SKID_LAPS_MIN) & ~launch].reset_index(drop=True)
        gym.insert(0, "recording_id", rec_id)
    return (accel, skid,
            pd.concat(arcs_all, ignore_index=True) if arcs_all else pd.DataFrame(), gym)


def run_event(event_dir: Path, rec_ids: list[str] | None = None):
    """이벤트의 가속 · 스키드패드 · 짐카나를 분석해 derived/ 에 표로 저장한다 (accel_runs · skidpad_laps · skidpad_arcs · gymkhana_runs)."""
    import json

    import pyarrow.parquet as pq

    norm = event_dir / "normalized"
    gate = json.loads((event_dir / "derived" / "gate.json").read_text(encoding="utf-8"))
    lat0, lon0 = gate["origin"]["lat0"], gate["origin"]["lon0"]
    if rec_ids is None:
        rec_ids = [p.name for p in lapmod.gps_recordings(norm)
                   if pq.ParquetFile(p / "gps.parquet").metadata.num_rows > 0]

    accels, laps_all, arcs_all, gyms = [], [], [], []
    for rid in rec_ids:
        a, l, ar, gy = analyze_recording(event_dir, rid, lat0, lon0)
        for df, acc in ((a, accels), (l, laps_all), (ar, arcs_all), (gy, gyms)):
            if not df.empty:
                acc.append(df)

    out = event_dir / "derived"
    out.mkdir(exist_ok=True)
    res = {}
    for name, parts in (("accel_runs", accels), ("skidpad_laps", laps_all), ("skidpad_arcs", arcs_all), ("gymkhana_runs", gyms)):
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
        res[name] = df
    return res["accel_runs"], res["skidpad_laps"], res["skidpad_arcs"]
