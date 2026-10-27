"""영상 GPS 로 나눈 구간에 로거 값을 붙이는 `lef26_laps` 검증.

앞쪽은 만들어 낸 데이터로 도는 단위 테스트, 뒤쪽은 실제 테스트위크 데이터의 골든 값이다 (값은 `lef26_laps.toml`).
골든은 `lefdata lef26` · `gopro --laps` · `events` · `lef26-clock` 이 끝나 있어야 돈다.
"""

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import lef26_clock as lc, lef26_laps as ll, paths

G = 9.80665
T0 = 1_000_000.0            # 참 시각(UTC 초)의 시작. 로거 경과시간 0 이 여기다


# ---------------------------------------------------------------- 단위 테스트

def test_clock_map_goes_both_ways():
    """to_rel 은 to_true 의 역이다 — 창 사이에서도, 창 바깥(연장)에서도."""
    cm = lc.ClockMap(np.array([100.0, 200.0, 300.0]), np.array([5000.0, 5100.2, 5200.1]), f_head=0.999, f_tail=1.002)
    t_rel = np.array([40.0, 100.0, 150.0, 260.0, 300.0, 380.0])
    assert cm.to_rel(cm.to_true(t_rel)) == pytest.approx(t_rel, abs=1e-6)
    assert cm.to_rel(5100.2) == pytest.approx(200.0)


def test_binned_mean_is_not_dragged_by_bursts():
    """한순간에 16 번 찍힌 값이 평균을 끌고 가지 않는다. 표본이 없던 초는 비율로 드러난다."""
    t = np.concatenate([[0.5], np.full(16, 1.5), [2.5]])
    v = np.concatenate([[100.0], np.full(16, 300.0), [200.0]])
    mean, share = ll.binned_mean(t, v, 0.0, 10.0)
    assert mean == pytest.approx(200.0)              # 그냥 평균이면 283
    assert share == pytest.approx(0.3)
    assert ll.binned_mean([], [], 0.0, 10.0) == (None, 0.0)


def drive(t):
    """만들어 낸 주행: 속도(m/s)와 요레이트(rad/s, 좌회전 양수)."""
    return 15 + 8 * np.sin(2 * np.pi * t / 20.0), 0.3 * np.sin(2 * np.pi * t / 13.0)


def synthetic(gps_late_s=0.2, long_off=0.05, lat_off=-0.03, logger_from=0.0, logger_to=120.0):
    """같은 주행을 본 로거(100 Hz 가속도계)와 영상 GPS(18 Hz, gps_late_s 만큼 늦다)."""
    t = np.arange(logger_from, logger_to, 0.01)
    v, yaw = drive(t)
    long_g = np.gradient(v, t) / G
    lat_left_g = v * yaw / G
    empty = lambda *cols: pd.DataFrame({c: [] for c in ("t_s", *cols)})
    log = ll.Logger("log", lc.ClockMap(np.array([0.0, 120.0]), np.array([T0, T0 + 120.0]), 1.0, 1.0), np.array([T0, T0 + 120.0]),
                    t, long_g + long_off, lat_left_g + lat_off, None,
                    empty("soc_pct", "cell_min_v"), empty("lowest_c", "highest_c", "average_c", "hottest_id"), {})
    tg = np.arange(0.0, 120.0, 0.055)
    vg, _ = drive(tg - gps_late_s)
    fine = np.arange(-1.0, 121.0, 0.001)
    heading = np.cumsum(drive(fine)[1]) * 0.001                               # 수학 각도(반시계 양수)
    course = (-np.degrees(np.interp(tg - gps_late_s, fine, heading))) % 360    # 진행 방위: 시계방향이 증가
    vid = ll.Video(tg, T0 + tg, vg * 3.6, *ll.gps_accel(T0 + tg, vg * 3.6, course))
    return log, vid


def test_gps_accel_signs():
    """가속하면 종 양수, 왼쪽으로 돌면 횡 양수."""
    _, vid = synthetic(gps_late_s=0.0)
    v, yaw = drive(vid.grid - T0)
    mid = slice(100, -100)
    assert np.corrcoef(vid.lat_left_g[mid], (v * yaw / G)[mid])[0, 1] > 0.99
    assert np.corrcoef(vid.long_g[mid], (np.gradient(v, vid.grid) / G)[mid])[0, 1] > 0.99


def test_alignment_finds_the_gps_delay_and_the_sensor_offset():
    log, vid = synthetic(gps_late_s=0.2, long_off=0.05, lat_off=-0.03)
    got = ll.alignment(log, vid, T0 + 30, T0 + 90)
    assert got["gps_lag_s"] == pytest.approx(0.2, abs=0.05)
    assert got["align_corr"] > 0.98
    assert got["imu_long_off_g"] == pytest.approx(0.05, abs=0.01)
    assert got["imu_lat_off_g"] == pytest.approx(-0.03, abs=0.01)


def test_alignment_notices_a_wrong_clock():
    """시계가 1 초 틀리면 gps_lag_s 가 1 초쯤으로 나온다 — 구간마다 적어 두는 이유."""
    log, vid = synthetic(gps_late_s=0.2)
    log.cm = lc.ClockMap(np.array([0.0, 120.0]), np.array([T0 + 1.0, T0 + 121.0]), 1.0, 1.0)     # 로거를 1 초 늦게 놓았다
    assert ll.alignment(log, vid, T0 + 30, T0 + 90)["gps_lag_s"] == pytest.approx(-0.8, abs=0.06)


def test_values_are_blank_when_the_logger_missed_part_of_the_segment():
    """로거가 구간의 절반만 기록했으면 최댓값을 내지 않는다. 얼마나 덮었는지만 적는다."""
    log, vid = synthetic(logger_to=60.0)
    half = ll.summarize_segment([log], vid, 30.0, 90.0)
    assert half["cover_share"] == pytest.approx(0.5, abs=0.01)
    assert half["logger_recording"] == "log" and "long_g_max" not in half
    full = ll.summarize_segment([log], vid, 10.0, 50.0)
    assert full["cover_share"] == 1.0
    v, yaw = drive(np.arange(10.0, 50.0, 0.01))
    assert full["lat_g_left_max"] == pytest.approx((v * yaw / G).max() - 0.03, abs=0.03)
    assert full["log_t_start_s"] == pytest.approx(10.0) and full["log_t_end_s"] == pytest.approx(50.0)
    assert ll.summarize_segment([], vid, 10.0, 50.0)["cover_share"] == 0.0


# ---------------------------------------------------------------- 골든 (테스트위크 실제 데이터 — lef26_laps.toml)

E = Expected("lef26_laps")
DERIVED = paths.LEF26_EVENT / "derived"
needs_data = pytest.mark.skipif(not all((DERIVED / f).exists() for f in ("laps.csv", "logger_clock_windows.csv")),
                                reason="derived/laps.csv 또는 logger_clock_windows.csv 없음")


@pytest.fixture(scope="module")
def result():
    return ll.run_event(paths.LEF26_EVENT)


@needs_data
def test_every_segment_is_fully_covered_by_the_logger(result):
    """구간마다 로거가 끝까지 기록하고 있었고, 그 구간 시각은 맞춘 파형 근처다."""
    for df in result.values():
        assert len(df) and (df["cover_share"] == 1.0).all()


@needs_data
@E.need
def test_segment_counts_and_recordings(result):
    assert {k: len(v) for k, v in result.items()} == E["segments"]
    for df in result.values():
        assert df["clock_dist_s"].max() < E["clock_dist_max_s"]
    assert result["lap"]["logger_recording"].value_counts().to_dict() == E["lap_logger_recordings"]


@needs_data
@E.need
def test_logger_accelerometer_agrees_with_video_gps(result):
    """시계는 자이로로 맞췄다. 가속도계 ↔ GPS 로 다시 대 봐도 구간마다 맞는다 (독립 검증)."""
    for kind, floor in E["agreement.corr_min"].items():
        df = result[kind]
        assert df["align_corr"].min() > floor, kind
        assert df["gps_lag_s"].between(*E.range("agreement.gps_lag_s")).all(), kind
    assert result["lap"]["gps_lag_s"].median() == E.approx("agreement.lap_gps_lag_median_s")


@needs_data
@E.need
def test_fastest_lap(result):
    lap = result["lap"]
    best = lap.loc[lap["lap_time_s"].idxmin()]
    f = "fastest_lap."
    assert (best["recording_id"], best["run"], best["lap"]) == (E[f + "recording"], E[f + "run"], E[f + "lap"])
    assert best["lap_time_s"] == E.approx(f + "lap_time_s")
    assert best["logger_recording"] == E[f + "logger_recording"]
    assert [best["soc_start_pct"], best["soc_end_pct"]] == E[f + "soc_pct"]
    assert best["iq_peak_a"] == E[f + "iq_peak_a"] and best["iq_cmd_peak_a"] == E[f + "iq_cmd_peak_a"]
    assert best["vdc_min_v"] == E.approx(f + "vdc_min_v")
    assert best["cell_min_v"] == E.approx(f + "cell_min_v")
    assert [best["batt_high_c"], best["batt_low_c"]] == E[f + "batt_c"]
    assert best["long_g_max"] == E.approx(f + "long_g_max")


@needs_data
def test_battery_only_drains_within_a_lap(result):
    """랩 안에서 SOC 가 오르는 일은 없다 (값이 다른 구간에서 오지 않았다는 확인)."""
    lap = result["lap"]
    assert len(lap) and (lap["soc_end_pct"] <= lap["soc_start_pct"]).all()


@needs_data
@E.need
def test_battery_extremes_within_laps(result):
    lap = result["lap"]
    assert (lap["soc_start_pct"] - lap["soc_end_pct"]).max() == E["battery.max_soc_drop_pct"]
    assert lap["batt_spread_c"].max() == E["battery.max_spread_c"] and lap["batt_high_c"].max() == E["battery.max_high_c"]
    assert lap["cell_min_v"].min() == E.approx("battery.cell_min_v")


@needs_data
@E.need
def test_laps_that_ran_the_course_backwards(result):
    """평균 횡가속도(센서 치우침을 뺀 값)의 부호가 도는 방향이다."""
    lap = result["lap"]
    net = lap["lat_g_mean"] - lap["imu_lat_off_g"]
    assert set(lap.loc[net < 0, "recording_id"]) == set(E["direction.clockwise_recordings"])
    assert (net > 0).sum() == E["direction.counterclockwise_laps"]


@needs_data
def test_skidpad_starts_with_the_right_hand_circle(result):
    """FSAE 규정: 스키드패드는 오른쪽 원부터 돈다."""
    sk = result["skidpad"]
    first = sk[(sk["arc"] == 1) & (sk["lap"] == 1)]
    assert len(first) and (first["turn"] == "right").all()


@needs_data
@E.need
def test_skidpad_lateral_g_matches_gps_circle(result):
    """스키드패드: 로거 평균 횡가속도 ↔ GPS 로 구한 v² / r. 방향도 맞는다."""
    sk = result["skidpad"]
    signed = np.where(sk["turn"] == "left", sk["lat_g_mean"], -sk["lat_g_mean"])
    assert (signed > E["skidpad.lat_g_min"]).all()
    ratio = signed / sk["lat_acc_g"]
    assert ratio.median() == E.approx("skidpad.ratio_median") and ratio.between(*E.range("skidpad.ratio")).all()


@needs_data
@E.need
def test_accel_runs_hit_the_current_limit(result):
    a = result["accel"]
    fast = a["v_max_kmh"] > E["accel.full_kmh"]
    full = a[fast & a["iq_peak_a"].notna()]
    assert len(full) == E["accel.full_with_current"]
    assert full["iq_cmd_peak_a"].between(*E.range("accel.iq_cmd_peak_a")).all()
    assert full["iq_peak_a"].between(*E.range("accel.iq_peak_a")).all()
    assert a.loc[fast, "long_g_max"].between(*E.range("accel.long_g_max")).all()
