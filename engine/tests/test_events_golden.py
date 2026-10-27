"""가속(75 m) · 스키드패드 검증 — 대회 리포트에 적힌 기록이 다시 나와야 한다 (값은 `events_golden.toml`).

먼저 `lefdata monolith` 와 `lefdata laps` 를 실행한 뒤 돌린다.
"""

import numpy as np
import pyarrow.parquet as pq
import pytest

from expected import Expected
from lefdata import events, paths

E = Expected("events_golden")
NORM = paths.LEF25EVO_EVENT / "normalized"
GATE = paths.LEF25EVO_EVENT / "derived" / "gate.json"
pytestmark = pytest.mark.skipif(not (NORM.exists() and GATE.exists()),
                                reason="normalized/ 또는 derived/gate.json 없음")

FSAE_SKIDPAD_R_M = 9.125     # FSAE 규정: 스키드패드 주행선 반지름


@pytest.fixture(scope="module")
def result():
    return events.run_event(paths.LEF25EVO_EVENT)


def _full_accel(result):
    a = result[0]
    return a[(a["direction"] == "forward") & (a["v_max_kmh"] >= E["full_kmh"])]


# ---------------------------------------------------------------- 가속

@E.need
def test_E1_accel_t75_matches_the_report(result):
    """정상출력 정방향 주행의 75 m 기록이 리포트 범위에 들어간다."""
    t = _full_accel(result)["t75_s"]
    assert len(t) == E["accel.full_runs"]
    lo, hi = E.range("accel.t75_min_s")
    assert lo <= t.min() <= hi
    assert t.max() <= E["accel.t75_max_s"]


@E.need
def test_E2_position_and_speed_integration_agree(result):
    """거리를 위치로 재든 속도로 재든 75 m 통과 시간이 0.25 s 안에서 같아야 한다."""
    a = _full_accel(result)
    assert len(a)
    assert (a["t75_s"] - a["t75_speed_int_s"]).abs().max() < 0.25


def test_E3_launch_starts_from_standstill(result):
    """정지 출발을 놓치면 기록이 빨라진다. 출발 속도가 STOP_KMH 아래여야 한다."""
    assert (result[0]["v_start_kmh"] <= events.STOP_KMH).all()


@E.need
def test_E4_reverse_run_detected(result):
    assert (result[0]["direction"] == "reverse").sum() == E["reverse_runs"]


# ---------------------------------------------------------------- 스키드패드

@E.need
def test_E5_skidpad_lap_count_matches_the_report(result):
    laps = result[1]
    assert len(laps) == E["skid_laps"]
    assert set(laps["recording_id"]) == {E["skid_recording"]}


@E.need
def test_E6_skidpad_best_lap(result):
    assert result[1]["lap_time_s"].min() == E.approx("skidpad.best_lap_s")


def test_E7_skidpad_radius_near_fsae_spec(result):
    """맞춘 원이 FSAE 규격 반지름에서 1 m 안에 들어오고 잔차가 작다."""
    laps, arcs = result[1], result[2]
    assert len(laps)
    assert abs(laps["circle_r_m"].mean() - FSAE_SKIDPAD_R_M) < 1.0
    assert arcs[arcs["laps"] > 0]["fit_rms_m"].max() < 1.5


@E.need
def test_E8_both_turn_directions_present(result):
    """8자 코스라 좌 · 우가 같은 수로 나와야 한다."""
    n = result[1]["turn"].value_counts()
    assert n["left"] == n["right"] == E["skid_laps_per_turn"]


@E.need
def test_E9_track_corners_are_not_counted_as_skidpad(result):
    """반지름만 맞는 오토크로스 코너가 랩으로 새지 않는지. 랩 = 0 인 호가 실제로 존재한다."""
    arcs = result[2]
    assert (arcs["laps"] == 0).any()
    assert set(arcs[arcs["laps"] > 0]["recording_id"]) == {E["skid_recording"]}


# ---------------------------------------------------------------- IMU 축 (축 이름이 물리적 뜻과 다른 로거)

def _imu_mean(rec_id, t0, t1, col):
    imu = pq.read_table(NORM / rec_id / "imu.parquet").to_pandas()
    t = imu["t_rel_ms"].to_numpy() / 1000.0
    return float(imu.loc[(t >= t0) & (t <= t1), col].mean())


def _per_turn(result, col):
    rec = E["skid_recording"]
    arcs = result[2]
    sk = arcs[(arcs["laps"] > 0) & (arcs["recording_id"] == rec)]
    return {t: [_imu_mean(rec, a["t_start_s"], a["t_start_s"] + a["duration_s"], col) for _, a in sk[sk["turn"] == t].iterrows()]
            for t in ("left", "right")}


@E.need
def test_E10_imu_ax_is_the_lateral_axis(result):
    """`ax_g` 는 이름과 달리 횡가속도다. 선회 방향에 따라 부호가 뒤집힌다 — 우회전에서 양수."""
    ax = _per_turn(result, "ax_g")
    assert np.mean(ax["right"]) > E["imu.ax_turn_g"]
    assert np.mean(ax["left"]) < -E["imu.ax_turn_g"]


@E.need
def test_E10b_imu_gz_is_positive_in_left_turns(result):
    """`gz_dps` 는 좌회전에서 양수다 (위에서 볼 때 반시계). X 오른쪽 · Y 앞 · Z 위 인 오른손 좌표계와 맞는다."""
    gz = _per_turn(result, "gz_dps")
    assert np.mean(gz["left"]) > E["imu.gz_turn_dps"]
    assert np.mean(gz["right"]) < -E["imu.gz_turn_dps"]


@E.need
def test_E11_imu_ay_is_the_longitudinal_axis(result):
    """`ay_g` 는 종가속도다. 직진 가속 중 GPS 로 구한 값과 같은 부호 · 자릿수."""
    a = _full_accel(result)
    ay = [_imu_mean(r["recording_id"], r["t_start_s"], r["t_start_s"] + r["t75_s"], "ay_g") for _, r in a.iterrows()]
    lo, hi = E.range("imu.ay_accel_g")
    assert lo < np.mean(ay) < hi


@E.need
def test_E12_imu_magnitude_is_not_trustworthy_yet(result):
    """크기가 GPS 와 어긋난다 — 보정 전에는 IMU 절댓값을 쓰면 안 된다는 근거."""
    ax = _per_turn(result, "ax_g")
    mean_abs = np.mean([abs(v) for v in ax["left"] + ax["right"]])
    assert mean_abs / result[1]["lat_acc_g"].mean() > E["imu.ax_over_gps_min"]
