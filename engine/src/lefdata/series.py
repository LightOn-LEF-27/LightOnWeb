"""그래프용 값 만들기 — 화면 폭에 맞게 줄이되 순간값을 잃지 않는다 (공개 계층: 차량 · 폴더를 모른다).

    reduce(x, y, lo, hi, n, breaks)  →  칸 n 개의 가운데 · 평균 · 최소 · 최대

x 축(시간 또는 거리)을 같은 폭의 칸 n 개로 나눈다.

- 표본이 든 칸: 평균 · 최소 · 최대. 최소 · 최대를 같이 주므로 4,000 칸으로 줄여도 피크(전류 254 A 같은 값)가 남는다
- 표본이 없는 칸(확대해서 칸이 표본 간격보다 좁을 때): 양옆 표본을 이어 직선으로 채운다. 단 그 두 표본이 끊긴 사이면 비운다
- 끊긴 사이 = 기록 간격이 크게 벌어진 곳(장비 공백 · 빠진 표본)이거나 값이 NaN 인 표본을 건너는 곳. 비운 칸은 NaN 이고, 화면은 거기서 선을 끊는다

없는 값을 지어내지 않는다 — 끊긴 곳을 잇지 않고, 칸 밖의 값을 끌어오지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

GAP_FACTOR = 2.5            # 기록 간격(중앙값)의 이 배보다 벌어지면 끊긴 것으로 본다


def median_dt(t: np.ndarray) -> float:
    d = np.diff(np.asarray(t, float))
    d = d[d > 0]
    return float(np.median(d)) if len(d) else np.nan


def time_breaks(t: np.ndarray, factor: float = GAP_FACTOR) -> np.ndarray:
    """표본 i 와 i+1 사이가 끊겼는가 (길이 len(t) - 1). 기록 간격 중앙값의 factor 배보다 벌어진 곳."""
    t = np.asarray(t, float)
    if len(t) < 2:
        return np.zeros(0, bool)
    return np.diff(t) > factor * median_dt(t)


def gaps(t: np.ndarray, factor: float = GAP_FACTOR) -> list[tuple[float, float]]:
    """끊긴 구간들 [(앞 표본 시각, 뒤 표본 시각)]."""
    t = np.asarray(t, float)
    i = np.flatnonzero(time_breaks(t, factor))
    return [(float(t[k]), float(t[k + 1])) for k in i]


def smooth(t: np.ndarray, v: np.ndarray, window_s: float) -> np.ndarray:
    """가운데 맞춘 이동평균 (창 = window_s, 기록 간격 중앙값으로 표본 수를 정한다). NaN 은 건너뛴다."""
    v = np.asarray(v, float)
    n = int(round(window_s / median_dt(t))) if window_s and len(v) > 1 else 1
    if n <= 1:
        return v.copy()
    ok = np.isfinite(v)
    k = np.ones(n)
    num = np.convolve(np.where(ok, v, 0.0), k, mode="same")
    den = np.convolve(ok.astype(float), k, mode="same")
    out = np.full(len(v), np.nan)
    np.divide(num, den, out=out, where=den >= max(n // 2, 1))      # 창의 절반 이상이 있으면 (끝에서도 값이 나온다)
    return np.where(ok, out, np.nan)


def _cuts(x: np.ndarray, y: np.ndarray, breaks: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """NaN 표본을 뺀 (x, y) 와, 남은 이웃 표본 사이가 끊겼는지 (NaN 을 건너거나 breaks 가 있는 사이)."""
    if breaks is None:
        breaks = np.zeros(max(len(x) - 1, 0), bool)
    keep = np.flatnonzero(np.isfinite(y) & np.isfinite(x))
    xs, ys = x[keep], y[keep]
    cut = np.zeros(max(len(xs) - 1, 0), bool)
    if len(xs) > 1:
        cum = np.concatenate([[0], np.cumsum(breaks.astype(int))])
        cut = (np.diff(keep) != 1) | ((cum[keep[1:]] - cum[keep[:-1]]) > 0)
    return xs, ys, cut


def sample_at(x: np.ndarray, y: np.ndarray, at: np.ndarray, breaks: np.ndarray | None = None) -> np.ndarray:
    """주어진 위치(at)의 값 — 양옆 표본을 직선으로 잇는다. 끊긴 사이 · 기록 밖이면 NaN (지어내지 않는다)."""
    xs, ys, cut = _cuts(np.asarray(x, float), np.asarray(y, float), breaks)
    at = np.asarray(at, float)
    out = np.full(len(at), np.nan)
    if len(xs) == 0:
        return out
    right = np.searchsorted(xs, at, side="right")
    left = right - 1
    exact = (left >= 0) & (xs[np.clip(left, 0, None)] == at)
    good = (left >= 0) & (right < len(xs))
    good[good] &= ~cut[left[good]]
    l, r = left[good], right[good]
    span = xs[r] - xs[l]
    w = np.divide(at[good] - xs[l], span, out=np.zeros(len(l)), where=span > 0)
    out[good] = ys[l] + w * (ys[r] - ys[l])
    out[exact] = ys[left[exact]]
    return out


@dataclass
class Reduced:
    x: np.ndarray           # 칸 가운데
    mean: np.ndarray
    lo: np.ndarray          # 칸 안의 최소
    hi: np.ndarray          # 칸 안의 최대
    samples: int            # 칸 범위 안에 있던 원래 표본 수


def reduce(x: np.ndarray, y: np.ndarray, lo: float, hi: float, n: int, breaks: np.ndarray | None = None) -> Reduced:
    """[lo, hi] 를 같은 폭의 칸 n 개로 나눠 칸마다 평균 · 최소 · 최대 (위 설명).

    x 는 커지는 순서(같은 값이 이어져도 된다). breaks[i] = 표본 i 와 i+1 을 잇지 말 것 (없으면 아무 데도 끊지 않는다).
    """
    x, y = np.asarray(x, float), np.asarray(y, float)
    if not (hi > lo) or n < 1:
        raise ValueError(f"칸을 만들 수 없다: lo={lo}, hi={hi}, n={n}")
    edges = np.linspace(lo, hi, n + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    mean, low, high = (np.full(n, np.nan) for _ in range(3))

    # NaN 표본은 버리고, 버린 표본을 건너는 사이는 끊긴 것으로 한다
    xs, ys, cut = _cuts(x, y, breaks)
    if len(xs) == 0:
        return Reduced(centers, mean, low, high, 0)

    # 표본이 든 칸: 평균 · 최소 · 최대
    inside = (xs >= lo) & (xs <= hi)
    width = (hi - lo) / n
    idx = np.minimum(((xs[inside] - lo) / width).astype(int), n - 1)
    samples = int(inside.sum())
    if samples:
        yi = ys[inside]
        starts = np.flatnonzero(np.concatenate([[True], idx[1:] != idx[:-1]]))
        b = idx[starts]
        counts = np.diff(np.concatenate([starts, [len(idx)]]))
        mean[b] = np.add.reduceat(yi, starts) / counts
        low[b] = np.minimum.reduceat(yi, starts)
        high[b] = np.maximum.reduceat(yi, starts)

    # 빈 칸: 양옆 표본이 끊기지 않은 사이면 직선으로 잇는다
    empty = np.flatnonzero(np.isnan(mean))
    if len(empty) and len(xs) > 1:
        c = centers[empty]
        right = np.searchsorted(xs, c, side="right")
        left = right - 1
        good = (left >= 0) & (right < len(xs))
        good[good] &= ~cut[left[good]]
        if good.any():
            l, r, cc = left[good], right[good], c[good]
            span = xs[r] - xs[l]
            w = np.divide(cc - xs[l], span, out=np.zeros(len(cc)), where=span > 0)
            v = ys[l] + w * (ys[r] - ys[l])
            e = empty[good]
            mean[e] = low[e] = high[e] = v
    return Reduced(centers, mean, low, high, samples)
