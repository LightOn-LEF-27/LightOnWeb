"""주행 상태 추정 (`lefdata.drive`) — 만들어 낸 주행으로 도는 단위 테스트 (데이터 없이 돈다)."""

import numpy as np
import pandas as pd
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


def test_light_throttle_at_low_speed_is_throttle():
    """전력 = 토크 × 회전수 — 저속에서 가볍게 밟으면 1 kW 도 안 된다. 그래도 가속이고, 0 kW 면 타력이다."""
    tv = np.arange(0.0, 20.0, 0.1)
    v = np.full(len(tv), 12.0)
    tp = np.arange(0.0, 20.0, 0.01)
    p = np.where(tp < 10, 0.8, 0.0)
    t, st = drive.estimate(tp, p, tv, v)
    assert (st[(t > 1) & (t < 9)] == 1).all() and (st[(t > 11) & (t < 19)] == 0).all()


def test_throttle_signal_decides_throttle_instead_of_power():
    """가속페달 신호(밟음 0 · 1)를 주면 전력은 보지 않는다 — 전력이 없어도 된다. 제동은 그대로 속도 변화율로."""
    tp, p, tv, v = run()
    ta = np.arange(0.0, 40.0, 0.05)
    on = (ta < 10).astype(float)                       # 0 ~ 10 s 만 밟음 (전력은 20 s 까지 30 kW)
    for args in ((tp, p), (None, None)):
        t, st = drive.estimate(*args, tv, v, throttle=(ta, on))
        at = lambda s: st[np.argmin(np.abs(t - s))]                  # noqa: E731
        assert at(5) == 1 and at(15) == 0 and at(25) == 0 and at(31) == -1


def test_a_hole_in_the_throttle_record_is_unknown():
    tp, p, tv, v = run()
    ta = np.concatenate([np.arange(0.0, 12.0, 0.05), np.arange(16.0, 40.0, 0.05)])
    t, st = drive.estimate(tp, p, tv, v, throttle=(ta, np.ones(len(ta))))
    inside = (t > 12.5) & (t < 15.5)
    assert inside.any() and np.isnan(st[inside]).all() and st[np.argmin(np.abs(t - 17))] == 1


def test_agreement_compares_the_same_moments():
    t = np.round(np.arange(0.0, 10.0, 0.1), 1)
    a = np.where(t < 5, 1.0, 0.0)
    b = a.copy()
    b[t >= 8] = -1.0
    b[t < 1] = np.nan
    same, n = drive.agreement(t, a, t[10:] + 1e-9, b[10:])          # 칸 시각이 아주 조금 달라도 같은 칸
    assert n == 90 and same == pytest.approx(70 / 90)


def _session(blocks):
    from lefdata.catalog import Session
    return Session("LEF-27_2027-06-05_S1", "LEF-27", "2027-06-05", "시험", pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul"), 40.0, blocks=blocks)


def _blocks(with_pedal=True, with_power=True):
    """run() 의 주행 + 가속페달 블록(토크 명령: 0 ~ 10 s 만 20 %, 뗀 동안 0) + 배터리 전력."""
    from lefdata.catalog import HAVE, Channel, DataBlock
    tp, p, tv, v = run()
    out = [DataBlock("gps", "GPS", HAVE, "시험용", channels=[Channel("speed_kmh", "속도", "km/h", tv, v)])]
    if with_pedal:
        ta = np.arange(0.0, 40.0, 0.05)
        cmd = np.where(ta < 10, 20.0, 0.0)
        out.append(DataBlock("pedal", "가속페달", HAVE, "시험용", channels=[Channel("apps_pct", "가속페달", "%", ta, cmd + 3.0),
                                                                     Channel("torque_cmd_pct", "토크 명령", "%", ta, cmd)]))
    if with_power:
        out.append(DataBlock("battery", "배터리", HAVE, "시험용", channels=[Channel("power_kw", "배터리 전력", "kW", tp, p)]))
    return out


def test_drive_block_uses_the_pedal_and_checks_the_power_estimate_against_it():
    from lefdata import session_store as store
    b = store.drive_block(_session(_blocks()))
    ch = b.channels[0]
    assert "토크 명령" in b.device and "토크 명령 > 0.5 %" in ch.derivation and ch.cautions[0].startswith("제동은 추정")
    at = lambda s: ch.v[np.argmin(np.abs(ch.t_s - s))]                 # noqa: E731
    assert at(5) == 1 and at(15) == 0                                 # 10 ~ 20 s 는 전력이 있어도 페달을 뗐다 → 타력
    assert len(b.checks) == 1 and "전력" in b.checks[0] and "같은 상태" in b.checks[0]
    only_power = store.drive_block(_session(_blocks(with_pedal=False)))
    assert "배터리 전력" in only_power.device and only_power.checks == []
    assert only_power.channels[0].v[np.argmin(np.abs(only_power.channels[0].t_s - 15))] == 1
    no_power = store.drive_block(_session(_blocks(with_power=False)))
    assert no_power is not None and no_power.checks == []
    assert store.drive_block(_session(_blocks(with_pedal=False, with_power=False))) is None
