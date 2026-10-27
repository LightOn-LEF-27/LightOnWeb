"""GPS 기반 주행 구간(run) 검출과 랩 분할.

입력은 정규화된 gps 테이블 (t_rel_ms, fix, lat, lon, speed_kmh, course_deg).
해석 결과이므로 폴더 구조가 아니라 표(runs.csv, laps.csv)로 저장한다.

방법
    1. 위경도를 기준점 중심의 평면 좌표(m)로 옮긴다 (트랙이 수백 m 라 평면 근사로 충분)
    2. 속도 > MOVING_KMH 인 구간을 묶어 run 을 만든다 (짧은 정지는 이어 붙임)
    3. 계측선(gate): run 중 가장 빠른 지점에, 진행 방향에 수직으로 긋는다
    4. 같은 방향으로 계측선을 지날 때마다 랩을 끊는다. 통과 시각은 앞뒤 샘플 사이를 선형 보간
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

MOVING_KMH = 10.0       # 이 속도 위면 주행 중
MERGE_STOP_S = 10.0     # 이보다 짧은 정지는 같은 run 으로 이어 붙임
MIN_RUN_S = 20.0        # 이보다 짧은 run 은 버림 (피트 이동 등)
GATE_HALF_WIDTH_M = 30.0      # GPS 위치가 한 랩 동안 20 m 넘게 밀린 사례가 있어 넉넉히
GATE_CLUSTER_M = 12.0         # 후보 검사: 연장선 통과들의 횡방향 퍼짐이 이 안이면 '한 줄기'
GATE_ISOLATION_M = 60.0       # 계측선 후보 주변 이 거리 안에 다른 트랙 구간이 없어야 한다
GATE_HEADING_TOL_DEG = 90.0   # 저속 통과 시 GPS course 가 흔들려 60° 로는 통과를 놓쳤다
MIN_LAP_S = 15.0        # 같은 통과를 두 번 세지 않기 위한 최소 랩 시간


def to_local_m(lat, lon, lat0: float, lon0: float):
    x = (np.asarray(lon) - lon0) * np.cos(np.radians(lat0)) * 111_320.0
    y = (np.asarray(lat) - lat0) * 110_950.0
    return x, y


def find_runs(t_s: np.ndarray, speed_kmh: np.ndarray) -> list[tuple[int, int]]:
    """주행 구간의 (시작 인덱스, 끝 인덱스) 목록."""
    idx = np.flatnonzero(speed_kmh > MOVING_KMH)
    if len(idx) == 0:
        return []
    breaks = np.flatnonzero(np.diff(t_s[idx]) > MERGE_STOP_S)
    starts = np.concatenate([[idx[0]], idx[breaks + 1]])
    ends = np.concatenate([idx[breaks], [idx[-1]]])
    return [(int(a), int(b)) for a, b in zip(starts, ends) if t_s[b] - t_s[a] >= MIN_RUN_S]


@dataclass
class Gate:
    x: float
    y: float
    heading_deg: float      # 통과 방향 (북=0, 시계방향 — GPS course 와 같은 기준)

    @property
    def unit(self):
        h = np.radians(self.heading_deg)
        return np.sin(h), np.cos(h)     # (동, 북)


def _line_passes(x, y, course_deg, gate: Gate, reach_m: float):
    """계측선의 연장선을 gate 방향으로 지나는 모든 통과의 (인덱스, 횡방향 위치)."""
    ux, uy = gate.unit
    dx, dy = x - gate.x, y - gate.y
    along = dx * ux + dy * uy
    lateral = -dx * uy + dy * ux
    dh = (course_deg - gate.heading_deg + 180.0) % 360.0 - 180.0
    ok = (np.abs(lateral) < reach_m) & (np.abs(dh) < GATE_HEADING_TOL_DEG)
    k = np.flatnonzero((along[:-1] < 0) & (along[1:] >= 0) & ok[:-1] & ok[1:])
    return k, along, lateral


def pick_gate(x, y, speed_kmh, course_deg) -> Gate:
    """빠른 지점 중, 계측선 주변에 다른 트랙 구간이 없는 곳을 고른다.

    빠른 곳 = 직선이라 통과 방향이 안정적이고 보간 오차가 작다.
    고립된 곳 = GPS 위치가 10 m 넘게 밀리는 랩이 있어 계측선을 넓게 잡아야 하는데,
    근처에 같은 방향으로 지나는 다른 구간이 있으면 그 통과까지 세어 버린다.
    """
    order = np.argsort(speed_kmh)[::-1]
    fast = order[: max(len(order) // 5, 1)][::5]           # 상위 20 % 를 솎아서 후보로
    for k in fast:
        gate = Gate(float(x[k]), float(y[k]), float(course_deg[k]))
        idx, _, lateral = _line_passes(x, y, course_deg, gate, GATE_ISOLATION_M)
        if len(idx) and np.ptp(lateral[idx]) < GATE_CLUSTER_M:   # 통과가 한 줄기로만 모여 있다
            return gate
    k = int(order[0])
    return Gate(float(x[k]), float(y[k]), float(course_deg[k]))


def gate_crossings(t_s, x, y, course_deg, gate: Gate) -> np.ndarray:
    """계측선을 gate 방향으로 통과한 시각(초) 배열. 통과 시각은 앞뒤 샘플 사이를 선형 보간."""
    k, along, _ = _line_passes(x, y, course_deg, gate, GATE_HALF_WIDTH_M)
    frac = -along[k] / (along[k + 1] - along[k])
    times = t_s[k] + frac * (t_s[k + 1] - t_s[k])
    keep = np.concatenate([[True], np.diff(times) > MIN_LAP_S]) if len(times) else np.array([], bool)
    return times[keep]


def analyze(gps: pd.DataFrame, lat0: float, lon0: float, gate: Gate | None = None):
    """gps 테이블 → (runs, laps, gate, 평면좌표 DataFrame).

    laps 에는 계측선 통과 사이의 '완주 랩'만 들어간다. run 시작~첫 통과(아웃랩)와
    마지막 통과~run 끝(인랩)은 runs 의 out_s / in_s 로 따로 남긴다.
    """
    g = gps[gps["fix"]].reset_index(drop=True)
    t = g["t_rel_ms"].to_numpy() / 1000.0
    x, y = to_local_m(g["lat"], g["lon"], lat0, lon0)
    v = g["speed_kmh"].to_numpy()
    c = g["course_deg"].to_numpy()
    xy = pd.DataFrame({"t_s": t, "x_m": x, "y_m": y, "speed_kmh": v, "course_deg": c})

    runs_idx = find_runs(t, v)
    if not runs_idx:
        return pd.DataFrame(), pd.DataFrame(), gate, xy
    if gate is None:
        a, b = max(runs_idx, key=lambda ab: t[ab[1]] - t[ab[0]])
        gate = pick_gate(x[a:b + 1], y[a:b + 1], v[a:b + 1], c[a:b + 1])

    step = np.hypot(np.diff(x), np.diff(y))
    runs, laps = [], []
    for ri, (a, b) in enumerate(runs_idx, 1):
        cross = gate_crossings(t[a:b + 1], x[a:b + 1], y[a:b + 1], c[a:b + 1], gate)
        runs.append({
            "run": ri, "t_start_s": t[a], "t_end_s": t[b], "duration_s": t[b] - t[a],
            "distance_m": float(step[a:b].sum()), "vmax_kmh": float(v[a:b + 1].max()),
            "gate_crossings": len(cross), "full_laps": max(len(cross) - 1, 0),
            "out_s": float(cross[0] - t[a]) if len(cross) else None,
            "in_s": float(t[b] - cross[-1]) if len(cross) else None,
        })
        for li, (t0, t1) in enumerate(zip(cross[:-1], cross[1:]), 1):
            m = (t >= t0) & (t < t1)
            k = np.flatnonzero(m)
            laps.append({
                "run": ri, "lap": li, "t_start_s": t0, "t_end_s": t1, "lap_time_s": t1 - t0,
                "distance_m": float(step[k[0]:k[-1]].sum()) if len(k) > 1 else 0.0,
                "vmax_kmh": float(v[m].max()), "vmin_kmh": float(v[m].min()),
                "vavg_kmh": float(v[m].mean()),
            })
    return pd.DataFrame(runs), pd.DataFrame(laps), gate, xy


GPS_COLUMNS = {"t_rel_ms", "fix", "lat", "lon", "speed_kmh", "course_deg"}


def gps_recordings(norm: Path) -> list[Path]:
    """랩 분할에 쓸 수 있는 레코딩 폴더 — gps 테이블에 필요한 열이 다 있는 것만.

    한 이벤트에 여러 장비가 섞인다. GPS 가 아예 없는 장비(에너지미터 등)도 있고,
    gps 테이블은 있지만 fix·속도 열이 없는 장비(위치만 적는 로거)도 있다.
    """
    return sorted(p for p in norm.iterdir() if p.is_dir() and (p / "gps.parquet").exists()
                  and GPS_COLUMNS <= set(pq.read_schema(p / "gps.parquet").names))


def run_event(event_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, Gate]:
    """이벤트의 모든 레코딩을 같은 계측선으로 분석 → derived/runs.csv, laps.csv, gate.json.

    계측선은 이벤트 전체에서 가장 긴 run 을 기준으로 한 번만 고른다 (랩타임을 레코딩끼리 비교하려면
    같은 선이어야 한다). 기준점(원점)은 그 레코딩 GPS 의 중앙값.
    """
    norm = event_dir / "normalized"
    recs = gps_recordings(norm)
    gps = {p.name: pq.read_table(p / "gps.parquet").to_pandas() for p in recs}
    labels = {p.name: json.loads((p / "meta.json").read_text(encoding="utf-8")).get("label") for p in recs}

    def longest_run_s(g):
        f = g[g["fix"]]
        t, v = f["t_rel_ms"].to_numpy() / 1000.0, f["speed_kmh"].to_numpy()
        return max((t[b] - t[a] for a, b in find_runs(t, v)), default=0.0)

    ref = max(gps, key=lambda k: longest_run_s(gps[k]))
    fixed = gps[ref][gps[ref]["fix"]]
    lat0, lon0 = float(fixed["lat"].median()), float(fixed["lon"].median())
    _, _, gate, _ = analyze(gps[ref], lat0, lon0)

    all_runs, all_laps = [], []
    for rid, g in gps.items():
        runs, laps, _, _ = analyze(g, lat0, lon0, gate)
        for df, acc in ((runs, all_runs), (laps, all_laps)):
            if len(df):
                df.insert(0, "label", labels[rid])
                df.insert(0, "recording_id", rid)
                acc.append(df)
    # 랩이 하나도 없는 이벤트도 있다 (단발 종목만 기록된 대회 데이터 등)
    runs = pd.concat(all_runs, ignore_index=True) if all_runs else pd.DataFrame()
    laps = pd.concat(all_laps, ignore_index=True) if all_laps else pd.DataFrame(
        columns=["recording_id", "label", "lap_time_s"])

    out = event_dir / "derived"
    out.mkdir(exist_ok=True)
    runs.round(3).to_csv(out / "runs.csv", index=False, encoding="utf-8-sig")
    laps.round(3).to_csv(out / "laps.csv", index=False, encoding="utf-8-sig")
    (out / "gate.json").write_text(json.dumps(
        {"origin": {"lat0": lat0, "lon0": lon0}, "gate": asdict(gate), "reference_recording": ref,
         "half_width_m": GATE_HALF_WIDTH_M, "note": "x=동, y=북 (m). heading 은 북=0 시계방향"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return runs, laps, gate
