"""모터 속도로 나눈 주행 구간(`motor_runs`) 검증. 먼저 `lefdata lef26` 로 변환한 뒤 실행한다 (값은 `motor_runs.toml`)."""

import pytest

from expected import Expected
from lefdata import motor_runs, paths

E = Expected("motor_runs")
pytestmark = pytest.mark.skipif(not (paths.LEF26_EVENT / "normalized" / "recordings.csv").exists(),
                                reason="normalized/ 없음")


@pytest.fixture(scope="module")
def runs():
    return motor_runs.run_event(paths.LEF26_EVENT)[0]


@E.need
def test_run_counts(runs):
    assert len(runs) == E["runs"]
    by = runs.groupby("recording_id").size()
    for rid, n in E["by_recording"].items():
        assert by[rid] == n, rid
    assert runs["duration_s"].sum() / 60 == E.approx("total_min")
    assert runs["duration_s"].max() == E.approx("longest_s")


def test_soc_only_goes_down_while_driving(runs):
    """주행 중에 배터리 잔량이 늘면 구간 검출이나 BMS 해독이 틀린 것이다 (회생은 미미)."""
    r = runs.dropna(subset=["soc_start_pct", "soc_end_pct"])
    assert len(r)
    assert (r["soc_end_pct"] <= r["soc_start_pct"] + 0.5).all()
    for _, g in r.groupby("recording_id"):
        assert g["soc_start_pct"].is_monotonic_decreasing


@E.need
def test_runs_are_real_driving(runs):
    """검출된 구간에서는 전류가 흐르고 부하 때문에 팩 전압이 내려가야 한다."""
    hard = runs[runs["iq_peak_a"] > E["hard_iq_peak_a"]].dropna(subset=["vdc_min_v"])
    assert len(hard) > E["hard_runs_at_least"]
    assert ((hard["vdc_max_v"] - hard["vdc_min_v"]) > E["hard_vdc_drop_v"]).all()
