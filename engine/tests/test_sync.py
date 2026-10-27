"""시계 맞추기(sync) 검증 — 만들어 낸 파형으로. 데이터 폴더가 없어도 돈다."""

import numpy as np
import pytest

from lefdata import sync


def wave(t, seed=0):
    """요레이트처럼 생긴 파형: 주기 2~20 초짜리 사인 여러 개의 합 (°/s)."""
    rng = np.random.default_rng(seed)
    out = np.zeros_like(np.asarray(t, float))
    for f, a, p in zip(rng.uniform(0.05, 0.5, 12), rng.uniform(5, 25, 12), rng.uniform(0, 2 * np.pi, 12)):
        out += a * np.sin(2 * np.pi * f * t + p)
    return out


def test_grid_averages_into_cells_and_marks_empty_ones():
    k0, v = sync.grid([1.00, 1.04, 1.31], [10.0, 20.0, 5.0], step=0.1)
    assert k0 == 10
    assert v[0] == pytest.approx(15.0)                 # 같은 칸에 든 두 표본의 평균
    assert np.isnan(v[1]) and np.isnan(v[2])           # 표본이 없는 칸은 채우지 않는다
    assert v[3] == pytest.approx(5.0)


def test_grid_of_nothing_is_empty():
    assert len(sync.grid([], [])[1]) == 0
    assert len(sync.grid([np.nan], [1.0])[1]) == 0


def test_ncc_finds_where_the_template_came_from():
    t = np.arange(0, 120, 0.1)
    s = wave(t)
    c = sync.ncc(s, 3.0 * s[400:700] + 50.0)           # 크기와 평균이 달라도 모양이 같으면 상관 1
    assert int(np.nanargmax(c)) == 400
    assert c[400] == pytest.approx(1.0, abs=1e-9)


def test_ncc_does_not_trust_windows_full_of_gaps():
    t = np.arange(0, 60, 0.1)
    s = wave(t)
    holed = s.copy()
    holed[100:160] = np.nan                            # 창 길이 100 칸 중 60 칸이 비었다
    c = sync.ncc(holed, s[100:200])
    assert np.isnan(c[100])


def test_slow_clock_is_recovered_from_waveforms():
    """한 장비의 시계가 몇 % 느리고 수십 초 어긋나 있을 때, 파형만으로 속도와 어긋남을 되찾는다."""
    f_true, a_true = 0.93, -93.0                       # 로거 시각 = a + f × 참 시각
    t = np.arange(0, 900, 0.01)                        # 참 시각
    ref_k0, ref = sync.grid(t, wave(t))
    logger_time = a_true + f_true * t
    f0 = 0.931                                         # 사전값으로 로거 시간축을 편 뒤 찾는다 (lef26_clock.measure_pair 와 같은 순서)
    sig_k0, sig = sync.grid(logger_time / f0, wave(t))
    m = sync.match_windows(ref_k0, ref, sig_k0, sig)
    assert len(m) > 60 and (m["corr"] > 0.99).all()
    fit = sync.fit_clock(m["ref_s"], m["sig_s"], slopes=np.arange(0.97, 1.0301, 0.0005))
    assert fit["f"] * f0 == pytest.approx(f_true, abs=2e-4)
    assert fit["a"] * f0 == pytest.approx(a_true, abs=0.1)
    assert fit["rms_s"] < 0.05


def test_fixed_offset_search_window_is_respected():
    t = np.arange(0, 300, 0.01)
    ref_k0, ref = sync.grid(t, wave(t))
    sig_k0, sig = sync.grid(t + 12.3, wave(t))
    m = sync.match_windows(ref_k0, ref, sig_k0, sig, expect=lambda x: x + 12.0, slack=lambda x: np.full_like(x, 5.0))
    assert np.median(m["sig_s"] - m["ref_s"]) == pytest.approx(12.3, abs=0.03)


def test_fit_clock_ignores_wrong_matches():
    """같은 코스를 도는 랩끼리는 파형이 닮아 틀린 짝이 섞인다. 맞는 짝만 한 직선 위에 있다."""
    rng = np.random.default_rng(1)
    x = np.arange(0, 600, 10.0)
    y = 5.0 + 0.9321 * x + rng.normal(0, 0.02, len(x))
    wrong = rng.choice(len(x), 20, replace=False)
    y[wrong] += rng.uniform(-200, 200, 20)
    fit = sync.fit_clock(x, y)
    assert fit["f"] == pytest.approx(0.9321, abs=2e-4)
    assert fit["n"] == len(x) - 20
    assert not fit["inliers"][wrong].any()


def test_fit_clock_gives_up_without_enough_points():
    assert sync.fit_clock([0, 10, 20], [0, 10, 20]) is None
    rng = np.random.default_rng(2)
    assert sync.fit_clock(np.arange(0, 100, 10.0), rng.uniform(-500, 500, 10), min_inliers=5) is None
