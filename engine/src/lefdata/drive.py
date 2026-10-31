"""주행 상태 추정 — 가속 · 타력 · 제동 (공개 계층: 차량 · 폴더를 모른다).

    estimate(t_power, power_kw, t_speed, speed_kmh, dt_s=0.1, throttle=None)  →  (t, state)
        state = +1 가속 · 0 타력 · −1 제동 · NaN 모름 (서 있을 때 · 기록이 끊긴 곳)
        throttle = (t, on) — 가속페달 쪽 신호로 '밟았다' (1) · '뗐다' (0). 주면 가속을 전력 대신 이것으로 가른다 (전력은 None 이어도 된다)

가속페달 개도 · 브레이크 압력 신호가 없을 때 쓰는 **추정**이다.
    - 가속: 구동 전력이 POWER_ON_KW 보다 크다 — 모터가 차를 밀고 있다. 가속페달 신호(throttle)가 있으면 그것으로
    - 제동: 가속이 아니면서 속도가 BRAKE_G 보다 빠르게 준다 — 공기 · 구름 저항만으로는 이만큼 줄지 않는다
    - 타력: 둘 다 아님
속도의 변화율은 GPS 속도에서 낸다 (차체가 기울어도 중력이 섞이지 않는다).

가르지 못하는 것
    - 가속페달과 브레이크를 같이 밟은 순간 → 가속으로 나온다
    - 아주 가볍게 밟은 브레이크 → 타력으로 나온다
    - 회생 제동이 큰 차 → 전력은 음수지만 감속이 깊으면 제동으로 나온다 (회생인지 마찰 제동인지는 모른다)
    - 전력 · 속도를 잰 장비의 시각이 조금(0.1 ~ 0.2 s) 어긋날 수 있어 상태가 바뀌는 순간은 그만큼 흐리다
    - 전력으로 가를 때, 페달 끝에 발을 걸친 순간 — 모터 컨트롤러가 토크를 줄여 가는 동안 전력이 남아 가속으로 나오기도 한다
"""

from __future__ import annotations

import numpy as np

from . import series as ser

POWER_ON_KW = 0.5           # 이보다 크면 가속. 페달을 뗀 동안 전력은 0 근처(회생이면 음수)라 그 위에 두되,
                            # 전력 = 토크 × 회전수라 저속에서 가볍게 밟으면 1 kW 도 안 된다 — 높게 두면 그 주행을 타력 · 제동으로 놓친다
BRAKE_G = -0.15             # 이보다 깊게 줄면 제동. 타력 감속보다 깊고, 보통의 제동보다 얕다
STOP_KMH = 5.0              # 이보다 느리면 상태를 정하지 않는다
POWER_SMOOTH_S = 0.3        # 전력 이동평균
SPEED_SMOOTH_S = 0.5        # 속도 이동평균 (미분하기 전)
ACCEL_SMOOTH_S = 0.3        # 속도 변화율 이동평균
G = 9.80665
LEVELS = [{"value": 1, "label": "가속"}, {"value": 0, "label": "타력"}, {"value": -1, "label": "제동"}]


def estimate(t_power, power_kw, t_speed, speed_kmh, dt_s: float = 0.1, throttle=None) -> tuple[np.ndarray, np.ndarray]:
    """가속을 가르는 신호(throttle, 없으면 전력)와 속도가 함께 있는 시간에 dt_s 간격으로 상태를 낸다 (위 설명)."""
    if throttle is not None:
        tp, p = (np.asarray(x, float) for x in throttle)
    else:
        tp, p = np.asarray(t_power, float), np.asarray(power_kw, float)
    tv, v = np.asarray(t_speed, float), np.asarray(speed_kmh, float)
    if len(tp) < 2 or len(tv) < 3:
        return np.zeros(0), np.zeros(0)
    lo, hi = max(tp[0], tv[0]), min(tp[-1], tv[-1])
    if not hi > lo:
        return np.zeros(0), np.zeros(0)
    grid = np.arange(np.ceil(lo / dt_s) * dt_s, hi + 1e-9, dt_s)
    if throttle is not None:                            # 밟음 0 · 1 — 잇지 않고 가까운 표본
        on = ser.sample_at(tp, p, grid, ser.time_breaks(tp), nearest=True)
        on, known = on > 0.5, np.isfinite(on)
    else:
        pw = ser.sample_at(tp, ser.smooth(tp, p, POWER_SMOOTH_S), grid, ser.time_breaks(tp))
        on, known = pw > POWER_ON_KW, np.isfinite(pw)
    vs = ser.smooth(tv, v, SPEED_SMOOTH_S)
    acc = ser.smooth(tv, np.gradient(vs / 3.6, tv) / G, ACCEL_SMOOTH_S)
    brk = ser.time_breaks(tv)
    sp = ser.sample_at(tv, v, grid, brk)
    ac = ser.sample_at(tv, acc, grid, brk)
    state = np.where(on, 1.0, np.where(ac < BRAKE_G, -1.0, 0.0))
    state[~(known & np.isfinite(ac) & np.isfinite(sp)) | (sp < STOP_KMH)] = np.nan
    return grid, state


def shares(state) -> dict[int, float]:
    """상태별 비율 (값이 있는 시간 중) — {1: 가속, 0: 타력, -1: 제동}."""
    st = np.asarray(state, float)
    st = st[np.isfinite(st)]
    n = len(st)
    return {k: (float(np.mean(st == k)) if n else float("nan")) for k in (1, 0, -1)}


def agreement(t_a, state_a, t_b, state_b, dt_s: float = 0.1) -> tuple[float, int]:
    """같은 dt_s 칸에서 낸 두 상태열이 같은 순간 같은 상태인 비율 (둘 다 값이 있는 칸 중) → (비율, 칸 수)."""
    ka, kb = (np.round(np.asarray(t, float) / dt_s).astype(np.int64) for t in (t_a, t_b))
    _, ia, ib = np.intersect1d(ka, kb, return_indices=True)
    a, b = np.asarray(state_a, float)[ia], np.asarray(state_b, float)[ib]
    ok = np.isfinite(a) & np.isfinite(b)
    return (float(np.mean(a[ok] == b[ok])) if ok.any() else float("nan")), int(ok.sum())
