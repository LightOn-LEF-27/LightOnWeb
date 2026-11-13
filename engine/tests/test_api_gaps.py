"""기록이 길게 빈 채널 · 코스가 없어진 세션을 API 로 볼 때 (`lefdata.api`) — 손으로 채운 세션으로 (데이터 없이).

계약: 기록이 끊긴 곳은 잇지 않는다 (§6-4 — 끊긴 곳 = 그 채널 기록 간격의 2 배 넘게 벌어진 곳). 코스가 없는 세션에는 지도를 주지 않는다 (§6-6).
"""

import numpy as np
import pytest

from lefdata import api, session_store as store
from lefdata.api import EngineError
from lefdata.catalog import HAVE, Channel, DataBlock
from test_api_contract import circle_session, strict

SID = "LEF-27_2027-06-05_S1"


def holed_session():
    """원 세션에 10 Hz 채널 하나 — 30 ~ 70 s 가 통째로 비어 있다 (로거가 꺼졌던 구간). 값은 세션 시각 그대로. 랩 2 (35 ~ 65 s) 가 그 안이다."""
    s = circle_session(SID)
    t = np.concatenate([np.arange(0.0, 30.0, 0.1), np.arange(70.0, 100.0, 0.1)])
    s.blocks.append(DataBlock("motor", "모터", HAVE, "시험용 로거 · 10 Hz", channels=[Channel("rpm", "회전수", "rpm", t, t.copy(), digits=1)],
                              time_keys=["rpm"]))
    return s


@pytest.fixture()
def holed(tmp_path):
    root = tmp_path / "store"
    store.write_session(holed_session(), root, tmp_path)
    store.write_index(root)
    store.write_frames(root)
    api.configure(store_root=root, data_root=tmp_path)
    yield root
    api.configure()


@pytest.mark.parametrize("t0, t1", [(25.0, 75.0), (31.5, 68.5), (45.0, 46.0)])
def test_zooming_inside_a_long_hole_stays_empty(holed, t0, t1):
    """빈 틈보다 좁게 확대해도 틈은 틈이다 — 틈 양옆 표본만 남은 조각의 간격으로 가리면 직선으로 이어 버린다."""
    r = strict(api.get_series(SID, ["motor.rpm"], t0=t0, t1=t1))
    x, v = np.array(r["x"]["values"]), r["series"][0]["values"]
    inside = [val for at, val in zip(x, v) if 30.5 < at < 69.5]
    assert inside and all(val is None for val in inside)
    assert all(val is not None for at, val in zip(x, v) if at < 29.5 or at > 70.5)


def test_a_lap_inside_the_hole_has_no_values_on_the_map_or_the_distance_axis(holed):
    color = strict(api.get_track(SID, lap=2, color="motor.rpm"))["color"]["values"]
    assert len(color) > 100 and all(val is None for val in color)
    for axis in ("distance", "ref_distance"):
        r = strict(api.get_series(SID, ["motor.rpm"], x=axis, lap=2))
        assert r["series"][0]["samples"] == 0 and all(val is None for val in r["series"][0]["values"])
    with_values = strict(api.get_track(SID, lap=3, color="motor.rpm"))["color"]["values"]        # 랩 3 (65 ~ 95 s) 은 70 s 부터 기록이 있다
    assert with_values[0] is None and with_values[-1] == pytest.approx(95.0, abs=0.2)


def test_smoothing_near_the_end_of_a_channel_is_not_an_error(holed):
    """창보다 표본이 적은 조각 (채널이 끝난 바로 뒤를 확대) — 예외가 아니라 값 없음."""
    r = strict(api.get_series(SID, ["motor.rpm"], t0=31.5, t1=40.0, smooth_s=5.0))
    assert all(val is None for val in r["series"][0]["values"])
    strict(api.get_track(SID, lap=2, color="motor.rpm", smooth_s=5.0))


def test_a_session_that_lost_its_track_gives_no_old_trajectory(holed, tmp_path):
    """같은 세션을 코스 없이 다시 만들면 (GPS 가 빠진 뒤) 지난번 궤적이 남아 있지 않다."""
    assert len(api.get_track(SID, "all")["x_m"]) > 100
    again = holed_session()
    again.track, again.laps, again.stats = None, None, {"distance_km": None, "laps_n": None, "best_lap_s": None}
    store.write_session(again, holed, tmp_path)
    store.write_index(holed)
    assert not (holed / SID / "track.parquet").exists() and api.get_session(SID)["summary"]["has_track"] is False
    for lap in (None, "all", 2):
        with pytest.raises(EngineError) as e:
            api.get_track(SID, lap)
        assert e.value.code == "bad_argument"


def test_a_levels_channel_reduced_to_bins_gives_the_share_not_a_rounded_state(tmp_path):
    """정해진 값만 갖는 채널을 칸으로 줄이면 평균은 비율이다 (계약 §6-3 — 가속 비율 − 제동 비율). 정수로 자르면 3 : 1 로 섞인 칸이 '가속' 으로 보인다."""
    s = circle_session(SID)
    t = np.arange(0.0, 100.0, 0.1)
    state = np.where((np.arange(len(t)) % 4) == 3, -1.0, 1.0)                # 네 표본에 셋은 가속, 하나는 제동
    levels = [{"value": 1, "label": "가속"}, {"value": 0, "label": "타력"}, {"value": -1, "label": "제동"}]
    s.blocks.append(DataBlock("drive", "주행 상태", HAVE, "시험용 · 10 Hz", channels=[Channel("state_est", "주행 상태", "", t, state, digits=0, levels=levels)],
                              time_keys=["state_est"]))
    root = tmp_path / "store"
    store.write_session(s, root, tmp_path)
    store.write_index(root)
    api.configure(store_root=root, data_root=tmp_path)
    try:
        r = strict(api.get_series(SID, ["drive.state_est"], t0=10.0, t1=90.0, max_points=20))["series"][0]     # 한 칸 = 40 표본
        assert all(v == pytest.approx(0.5, abs=0.06) for v in r["values"])
        assert set(r["y_min"]) == {-1.0} and set(r["y_max"]) == {1.0}
        full = strict(api.get_series(SID, ["drive.state_est"], t0=10.0, t1=12.0))["series"][0]                # 줄이지 않으면 원래 값 그대로
        assert set(full["values"]) <= {-1.0, 1.0}
    finally:
        api.configure()
