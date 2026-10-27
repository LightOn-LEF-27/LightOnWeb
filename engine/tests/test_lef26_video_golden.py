"""온보드 영상(GoPro) 변환 · 랩 분할 · 종목 검증 (값과 그 이유는 `lef26_video_golden.toml`).

먼저 `lefdata gopro --event <이벤트> --laps` 로 변환한 뒤 실행한다. 원본 영상은 읽지 않는다.
"""

import json

import pandas as pd
import pytest

from expected import Expected
from lefdata import paths, video

E = Expected("lef26_video_golden")


def _index(event):
    return pd.read_csv(event / "normalized" / video.INDEX_NAME, encoding="utf-8-sig", dtype={"imu_shift_s": str}).set_index("recording_id")


def _derived(event, name):
    return pd.read_csv(event / "derived" / name, encoding="utf-8-sig")


week = pytest.mark.skipif(not (paths.LEF26_EVENT / "normalized" / video.INDEX_NAME).exists(), reason="테스트위크 영상 normalized/ 없음")
race = pytest.mark.skipif(not (paths.LEF26_FSK / "normalized" / video.INDEX_NAME).exists(), reason="대회 영상 normalized/ 없음")


@week
@E.need
def test_week_recordings_are_named_by_gps_date():
    """카메라 시계가 틀려 있어도 이름의 날짜는 GPS 시각에서 온다."""
    idx = _index(paths.LEF26_EVENT)
    assert len(idx) == E["week.recordings"]
    assert idx.index.str[:10].value_counts().to_dict() == E["week.by_date"]
    assert idx["gps_hz"].between(*E.range("week.gps_hz")).all()


@week
@E.need
def test_week_imu_is_pulled_onto_gps_time():
    """같은 블록의 IMU 가 GPS 보다 늦게 들어 있는 만큼을 파일마다 재서 당긴다."""
    idx = _index(paths.LEF26_EVENT)
    for rec, shift in E["week.imu_shift"].items():
        assert idx.loc[rec, "imu_shift_s"] == shift, rec
    assert not idx["imu_shift_s"].str.contains(r"\?").any()             # 전부 잴 수 있었다
    meta = json.loads((paths.LEF26_EVENT / "normalized" / E["week.imu_late_recording"] / "meta.json").read_text(encoding="utf-8"))
    first = meta["imu_time"]["files"][0]
    assert first["calibrated"] and first["imu_late_measured_s"] == E.approx("week.imu_late_s")
    assert first["corr"] > E["week.imu_late_corr_min"]


@week
@E.need
def test_week_laps():
    laps, runs = _derived(paths.LEF26_EVENT, "laps.csv"), _derived(paths.LEF26_EVENT, "runs.csv")
    assert len(laps) == E["week_laps.laps"] and len(runs) == E["week_laps.runs"]
    assert laps["lap_time_s"].min() == E.approx("week_laps.best_s")
    by = laps.groupby("recording_id")["lap_time_s"].agg(["size", "min"])
    for r in E["week_laps.by_recording"]:
        assert by.loc[r["recording"], "size"] == r["laps"]
        assert by.loc[r["recording"], "min"] == pytest.approx(r["best_s"], abs=E["week_laps.best_abs_s"])
    assert laps["distance_m"].between(*E.range("week_laps.distance_m")).all()


@week
def test_logger_gps_tables_are_not_mistaken_for_tracks():
    """같은 이벤트 폴더에 로거 레코딩이 같이 있다. 로거 GPS 표는 속도 · fix 가 없어 랩 분할 대상이 아니다 (영상 레코딩만 남는다)."""
    runs = _derived(paths.LEF26_EVENT, "runs.csv")
    assert runs["recording_id"].str.match(r"^\d{4}-\d{2}-\d{2}_\d{4}$").all()
    assert (paths.LEF26_EVENT / "derived" / "motor_runs.csv").exists()   # 모터 속도 기반 구간은 다른 파일이다


@race
@E.need
def test_race_laps():
    laps, runs = _derived(paths.LEF26_FSK, "laps.csv"), _derived(paths.LEF26_FSK, "runs.csv")
    assert len(laps) == E["race.laps"] and len(runs) == E["race.runs"]
    assert laps["lap_time_s"].min() == E.approx("race.best_s")
    assert laps["lap_time_s"].median() == E.approx("race.median_s")
    assert laps["distance_m"].median() == E.approx("race.median_m")
    assert runs["distance_m"].sum() == E.approx("race.total_m")


@race
@E.need
def test_race_stationary_chunks_stay_flagged():
    """차가 서 있던 조각은 IMU 시각을 잴 수 없다 → 맞췄다고 하지 않는다 ('?')."""
    idx = _index(paths.LEF26_FSK)
    for rec, shift in E["race.imu_shift"].items():
        assert idx.loc[rec, "imu_shift_s"] == shift


# ---------------------------------------------------------------- 종목 (lefdata events --event <테스트위크>)

events = pytest.mark.skipif(not (paths.LEF26_EVENT / "derived" / "accel_runs.csv").exists(), reason="derived/accel_runs.csv 없음")


@events
def test_week_acceleration_runs_are_straight():
    a = _derived(paths.LEF26_EVENT, "accel_runs.csv")
    fw = a[a["direction"] == "forward"]
    assert len(fw) and (fw["straightness"] > 0.99).all()


@events
@E.need
def test_week_acceleration_runs():
    a = _derived(paths.LEF26_EVENT, "accel_runs.csv")
    fw = a[a["direction"] == "forward"]
    assert len(a) == E["accel.runs"] and len(fw) == E["accel.forward"]
    assert fw.groupby("recording_id").size().to_dict() == E["accel.by_recording"]
    assert fw["t75_s"].min() == E.approx("accel.best_s")
    assert fw["t75_s"].median() == E.approx("accel.median_s")
    assert fw["v_max_kmh"].between(*E.range("accel.v_max_kmh")).all()


@events
def test_week_skidpad_arcs_fit_circles():
    """원에 안 맞는 호(빠져나가는 구간 등)는 한 바퀴로 세지 않는다."""
    arcs = _derived(paths.LEF26_EVENT, "skidpad_arcs.csv")
    assert arcs.loc[arcs["laps"] > 0, "fit_rms_m"].max() < 1.5


@events
@E.need
def test_week_skidpad_laps():
    """8자 두 원의 반지름이 규격과 맞아야 스키드패드다."""
    s = _derived(paths.LEF26_EVENT, "skidpad_laps.csv")
    assert len(s) == E["skidpad.laps"]
    assert s["recording_id"].nunique() == E["skidpad.recordings"]
    assert s["circle_r_m"].between(*E.range("skidpad.radius_m")).all()
    assert s["lap_time_s"].min() == E.approx("skidpad.best_s")
    assert s["lat_acc_g"].max() == E.approx("skidpad.lat_max_g")
