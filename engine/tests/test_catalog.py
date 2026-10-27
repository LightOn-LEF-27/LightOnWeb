"""주행 데이터 카탈로그 검증.

앞쪽은 손으로 채운 자료로 도는 테스트다 — **차량이 바뀌어도 `Session` 만 채우면 같은 페이지가 나온다**는 약속을 지킨다.
뒤쪽은 실제 대회 이벤트의 골든 값이다 — 값은 기대값 파일 `catalog.toml` (`lefdata em-laps` 까지 끝나 있고 `catalog_notes.toml` 이 있어야 돈다).
"""

import ast
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import catalog as cat, catalog_build as cb, paths
from lefdata.catalog import HAVE, NONE, SEEK, Channel, CodeRef, DataBlock, Export, FileRef, LapColumn, Missing, Session

SRC = Path(cat.__file__).parent
G = 9.80665


# ---------------------------------------------------------------- 손으로 채운 세션

def hand_filled_session(tmp_path: Path) -> Session:
    """LEF-27 이라면 이렇게 채울 것이다: 반지름 40 m 원을 세 바퀴, 속도 채널 하나, 붙일 원본 하나와 못 붙일 원본 하나."""
    lap_s, n = 30.0, 3
    t = np.arange(0.0, lap_s * n + 10, 0.1)
    ang = 2 * np.pi * np.clip(t - 5, 0, lap_s * n) / lap_s
    track = pd.DataFrame({"t_s": t, "x_m": 40 * np.sin(ang), "y_m": 40 * (1 - np.cos(ang)), "dist_m": 40 * ang})
    laps = pd.DataFrame({"lap": [1, 2, 3], "t_start_s": [5.0, 35.0, 65.0], "t_end_s": [35.0, 65.0, 95.0], "lap_time_s": [30.4, 29.8, 30.1],
                         "vmax_kmh": [68.0, 70.0, 69.5]})
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"x" * 2048)
    block = DataBlock(
        "gps", "위치 · 속도 (GPS)", HAVE, "시험용 GPS · 10 Hz", ["표본 1,000개"],
        raw=[FileRef("로거 원본", "raw.log", raw, 2048), FileRef("온보드 영상", "video.mp4", size_bytes=4_000_000_000, where="팀 드라이브")],
        code=[CodeRef("laps", "GPS → 랩", "랩으로 나눔")],
        exports=[Export("gps.csv", "이 세션의 GPS", track.round(2), "1,000 행")],
        channels=[Channel("speed_kmh", "속도", "km/h", t, 50 + 20 * np.sin(3 * ang))],
        time_keys=["speed_kmh"], trace_keys=["speed_kmh"], map_key="speed_kmh",
        lap_columns=[LapColumn("lap_time_s", "랩타임", "s", 2, chart=True), LapColumn("vmax_kmh", "최고 속도", "km/h", 1, chart=True)],
        checks=["다른 센서와 대조함"], cautions=["값이 50 ~ 70 사이 | 표 구분자가 섞인 글"])
    return Session("LEF-27_2027-06-05_S1", "LEF-27", "2027-06-05", "셰이크다운 1", pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul"), 100.0,
                   event_kind="테스트", discipline="연습 주행", driver="드라이버 A", stats={"distance_km": 0.75, "laps_n": 3, "best_lap_s": 29.8},
                   laps=laps, track=track, blocks=[block],
                   missing=[Missing("배터리 전압 · 전류", SEEK, "로거 파일을 못 받음", "SD 카드를 가진 분"),
                            Missing("조향각", NONE, "센서 없음")],
                   made={"generated": "2027-06-05", "commit": "abc1234"})


def test_page_comes_out_of_a_hand_filled_session(tmp_path):
    """차량별 코드 없이 Session 만으로 문서 · 그림 · 파일이 나온다."""
    s = hand_filled_session(tmp_path)
    row = cat.write_session(s, tmp_path / "out", tmp_path / "code")
    doc = (tmp_path / "out" / "page.md").read_text(encoding="utf-8")
    assert sorted(p.stem for p in (tmp_path / "out" / "figures").glob("*.png")) == ["gps_laps", "gps_map", "gps_time", "gps_trace"]
    assert (tmp_path / "out" / "files" / "gps.csv").exists()
    for expected in ("# LEF-27 · 06-05 · 셰이크다운 1", "## 데이터 현황", "## 1. 위치 · 속도 (GPS)", "원본 → 코드 → 결과물", "[gps.csv](files/gps.csv)",
                     "[raw.log](../raw.log)", "`video.mp4`", "있는 곳: 팀 드라이브", "(../code/laps.md)", "| 2 ★ | 29.80 | 70.0 |", "## 찾는 데이터",
                     "SD 카드를 가진 분", "그림 4.", "abc1234"):
        assert expected in doc, expected
    assert "그림 5." not in doc                                  # 그림 번호는 있는 그림에만 차례로 붙는다
    assert doc.count("조향각") == 1                              # 기록 안 된 데이터는 현황 표에만 (찾는 데이터가 아니다)
    assert row["have"] == ["위치 · 속도 (GPS)"] and row["seek"] == ["배터리 전압 · 전류"] and row["none"] == ["조향각"]
    assert row["uploads"] == ["../raw.log", "figures/gps_laps.png", "figures/gps_map.png", "figures/gps_time.png", "figures/gps_trace.png",
                              "files/gps.csv"]                  # 큰 영상은 올리지 않는다
    assert json.loads((tmp_path / "out" / "page.json").read_text(encoding="utf-8"))["row"]["sid"] == s.sid


def test_a_session_with_nothing_still_gets_a_page(tmp_path):
    """데이터가 하나도 없는 세션도 페이지는 나온다 — '찾는 데이터' 만 적힌 채로 (없는 것을 지어내지 않는다)."""
    s = Session("X_2027-06-05_S1", "LEF-27", "2027-06-05", "가속", pd.Timestamp("2027-06-05 11:00", tz="Asia/Seoul"), 30.0,
                missing=[Missing("위치 · 속도 (GPS)", SEEK, "파일 없음")])
    row = cat.write_session(s, tmp_path)
    doc = (tmp_path / "page.md").read_text(encoding="utf-8")
    assert "찾는 데이터" in doc and "그림 1." not in doc and row["have"] == [] and row["uploads"] == []


def test_notion_copy_is_made_from_the_same_blocks(tmp_path):
    """노션용 마크다운: 뜻이 있는 글자를 막고, 그림 · 파일은 업로드 ID 로, 코드는 페이지 주소로 바꾼다."""
    s = hand_filled_session(tmp_path)
    cat.write_session(s, tmp_path / "out")
    blocks = json.loads((tmp_path / "out" / "page.json").read_text(encoding="utf-8"))["blocks"]
    text = cat.to_notion(blocks, {"figures/gps_time.png": "UP1", "files/gps.csv": "UP2"}, {"laps": "https://example.invalid/laps"})
    assert '<image src="file-upload://UP1">' in text and '<file src="file-upload://UP2">' in text
    assert '<mention-page url="https://example.invalid/laps">GPS → 랩</mention-page>' in text
    assert "(그림 없음: 그림 2." in text                          # 올리지 못한 그림은 빼지 않고 글로 남긴다
    assert r"50 \~ 70 사이 \| 표" in text and "<table header-row=\"true\">" in text
    assert "~" not in text.replace(r"\~", "")                    # 막지 않은 물결표가 없다 (노션에서 취소선이 된다)


@pytest.mark.parametrize("mod", ["catalog", "catalog_charts"])
def test_catalog_layer_does_not_know_the_car(mod):
    """문서와 그림은 차량 · 폴더 구조를 아는 코드를 import 하지 않는다 — LEF-27 에서 그대로 쓰려면 지켜야 한다."""
    names = set()
    for node in ast.walk(ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names.update(([node.module] if node.module else []) + [a.name for a in node.names])
    assert not names & {"catalog_build", "paths", "em_laps", "video", "energymeter", "lef26", "lef26_laps", "lef26_report", "monolith"}


# ---------------------------------------------------------------- 차량 거동

def test_gps_motion_on_a_left_hand_circle():
    """반지름 50 m 원을 15 m/s 로 왼쪽으로 돈다 → 좌우 가속도 +0.46 g, 요레이트 +17.2 °/s, 앞뒤 가속도 0."""
    t = np.arange(0.0, 40.0, 0.05)
    w = 15.0 / 50.0
    v, long_g, lat_g, yaw = cb.gps_motion(t, 50 * np.cos(w * t), 50 * np.sin(w * t), np.arange(5.0, 35.0, 0.05))
    assert np.nanmedian(v) == pytest.approx(15.0, abs=0.05)
    assert np.nanmedian(lat_g) == pytest.approx(15.0 ** 2 / 50.0 / G, abs=0.01)
    assert np.nanmedian(yaw) == pytest.approx(np.degrees(w), abs=0.2)
    assert abs(np.nanmedian(long_g)) < 0.01


def test_camera_axes_become_vehicle_axes():
    """앞으로 30° 숙인 카메라. 가속하면서 왼쪽으로 돌 때 카메라가 재는 값을 만들어 넣으면 차량 축 값이 그대로 나온다."""
    tilt = np.radians(30.0)
    up = np.array([np.cos(tilt), 0.0, np.sin(tilt)])            # 카메라 축으로 본 '위' (a0 위아래 · a1 좌우 · a2 앞뒤)
    fwd = cb.CAMERA_FORWARD * np.array([-np.sin(tilt), 0.0, np.cos(tilt)])
    left = np.cross(up, fwd)
    long_true, lat_true = np.array([0.5, -0.8, 0.0]), np.array([0.0, 0.3, 1.2])
    acc = up[None, :] + long_true[:, None] * fwd[None, :] + lat_true[:, None] * left[None, :]
    gyro = 20.0 * cb.CAMERA_YAW * np.tile(up, (3, 1))            # 위 축 둘레로 20 °/s
    long_g, lat_g, yaw = cb.camera_to_vehicle(acc, gyro, up * 1.03)     # 가속도계 크기가 3 % 틀려도 방향만 쓴다
    assert long_g == pytest.approx(long_true, abs=1e-9) and lat_g == pytest.approx(lat_true, abs=1e-9)
    assert yaw == pytest.approx(20.0)


def test_up_is_measured_where_the_car_stood_still():
    rng = np.random.default_rng(0)
    t = np.arange(0.0, 120.0, 0.005)
    acc = np.tile([0.88, -0.12, 0.45], (len(t), 1)) + rng.normal(0, 0.005, (len(t), 3))
    acc[t > 60] += rng.normal(0, 0.3, ((t > 60).sum(), 3))       # 60 초부터 달린다
    assert cb.still_up(t, acc, 0.0, 120.0) == pytest.approx([0.88, -0.12, 0.45], abs=0.002)
    assert cb.still_up(t, acc, 70.0, 120.0) is None              # 가만히 있던 곳이 없으면 재지 않는다


# ---------------------------------------------------------------- 골든 (실제 대회 이벤트 — catalog.toml)

E = Expected("catalog")
RACE = paths.LEF26_FSK
race = pytest.mark.skipif(not ((RACE / cb.NOTES_NAME).exists() and (RACE / "derived" / "lap_energy.csv").exists()),
                          reason="대회 이벤트에 catalog_notes.toml · lefdata em-laps 결과 없음")


@pytest.fixture(scope="module")
def sessions():
    return cb.build_event(RACE)


@race
def test_race_sessions_hide_driver_names(sessions):
    for s in sessions:
        assert s.driver.startswith("드라이버 ") and len(s.driver) == len("드라이버 A")      # 실명은 적지 않는다
        assert len(s.laps) == s.stats["laps_n"]


@race
@E.need
def test_race_sessions_and_kinds_of_data(sessions):
    assert [s.sid for s in sessions] == E["sids"]
    assert [s.name for s in sessions] == E["names"]
    for s in sessions:
        assert [b.key for b in s.blocks] == E["blocks"] and all(b.status == HAVE for b in s.blocks)
        assert [m.status for m in s.missing] == E["missing"]
        assert s.stats["laps_n"] == E["laps"]
    first, second = sessions
    assert f"{first.start:%H:%M}" == E["first.start_hm"] and first.duration_s == E.approx("first.duration_s")
    assert first.stats == E["first.stats"]
    assert second.stats["net_wh"] == E["second.net_wh"] and second.stats["best_lap_s"] == E["second.best_lap_s"]
    assert int(first.best_lap["lap"]) == E["first.best_lap"]


@race
@E.need
def test_race_camera_accelerations_agree_with_gps(sessions):
    """카메라 축을 차량 축으로 바꾼 값이 GPS 위치에서 낸 값과 맞는다 (부호 · 크기의 독립 확인)."""
    lo, hi = E.range("camera.slope")
    for s in sessions:
        imu = next(b for b in s.blocks if b.key == "imu")
        t, x, y = (s.track[c].to_numpy() for c in ("t_s", "x_m", "y_m"))
        grid = imu.channel("long_g").t_s
        v, g_long, g_lat, g_yaw = cb.gps_motion(t, x, y, grid)
        moving = v * 3.6 > cb.MOVING_KMH
        k = round(cb.SMOOTH_S / cb.STEP_S)
        for key, ref in (("long_g", g_long), ("lat_g", g_lat), ("yaw_dps", g_yaw)):
            corr, slope = cb._agreement(cb._roll(imu.channel(key).v, k), ref, moving)
            assert corr > E["camera.corr_min"][key] and lo < slope < hi, (s.name, key, corr, slope)
        assert s.laps["lat_left_max"].between(*E.range("camera.lat_left_max_g")).all()
        assert s.laps["long_g_min"].between(*E.range("camera.long_g_min_g")).all()
    assert len(sessions[0].blocks[1].gg) > E["camera.gg_points_min"]


@race
def test_race_battery_block_is_on_the_gps_clock(sessions):
    """배터리 표의 시간은 GPS 에 맞춘 세션 시간이다 — 세션 전체를 덮고, 랩 표의 에너지와 그 구간의 (시간) 적분이 같다."""
    s = sessions[0]
    b = next(b for b in s.blocks if b.key == "battery")
    kw = b.channel("power_kw")
    assert kw.t_s[0] == pytest.approx(0.0, abs=0.05) and kw.t_s[-1] == pytest.approx(s.duration_s, abs=0.05)
    lap = s.best_lap
    m = (kw.t_s >= lap["t_start_s"]) & (kw.t_s < lap["t_end_s"])
    wh = float(np.sum((kw.v[m][1:] + kw.v[m][:-1]) / 2 * np.diff(kw.t_s[m])) / 3.6)      # 빠진 표본이 있을 수 있어 시간으로 적분한다
    assert wh == pytest.approx(lap["net_wh"], abs=1.0)


@race
@E.need
def test_race_battery_golden(sessions):
    s = sessions[0]
    b = next(b for b in s.blocks if b.key == "battery")
    assert any("시간 열(t_s)로 적분" in c for c in b.cautions) is E["battery.has_dropped_sample_caution"]
    assert s.best_lap["net_wh"] == E.approx("battery.best_lap_net_wh")
    assert s.laps["i_max_a"].iloc[1:].between(*E.range("battery.i_max_a")).all()
    assert any(E["battery.check_contains"] in c for c in b.checks)


@race
def test_race_session_files_fit_on_a_page(sessions, tmp_path):
    """세션 구간 표는 페이지에 붙일 수 있는 크기다."""
    cat.write_session(copy.deepcopy(sessions[1]), tmp_path)             # write_session 은 세션의 결과물 목록을 고친다
    assert max(p.stat().st_size for p in (tmp_path / "files").iterdir()) < cat.ATTACH_MAX_BYTES


@race
@E.need
def test_race_pages_are_written(sessions, tmp_path):
    row = cat.write_session(copy.deepcopy(sessions[1]), tmp_path)
    doc = (tmp_path / "page.md").read_text(encoding="utf-8")
    n = E["page.figures"]
    assert len(list((tmp_path / "figures").glob("*.png"))) == n and doc.count("![그림") == n
    assert sorted(p.name for p in (tmp_path / "files").glob("*.csv")) == E["page.csvs"]
    assert row["seek"] == E["page.seek"]
    assert all(s in doc for s in E["page.contains"])


def test_inventory_lists_every_converted_recording():
    inv = cb.inventory(paths.DATA_ROOT)
    if inv.empty:
        pytest.skip("변환된 레코딩 없음")
    assert {"car", "event", "device", "recording_id", "start", "duration_s", "tables"} <= set(inv.columns)
    assert not inv["recording_id"].duplicated().any()
