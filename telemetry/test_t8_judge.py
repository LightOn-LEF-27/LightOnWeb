"""합성 데이터로 판정 계산을 확인한다 (실제 주행 데이터 없음)."""

import numpy as np
import pandas as pd
import pytest

import t8_judge as t8


def head(ms):
    return [int(v).to_bytes(4, "little").hex() for v in ms]


def session(n=400, intv=0.5, clock=1000.0, delay=0.2, ppm=0.0, gap=None):
    """장치가 intv 마다 보내고 delay 뒤에 서버가 받는 세션. clock = 서버 시계 − 장치 시계."""
    dev = (np.arange(n) * intv + 5.0) * 1000.0
    recv = dev / 1000.0 * (1 + ppm * 1e-6) + clock + delay
    if gap:
        keep = (dev < gap[0]) | (dev >= gap[1])
        dev, recv = dev[keep], recv[keep]
    return pd.DataFrame({"recv_s": recv, "topic": "x/d", "head": head(dev)}), dev, recv


def frames_of(dev, recv, wait_ms=250.0):
    return pd.DataFrame({"recv_s": recv, "device_ts_ms": dev - wait_ms, "can_id": 1094.0})


def test_clock_difference_does_not_change_age():
    a, dev, recv = session(clock=1000.0)
    b, dev2, recv2 = session(clock=1234.5)
    ra = t8.judge_session(a, frames_of(dev, recv), floor_s=0.1)
    rb = t8.judge_session(b, frames_of(dev2, recv2), floor_s=0.1)
    assert ra["age_p50_s"] == pytest.approx(rb["age_p50_s"], abs=1e-3)
    # 고정 지연은 바닥으로 빠지고 묶음 대기 + 가정한 바닥만 남는다
    assert ra["age_p50_s"] == pytest.approx(0.25 + 0.1, abs=1e-3)


def test_clock_rate_difference_is_removed():
    m, dev, recv = session(n=4000, ppm=50.0)
    r = t8.judge_session(m, frames_of(dev, recv), floor_s=0.0)
    assert r["transit_p95_s"] < 0.005


def test_extra_delay_shows_up():
    m, dev, recv = session(n=1000)
    recv = recv.copy()
    recv[500:] += 2.0
    m["recv_s"] = recv
    r = t8.judge_session(m, frames_of(dev, recv), floor_s=0.0)
    # 길게 이어진 혼잡이 시계 속도 차이로 흡수되지 않는다 (상한 100 ppm × 500 s = 0.05 s 안)
    assert r["transit_p95_s"] == pytest.approx(2.0, abs=0.06)
    assert t8.verdict(r)["age_p95"] is True
    recv[500:] += 2.0
    m["recv_s"] = recv
    r = t8.judge_session(m, frames_of(dev, recv), floor_s=0.0)
    assert t8.verdict(r)["age_p95"] is False


def test_outage_found_in_device_time_and_counted_per_lap():
    m, dev, recv = session(n=400, gap=(50_000, 65_000))
    cross = np.array([10_000.0, 60_000.0, 110_000.0, 160_000.0])
    r = t8.judge_session(m, frames_of(dev, recv), crossings_ms=cross, floor_s=0.1)
    assert r["outages"] == 1
    assert r["outage_longest_s"] == pytest.approx(15.5, abs=0.01)
    # 계측선을 걸친 끊김은 두 바퀴 모두에 센다
    assert r["outages_per_lap_max"] == 1 and r["laps"] == 3


def test_short_gap_is_not_an_outage():
    m, dev, recv = session(n=400, gap=(50_000, 58_000))
    assert t8.judge_session(m, frames_of(dev, recv))["outages"] == 0


def test_boot_connect_time_is_not_an_outage_but_tail_is():
    m, dev, recv = session(n=100)
    span = (0.0, dev[-1] + 30_000.0)
    r = t8.judge_session(m, frames_of(dev, recv), sd_span_ms=span)
    assert r["connect_s"] == pytest.approx(5.0)
    assert r["outages"] == 1 and r["outage_longest_s"] == pytest.approx(30.0, abs=0.01)


def test_broker_last_will_after_power_off_is_not_an_outage():
    m, dev, recv = session(n=100)
    will = pd.DataFrame({"recv_s": [recv[-1] + 15.0], "topic": ["x/d/offline"], "head": [""]})
    r = t8.judge_session(pd.concat([m, will]), frames_of(dev, recv))
    assert r["outages"] == 0


def test_two_outages_in_a_lap_fail():
    m, dev, recv = session(n=400)
    keep = ((dev < 20_000) | (dev >= 32_000)) & ((dev < 40_000) | (dev >= 52_000))
    m2 = m[keep]
    r = t8.judge_session(m2, frames_of(dev[keep], recv[keep]), crossings_ms=np.array([10_000.0, 100_000.0]))
    assert r["outages_per_lap_max"] == 2
    assert t8.verdict(r)["outages"] is False


def test_windows_limit_what_is_judged():
    m, dev, recv = session(n=400, gap=(50_000, 65_000))
    r = t8.judge_session(m, frames_of(dev, recv), windows=[(100_000.0, 150_000.0)])
    assert r["outages"] == 0
    assert r["watched_s"] == pytest.approx(50.0, abs=1.0)


def test_throttle_matches_firmware_rule():
    ts = np.arange(0, 2000, 100.0)
    w = t8.throttle(ts, np.full(len(ts), 1.0), 500.0)
    assert list(w["device_ts_ms"]) == [0, 500, 1000, 1500]


def test_throttle_is_per_id():
    ts = np.array([0.0, 100.0, 200.0, 600.0])
    ids = np.array([1.0, 2.0, 1.0, 2.0])
    assert len(t8.throttle(ts, ids, 500.0)) == 3


def test_delivered_ratio_uses_setting_not_loop_period():
    m, dev, recv = session(n=400, intv=0.504)
    sd = pd.DataFrame({"device_ts_ms": np.arange(0, 200_000, 100.0), "can_id": 1.0})
    got = t8.throttle(sd["device_ts_ms"].to_numpy(), sd["can_id"].to_numpy(), 500.0)
    f = pd.DataFrame({"recv_s": 2000.0 + got["device_ts_ms"] / 1000.0, "device_ts_ms": got["device_ts_ms"], "can_id": 1.0})
    r = t8.judge_session(m, f, sd_frames=sd)
    assert r["delivered_ratio"] == pytest.approx(1.0)
    assert r["in_sd_ratio"] == pytest.approx(1.0)


def test_too_short_session_is_skipped():
    m, dev, recv = session(n=5)
    assert t8.judge_session(m, frames_of(dev, recv)) is None


def test_no_frames_cannot_be_judged():
    m, dev, recv = session()
    r = t8.judge_session(m, pd.DataFrame(columns=["recv_s", "device_ts_ms", "can_id"]))
    v = t8.verdict(r)
    assert v["age_median"] is None and v["age_p95"] is None and v["outages"] is None


def test_ddmm_conversion():
    assert t8.ddmm_to_deg(37.3311) == pytest.approx(37 + 33.11 / 60)


def test_spots_group_by_distance_and_laps():
    lat = np.array([35.0, 35.0001, 35.01])
    lon = np.array([126.0, 126.0001, 126.0])
    s = t8.spots(lat, lon, [1, 3, 2])
    assert s[0]["count"] == 2 and s[0]["laps"] == [1, 3] and s[1]["count"] == 1


def test_assign_sessions_by_start_time():
    m = pd.DataFrame({"recv_s": [100.0, 150.0, 300.0]})
    s = pd.DataFrame({"id": [7, 8], "started_s": [100.0, 200.0]})
    assert list(t8.assign_sessions(m, s)) == [7, 7, 8]
