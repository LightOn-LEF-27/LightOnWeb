"""코스 계측선으로 시도 나누기 — 짐카나처럼 같은 선으로 들어가고 나오는 짧은 코스 (2026-11-17).

서킷 랩(`laps`)은 계측선을 한 방향으로 지날 때마다 끊는다. 짐카나는 입구 = 출구라 **선을 넘어 코스로 들어가서 다시 넘어 나올 때까지**가
시도 한 번이다. 그 사이 줄서기 · 돌아 나오는 길은 시도가 아니다.

    gate      Gate(name, x1, y1, x2, y2, zone_x)  — 평면 좌표 (m, x 동 · y 북). 선을 (x1, y1) → (x2, y2) 로 볼 때 **오른쪽이 코스**.
              zone_x = (서쪽 끝, 동쪽 끝) — 코스 구역의 동서 범위 (같은 코스가 나란히 둘이면 둘을 가른다)
    align     GPS 가 기록마다 통째로 몇 m 씩 밀린다 — 기준 기록의 궤적에 겹치는 (dx, dy) 를 ±shift_m 안에서 찾는다 (1 m 격자)
    find      1) 선 (양쪽 extend_m 늘린 선분) 을 넘어 들어가 min_lap_m 넘게 달린 뒤 넘어 나온 것 — 시각이 정확하다 (how = "line")
              2) 코스 구역 (선의 코스 쪽 · zone_x 안) 에 들어갔다 나온 것 중 1) 과 시간이 안 겹치는 것 (how = "zone" — GPS 가 끊겼거나
                 코스를 놓쳐 선 밖으로 드나든 시도. 시각이 몇 초 어긋날 수 있다). GPS 기록의 처음 · 끝에 걸린 것은 뺀다 (반쪽)

이 모듈은 차량 · 폴더를 모른다 (공개 계층). 어느 이벤트에 어떤 선이 있는지는 팀 계층이 메모에서 읽어 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

EXTEND_M = 10.0         # 선분을 양쪽으로 이만큼 늘려 센다 (GPS 흔들림 · 그린 선의 길이)
MIN_LAP_M = 150.0       # 넘어 들어가 이만큼은 달려야 시도 (줄 서서 선을 넘나든 것은 뺀다)
MAX_LAP_S = 180.0       # 선으로 잡는 시도의 최대 길이
MAX_ZONE_S = 200.0      # 구역으로 잡는 시도의 최대 길이
MERGE_S = 5.0           # 이보다 짧게 구역 밖에 나간 것은 이어 본다 (경계 · GPS 흔들림)
SHIFT_M, NEAR_M = 25, 2  # 밀림 찾기: ±SHIFT_M (m, 1 m 걸음) · 기준 궤적 NEAR_M m 안에 든 점의 비율이 가장 큰 자리
ALIGN_POINTS = 1500
COLUMNS = ["course", "course_no", "how", "t_start_s", "t_end_s", "lap_time_s", "distance_m", "vmax_kmh", "vmin_kmh"]


@dataclass(frozen=True)
class Gate:
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    zone_x: tuple[float, float] = (-np.inf, np.inf)

    @property
    def length(self) -> float:
        return float(np.hypot(self.x2 - self.x1, self.y2 - self.y1))

    def side(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """음수 = 코스 쪽 (선의 오른쪽)."""
        return (self.x2 - self.x1) * (y - self.y1) - (self.y2 - self.y1) * (x - self.x1)

    def along(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """선을 따라 잰 위치 (m, 0 = 시작점)."""
        return ((x - self.x1) * (self.x2 - self.x1) + (y - self.y1) * (self.y2 - self.y1)) / self.length


def align(x: np.ndarray, y: np.ndarray, ref_x: np.ndarray, ref_y: np.ndarray, shift_m: int = SHIFT_M, near_m: int = NEAR_M) -> tuple[float, float, float]:
    """(x, y) 에 더하면 기준 궤적에 가장 잘 겹치는 (dx, dy) 와 그때 겹친 점의 비율."""
    ref_x, ref_y = np.asarray(ref_x, float), np.asarray(ref_y, float)
    if len(x) == 0 or len(ref_x) == 0:
        return 0.0, 0.0, 0.0
    x0, y0 = int(np.floor(ref_x.min())) - shift_m - near_m - 2, int(np.floor(ref_y.min())) - shift_m - near_m - 2
    grid = np.zeros((int(ref_y.max()) - y0 + shift_m + near_m + 3, int(ref_x.max()) - x0 + shift_m + near_m + 3), bool)
    grid[(ref_y - y0).astype(int), (ref_x - x0).astype(int)] = True
    for _ in range(near_m):                                  # near_m 칸 (m) 만큼 넓힌다
        g = grid.copy()
        g[1:] |= grid[:-1]
        g[:-1] |= grid[1:]
        g[:, 1:] |= grid[:, :-1]
        g[:, :-1] |= grid[:, 1:]
        grid = g
    k = np.linspace(0, len(x) - 1, min(len(x), ALIGN_POINTS)).astype(int)
    px, py = np.asarray(x, float)[k], np.asarray(y, float)[k]
    best, arg = -1, (0, 0)
    for dx in range(-shift_m, shift_m + 1):
        ix = (px + dx - x0).astype(int)
        okx = (ix >= 0) & (ix < grid.shape[1])
        for dy in range(-shift_m, shift_m + 1):
            iy = (py + dy - y0).astype(int)
            ok = okx & (iy >= 0) & (iy < grid.shape[0])
            score = int(grid[iy[ok], ix[ok]].sum())
            if score > best or (score == best and abs(dx) + abs(dy) < abs(arg[0]) + abs(arg[1])):
                best, arg = score, (dx, dy)
    return float(arg[0]), float(arg[1]), best / len(k)


def find(t: np.ndarray, x: np.ndarray, y: np.ndarray, speed_kmh: np.ndarray, gates: list[Gate], extend_m: float = EXTEND_M,
         min_lap_m: float = MIN_LAP_M) -> pd.DataFrame:
    """한 기록의 궤적 (이미 밀림을 맞춘 것) → 시도 표 (COLUMNS, 시작 순). course_no = gates 의 순번 (1 부터)."""
    t, x, y, v = (np.asarray(a, float) for a in (t, x, y, speed_kmh))
    if len(t) < 2 or not gates:
        return pd.DataFrame(columns=COLUMNS)
    seg = np.r_[0.0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    sides = [np.where(sd == 0, 1e-9, sd) for sd in (g.side(x, y) for g in gates)]     # 선 위에 딱 놓인 점은 코스 밖으로 (넘음을 놓치지 않게)
    found: list[tuple[int, int, int, str]] = []
    for n, (g, sd) in enumerate(zip(gates, sides), 1):
        along, sg = g.along(x, y), np.sign(sd)
        last_in = None
        for i in np.flatnonzero(sg[:-1] * sg[1:] < 0):
            if not (-extend_m <= (along[i] + along[i + 1]) / 2 <= g.length + extend_m):
                continue
            if sg[i + 1] < 0:
                last_in = i                                   # 들어간 마지막 순간 (줄 서서 넘나들면 뒤의 것)
            elif last_in is not None and seg[i] - seg[last_in] >= min_lap_m and t[i] - t[last_in] <= MAX_LAP_S:
                found.append((last_in, i + 1, n, "line"))
                last_in = None
    zone = np.zeros(len(t), int)
    for n, (g, sd) in enumerate(zip(gates, sides), 1):
        zone[(zone == 0) & (sd < 0) & (x >= g.zone_x[0]) & (x < g.zone_x[1])] = n
    runs: list[list[int]] = []
    k = 0
    while k < len(zone):
        j = k
        while j + 1 < len(zone) and zone[j + 1] == zone[k]:
            j += 1
        if zone[k]:
            if runs and runs[-1][2] == zone[k] and t[k] - t[runs[-1][1]] < MERGE_S:
                runs[-1][1] = j
            else:
                runs.append([k, j, int(zone[k])])
        k = j + 1
    for a, b, n in runs:
        if a == 0 or b == len(t) - 1:                           # 기록 (GPS) 의 처음 · 끝에 걸림 — 이미 코스 안이었거나 끝나기 전에 끊김
            continue
        if seg[b] - seg[a] >= min_lap_m and t[b] - t[a] <= MAX_ZONE_S and not any(t[a] < t[q] and t[b] > t[p] for p, q, _, _ in found):
            found.append((a, b, n, "zone"))
    rows = []
    for a, b, n, how in sorted(found):
        rows.append({"course": gates[n - 1].name, "course_no": n, "how": how, "t_start_s": float(t[a]), "t_end_s": float(t[b]),
                     "lap_time_s": float(t[b] - t[a]), "distance_m": float(seg[b] - seg[a]),
                     "vmax_kmh": float(np.nanmax(v[a:b + 1])), "vmin_kmh": float(np.nanmin(v[a:b + 1]))})
    return pd.DataFrame(rows, columns=COLUMNS)
