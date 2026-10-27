"""주행 상태 추정 — 가속 · 타력 · 제동 (공개 계층: 차량 · 폴더를 모른다).

    estimate(t_power, power_kw, t_speed, speed_kmh, dt_s=0.1)  →  (t, state)
        state = +1 가속 · 0 타력 · −1 제동 · NaN 모름 (서 있을 때 · 기록이 끊긴 곳)

가속페달 개도 · 브레이크 압력 신호가 없을 때 쓰는 **추정**이다.
    - 가속: 구동 전력이 POWER_ON_KW 보다 크다 — 모터가 차를 밀고 있다
    - 제동: 가속이 아니면서 속도가 BRAKE_G 보다 빠르게 준다 — 공기 · 구름 저항만으로는 이만큼 줄지 않는다
    - 타력: 둘 다 아님
속도의 변화율은 GPS 속도에서 낸다 (차체가 기울어도 중력이 섞이지 않는다).

가르지 못하는 것
    - 가속페달과 브레이크를 같이 밟은 순간 → 가속으로 나온다
    - 아주 가볍게 밟은 브레이크 → 타력으로 나온다
    - 회생 제동이 큰 차 → 전력은 음수지만 감속이 깊으면 제동으로 나온다 (회생인지 마찰 제동인지는 모른다)
    - 전력 · 속도를 잰 장비의 시각이 조금(0.1 ~ 0.2 s) 어긋날 수 있어 상태가 바뀌는 순간은 그만큼 흐리다
"""

from __future__ import annotations

import numpy as np

from . import series as ser

POWER_ON_KW = 2.0           # 이보다 크면 가속. 타력 중에도 전력이 0 근처(조금 음수)로 나오므로 0 보다 위에 둔다
BRAKE_G = -0.15             # 이보다 깊게 줄면 제동. 타력 감속보다 깊고, 보통의 제동보다 얕다
STOP_KMH = 5.0              # 이보다 느리면 상태를 정하지 않는다
POWER_SMOOTH_S = 0.3        # 전력 이동평균
SPEED_SMOOTH_S = 0.5        # 속도 이동평균 (미분하기 전)
ACCEL_SMOOTH_S = 0.3        # 속도 변화율 이동평균
G = 9.80665
LEVELS = [{"value": 1, "label": "가속"}, {"value": 0, "label": "타력"}, {"value": -1, "label": "제동"}]


def estimate(t_power, power_kw, t_speed, speed_kmh, dt_s: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """두 신호가 함께 있는 시간에 dt_s 간격으로 상태를 낸다 (위 설명)."""
    tp, p = np.asarray(t_power, float), np.asarray(power_kw, float)
    tv, v = np.asarray(t_speed, float), np.asarray(speed_kmh, float)
    if len(tp) < 2 or len(tv) < 3:
        return np.zeros(0), np.zeros(0)
    lo, hi = max(tp[0], tv[0]), min(tp[-1], tv[-1])
    if not hi > lo:
        return np.zeros(0), np.zeros(0)
    grid = np.arange(np.ceil(lo / dt_s) * dt_s, hi + 1e-9, dt_s)
    pw = ser.sample_at(tp, ser.smooth(tp, p, POWER_SMOOTH_S), grid, ser.time_breaks(tp))
    vs = ser.smooth(tv, v, SPEED_SMOOTH_S)
    acc = ser.smooth(tv, np.gradient(vs / 3.6, tv) / G, ACCEL_SMOOTH_S)
    brk = ser.time_breaks(tv)
    sp = ser.sample_at(tv, v, grid, brk)
    ac = ser.sample_at(tv, acc, grid, brk)
    state = np.where(pw > POWER_ON_KW, 1.0, np.where(ac < BRAKE_G, -1.0, 0.0))
    state[~(np.isfinite(pw) & np.isfinite(ac) & np.isfinite(sp)) | (sp < STOP_KMH)] = np.nan
    return grid, state


def shares(state) -> dict[int, float]:
    """상태별 비율 (값이 있는 시간 중) — {1: 가속, 0: 타력, -1: 제동}."""
    st = np.asarray(state, float)
    st = st[np.isfinite(st)]
    n = len(st)
    return {k: (float(np.mean(st == k)) if n else float("nan")) for k in (1, 0, -1)}
