"""GoPro 표본 시각 검증 — 만들어 낸 궤적으로. 영상 파일이 없어도 돈다.

2026-10-01 에 고친 것: (1) 자이로를 앞 절반만 쓰던 것, (2) 같은 블록의 IMU 가 GPS 보다 0 · 1 · 2 초 늦게 들어 있는 것.
"""

import numpy as np
import pytest

from lefdata import gopro

R_EARTH = 6371000.0
LAT0, LON0 = 50.0, 8.0


def track(t, yaw_dps, speed_ms=15.0):
    """요레이트(°/s, 좌회전 양수)와 속도로 궤적을 만든다 → (위도, 경도)."""
    dt = np.gradient(t)
    heading = np.radians(90.0) + np.cumsum(np.radians(yaw_dps) * dt)       # 수학 좌표: 0 = 동쪽, 반시계가 양수
    x = np.cumsum(speed_ms * np.cos(heading) * dt)
    y = np.cumsum(speed_ms * np.sin(heading) * dt)
    return LAT0 + np.degrees(y / R_EARTH), LON0 + np.degrees(x / (R_EARTH * np.cos(np.radians(LAT0))))


def test_gyro_block_keeps_its_whole_second():
    """자이로 395 개를 가속도 197 개에 맞출 때 블록 끝까지 다 써야 한다 (앞 절반만 쓰면 끝 값이 0.5 가 된다)."""
    ramp = np.linspace(0.0, 1.0, 395)
    out = gopro._resample_gyro(np.column_stack([ramp, ramp, ramp]), 197)
    assert out.shape == (197, 3)
    assert out[0, 0] == pytest.approx(0.0, abs=0.01)
    assert out[-1, 0] == pytest.approx(1.0, abs=0.01)
    assert out[98, 0] == pytest.approx(0.5, abs=0.01)


def test_course_rate_on_a_left_hand_circle():
    """반지름 50 m 원을 15 m/s 로 왼쪽으로 돌면 요레이트는 v / r = 17.2 °/s."""
    t = np.arange(0, 60, 0.055)
    lat, lon = track(t, np.full(len(t), np.degrees(15.0 / 50.0)))
    rate = gopro.course_rate(t, lat, lon, np.full(len(t), 54.0))
    assert np.nanmedian(rate) == pytest.approx(np.degrees(15.0 / 50.0), rel=0.01)


def test_course_rate_is_blank_when_slow():
    t = np.arange(0, 20, 0.055)
    lat, lon = track(t, np.full(len(t), 10.0), speed_ms=2.0)
    assert np.isnan(gopro.course_rate(t, lat, lon, np.full(len(t), 7.2))).all()


@pytest.mark.parametrize("late", [0.0, 1.0, 2.0])
def test_imu_delay_is_measured_against_the_gps_track(late):
    """IMU 가 GPS 보다 `late` 초 늦게 찍혀 있으면 그 값을 되찾는다. 카메라는 기울어 달려 있다."""
    rng = np.random.default_rng(3)
    t = np.arange(0, 200, 0.005)
    yaw = sum(a * np.sin(2 * np.pi * f * t + p) for f, a, p in
              zip(rng.uniform(0.03, 0.25, 8), rng.uniform(4, 14, 8), rng.uniform(0, 6.28, 8)))
    tg = np.arange(1, 199, 0.055)
    lat, lon = track(t, yaw)
    up = np.array([0.94, 0.2, 0.28])
    up /= np.linalg.norm(up)
    acc = np.tile(up, (len(t), 1)) + rng.normal(0, 0.02, (len(t), 3))
    gyro = np.outer(yaw, up) + rng.normal(0, 0.5, (len(t), 3))
    cal = gopro.imu_late_by(tg, np.interp(tg, t, lat), np.interp(tg, t, lon), np.full(len(tg), 54.0),
                            t + late, acc, gyro)
    assert cal is not None and cal["corr"] > 0.95
    assert cal["late_s"] == pytest.approx(late, abs=0.08)


def test_imu_delay_cannot_be_measured_on_a_straight():
    """돌지 않으면 맞출 파형이 없다 — 억지로 값을 내지 않는다."""
    t = np.arange(0, 120, 0.005)
    tg = np.arange(1, 119, 0.055)
    lat, lon = track(t, np.zeros(len(t)))
    acc = np.tile([1.0, 0.0, 0.0], (len(t), 1))
    gyro = np.random.default_rng(4).normal(0, 0.5, (len(t), 3))
    assert gopro.imu_late_by(tg, np.interp(tg, t, lat), np.interp(tg, t, lon), np.full(len(tg), 54.0), t, acc, gyro) is None


def test_yaw_rate_does_not_depend_on_how_the_camera_is_mounted():
    up = np.array([0.6, 0.0, 0.8])
    gyro = np.outer([10.0, -20.0, 0.0], up)                    # 수직 축 둘레로만 도는 회전
    assert gopro.yaw_rate(gyro, up) == pytest.approx([10.0, -20.0, 0.0])
    acc = np.tile(up, (500, 1))
    assert gopro.vertical_axis(acc) == pytest.approx(up)
