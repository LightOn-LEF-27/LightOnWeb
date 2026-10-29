"""가짜 연결부의 코스 · 구간 · 시간 차 (계약 §6-6 F7 · §6-8 F13 · §6-9 F14) — 합성 값, 실제 주행이 아니다.

랩마다 "기준 거리 → 그 거리까지 걸린 시간" 표(랩 시계)를 하나 만들고, 랩 표의 S1 ~ S3 · 미니 구간 · 시간 차 · 트랙맵 점의 시각을 전부 거기서 꺼낸다.
그래서 계약의 규칙(구간 합 = 랩타임, 같은 랩끼리 시간 차 = 0, 끝 값 = 랩타임 차)이 저절로 맞는다.
"""

from __future__ import annotations

import bisect
import math
import random
from functools import lru_cache

from .base import EngineError
from .fake_series import TAU, _signals, _speed

REF_LEN_M = 858.0          # 합성 코스의 기준 길이 (frame 마다 하나 — 가짜는 코스가 하나뿐)
STEP_M = 5.0               # 랩 시계의 칸
GPS_HZ = 18.0
MINI_SIZES = (10, 25, 50)


def geometry(ph: float) -> tuple[float, float]:
    """랩 안 위치(0 ~ 1) → 평면 좌표 (m, x 동쪽 · y 북쪽) — 그래프 값의 gps.x_m · y_m 과 같은 곡선."""
    r = 1 + 0.28 * math.cos(3 * TAU * ph) + 0.12 * math.sin(2 * TAU * ph)
    return r * math.cos(TAU * ph) * 160, r * math.sin(TAU * ph) * 95


def heading(ph: float) -> float:
    """진행 방위 (북 = 0, 시계 방향, 도)."""
    (x0, y0), (x1, y1) = geometry(ph), geometry(ph + 1e-4)
    return round(math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360, 1)


def jitter(sid: str, lap: int) -> float:
    return random.Random(f"{sid}/{lap}").uniform(-0.03, 0.03)     # fake_series 의 랩마다 흔들림과 같은 값


@lru_cache(maxsize=512)
def lap_clock(sid: str, lap: int, lap_time: float, vmax: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """랩 시계 — (기준 거리 칸, 그 거리까지 걸린 시간). 시간은 1/속도 에 비례해 나누고 끝 = 랩타임."""
    n = math.ceil(REF_LEN_M / STEP_M)
    ds = [min(i * STEP_M, REF_LEN_M) for i in range(n + 1)]
    j = jitter(sid, lap)
    w = [(b - a) / _speed((a + b) / 2 / REF_LEN_M, vmax, j) for a, b in zip(ds, ds[1:])]
    total = sum(w)
    ts, acc = [0.0], 0.0
    for x in w:
        acc += x
        ts.append(acc / total * lap_time)
    return tuple(ds), tuple(ts)


def at_distance(clock, d: float) -> float:
    ds, ts = clock
    d = min(max(d, 0.0), REF_LEN_M)
    i = min(max(bisect.bisect_right(ds, d) - 1, 0), len(ds) - 2)
    return ts[i] + (ts[i + 1] - ts[i]) * (d - ds[i]) / (ds[i + 1] - ds[i])


def at_time(clock, t: float) -> float:
    ds, ts = clock
    t = min(max(t, 0.0), ts[-1])
    i = min(max(bisect.bisect_right(ts, t) - 1, 0), len(ts) - 2)
    return ds[i] + (ds[i + 1] - ds[i]) * (t - ts[i]) / (ts[i + 1] - ts[i])


def clock_of(s, row: dict):
    return lap_clock(s.sid, row["lap"], row["lap_time_s"], s.vmax_kmh or 80.0)


def sector_times(s, row: dict) -> tuple[float, float, float]:
    """랩 표의 S1 ~ S3 — 기준 길이 3 등분 경계를 지난 시각으로 (합 = 랩타임)."""
    c = lap_clock(s.sid, row["lap"], row["lap_time_s"], s.vmax_kmh or 80.0)
    s1 = round(at_distance(c, REF_LEN_M / 3), 3)
    s2 = round(at_distance(c, 2 * REF_LEN_M / 3) - s1, 3)
    return s1, s2, round(row["lap_time_s"] - s1 - s2, 3)


def _row(rows: list[dict], lap, sid: str) -> dict:
    if isinstance(lap, bool) or not isinstance(lap, int):
        raise EngineError("bad_argument", f"lap 은 랩 번호(정수)여야 함: {lap!r}")
    hit = next((r for r in rows if r["lap"] == lap), None)
    if hit is None:
        raise EngineError("bad_argument", f"세션 {sid} 에 랩 {lap} 이 없음", {"laps": [r["lap"] for r in rows]})
    return hit


def _points(s, row: dict) -> list[tuple[float, float]]:
    """랩 한 바퀴의 GPS 점 — (랩 시작부터 시간, 기준 거리). 첫 점 = 계측선, 끝 점 = 다시 계측선."""
    c = clock_of(s, row)
    n = max(2, int(row["lap_time_s"] * GPS_HZ))
    return [(t, at_time(c, t)) for t in (row["lap_time_s"] * i / (n - 1) for i in range(n))]


def get_track(s, rows: list[dict], channels: dict, lap=None, color=None, smooth_s=None) -> dict:
    if not s.has_track:
        raise EngineError("bad_argument", f"세션 {s.sid} 에는 코스(위치) 기록이 없음")
    vmax = s.vmax_kmh or 80.0
    if lap == "all" and not rows:                          # 랩이 없는 세션 (가속 · 스키드패드) — 코스 일부를 지나간 궤적
        length = min((s.distance_km or 0.3) * 1000, REF_LEN_M)
        row = {"lap": 0, "lap_time_s": s.duration_s, "t_start_s": 0.0, "distance_m": REF_LEN_M}
        n = max(2, int(s.duration_s * GPS_HZ))
        pts = [(row, t, t / s.duration_s * length) for t in (s.duration_s * i / (n - 1) for i in range(n))]
        lap_out = None
    elif lap == "all":
        pts = [(r, t, d) for r in rows for t, d in _points(s, r)]
        lap_out = None
    else:
        lap = s.best_lap if lap is None else lap
        if lap is None:                                     # 랩이 없는 세션 — 한 바퀴 모양만
            row = {"lap": 0, "lap_time_s": s.duration_s, "t_start_s": 0.0, "t_end_s": s.duration_s, "distance_m": REF_LEN_M}
        else:
            row = _row(rows, lap, s.sid)
        pts = [(row, t, d) for t, d in _points(s, row)]
        lap_out = lap
    xs, ys, ts, dist, refs = [], [], [], [], []
    run = 0.0
    for k, (r, t, d) in enumerate(pts):
        x, y = geometry(d / REF_LEN_M)
        if k and lap == "all" and pts[k - 1][0] is not r:
            run += pts[k - 1][2] * r.get("distance_m", REF_LEN_M) / REF_LEN_M
        xs.append(round(x, 2)), ys.append(round(y, 2)), ts.append(round(r["t_start_s"] + t, 3))
        dist.append(round(run + d * r.get("distance_m", REF_LEN_M) / REF_LEN_M, 2)), refs.append(round(d, 2))
    gx, gy = geometry(0.0)
    out = {"sid": s.sid, "lap": lap_out, "t_s": ts, "x_m": xs, "y_m": ys, "dist_m": dist,
           "gate": {"x_m": round(gx, 2), "y_m": round(gy, 2), "heading_deg": heading(0.0), "half_width_m": 30.0},
           "frame_id": s.event.frame_id, "direction": "ccw", "frame": "평면 좌표 (m, 합성) — x 동쪽 · y 북쪽. 계측선(gate)도 같은 좌표",
           "ref_m": None if lap == "all" else refs}
    if color is not None:
        if color not in channels:
            raise EngineError("unknown_channel", f"없는 채널: {color}")
        meta = channels[color]
        vals = []
        for (r, t, d) in pts:
            sig = _signals(d / REF_LEN_M, (r["t_start_s"] + t) / s.duration_s, vmax, jitter(s.sid, r["lap"]))
            vals.append(round(sig.get(color, 0.0), meta["digits"]))
        out["color"] = {"channel_id": color, "unit": meta["unit"], "values": vals, "smooth_s": smooth_s}
        if "levels" in meta:
            out["color"]["levels"] = meta["levels"]
    return out


def _mini_edges(mini_m) -> list[float]:
    if isinstance(mini_m, bool) or mini_m not in MINI_SIZES:
        raise EngineError("bad_argument", f"mini_m 은 {' · '.join(map(str, MINI_SIZES))} m 중 하나: {mini_m!r}")
    edges = [float(e) for e in range(0, int(REF_LEN_M), int(mini_m))]
    return edges + [REF_LEN_M]


def get_sectors(s, rows: list[dict], mini_m=25) -> dict:
    edges = _mini_edges(mini_m)
    if not rows:
        return {"sid": s.sid, "frame_id": None, "ref": None, "sectors": [], "mini": {"mini_m": int(mini_m), "edges_ref_m": edges, "count": len(edges) - 1},
                "laps": [], "best": None, "mean": None}
    sec_edges = [0.0, REF_LEN_M / 3, 2 * REF_LEN_M / 3, REF_LEN_M]
    sectors = []
    for k in range(3):
        x, y = geometry(sec_edges[k] / REF_LEN_M)
        sectors.append({"sector": k + 1, "ref_m0": round(sec_edges[k], 2), "ref_m1": round(sec_edges[k + 1], 2), "x_m": round(x, 2), "y_m": round(y, 2),
                        "heading_deg": heading(sec_edges[k] / REF_LEN_M)})
    laps = []
    for r in rows:
        c = clock_of(s, r)
        mini = [round(at_distance(c, b) - at_distance(c, a), 3) for a, b in zip(edges, edges[1:])]
        laps.append({"lap": r["lap"], "sector_s": [r["s1_s"], r["s2_s"], r["s3_s"]], "mini_s": mini})

    def best_of(key: str, n: int):
        vals = [min((L[key][i], L["lap"]) for L in laps) for i in range(n)]
        return [v for v, _ in vals], [lap for _, lap in vals]

    bs, bl = best_of("sector_s", 3)
    bm, bml = best_of("mini_s", len(edges) - 1)
    mean = lambda key, n: [round(sum(L[key][i] for L in laps) / len(laps), 3) for i in range(n)]  # noqa: E731
    best_row = next(r for r in rows if r["best"])
    return {"sid": s.sid, "frame_id": s.event.frame_id,
            "ref": {"ref_len_m": REF_LEN_M, "method": "projection", "source": "auto", "fixed_at": "2027-05-09T09:00:00+09:00",
                    "reference_lap": {"sid": s.sid, "lap": best_row["lap"]}},
            "sectors": sectors, "mini": {"mini_m": int(mini_m), "edges_ref_m": edges, "count": len(edges) - 1}, "laps": laps,
            "best": {"sector_s": bs, "sector_lap": bl, "mini_s": bm, "mini_lap": bml},
            "mean": {"sector_s": mean("sector_s", 3), "mini_s": mean("mini_s", len(edges) - 1), "n_laps": len(laps)}}


def get_delta(s, rows: list[dict], lap, ref_s, ref_rows: list[dict], ref_lap, mini_m=25, max_points=4000) -> dict:
    if s.event.frame_id != ref_s.event.frame_id or not s.has_track or not ref_s.has_track:
        raise EngineError("frame_mismatch", f"두 랩의 frame 이 다름: {s.event.frame_id} · {ref_s.event.frame_id}")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or not 2 <= max_points <= 20_000:
        raise EngineError("bad_argument", f"max_points 는 2 ~ 20000 의 정수: {max_points!r}")
    row, ref_row = _row(rows, lap, s.sid), _row(ref_rows, ref_lap, ref_s.sid)
    c, rc = clock_of(s, row), clock_of(ref_s, ref_row)
    n = min(max_points, 2000)
    xs = [REF_LEN_M * i / (n - 1) for i in range(n)]
    edges = _mini_edges(mini_m)
    diff = lambda d: at_distance(c, d) - at_distance(rc, d)  # noqa: E731
    return {"lap": {"sid": s.sid, "lap": lap}, "ref": {"sid": ref_s.sid, "lap": ref_lap}, "frame_id": s.event.frame_id,
            "x": {"kind": "ref_distance", "unit": "m", "values": [round(x, 2) for x in xs]},
            "delta_s": [round(diff(x), 3) for x in xs],
            "mini": {"mini_m": int(mini_m), "edges_ref_m": edges,
                     "delta_s": [round(diff(b) - diff(a), 3) for a, b in zip(edges, edges[1:])]},
            "on_track": {"values": [round(diff(d), 3) for _, d in _points(s, row)]},
            "total_s": round(row["lap_time_s"] - ref_row["lap_time_s"], 3),
            "lap_time_s": {"lap": row["lap_time_s"], "ref": ref_row["lap_time_s"]}}
