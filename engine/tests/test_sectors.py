"""구간 · 시간 차 (`lefdata.sectors`) — 만들어 낸 코스로 도는 단위 테스트 (데이터 없이 돈다)."""

import numpy as np
import pytest

from lefdata import sectors

R = 40.0


def circle(n: int = 4000, radius=R):
    """반지름 R 원을 반시계로 한 바퀴 — 원점에서 동쪽으로 출발 (계약 테스트의 원과 같은 모양). radius 는 각도의 함수여도 된다."""
    ang = np.linspace(0.0, 2 * np.pi, n)
    r = radius(ang) if callable(radius) else np.full(n, float(radius))
    return ang, r * np.sin(ang), R - r * np.cos(ang)


@pytest.fixture(scope="module")
def ref():
    _, x, y = circle()
    return sectors.Reference(x, y)


def test_reference_length_and_heading(ref):
    assert ref.length == pytest.approx(2 * np.pi * R, rel=1e-5)
    x, y, h = ref.at([0.0, ref.length / 4])
    assert (x[0], y[0]) == pytest.approx((0.0, 0.0), abs=1e-6) and h[0] == pytest.approx(90.0, abs=0.5)     # 동쪽으로 출발
    assert (x[1], y[1]) == pytest.approx((R, R), abs=0.01) and h[1] == pytest.approx(0.0, abs=0.5) or h[1] == pytest.approx(360.0, abs=0.5)


def test_projection_follows_the_place_not_the_distance(ref):
    """주행선이 달라 달린 거리가 달라도, 같은 각도(코스의 같은 곳)면 같은 기준 거리다. 랩 길이로 늘이고 줄이면 몇 m 어긋난다."""
    ang, x, y = circle(1500, radius=lambda a: R + 2.0 * np.sin(a))      # 앞 절반은 바깥, 뒤 절반은 안쪽으로 돈 랩
    s = sectors.project(ref, x, y)
    want = R * ang
    assert np.abs(s - want).max() < 0.1
    own = sectors.path_length(x, y)
    scaled = own * ref.length / own[-1]
    assert np.abs(scaled - want).max() > 1.0                            # 늘이고 줄이기로는 이만큼 틀린다 (이 시험의 의미)


def test_projection_is_monotone_and_pinned_at_the_gate(ref):
    rng = np.random.default_rng(1)
    _, x, y = circle(1200)
    s = sectors.project(ref, x + rng.normal(0, 0.4, len(x)), y + rng.normal(0, 0.4, len(y)))
    assert s[0] == 0.0 and s[-1] == pytest.approx(ref.length) and np.all(np.diff(s) >= 0)


def test_projection_does_not_jump_to_the_other_leg():
    """헤어핀 — 두 직선이 6 m 떨어져 있다. 바깥으로 4 m 벗어난 점이 반대쪽 직선에 더 가까워도 제 쪽에 내린다."""
    gap, length = 6.0, 200.0
    top = np.linspace(0, length, 801)
    arc = np.linspace(-np.pi / 2, np.pi / 2, 61)[1:-1]
    xs = np.concatenate([top, length + gap / 2 * np.cos(arc), top[::-1], -gap / 2 * np.cos(arc)[::-1], [0.0]])
    ys = np.concatenate([np.zeros_like(top), gap / 2 + gap / 2 * np.sin(arc), np.full_like(top, gap), gap / 2 - gap / 2 * np.sin(arc)[::-1], [0.0]])
    ref = sectors.Reference(xs, ys)
    lap_x = xs.copy()
    lap_y = ys.copy()
    first = slice(100, 700)                                             # 첫 직선 가운데를 반대쪽으로 4 m 벗어나 달린다
    lap_y[first] += 4.0
    s = sectors.project(ref, lap_x, lap_y)
    assert np.abs(s[first] - lap_x[first]).max() < 0.5                  # 첫 직선의 거리 = x (반대쪽 직선이면 300 m 넘게 틀린다)


def test_split_sums_to_the_lap_and_handles_flat_parts():
    t = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    s = np.array([0.0, 10.0, 10.0, 25.0, 30.0, 30.0])                  # 멈춘 곳(같은 값)이 있다
    edges = np.array([0.0, 10.0, 20.0, 30.0])
    parts = sectors.split(t, s, edges)
    assert parts.sum() == pytest.approx(5.0)                           # 0 과 끝은 랩의 첫 · 끝 시각 그대로
    assert sectors.crossing_times(t, s, [10.0])[0] == pytest.approx(1.0)     # 처음 닿은 시각


def test_delta_grows_only_where_the_lap_is_slower(ref):
    """기준 랩은 일정한 속도. 비교 랩은 앞 절반은 같고 뒤 절반만 10 % 느리다 → 시간 차는 절반부터 늘어 끝에서 랩타임 차."""
    ang, x, y = circle(2000)
    s_ref = sectors.project(ref, x, y)
    t_ref = 30.0 * ang / (2 * np.pi)
    t_cmp = np.where(ang <= np.pi, t_ref, 15.0 + 1.1 * (t_ref - 15.0))
    at = np.linspace(0, ref.length, 9)
    d = sectors.delta(t_cmp + 100.0, s_ref, t_ref + 7.0, s_ref, at)  # 세션 시각이 달라도 각자 랩 시작부터 잰다
    assert np.abs(d[:4]).max() < 1e-6 and abs(d[4]) < 1e-3          # 절반(d[4])은 점 간격만큼의 보간 오차
    assert d[-1] == pytest.approx(1.5, abs=1e-6) and d[6] == pytest.approx(0.75, abs=1e-3)
    pts = sectors.delta_at_points(t_cmp, s_ref, t_ref, s_ref)
    assert pts[0] == 0.0 and pts[-1] == pytest.approx(1.5, abs=1e-6)


def test_pick_reference_skips_a_cut_lap():
    laps = [{"key": "a", "length_m": 850.0, "time_s": 66.0}, {"key": "b", "length_m": 853.0, "time_s": 65.5},
            {"key": "cut", "length_m": 800.0, "time_s": 60.0}, {"key": "c", "length_m": 851.0, "time_s": 67.0}]
    assert sectors.pick_reference(laps) == "b"
    assert sectors.pick_reference([]) is None


def test_edges():
    assert list(sectors.equal_edges(90.0, 3)) == [0.0, 30.0, 60.0, 90.0]
    assert list(sectors.mini_edges(60.0, 25.0)) == [0.0, 25.0, 50.0, 60.0]        # 마지막은 남은 10 m
    assert list(sectors.mini_edges(75.0, 25.0)) == [0.0, 25.0, 50.0, 75.0]


def test_isotonic_keeps_order_and_mean():
    y = np.array([0.0, 2.0, 1.0, 3.0, 2.5, 4.0])
    z = sectors.isotonic(y)
    assert np.all(np.diff(z) >= 0) and z.mean() == pytest.approx(y.mean())
