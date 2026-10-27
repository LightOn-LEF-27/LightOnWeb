"""주행 상태 추정 (`lefdata.drive`) — 만들어 낸 주행으로 도는 단위 테스트 (데이터 없이 돈다)."""

import numpy as np
import pytest

from lefdata import drive


def run():
    """0 ~ 20 s 가속(전력 30 kW, 0.1 g) → 20 ~ 30 s 타력(전력 −1 kW, 0.05 g 감속) → 30 s 부터 제동(0.6 g, 약 32.5 s 에 선다)."""
    tv = np.arange(0.0, 40.0, 1 / 18)
    a = np.select([tv < 20, tv < 30, tv < 34], [0.1, -0.05, -0.6], 0.0) * drive.G
    v = np.clip(np.cumsum(a) / 18, 0, None) * 3.6
    tp = np.arange(0.0, 40.0, 0.01)
    p = np.select([tp < 20, tp < 34], [30.0, -1.0], 0.0)
    return tp, p, tv, v


def test_phases_come_out_as_states():
    tp, p, tv, v = run()
    t, st = drive.estimate(tp, p, tv, v)
    at = lambda s: st[np.argmin(np.abs(t - s))]                      # noqa: E731
    assert at(10) == 1 and at(25) == 0 and at(31) == -1
    assert np.isnan(at(38))                                           # 서 있으면 정하지 않는다
    assert set(np.unique(st[np.isfinite(st)])) <= {-1.0, 0.0, 1.0}
    sh = drive.shares(st)
    assert sum(sh.values()) == pytest.approx(1.0) and sh[1] > sh[0] > sh[-1] > 0


def test_a_hole_in_the_power_record_is_unknown_not_coast():
    tp, p, tv, v = run()
    keep = (tp < 22) | (tp > 26)
    t, st = drive.estimate(tp[keep], p[keep], tv, v)
    inside = (t > 22.5) & (t < 25.5)
    assert inside.any() and np.isnan(st[inside]).all()
    assert st[np.argmin(np.abs(t - 27))] == 0


def test_no_overlap_gives_nothing():
    t, st = drive.estimate([0.0, 1.0], [5.0, 5.0], [10.0, 11.0, 12.0], [30.0, 30.0, 30.0])
    assert len(t) == len(st) == 0
