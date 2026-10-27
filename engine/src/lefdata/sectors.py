"""구간 · 미니 구간 · 두 랩의 시간 차 — 기준 랩 주행선 위의 거리(기준 거리)로 잰다 (공개 계층: 차량 · 폴더를 모른다).

    lap_points(t, x, y, ta, tb)         궤적에서 랩 하나의 점. 계측선을 지난 순간(ta · tb)이 첫 점 · 끝 점
    pick_reference(laps)                기준 랩 고르기 — 길이가 중앙값의 1 % 안인 랩 중 가장 빠른 것
    Reference(x, y)                     기준 랩 주행선 (누적 거리 s, 길이 length)
    project(ref, x, y)                  다른 랩의 점마다 기준 거리. 커지기만 하고, 첫 점 0 · 끝 점 = 기준 길이
    equal_edges · mini_edges            구간 · 미니 구간 경계 (기준 거리)
    crossing_times(t, s, marks)         기준 거리가 marks 를 처음 지난 시각
    delta · delta_at_points             같은 기준 거리에 닿은 시각의 차 (각자 랩 시작부터)

왜 기준 거리인가
    랩마다 주행선이 달라 달린 거리가 1 % 안팎 다르다. 랩의 거리를 그대로 쓰거나 랩 길이로 늘이고 줄이면
    같은 거리의 점이 랩마다 코스의 다른 곳(몇 m 차이)에 놓이고, 빠른 곳에서는 그것이 0.1 s 넘는 시간 차가 된다.
    그래서 점마다 기준 랩 주행선에서 가장 가까운 곳을 찾아, 기준선을 따라 그곳까지 간 거리를 쓴다.
    그러면 "기준 거리 s 를 지났다" = "기준선의 s 지점에서 기준선에 수직으로 그은 선을 지났다" 가 되어 랩마다 같은 곳에서 잰다.

    가까운 곳은 랩 길이로 늘이고 줄인 거리 둘레(± window_m)에서만 찾는다 — 헤어핀처럼 코스가 제 옆을 다시 지나는 곳에서
    엉뚱한 쪽에 내리지 않게. 내린 값이 GPS 흔들림으로 조금 뒤로 가면 단조 회귀(이웃끼리 평균해 커지기만 하게)로 편다.
    계측선은 기준 랩과 같은 선이므로 첫 점은 0, 끝 점은 기준 길이다. 그래서 구간 시간의 합 = 랩타임 이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

WINDOW_M = 30.0             # 가까운 곳을 찾는 범위 — 랩 길이로 늘이고 줄인 거리 둘레 ± 이만큼
LENGTH_TOL = 0.01           # 기준 랩 후보: 길이가 중앙값의 이 비율 안 (코스를 자른 랩 · 크게 돈 랩을 빼려고)


def lap_points(t, x, y, ta: float, tb: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """궤적(t, x, y)에서 랩 하나 — 계측선을 지난 순간 ta 부터 tb 까지. 첫 점 · 끝 점은 그 순간으로 보간한 위치."""
    t = np.asarray(t, float)
    inner = (t > ta) & (t < tb)
    pt = np.concatenate([[ta], t[inner], [tb]])
    return pt, np.interp(pt, t, np.asarray(x, float)), np.interp(pt, t, np.asarray(y, float))


def path_length(x, y) -> np.ndarray:
    """점을 이은 선을 따라 간 누적 거리 (첫 점 0)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])


def pick_reference(laps: list[dict]):
    """laps = [{"key": …, "length_m": …, "time_s": …}] → 기준 랩의 key. 비면 None.

    가장 빠른 랩이 코스를 잘랐을 수 있어, 길이가 중앙값의 LENGTH_TOL 안인 랩 중에서 가장 빠른 것을 고른다 (없으면 전체에서).
    """
    if not laps:
        return None
    med = float(np.median([l["length_m"] for l in laps]))
    near = [l for l in laps if abs(l["length_m"] - med) <= LENGTH_TOL * med] or laps
    return min(near, key=lambda l: l["time_s"])["key"]


@dataclass
class Reference:
    """기준 랩 주행선. 같은 자리에 겹친 점은 뺀다 (길이 0 인 마디는 방향이 없다)."""
    x: np.ndarray
    y: np.ndarray
    s: np.ndarray = field(init=False)

    def __post_init__(self):
        x, y = np.asarray(self.x, float), np.asarray(self.y, float)
        keep = np.concatenate([[True], np.hypot(np.diff(x), np.diff(y)) > 1e-9])
        self.x, self.y = x[keep], y[keep]
        if len(self.x) < 2:
            raise ValueError("기준선에 점이 둘 이상 있어야 한다")
        self.s = path_length(self.x, self.y)

    @property
    def length(self) -> float:
        return float(self.s[-1])

    def at(self, s) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """기준 거리 s 의 위치와 진행 방위(북 = 0, 시계 방향 °)."""
        s = np.asarray(s, float)
        k = np.clip(np.searchsorted(self.s, s, side="right") - 1, 0, len(self.s) - 2)
        dx, dy = self.x[k + 1] - self.x[k], self.y[k + 1] - self.y[k]
        heading = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
        return np.interp(s, self.s, self.x), np.interp(s, self.s, self.y), heading


def isotonic(y) -> np.ndarray:
    """커지기만 하는 값 중 y 에 가장 가까운 것 (제곱 오차 최소, 이웃 평균으로 편다)."""
    vals: list[float] = []
    wts: list[int] = []
    for v in np.asarray(y, float):
        vals.append(float(v))
        wts.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w = wts[-2] + wts[-1]
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / w
            vals.pop()
            wts.pop()
            vals[-1], wts[-1] = v, w
    return np.repeat(vals, wts)


def project(ref: Reference, x, y, window_m: float = WINDOW_M, chunk: int = 64) -> np.ndarray:
    """랩 하나의 점(x, y — 계측선에서 계측선까지) → 점마다 기준 거리 (위 설명)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    own = path_length(x, y)
    length = ref.length
    guess = own * (length / own[-1]) if own[-1] > 0 else np.zeros(len(x))
    bx_all, by_all = np.diff(ref.x), np.diff(ref.y)
    out = guess.copy()
    for i in range(0, len(x), chunk):
        px, py, g = x[i:i + chunk, None], y[i:i + chunk, None], guess[i:i + chunk, None]
        # 이 점들의 찾는 범위에 걸치는 마디만 본다 (기준선 전체와 견주면 느리다)
        j0 = max(int(np.searchsorted(ref.s, g.min() - window_m, side="right")) - 1, 0)
        j1 = min(int(np.searchsorted(ref.s, g.max() + window_m)) + 1, len(ref.s) - 1)
        ax, ay, bx, by, s0 = ref.x[j0:j1], ref.y[j0:j1], bx_all[j0:j1], by_all[j0:j1], ref.s[j0:j1]
        seg2 = bx * bx + by * by
        u = np.clip(((px - ax) * bx + (py - ay) * by) / seg2, 0.0, 1.0)
        foot = s0 + u * np.sqrt(seg2)
        d2 = (px - (ax + u * bx)) ** 2 + (py - (ay + u * by)) ** 2
        d2[np.abs(foot - g) > window_m] = np.inf
        k = np.argmin(d2, axis=1)
        rows = np.arange(len(k))
        found = np.isfinite(d2[rows, k])
        part = out[i:i + chunk]
        part[found] = foot[rows, k][found]
    out[0], out[-1] = 0.0, length
    return np.clip(isotonic(out), 0.0, length)


def equal_edges(length: float, n: int = 3) -> np.ndarray:
    """기준 길이를 n 등분한 경계 (0 과 길이 포함)."""
    return np.linspace(0.0, float(length), n + 1)


def mini_edges(length: float, size_m: float) -> np.ndarray:
    """size_m 마다 끊은 경계 (0 과 길이 포함). 마지막 구간은 남은 길이만큼이라 짧을 수 있다."""
    e = np.arange(0.0, float(length), float(size_m))
    if length - e[-1] < 1e-6:
        e = e[:-1]
    return np.append(e, float(length))


def crossing_times(t, s, marks) -> np.ndarray:
    """기준 거리 s(커지기만 하는 값)가 marks 를 처음 지난 시각. 0 과 끝(계측선)은 랩의 첫 · 끝 시각 그대로."""
    t, s, marks = np.asarray(t, float), np.asarray(s, float), np.asarray(marks, float)
    keep = np.concatenate([[True], np.diff(s) > 0])
    out = np.interp(marks, s[keep], t[keep])
    out[marks <= s[0]] = t[0]
    out[marks >= s[-1]] = t[-1]
    return out


def split(t, s, edges) -> np.ndarray:
    """경계 사이마다 걸린 시간 (s)."""
    return np.diff(crossing_times(t, s, edges))


def delta(t, s, ref_t, ref_s, at) -> np.ndarray:
    """기준 거리 at 마다 (이 랩이 닿은 시각 − 기준 랩이 닿은 시각), 둘 다 각자 랩 시작부터. + 면 이 랩이 늦다."""
    t, ref_t = np.asarray(t, float), np.asarray(ref_t, float)
    return (crossing_times(t, s, at) - t[0]) - (crossing_times(ref_t, ref_s, at) - ref_t[0])


def delta_at_points(t, s, ref_t, ref_s) -> np.ndarray:
    """이 랩의 점마다 누적 시간 차 — 그 점의 기준 거리에 기준 랩이 닿은 시각과 비교."""
    t, ref_t = np.asarray(t, float), np.asarray(ref_t, float)
    return (t - t[0]) - (crossing_times(ref_t, ref_s, s) - ref_t[0])
