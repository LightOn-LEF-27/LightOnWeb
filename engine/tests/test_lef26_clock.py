"""LEF-26 로거 시계 검증.

앞쪽은 만들어 낸 데이터로 도는 단위 테스트, 뒤쪽은 실제 테스트위크 데이터의 골든 값이다 (값과 그 이유는 `lef26_clock.toml`).
골든은 먼저 `lefdata lef26` · `lefdata gopro --event <테스트위크> --laps` 로 변환해 둬야 돈다.
"""

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import lef26_clock as lc, paths, video


# ---------------------------------------------------------------- 단위 테스트

def logger_times(duration_s, f, sync_every_ticks_s=5.0, tick_rate=1.02, gap_s=0.039, extra_gaps=0, seed=0):
    """로거가 찍는 시각을 흉내 낸다: 5 ms 마다 한 줄, 틱 5 초마다 SD 동기화로 39 ms 멈춤."""
    rng = np.random.default_rng(seed)
    true = np.arange(0, duration_s, 0.005)
    period = sync_every_ticks_s / tick_rate + gap_s                 # 참 시간으로 본 동기화 주기
    starts = np.arange(1.0, duration_s, period)
    extra = rng.uniform(0, duration_s, extra_gaps)                  # SD 동기화가 아닌 공백
    stalled = np.zeros(len(true), bool)
    for s in np.concatenate([starts, extra]):
        stalled |= (true > s) & (true < s + gap_s)
    return np.round(true[~stalled] * f * 256) / 256                 # RTC subsecond 는 1/256 s 단위


def test_sync_period_reads_the_clock_rate_from_the_log():
    f, tick_rate = 0.931, 1.02
    got = lc.sync_period(logger_times(600, f, tick_rate=tick_rate, extra_gaps=40))
    assert got["sync_spans"] > 100
    assert got["rtc_per_tick"] == pytest.approx(f / tick_rate, abs=0.001)


def test_sync_period_says_nothing_without_a_period():
    t = np.sort(np.random.default_rng(1).uniform(0, 600, 3000))     # 띄엄띄엄 찍힌 로그: 공백투성이
    assert lc.sync_period(t)["rtc_per_tick"] is None
    assert lc.sync_period([])["rtc_per_tick"] is None
    assert lc.sync_period(np.arange(0, 600, 0.005))["sync_spans"] == 0      # 공백이 하나도 없다


def test_mode_of():
    assert lc.mode_of(1.0) == "gps_pps" and lc.mode_of(0.0) == "free"
    assert lc.mode_of(0.5) == "mixed" and lc.mode_of(None) == "unknown"


def test_clock_map_interpolates_and_extends():
    win = pd.DataFrame({"recording_id": "r", "video": ["a"] * 3 + ["b"] * 3,
                        "t_rel_s": [100.0, 110, 120, 500, 510, 520],
                        "true_s": [1000.0, 1010.75, 1021.5, 1430, 1440.5, 1451]})
    pairs = pd.DataFrame({"recording_id": "r", "video": ["a", "b"], "accepted": True, "f": [0.93, 0.95]})
    cm = lc.clock_map(win, pairs, "r")
    assert cm.to_true(105.0) == pytest.approx(1005.375)             # 영상 안: 이웃한 창 사이를 잇는다
    assert cm.to_true(310.0) == pytest.approx((1021.5 + 1430) / 2)  # 두 영상 사이도 양끝이 잡혀 있다
    assert cm.to_true(100.0 - 93.0) == pytest.approx(900.0)         # 앞쪽 바깥: 첫 영상의 속도로
    assert cm.to_true(520.0 + 95.0) == pytest.approx(1551.0)        # 뒤쪽 바깥: 마지막 영상의 속도로
    assert lc.clock_map(win.iloc[:3], pairs, "r") is None           # 창이 모자라면 지도를 만들지 않는다


def test_speed_ratio_survives_stale_samples():
    """표본의 40 % 가 옛 값이어도 맞는 표본끼리 모이는 환산값을 찾는다. GPS 는 0.2 초 늦다."""
    rng = np.random.default_rng(5)
    t = np.sort(rng.uniform(0, 600, 3000))
    speed = lambda x: 55 + 30 * np.sin(2 * np.pi * x / 37.0) + 10 * np.sin(2 * np.pi * x / 11.0)
    k_true, lag_true = 1.17, 0.2
    pct = speed(t) / k_true
    stale = rng.random(len(t)) < 0.4
    pct[stale] = speed(t[stale] - rng.uniform(1, 5, stale.sum())) / k_true
    fit = lc.fit_speed_ratio(lambda lag: speed(t + lag - lag_true) + rng.normal(0, 0.5, len(t)), pct,
                             np.arange(1.0, 1.3, 0.002))
    assert fit["k"] == pytest.approx(k_true, rel=0.005)
    assert fit["lag_s"] == pytest.approx(lag_true, abs=0.06)
    assert 0.5 < fit["agree_share"] < 0.75


def test_speed_ratio_needs_enough_samples():
    assert lc.fit_speed_ratio(lambda lag: np.full(10, 50.0), np.full(10, 40.0), np.arange(1.0, 1.3, 0.002)) is None


# ---------------------------------------------------------------- 골든 (테스트위크 실제 데이터 — lef26_clock.toml)

E = Expected("lef26_clock")
NORM = paths.LEF26_EVENT / "normalized"
needs_data = pytest.mark.skipif(not ((NORM / "recordings.csv").exists() and (NORM / video.INDEX_NAME).exists()),
                                reason="로거 또는 영상 normalized/ 없음")


@pytest.fixture(scope="module")
def result():
    pairs, windows = lc.measure_event(paths.LEF26_EVENT)
    clock = lc.summarize(paths.LEF26_EVENT, pairs, windows).set_index("recording_id")
    return clock, pairs, windows


def _seen(result, mode):
    clock = result[0]
    return clock[(clock["f_basis"] == "video") & (clock["mode"] == mode)]


@needs_data
@E.need
def test_log_alone_tells_which_clock_mode(result):
    """밀리초 분포만으로 시계 상태(GPS 가 잡음 · 혼자 돎 · 도중에 바뀜)를 가린다."""
    clock = result[0]
    assert clock["mode"].value_counts().to_dict() == E["mode.counts"]
    day = clock.index.str[:10]
    first = E["mode.first_day"]
    assert (clock[day == first]["mode"] != "free").sum() == E["mode.first_day_not_free"]
    assert (clock[day != first]["mode"] == "gps_pps").sum() == E["mode.other_days_gps_pps"]
    assert clock.loc[E["mode.lsi_recording"], "lsi_ratio_from_pps"] == E.approx("mode.lsi_ratio_from_pps")


@needs_data
def test_converter_leaves_gps_disciplined_clocks_alone(result):
    seen = _seen(result, "gps_pps")
    assert (seen["f_applied"] == 1.0).all()                                 # 변환기는 손대지 않았다


@needs_data
@E.need
def test_gps_disciplined_clock_matches_video(result):
    seen = _seen(result, "gps_pps")
    assert sorted(seen.index) == E["gps_pps.seen"]
    assert (seen["f"] - 1).abs().max() < E["gps_pps.f_err_max"]
    assert seen[["normalized_minus_true_first_obs_s", "normalized_minus_true_last_obs_s"]].abs().max().max() < E["gps_pps.obs_err_max_s"]


@needs_data
@E.need
def test_free_running_clock_rate(result):
    seen = _seen(result, "free")
    assert len(seen) == E["free.seen"]
    assert seen["f"].between(*E.range("free.f")).all()
    assert seen["line_rms_s"].max() < E["free.line_rms_max_s"]
    r = seen.loc[E["free.longest"]]
    assert r["videos"] == E["free.longest_videos"]
    assert r["logger_minus_true_at_end_s"] == E.approx("free.longest_end_s")
    assert r["true_duration_s"] - r["logger_duration_s"] == E.approx("free.longest_gap_s")


@needs_data
@E.need
def test_converter_correction_holds_against_video(result):
    """변환기가 로그만으로 바로잡은 시간(시계 모델 v2)을 영상으로 검증한다."""
    seen = _seen(result, "free")
    assert (seen["f_applied_basis"] == "sd_sync").all()
    assert (seen["f_residual"] - 1).abs().max() < E["corrected.f_residual_max"]
    end = seen["normalized_minus_true_at_end_s"]
    odd = E["corrected.exception"]
    assert end.drop(odd).abs().max() < E["corrected.end_err_max_s"]
    assert end[odd] == E.approx("corrected.exception_end_s")
    assert seen.loc[odd, "normalized_minus_true_first_obs_s"] == E.approx("corrected.exception_first_s")


@needs_data
@E.need
def test_sd_sync_period_agrees_with_video(result):
    """로그만으로 잰 속도(SD 동기화 주기 × 틱 속도)가 영상으로 잰 속도와 맞는다."""
    seen = _seen(result, "free")
    assert seen["tick_rate"].between(*E.range("sd_sync.tick_rate")).all()
    predicted = seen["rtc_per_tick"] * seen["tick_rate"].median()
    assert ((predicted / seen["f"]) - 1).abs().max() < E["sd_sync.predict_err_max"]


@needs_data
def test_mixed_recordings_get_no_single_rate(result):
    """도중에 시계 상태가 바뀐 레코딩은 속도 하나로 말하지 않는다."""
    clock = result[0]
    assert clock[clock["mode"] == "mixed"]["f"].isna().all()


@needs_data
@E.need
def test_recordings_without_video_still_get_a_rate(result):
    clock = result[0]
    basis = {("none" if pd.isna(k) else k): v for k, v in clock["f_basis"].value_counts(dropna=False).to_dict().items()}
    assert basis == E["no_video.basis"]
    sd = clock[clock["f_basis"] == "sd_sync"]
    assert sd["f"].between(*E.range("no_video.sd_f")).all()
    assert (sd["true_duration_s"] > sd["logger_duration_s"] * E["no_video.sd_stretch_min"]).all()


@needs_data
def test_windows_meet_the_match_threshold(result):
    _, _, windows = result
    assert len(windows) and (windows["corr"] >= lc.MIN_CORR).all()


@needs_data
@E.need
def test_windows_are_clean_matches(result):
    _, pairs, windows = result
    assert pairs["accepted"].sum() == E["windows.accepted_pairs"] and windows["video"].nunique() == E["windows.videos"]
    assert pairs[pairs["accepted"]]["corr_median"].min() > E["windows.corr_median_min"]


@needs_data
@E.need
def test_motor_speed_ratio(result):
    """모터 속도 → 차속 환산값을 영상 GPS 로 잰다. 영상마다 같은 값이 나온다."""
    _, pairs, windows = result
    speed = lc.speed_calibration(paths.LEF26_EVENT, pairs, windows)
    k = lc.speed_ratio(speed)
    assert k == E.approx("motor_speed.k_kmh_per_pct")
    big = speed[speed["used"] >= lc.SPEED_SUMMARY_MIN]
    assert len(big) == E["motor_speed.videos_used"]
    assert ((big["kmh_per_pct"] / k) - 1).abs().max() < E["motor_speed.per_video_err_max"]
    assert big["gps_lag_s"].between(*E.range("motor_speed.gps_lag_s")).all()
    assert big["repeat_share"].min() > E["motor_speed.repeat_share_min"]
