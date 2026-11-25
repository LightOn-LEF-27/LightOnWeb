"""종목 분석 단위 테스트 — 만들어 낸 궤적으로. 데이터가 없어도 돈다."""

import numpy as np
import pandas as pd
import pytest

from lefdata import events

LAT0, LON0 = 50.0, 8.0


def circle_gps(turn: str, r_m=9.0, v_ms=10.0, laps=2.2, hz=10.0) -> pd.DataFrame:
    """반지름 r 인 원을 도는 GPS 표. turn = "left" 면 위에서 볼 때 반시계."""
    sign = 1.0 if turn == "left" else -1.0
    t = np.arange(0, laps * 2 * np.pi * r_m / v_ms, 1 / hz)
    heading = sign * v_ms / r_m * t                     # 수학 각도: 0 = 동쪽, 반시계가 양수
    x = sign * r_m * np.sin(heading)                    # 중심이 진행 방향의 왼쪽(좌) 또는 오른쪽(우)에 있는 원
    y = sign * r_m * (1 - np.cos(heading))
    return pd.DataFrame({
        "t_rel_ms": np.round(t * 1000).astype("int64"), "fix": True,
        "lat": LAT0 + y / 110_950.0, "lon": LON0 + x / (np.cos(np.radians(LAT0)) * 111_320.0),
        "speed_kmh": v_ms * 3.6,
        "course_deg": (90.0 - np.degrees(heading)) % 360.0,      # GPS 진행 방위: 북 = 0, 시계방향이 증가
    })


@pytest.mark.parametrize("turn", ["left", "right"])
def test_skidpad_turn_label_follows_the_real_direction(turn):
    """왼쪽으로 돈 원은 left 다. 진행 방위는 시계방향이 증가라서 부호를 그대로 쓰면 뒤집힌다 (2026-10-01 까지의 버그)."""
    gps = circle_gps(turn)
    laps, arcs = events.skidpad_laps(gps, LAT0, LON0, 0.0, gps["t_rel_ms"].iloc[-1] / 1000.0)
    assert len(laps) == 2
    assert set(laps["turn"]) == {turn} and set(arcs["turn"]) == {turn}
    assert laps["circle_r_m"].iloc[0] == pytest.approx(9.0, abs=0.1)
    assert laps["lap_time_s"].iloc[1] == pytest.approx(2 * np.pi * 9.0 / 10.0, abs=0.05)


def launch_gps(hole=None, a_ms2=6.0, wait_s=5.0, hz=10.0) -> pd.DataFrame:
    """서 있다가 wait_s 에 출발해 a_ms2 로 북쪽 직선 가속. hole = (시작, 끝) 초 — 그 사이 GPS 표본 없음."""
    t = np.arange(0, wait_s + 30.0, 1 / hz)                      # 주행 구간은 20 s 넘게 (laps.MIN_RUN_S)
    run = np.clip(t - wait_s, 0, None)
    v = np.minimum(a_ms2 * run, 30.0)
    y = np.cumsum(np.r_[0.0, (v[1:] + v[:-1]) / 2 * np.diff(t)])
    g = pd.DataFrame({"t_rel_ms": np.round(t * 1000).astype("int64"), "fix": True, "lat": LAT0 + y / 110_950.0, "lon": LON0,
                      "speed_kmh": v * 3.6, "course_deg": 0.0,
                      "t_abs": pd.Timestamp("2027-01-02 10:00", tz="Asia/Seoul") + pd.to_timedelta(t, unit="s")})
    if hole:
        g = g[(t < hole[0]) | (t > hole[1])].reset_index(drop=True)
    return g


def test_accel_75m_time_from_a_clean_launch():
    a = events.accel_runs(launch_gps(), LAT0, LON0)
    assert len(a) == 1 and a["t75_s"].iloc[0] == pytest.approx(np.sqrt(2 * 75 / 6.0), abs=0.15)
    assert a["gps_gap_s"].iloc[0] <= 0.11 and pd.isna(a["gps_gap_from_s"].iloc[0])


def test_accel_time_is_not_given_when_gps_is_missing_over_the_launch():
    """출발이 GPS 빈 곳 안이면 출발점이 끊기기 전 마지막 정지 표본이 된다 — 서 있던 시간이 들어간 75 m 시간을 내지 않는다."""
    a = events.accel_runs(launch_gps(hole=(4.0, 7.0)), LAT0, LON0)
    assert len(a) == 1                                                         # 런은 남긴다 (세션 · 종목은 그대로)
    assert pd.isna(a["t75_s"].iloc[0]) and pd.isna(a["v_at_75_kmh"].iloc[0])
    assert a["gps_gap_s"].iloc[0] == pytest.approx(3.1, abs=0.15) and a["gps_gap_from_s"].iloc[0] < 0.5
