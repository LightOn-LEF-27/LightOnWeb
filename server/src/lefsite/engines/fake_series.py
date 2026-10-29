"""가짜 연결부의 그래프 값 (계약 §6-4 F5) — 합성 신호. 실제 주행이 아니다.

랩 안의 위치(0 ~ 1)에 따라 정해지는 곡선 + 랩마다 조금씩 다른 흔들림이라, 같은 세션 · 같은 랩이면 늘 같은 값이 나온다.
품질 경고(`t_ranges`)가 있는 블록은 그 구간을 비워(null) 화면이 선을 끊는지 볼 수 있게 한다.
"""

from __future__ import annotations

import math
import random

from .base import EngineError

TAU = 2 * math.pi
MAX_POINTS = 20_000
MAX_CHANNELS = 16
LAP_LEN_M = 858.0


def _speed(ph: float, vmax: float, j: float) -> float:
    ph += j * 0.4                                     # 랩마다 코너에 들어가는 자리도 조금씩 다르다
    return max(12.0, vmax * (0.62 + 0.26 * math.sin(TAU * 2 * ph) + 0.1 * math.sin(TAU * 5 * ph + 1)) * (1 + j))


def _dv(ph: float, vmax: float, j: float) -> float:
    return (_speed(ph + 0.002, vmax, j) - _speed(ph, vmax, j)) / 0.002 / 100.0


def _signals(ph: float, frac: float, vmax: float, j: float) -> dict[str, float]:
    """랩 안 위치 ph · 세션 안 위치 frac(0 ~ 1) → 채널마다 값."""
    v = _speed(ph, vmax, j)
    dv = _dv(ph, vmax, j)
    steer = 40 * math.sin(TAU * 3 * ph + 0.5) + 14 * math.sin(TAU * 7 * ph)
    lat = steer / 60 * (v / 100) ** 2 * 1.7
    apps = 100 / (1 + math.exp(-dv * 4))
    brake = max(0.0, min(75.0, -dv * 30))
    cur = apps * 2.3 - brake * 0.8
    volt = 312 - frac * 18 - cur * 0.06
    r = 1 + 0.28 * math.cos(3 * TAU * ph) + 0.12 * math.sin(2 * TAU * ph)
    return {
        "gps.speed_kmh": v, "gps.x_m": r * math.cos(TAU * ph) * 160, "gps.y_m": r * math.sin(TAU * ph) * 95,
        "imu.long_g": max(-1.3, min(0.8, dv * 0.35)), "imu.lat_g": lat, "imu.yaw_dps": lat * 22,
        "pedal.apps_pct": apps, "pedal.brake_f_bar": brake, "steer.angle_deg": steer,
        "wheel.fl_kmh": v * 1.01, "wheel.fr_kmh": v * 0.99,
        "battery.current_a": cur, "battery.voltage_v": volt, "battery.power_kw": cur * volt / 1000,
        "bms.soc_pct": 96 - frac * 22, "bms.cell_min_v": 3.95 - frac * 0.25 - max(cur, 0) * 0.0009, "bms.temp_max_c": 30 + frac * 14,
        "motor.rpm": v * 68, "motor.inverter_c": 36 + frac * 22 + apps * 0.03, "motor.motor_c": 40 + frac * 30,
        "cooling.coolant_in_c": 33 + frac * 15,
        "suspension.fl_mm": 32 + lat * 6 - dv * 1.5, "suspension.rr_mm": 30 - lat * 6 + dv * 1.2,
        "tire.fl_c": 55 + frac * 25 + abs(lat) * 3,
        "drive.state_est": 1.0 if dv > 0.15 else (-1.0 if dv < -0.35 else 0.0),
    }


def get_series(s, channels: dict[str, dict], lap_rows: list[dict], quality: tuple, channel_ids, x: str = "time",
               t0=None, t1=None, lap=None, max_points: int = 4000, smooth_s=None, d0=None, d1=None) -> dict:
    """s = FakeSession, channels = 그 세션의 채널 표 (id → list_channels 의 줄)."""
    if isinstance(channel_ids, str):
        channel_ids = [channel_ids]
    if not channel_ids or len(channel_ids) > MAX_CHANNELS:
        raise EngineError("bad_argument", f"채널은 1 ~ {MAX_CHANNELS}개: {len(channel_ids or [])}개")
    for c in channel_ids:
        if c not in channels:
            raise EngineError("unknown_channel", f"없는 채널: {c}")
    if x not in ("time", "distance", "ref_distance"):
        raise EngineError("bad_argument", f"x 는 time · distance · ref_distance 중 하나: {x!r}")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or not 2 <= max_points <= MAX_POINTS:
        raise EngineError("bad_argument", f"max_points 는 2 ~ {MAX_POINTS} 의 정수: {max_points!r}")
    if smooth_s is not None and not 0 <= smooth_s <= 10:
        raise EngineError("bad_argument", f"smooth_s 는 0 ~ 10 s: {smooth_s!r}")
    if x == "time" and (d0 is not None or d1 is not None):
        raise EngineError("bad_argument", "시간축에는 d0 · d1 을 쓰지 않는다 (t0 · t1)")
    if x != "time" and (t0 is not None or t1 is not None):
        raise EngineError("bad_argument", "거리축에는 t0 · t1 을 쓰지 않는다 (d0 · d1)")
    rows = {r["lap"]: r for r in lap_rows}
    if lap is not None and lap not in rows:
        raise EngineError("bad_argument", f"세션 {s.sid} 에 랩 {lap} 이 없음", {"laps": sorted(rows)})
    if x != "time" and lap is None:
        raise EngineError("bad_argument", "거리축은 lap 을 꼭 줘야 한다")

    rate = max(channels[c]["rate_hz"] for c in channel_ids)
    if x == "time":
        lo, hi = (rows[lap]["t_start_s"], rows[lap]["t_end_s"]) if lap is not None else (0.0, s.duration_s)
        lo, hi = max(lo, t0 if t0 is not None else lo), min(hi, t1 if t1 is not None else hi)
        native = int((hi - lo) * rate)
    else:
        length = rows[lap].get("distance_m", LAP_LEN_M)
        lo, hi = max(0.0, d0 if d0 is not None else 0.0), min(length, d1 if d1 is not None else length)
        native = int((hi - lo) / length * rows[lap]["lap_time_s"] * rate)   # 그 거리를 달린 시간 동안의 표본 수
    if hi <= lo:
        raise EngineError("bad_argument", f"범위가 비었음: {lo} ~ {hi}")
    n = max(2, min(max_points, native))
    step = (hi - lo) / n
    xs = [lo + (i + 0.5) * step for i in range(n)]
    best = s.best_lap_s or 60.0

    def at(xv: float) -> tuple[float, float, float]:
        """x 값 → (랩 안 위치, 세션 안 위치, 랩마다 흔들림)."""
        if x == "time":
            hit = next((r for r in lap_rows if r["t_start_s"] <= xv < r["t_end_s"]), None)
            ph = (xv - hit["t_start_s"]) / (hit["t_end_s"] - hit["t_start_s"]) if hit else (xv / best) % 1.0
            k = hit["lap"] if hit else 0
            return ph, xv / s.duration_s, random.Random(f"{s.sid}/{k}").uniform(-0.03, 0.03)
        r = rows[lap]
        ph = xv / r.get("distance_m", LAP_LEN_M)
        return ph, (r["t_start_s"] + ph * (r["t_end_s"] - r["t_start_s"])) / s.duration_s, random.Random(f"{s.sid}/{lap}").uniform(-0.03, 0.03)

    gaps = [(q[2], rng) for q in quality if q[4] for rng in q[4]] if x == "time" else []
    vmax = s.vmax_kmh or 80.0
    out = []
    table = [_signals(ph, frac, vmax, j) for ph, frac, j in map(at, xs)]
    for c in channel_ids:
        meta = channels[c]
        block = c.split(".", 1)[0]
        d = meta["digits"]
        vals, mins, maxs = [], [], []
        for xv, sig in zip(xs, table):
            if any(b == block and a <= xv <= e for b, (a, e) in gaps):
                vals.append(None), mins.append(None), maxs.append(None)
                continue
            v = sig.get(c, 0.0)
            spread = 0.0 if "levels" in meta else abs(v) * 0.02 + 10 ** -d
            vals.append(round(v, d)), mins.append(round(v - spread, d)), maxs.append(round(v + spread, d))
        out.append({"channel_id": c, "unit": meta["unit"], "values": vals, "y_min": mins, "y_max": maxs, "samples": native})

    if x == "time":
        laps = [{"lap": r["lap"], "x_start": r["t_start_s"], "x_end": r["t_end_s"]} for r in lap_rows if lap is None or r["lap"] == lap]
    else:
        laps = [{"lap": lap, "x_start": 0.0, "x_end": rows[lap].get("distance_m", LAP_LEN_M)}]
    xinfo = {"kind": x, "unit": "s" if x == "time" else "m", "values": [round(v, 3 if x == "time" else 2) for v in xs], "lap": lap,
             "frame_id": s.event.frame_id if s.has_track else None}
    return {"sid": s.sid, "x": xinfo, "series": out, "laps": laps,
            "reduced": {"from_points": native, "to_points": n, "method": "minmax", "smooth_s": smooth_s}}
