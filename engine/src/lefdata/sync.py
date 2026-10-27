"""두 장비의 시계 관계를 파형으로 구한다.

서로 다른 장비가 **같은 물리량**(요레이트, 속도, 전류 …)을 쟀다면, 한쪽 파형의 짧은 조각을
다른 쪽에서 찾아 "기준 장비의 이 순간 = 저 장비의 저 순간" 이라는 짝을 여러 개 얻을 수 있다.
그 짝들에 직선을 맞추면 두 가지가 한 번에 나온다.

    sig 시각 = a + f × ref 시각
        a   시계가 얼마나 어긋나 있나 (offset)
        f   시계가 얼마나 빠르게/느리게 가나 (rate). 1 이면 같은 속도

offset 만 재는 방법(전체 파형을 밀어 가며 상관을 보는 것)은 f 가 1 이 아니면 실패한다 —
긴 구간에서는 어느 한 지점도 맞지 않기 때문이다. 짧은 창은 f 가 조금 틀려도 맞는다.

장비·폴더 구조를 모르는 순수 계층이다. 입력은 전부 배열이다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STEP_S = 0.1            # 격자 간격. 요레이트처럼 1 초 안팎의 변화를 보려면 10 Hz 면 충분하다


def grid(t_s, values, step: float = STEP_S) -> tuple[int, np.ndarray]:
    """불규칙한 표본 → 고정 간격 격자 (칸 안 평균). 반환 (첫 칸 번호, 값 배열). 빈 칸은 NaN.

    칸 k 는 시각 [k × step, (k+1) × step) 을 뜻한다. 두 장비의 격자를 같은 시간축에 놓으려면
    칸 번호를 그대로 비교하면 된다.
    """
    t_s, values = np.asarray(t_s, float), np.asarray(values, float)
    ok = np.isfinite(t_s) & np.isfinite(values)
    if not ok.any():
        return 0, np.array([])
    k = np.floor(t_s[ok] / step).astype(np.int64)
    k0 = int(k.min())
    n = int(k.max()) - k0 + 1
    total = np.bincount(k - k0, weights=values[ok], minlength=n)
    count = np.bincount(k - k0, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return k0, np.where(count > 0, total / count, np.nan)


def ncc(signal: np.ndarray, template: np.ndarray, max_missing: float = 0.1) -> np.ndarray:
    """정규화 상호상관: template 을 signal 의 모든 위치에 대어 본 상관계수. 길이 len(signal) − len(template) + 1.

    signal 의 NaN 은 0 으로 두고 계산하되, NaN 이 창의 max_missing 넘게 들어간 위치는 NaN 으로 돌려준다.
    """
    m = len(template)
    if len(signal) < m:
        return np.array([])
    t = template - template.mean()
    t_norm = np.sqrt((t * t).sum())
    s = np.nan_to_num(signal)
    num = np.correlate(s, t, mode="valid")          # t 의 평균이 0 이라 s 의 평균 항은 사라진다
    c1 = np.concatenate([[0.0], np.cumsum(s)])
    c2 = np.concatenate([[0.0], np.cumsum(s * s)])
    var = np.maximum((c2[m:] - c2[:-m]) - (c1[m:] - c1[:-m]) ** 2 / m, 0.0)
    den = np.sqrt(var) * t_norm
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 1e-9, num / den, np.nan)
    missing = np.concatenate([[0], np.cumsum(~np.isfinite(signal))])
    out[(missing[m:] - missing[:-m]) > max_missing * m] = np.nan
    return out


def _peak(c: np.ndarray, guard: int) -> tuple[float, float, float] | None:
    """상관 배열에서 (최고 위치(칸, 소수), 최고 상관, guard 칸 밖의 2위 상관)."""
    if not np.isfinite(c).any():
        return None
    i = int(np.nanargmax(c))
    pos = float(i)
    if 0 < i < len(c) - 1 and np.isfinite(c[i - 1]) and np.isfinite(c[i + 1]):
        den = c[i - 1] - 2 * c[i] + c[i + 1]
        if den < 0:
            pos += 0.5 * (c[i - 1] - c[i + 1]) / den          # 포물선 보간 → 격자보다 고운 위치
    rest = c.copy()
    rest[max(0, i - guard): i + guard + 1] = np.nan
    return pos, float(c[i]), float(np.nanmax(rest)) if np.isfinite(rest).any() else float("nan")


def match_windows(ref_k0: int, ref: np.ndarray, sig_k0: int, sig: np.ndarray, *, step: float = STEP_S,
                  win_s: float = 30.0, hop_s: float = 10.0, min_std: float = 3.0,
                  expect=None, slack=None) -> pd.DataFrame:
    """ref 를 창으로 잘라 창마다 sig 에서 가장 닮은 위치를 찾는다.

    expect(ref 시각 배열) → sig 에서 예상되는 시각, slack(ref 시각 배열) → 그 앞뒤로 볼 범위(초).
    둘 다 None 이면 sig 전체에서 찾는다.
    반환 열: ref_s·sig_s (창 중앙의 시각, 각자 시간축), corr, runner_up (최고 위치에서 창 길이 넘게 떨어진 2위).
    변화가 없는 창(표준편차 < min_std)은 어디에나 맞으므로 건너뛴다.
    """
    w, hop = int(round(win_s / step)), int(round(hop_s / step))
    rows = []
    for s in range(0, len(ref) - w + 1, hop):
        tpl = ref[s: s + w]
        if not np.isfinite(tpl).all() or tpl.std() < min_std:
            continue
        ref_mid = (ref_k0 + s + w / 2) * step
        lo, hi = 0, len(sig) - w
        if expect is not None:
            e = float(expect(np.array([(ref_k0 + s) * step]))[0])            # 창 시작의 예상 sig 시각
            r = float(slack(np.array([ref_mid]))[0])
            lo = max(lo, int(np.floor((e - r) / step)) - sig_k0)
            hi = min(hi, int(np.ceil((e + r) / step)) - sig_k0)
        if hi < lo:
            continue
        pk = _peak(ncc(sig[lo: hi + w], tpl), guard=w)
        if pk is None:
            continue
        pos, corr, runner_up = pk
        rows.append({"ref_s": ref_mid, "sig_s": (sig_k0 + lo + pos + w / 2) * step, "corr": corr, "runner_up": runner_up})
    return pd.DataFrame(rows, columns=["ref_s", "sig_s", "corr", "runner_up"])


def fit_clock(ref_s, sig_s, slopes=None, tol_s: float = 0.3, min_inliers: int = 5) -> dict | None:
    """sig_s = a + f × ref_s 를 이상치에 강하게 맞춘다. 실패하면 None.

    창 매칭에는 틀린 짝이 섞인다 (같은 코스를 도는 랩끼리는 파형이 닮았다). 틀린 짝은 서로 다른 곳을
    가리키고 맞는 짝만 한 직선 위에 모이므로, **직선 하나에 가장 많은 점이 모이는 기울기**를 고른 뒤
    그 점들로 최소제곱을 다시 한다.
    반환: f, a, inliers(bool 배열), n, rms_s (맞은 점들의 잔차 RMS), span_s (맞은 점들이 덮은 ref 시간)
    """
    x, y = np.asarray(ref_s, float), np.asarray(sig_s, float)
    if len(x) < min_inliers:
        return None
    x0 = x.mean()
    xc = x - x0
    slopes = np.arange(0.90, 1.1001, 0.0005) if slopes is None else np.asarray(slopes, float)
    best = (0, np.inf, None, None)
    for f in slopes:
        d = y - f * xc
        order = np.argsort(d)
        ds = d[order]
        hi = np.searchsorted(ds, ds + 2 * tol_s, side="right")      # ds[i] 부터 2·tol 폭 안에 든 점 수
        cnt = hi - np.arange(len(ds))
        i = int(np.argmax(cnt))
        n = int(cnt[i])
        spread = float(ds[i + n - 1] - ds[i])
        if n > best[0] or (n == best[0] and spread < best[1]):
            best = (n, spread, f, order[i: i + n])
    n, _, f, idx = best
    if n < min_inliers:
        return None
    keep = np.zeros(len(x), bool)
    keep[idx] = True
    for _ in range(3):                                   # 최소제곱 → 잔차로 다시 고르기
        if np.ptp(xc[keep]) > 0:
            f, a = np.polyfit(xc[keep], y[keep], 1)
        else:
            a = float(np.mean(y[keep] - f * xc[keep]))
        res = y - (a + f * xc)
        new = np.abs(res) <= tol_s
        if new.sum() < min_inliers or (new == keep).all():
            break
        keep = new
    res = y - (a + f * xc)
    return {"f": float(f), "a": float(a - f * x0), "inliers": keep, "n": int(keep.sum()),
            "rms_s": float(np.sqrt(np.mean(res[keep] ** 2))), "span_s": float(np.ptp(x[keep]))}
