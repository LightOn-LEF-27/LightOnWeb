"""그래프용 값 줄이기 (`series`) — 손으로 만든 신호로 약속을 고정한다.

1. 줄여도 순간값(피크)이 남는다          — 칸마다 최소 · 최대를 같이 준다
2. 기록이 빈 곳은 잇지 않는다             — 확대하면 그 사이가 비어(NaN) 화면이 선을 끊는다
3. 표본보다 칸이 많으면(확대) 직선으로 잇는다 — 끊기지 않은 사이만
4. NaN 표본은 없는 값이다                  — 그 양옆을 이어 붙이지 않는다
"""

import numpy as np
import pytest

from lefdata import series as ser


def test_peak_survives_reduction():
    t = np.arange(0.0, 1000.0, 0.01)                          # 100 Hz · 100,000 표본
    y = 100 + 50 * np.sin(t / 7)
    y[43_210] = 254.0                                         # 표본 하나짜리 피크
    r = ser.reduce(t, y, 0.0, 1000.0, 4000, ser.time_breaks(t))
    assert len(r.x) == 4000 and r.samples == len(t)
    assert np.nanmax(r.hi) == 254.0                           # 최대는 그대로 남는다
    assert np.nanmax(r.mean) < 254.0                          # 평균만 줬다면 사라졌을 값
    assert np.nanmin(r.lo) == pytest.approx(y.min())
    assert not np.isnan(r.mean).any()


def test_bucket_mean_is_the_mean_of_its_samples():
    t = np.arange(10.0)
    y = np.arange(10.0) ** 2
    r = ser.reduce(t, y, 0.0, 10.0, 2)                        # 칸 [0, 5) · [5, 10]
    assert r.mean.tolist() == [np.mean(y[:5]), np.mean(y[5:])]
    assert r.lo.tolist() == [0.0, 25.0] and r.hi.tolist() == [16.0, 81.0]


def test_a_hole_in_the_record_is_not_bridged_when_zoomed_in():
    t = np.concatenate([np.arange(0.0, 5.0, 0.01), np.arange(5.5, 10.0, 0.01)])   # 5.0 ~ 5.5 s 가 비었다
    y = np.ones(len(t))
    r = ser.reduce(t, y, 4.0, 6.5, 2500, ser.time_breaks(t))
    hole = (r.x > 5.0) & (r.x < 5.49)
    assert np.isnan(r.mean[hole]).all()                       # 빈 곳은 비워 둔다
    assert not np.isnan(r.mean[~hole & ((r.x < 4.99) | (r.x > 5.5))]).any()
    assert ser.gaps(t) == [(pytest.approx(4.99), pytest.approx(5.5))]


def test_sparse_samples_are_joined_with_straight_lines_when_zoomed_in():
    t = np.arange(0.0, 10.01, 0.1)                            # 10 Hz
    y = 3 * t
    r = ser.reduce(t, y, 0.0, 10.0, 1000, ser.time_breaks(t))
    assert not np.isnan(r.mean).any()
    assert np.allclose(r.mean, 3 * r.x, atol=0.16)            # 칸 안 표본 하나(칸 가운데와 최대 반 칸 차이) 또는 직선 보간


def test_nan_samples_are_missing_values_and_break_the_line():
    t = np.arange(0.0, 2.0, 0.01)
    y = np.ones(len(t))
    y[100] = np.nan
    r = ser.reduce(t, y, 0.9, 1.1, 400, ser.time_breaks(t))
    gap = (r.x > 0.991) & (r.x < 1.009)                       # 0.99 · 1.01 표본이 든 칸은 값이 있다
    assert np.isnan(r.mean[gap]).all() and not np.isnan(r.mean[(r.x < 0.985) | (r.x > 1.015)]).any()


def test_smoothing_keeps_constants_and_averages_steps():
    t = np.arange(0.0, 10.0, 0.01)
    assert np.allclose(ser.smooth(t, np.full(len(t), 2.0), 0.5), 2.0)
    step = (t >= 5).astype(float)
    s = ser.smooth(t, step, 0.5)
    assert s[np.searchsorted(t, 5.0)] == pytest.approx(0.5, abs=0.02) and s[100] == 0.0 and s[-100] == 1.0
    assert ser.smooth(t, step, 0) is not step and np.array_equal(ser.smooth(t, step, 0), step)


def test_reduce_rejects_an_empty_range():
    with pytest.raises(ValueError):
        ser.reduce(np.arange(3.0), np.arange(3.0), 1.0, 1.0, 10)
