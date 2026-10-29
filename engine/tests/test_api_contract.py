"""분석 엔진 계약 — 사이트가 기대는 함수의 모양과 값 (`lefdata.api`, 계약 문서 `분석엔진_인터페이스.md`).

앞쪽은 손으로 채운 세션으로 도는 테스트다 (데이터가 없어도 돈다) — 차량이 바뀌어도 `Session` 만 채우면 같은 모양이 나온다.
뒤쪽은 실제 대회 이벤트에 돌린 것이다 — 어떤 세션에도 맞는 약속(모양 · 불변식)과, 값은 기대값 파일 `api_contract.toml` 에 둔 골든
(`catalog_notes.toml` 과 `lefdata em-laps` 결과가 있어야 돈다). 손으로 채운 세션의 값은 공개 예시 파일 `tests/expected_example/example.toml` 로도 본다.

여기 있는 값이 바뀌면 사이트가 깨질 수 있다. 바꿔야 하면 계약 문서를 먼저 고치고 버전을 올린다.
"""

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from expected import EXAMPLE_ROOT, Expected
from lefdata import api, catalog_build as cb, paths, session_store as store
from lefdata.api import EngineError
from lefdata.catalog import HAVE, NONE, SEEK, Channel, DataBlock, Export, LapColumn, Missing, Session

REPO = Path(__file__).resolve().parents[1]
CONTRACT_DOC = REPO.parents[1] / "03_설계" / "분석엔진_인터페이스.md"


def strict(obj):
    """사이트로 보낼 수 있는 JSON 인가 (NaN · numpy 값이 섞이면 실패)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, allow_nan=False))


def error_code(fn) -> str:
    with pytest.raises(EngineError) as e:
        fn()
    strict(e.value.to_json())
    return e.value.code


def test_contract_version_matches_the_document():
    """코드의 계약 버전과 계약 문서 머리의 버전이 같다 — 한쪽만 바뀌면 사이트 세션이 엇갈린다."""
    if not CONTRACT_DOC.exists():
        pytest.skip("계약 문서 없음 (저장소만 따로 받은 경우)")
    head = CONTRACT_DOC.read_text(encoding="utf-8")[:400]
    assert re.search(r"계약 버전 `([0-9.]+)`", head).group(1) == api.CONTRACT_VERSION


# ---------------------------------------------------------------- 손으로 채운 세션 (데이터 없이)

FRAME = "LEF-27/2027-06-05_test"


def circle_session(sid: str = "LEF-27_2027-06-05_S1", lap_s: float = 30.0, frame_id: str = "", power_kw: float | None = None) -> Session:
    """반지름 40 m 원을 반시계 방향으로 세 바퀴 — 10 Hz 속도 하나, 50 Hz 전류 하나(0.5 s 빈 곳 하나).

    lap_s 를 바꾸면 그 속도로 돈다 (랩 표도 그 값). power_kw 를 주면 배터리 전력 채널도 넣는다 (엔진이 주행 상태를 추정할 재료).
    """
    n = 3
    t = np.arange(0.0, lap_s * n + 10, 0.1)
    ang = 2 * np.pi * np.clip(t - 5, 0, lap_s * n) / lap_s
    track = pd.DataFrame({"t_s": t, "x_m": 40 * np.sin(ang), "y_m": 40 * (1 - np.cos(ang)), "dist_m": 40 * ang})
    if lap_s == 30.0:
        laps = pd.DataFrame({"lap": [1, 2, 3], "t_start_s": [5.0, 35.0, 65.0], "t_end_s": [35.0, 65.0, 95.0], "lap_time_s": [30.4, 29.8, 30.1],
                             "vmax_kmh": [68.0, 70.0, 69.5], "i_max_a": [150.0, 200.0, 180.0]})
    else:
        starts = 5.0 + lap_s * np.arange(n)
        laps = pd.DataFrame({"lap": [1, 2, 3], "t_start_s": starts, "t_end_s": starts + lap_s, "lap_time_s": [lap_s] * n,
                             "vmax_kmh": [68.0, 70.0, 69.5], "i_max_a": [150.0, 200.0, 180.0]})
    speed = 50 + 20 * np.sin(3 * ang)
    ti = np.concatenate([np.arange(0.0, 50.0, 0.02), np.arange(50.5, 100.0, 0.02)])
    amp = 100 + 50 * np.sin(ti)
    amp[np.searchsorted(ti, 40.0)] = 200.0
    gps = DataBlock("gps", "위치 · 속도 (GPS)", HAVE, "시험용 GPS · 10 Hz", ["표본 1,000개"],
                    exports=[Export("gps.csv", "이 세션의 GPS", track.round(2), "1,000 행")],
                    channels=[Channel("speed_kmh", "속도", "km/h", t, speed, digits=2)], time_keys=["speed_kmh"], trace_keys=["speed_kmh"],
                    map_key="speed_kmh", lap_columns=[LapColumn("lap_time_s", "랩타임", "s", 2, chart=True), LapColumn("vmax_kmh", "최고 속도", "km/h", 1)])
    bat_channels = [Channel("current_a", "배터리 전류", "A", ti, amp, sign="방전 +", digits=2)]
    if power_kw is not None:
        bat_channels.append(Channel("power_kw", "배터리 전력", "kW", ti, np.full(len(ti), float(power_kw)), sign="방전 +", digits=2))
    bat = DataBlock("battery", "배터리 전압 · 전류", HAVE, "시험용 미터 · 50 Hz",
                    channels=bat_channels, time_keys=["current_a"],
                    lap_columns=[LapColumn("i_max_a", "전류 최대", "A", 0)], cautions=["50.0 ~ 50.5 s 가 비어 있음"],
                    findings=[{"level": "warn", "code": "em_dropped_samples", "message": "빈 틈 1곳", "t_ranges": [[49.98, 50.5]]}])
    return Session(sid, "LEF-27", "2027-06-05", "셰이크다운 1", pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul"), lap_s * n + 10,
                   event_kind="테스트", discipline="연습 주행", driver="드라이버 A", event_id="LEF-27/2027-06-05_test", frame_id=frame_id,
                   stats={"distance_km": 0.75, "laps_n": 3, "best_lap_s": 29.8}, laps=laps, track=track,
                   gate={"x_m": 0.0, "y_m": 0.0, "heading_deg": 90.0, "half_width_m": 10.0}, blocks=[gps, bat],
                   missing=[Missing("모터 · 인버터", SEEK, "로거 파일을 못 받음", "SD 카드를 가진 분", "motor"), Missing("조향각", NONE, "센서 없음")])


@pytest.fixture()
def circle(tmp_path):
    root = tmp_path / "store"
    store.write_session(circle_session(), root, tmp_path)
    store.write_index(root)
    store.write_frames(root)
    api.configure(store_root=root, data_root=tmp_path)
    yield "LEF-27_2027-06-05_S1"
    api.configure()


@pytest.fixture()
def pair(tmp_path):
    """같은 frame 의 두 세션(30 s · 31 s 랩)과 frame 이 없는 세션 하나."""
    root = tmp_path / "store"
    sids = ["LEF-27_2027-06-05_S1", "LEF-27_2027-06-05_S2", "LEF-27_2027-06-05_S3"]
    for sid, lap_s, frame in zip(sids, (30.0, 31.0, 30.0), (FRAME, FRAME, "")):
        store.write_session(circle_session(sid, lap_s, frame), root, tmp_path)
    store.write_index(root)
    store.write_frames(root)
    api.configure(store_root=root, data_root=tmp_path)
    yield sids
    api.configure()


def test_a_hand_filled_session_answers_every_function(circle):
    """차량별 코드 없이 Session 만으로 F1 ~ F8 · F12 가 나온다. 전부 JSON 으로 바꿀 수 있다."""
    sid = circle
    info = strict(api.engine_info())
    assert info["contract"] == api.CONTRACT_VERSION and info["store"]["sessions"] == 1
    rows = strict(api.list_sessions())
    assert [r["sid"] for r in rows] == [sid] and api.list_sessions(car="LEF-26") == []
    status = [(d["block"], d["status"]) for d in rows[0]["data_status"]]
    assert status == [("gps", "have"), ("battery", "have"), ("motor", "seek"), (None, "none")]
    assert rows[0]["blocks"] == {"have": ["gps", "battery"], "warn": [], "seek": ["motor"], "none": []}     # 블록 이름을 모르는 항목은 data_status 에만
    s = strict(api.get_session(sid))
    assert [b["block"] for b in s["blocks"]] == ["gps", "battery"] and s["best_lap"] == 2
    assert s["blocks"][0]["default_views"] == {"time": ["gps.speed_kmh"], "distance": ["gps.speed_kmh"], "map": "gps.speed_kmh", "gg": False}
    ch = {c["channel_id"]: c for c in strict(api.list_channels(sid))}
    assert set(ch) == {"gps.speed_kmh", "battery.current_a"} and ch["gps.speed_kmh"]["rate_hz"] == 10.0
    assert not any(k.startswith("_") for c in ch.values() for k in c)              # 저장소 안쪽 열은 보이지 않는다
    for call in (lambda: api.get_series(sid, ["gps.speed_kmh", "battery.current_a"]), lambda: api.get_laps(sid),
                 lambda: api.get_track(sid, color="battery.current_a"), lambda: api.get_quality(sid), lambda: api.get_sectors(sid),
                 lambda: api.get_delta(sid, 1, sid, 2), lambda: api.get_series(sid, ["gps.speed_kmh"], x="ref_distance", lap=1)):
        strict(call())
    f = strict(api.open_file(s["blocks"][0]["exports"][0]["file_id"]))
    assert Path(f["path"]).read_text(encoding="utf-8-sig").startswith("t_s,x_m,y_m,dist_m") and f["content_type"] == "text/csv"


def test_hand_filled_session_matches_the_example_expected_file(circle):
    """기대값 파일을 쓰는 법의 예 — 공개 저장소에 있는 유일한 기대값 파일(합성 세션의 값)."""
    EX = Expected("example", EXAMPLE_ROOT)
    row = api.list_sessions()[0]
    assert row["laps"] == EX["circle.laps"] and api.get_session(circle)["best_lap"] == EX["circle.best_lap"]
    d = api.get_series(circle, ["gps.speed_kmh"], x="distance", lap=EX["circle.best_lap"])
    assert d["laps"][0]["x_end"] == EX.approx("circle.lap_m")
    assert api.get_track(circle)["direction"] == EX["circle.direction"]
    lo, hi = EX.range("circle.speed_kmh")
    assert all(lo <= v <= hi for v in d["series"][0]["values"] if v is not None)
    times = {r["lap"]: r["lap_time_s"] for r in api.get_laps(circle)["rows"]}
    for c in EX.cases("circle.cases"):
        assert times[c["lap"]] == pytest.approx(c["time_s"])


def test_series_share_one_x_and_keep_the_peak(circle):
    r = api.get_series(circle, ["gps.speed_kmh", "battery.current_a"], max_points=500)
    assert r["x"]["kind"] == "time" and len(r["x"]["values"]) == 500
    assert all(len(s["values"]) == len(s["y_min"]) == len(s["y_max"]) == 500 for s in r["series"])
    cur = r["series"][1]
    assert max(v for v in cur["y_max"] if v is not None) == 200.0          # 표본 하나짜리 피크가 남는다
    assert [m["lap"] for m in r["laps"]] == [1, 2, 3]


def test_zooming_into_a_hole_gives_nulls(circle):
    """기록이 빈 곳(50.0 ~ 50.5 s)은 확대하면 비어 있다 — 사이트는 거기서 선을 끊는다."""
    r = api.get_series(circle, ["battery.current_a"], t0=49.0, t1=51.5, max_points=1000)
    x, v = np.array(r["x"]["values"]), r["series"][0]["values"]
    inside = [v[i] for i in np.flatnonzero((x > 50.0) & (x < 50.48))]
    assert inside and all(val is None for val in inside)
    assert all(val is not None for val in (v[i] for i in np.flatnonzero((x < 49.97) | (x > 50.52))))


def test_distance_axis_is_per_lap(circle):
    r = api.get_series(circle, ["gps.speed_kmh"], x="distance", lap=2)
    x = r["x"]["values"]
    assert r["x"]["unit"] == "m" and r["x"]["lap"] == 2
    assert x[0] == pytest.approx(0.0, abs=0.5) and x[-1] == pytest.approx(2 * np.pi * 40, abs=0.5)
    assert r["laps"] == [{"lap": 2, "x_start": 0.0, "x_end": pytest.approx(2 * np.pi * 40, abs=0.01)}]
    part = api.get_series(circle, ["gps.speed_kmh"], x="distance", lap=2, d0=50.0, d1=100.0)
    assert part["x"]["values"][0] > 50.0 and part["x"]["values"][-1] < 100.0


def test_track_direction_and_lap(circle):
    t = api.get_track(circle)
    assert t["lap"] == 2 and t["direction"] == "ccw" and t["dist_m"][0] == 0.0
    assert t["t_s"][0] == 35.0 and t["t_s"][-1] == 65.0                         # 계측선을 지난 순간부터 다음 순간까지
    assert api.get_track(circle, "all")["lap"] is None and "color" not in t and t["frame_id"] is None      # 이름을 안 붙인 세션


def test_track_color_is_the_channel_at_each_point(circle):
    """지도 색은 엔진이 주행선 점마다 맞춘 값이다. 기록이 빈 곳(50.0 ~ 50.5 s)은 None."""
    t = api.get_track(circle, lap=2, color="battery.current_a")
    c = t["color"]
    assert c["channel_id"] == "battery.current_a" and c["unit"] == "A" and len(c["values"]) == len(t["x_m"])
    ts = np.array(t["t_s"])
    for k in np.flatnonzero((ts > 50.05) & (ts < 50.45)):
        assert c["values"][k] is None
    k = int(np.argmin(np.abs(ts - 40.0)))
    assert c["values"][k] == pytest.approx(200.0 if ts[k] == 40.0 else 100 + 50 * np.sin(ts[k]), abs=6)
    g = api.get_track(circle, lap=2, color="gps.speed_kmh")["color"]["values"]
    assert None not in g


def test_arguments_are_checked(circle):
    sid = circle
    assert error_code(lambda: api.get_session("nope")) == "unknown_session"
    assert error_code(lambda: api.get_session("../escape")) == "unknown_session"
    assert error_code(lambda: api.get_series(sid, ["gps.nope"])) == "unknown_channel"
    assert error_code(lambda: api.get_series(sid, [])) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], x="distance")) == "bad_argument"     # 거리축은 랩이 있어야
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], x="space")) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], lap=9)) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], max_points=1)) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], smooth_s=60)) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], t0=500)) == "bad_argument"           # 세션 밖
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], x="distance", lap=2, t0=1)) == "bad_argument"   # 거리축은 d0 · d1
    assert error_code(lambda: api.get_track(sid, color="gps.nope")) == "unknown_channel"
    assert error_code(lambda: api.list_sessions(limit=0)) == "bad_argument"
    assert error_code(lambda: api.open_file(f"{sid}/files/../../secret.txt")) == "unknown_file"         # 표에 있는 ID 만
    assert error_code(lambda: api.detect("없는 파일.log")) == "bad_argument"                             # 올리기 (v0.10) — 자세히는 test_ingest
    assert error_code(lambda: api.get_job("j")) == "unknown_job"
    assert error_code(lambda: api.get_sectors(sid, mini_m=20)) == "bad_argument"                       # 10 · 25 · 50 만
    assert error_code(lambda: api.get_sectors(sid, mini_m=True)) == "bad_argument"
    assert error_code(lambda: api.get_series(sid, ["gps.speed_kmh"], x="ref_distance")) == "bad_argument"   # 랩이 있어야
    assert error_code(lambda: api.get_delta(sid, 9, sid, 1)) == "bad_argument"
    assert error_code(lambda: api.get_delta(sid, 1, sid, 2, max_points=1)) == "bad_argument"


# ---- v0.5 구간 · 시간 차 · 주행 상태 (손으로 채운 세션)

LAP_M = 2 * np.pi * 40


def test_sectors_of_a_steady_lap(circle):
    """일정한 속도의 원 — 구간 셋이 같은 시간, 미니 구간은 25 m 마다 (마지막은 남은 길이). 합 = 랩 시간."""
    r = strict(api.get_sectors(circle))
    assert r["ref"]["ref_len_m"] == pytest.approx(LAP_M, abs=0.05) and (r["ref"]["method"], r["ref"]["source"]) == ("projection", "auto")
    assert r["frame_id"] is None and r["ref"]["reference_lap"]["sid"] == circle                        # frame_id 가 없으면 그 세션만의 frame
    assert [c["sector"] for c in r["sectors"]] == [1, 2, 3] and r["sectors"][-1]["ref_m1"] == pytest.approx(LAP_M, abs=0.05)
    assert r["sectors"][0]["heading_deg"] == pytest.approx(90.0, abs=1.0)                              # 동쪽으로 출발
    assert r["mini"]["count"] == int(np.ceil(LAP_M / 25)) and r["mini"]["edges_ref_m"][-1] == pytest.approx(LAP_M, abs=0.05)
    for lap in r["laps"]:
        assert sum(lap["sector_s"]) == pytest.approx(30.0, abs=0.002) and sum(lap["mini_s"]) == pytest.approx(30.0, abs=0.01)
        assert lap["sector_s"] == pytest.approx([10.0] * 3, abs=0.01)
    assert r["mean"]["n_laps"] == 3 and len(r["best"]["mini_s"]) == len(r["best"]["mini_lap"]) == r["mini"]["count"]
    assert api.get_sectors(circle, mini_m=10)["mini"]["count"] == int(np.ceil(LAP_M / 10))


def test_lap_table_has_sector_columns(circle):
    laps = strict(api.get_laps(circle))
    assert [c["key"] for c in laps["columns"]][:4] == ["lap_time_s", "s1_s", "s2_s", "s3_s"]        # 랩타임 바로 뒤
    for row in laps["rows"]:
        assert row["s1_s"] + row["s2_s"] + row["s3_s"] == pytest.approx(row["t_end_s"] - row["t_start_s"], abs=0.002)
    sec = laps["sectors"]
    assert sec["n_laps"] == 3 and set(sec["best"]) == {"s1_s", "s2_s", "s3_s"}
    assert sec["theoretical_best_s"] == pytest.approx(sum(v["value"] for v in sec["best"].values()), abs=0.002)
    held = [(row["lap"], k) for row in laps["rows"] for k in row["best_sectors"]]
    assert sorted(k for _, k in held) == [1, 2, 3] and all(sec["best"][f"s{k}_s"]["lap"] == lap for lap, k in held)   # 구간마다 최속은 한 랩


def test_track_and_series_carry_reference_distance(circle):
    t = api.get_track(circle, lap=2)
    ref = np.array(t["ref_m"], float)
    assert len(ref) == len(t["x_m"]) and ref[0] == 0.0 and ref[-1] == pytest.approx(LAP_M, abs=0.05) and np.all(np.diff(ref) >= 0)
    assert api.get_track(circle, "all")["ref_m"] is None
    g = api.get_series(circle, ["gps.speed_kmh"], x="ref_distance", lap=2, max_points=200)
    assert g["x"]["kind"] == "ref_distance" and g["x"]["unit"] == "m" and len(g["x"]["values"]) == 200
    assert g["x"]["values"][-1] == pytest.approx(LAP_M, abs=1.0) and g["laps"][0]["x_end"] == pytest.approx(LAP_M, abs=0.05)
    part = api.get_series(circle, ["gps.speed_kmh"], x="ref_distance", lap=2, d0=100.0, d1=150.0)
    assert 100.0 < part["x"]["values"][0] < part["x"]["values"][-1] < 150.0


def test_delta_between_sessions_of_one_frame(pair):
    """30 s 랩과 31 s 랩 (둘 다 일정한 속도) — 시간 차는 거리에 비례해 0 에서 1 s 로. frame 이 다르면 비교하지 않는다."""
    s1, s2, alone = pair
    d = strict(api.get_delta(s2, 1, s1, 1))
    assert d["frame_id"] == FRAME and d["total_s"] == pytest.approx(1.0, abs=0.002) and d["delta_s"][-1] == d["total_s"]
    x, dd = np.array(d["x"]["values"]), np.array(d["delta_s"])
    assert x[0] == 0.0 and x[-1] == pytest.approx(LAP_M, abs=0.05) and np.all(np.diff(dd) >= -0.002)
    assert dd[np.argmin(np.abs(x - LAP_M / 2))] == pytest.approx(0.5, abs=0.01)
    assert sum(d["mini"]["delta_s"]) == pytest.approx(1.0, abs=0.01) and len(d["mini"]["delta_s"]) == len(d["mini"]["edges_ref_m"]) - 1
    on = d["on_track"]["values"]
    assert len(on) == len(api.get_track(s2, 1)["x_m"]) and on[0] == 0.0 and on[-1] == pytest.approx(1.0, abs=0.002)
    assert max(abs(v) for v in api.get_delta(s1, 2, s1, 2)["delta_s"]) == 0.0
    assert len(api.get_delta(s2, 1, s1, 1, max_points=100)["x"]["values"]) == 100
    assert api.get_sectors(s1)["ref"]["reference_lap"]["sid"] == s1 and api.get_sectors(s2)["ref"] == api.get_sectors(s1)["ref"]   # 한 frame
    assert error_code(lambda: api.get_delta(alone, 1, s1, 1)) == "frame_mismatch"


def test_frame_is_fixed_until_reframe(tmp_path):
    """frame 은 한 번 정하면 그대로다 — 더 빠른 랩이 들어와도 구간 경계 · 기준 랩이 바뀌지 않는다 (reframe 으로만)."""
    root = tmp_path / "store"
    store.write_session(circle_session("LEF-27_2027-06-05_S1", 30.0, FRAME), root, tmp_path)
    store.write_index(root)
    assert store.write_frames(root) == [FRAME]
    path = store.frame_path(root, FRAME)
    before = path.read_text(encoding="utf-8")
    store.write_session(circle_session("LEF-27_2027-06-05_S2", 29.0, FRAME), root, tmp_path)
    store.write_index(root)
    assert store.write_frames(root) == [] and path.read_text(encoding="utf-8") == before
    assert store.write_frames(root, reframe=True, keys={"other"}) == []                 # 다른 frame 만 다시 정하라면 그대로
    assert store.write_frames(root, reframe=True) == [FRAME]
    assert json.loads(path.read_text(encoding="utf-8"))["reference"]["sid"] == "LEF-27_2027-06-05_S2"


def test_missing_frame_file_is_reported(circle, tmp_path):
    """옛 코드로 만든 저장소(frame 파일 없음) — 구간 함수는 다시 만들라고 하고, 랩 표 · 코스는 구간 없이 나온다."""
    store.frame_path(tmp_path / "store", circle).unlink()
    api.configure(store_root=tmp_path / "store", data_root=tmp_path)
    assert error_code(lambda: api.get_sectors(circle)) == "internal"
    laps = api.get_laps(circle)
    assert laps["sectors"] is None and "s1_s" not in laps["rows"][0] and any("frame" in n for n in laps["notes"])
    assert api.get_track(circle)["ref_m"] is None


def test_engine_estimates_drive_state_when_power_and_speed_exist(tmp_path):
    """배터리 전력과 GPS 속도가 있으면 엔진이 주행 상태(추정) 블록을 더한다 — 정해진 값 셋과 범례, 지도 색은 잇지 않은 값."""
    root = tmp_path / "store"
    store.write_session(circle_session(power_kw=12.0), root, tmp_path)
    store.write_index(root)
    store.write_frames(root)
    api.configure(store_root=root, data_root=tmp_path)
    try:
        sid = "LEF-27_2027-06-05_S1"
        row = api.list_sessions()[0]
        assert row["blocks"]["warn"] == ["drive"] and ("drive", "warn") in [(d["block"], d["status"]) for d in row["data_status"]]
        ch = {c["channel_id"]: c for c in api.list_channels(sid)}["drive.state_est"]
        assert ch["derived"] and [lv["label"] for lv in ch["levels"]] == ["가속", "타력", "제동"] and ch["rate_hz"] == 10.0
        assert any("추정" in c for c in ch["cautions"])
        col = strict(api.get_track(sid, color="drive.state_est"))["color"]
        assert col["levels"] == ch["levels"] and {v for v in col["values"] if v is not None} == {1.0}    # 일정한 속도 + 전력 → 가속
        blocks = {b["block"]: b for b in api.get_session(sid)["blocks"]}
        assert blocks["drive"]["status"] == "warn" and blocks["drive"]["default_views"]["map"] == "drive.state_est"
    finally:
        api.configure()


def test_real_driver_names_never_enter_the_store(tmp_path):
    s = circle_session()
    s.driver = "홍길동"
    with pytest.raises(ValueError):
        store.write_session(s, tmp_path)


# ---------------------------------------------------------------- 실제 대회 이벤트 (값은 api_contract.toml)

E = Expected("api_contract")
RACE = paths.LEF26_FSK
race = pytest.mark.skipif(not ((RACE / cb.NOTES_NAME).exists() and (RACE / "derived" / "lap_energy.csv").exists()),
                          reason="대회 이벤트에 catalog_notes.toml · lefdata em-laps 결과 없음")


@pytest.fixture(scope="module")
def race_store(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions")
    store.build_event(RACE, root)
    api.configure(store_root=root)
    yield root
    api.configure()


@pytest.fixture(scope="module")
def race_sids(race_store):
    """그 이벤트의 세션 — 오래된 것부터."""
    return [r["sid"] for r in reversed(api.list_sessions(event_id=f"{RACE.parent.name}/{RACE.name}"))]


def _blocks(sid):
    return {b["block"]: b for b in api.get_session(sid)["blocks"]}


# ---- 어떤 세션에도 맞는 약속 (모양 · 불변식)

@race
def test_race_every_answer_is_strict_json(race_sids):
    for sid in race_sids:
        for call in (lambda: api.get_session(sid), lambda: api.list_channels(sid), lambda: api.get_laps(sid), lambda: api.get_quality(sid),
                     lambda: api.get_track(sid, color="battery.power_kw"), lambda: api.get_series(sid, ["battery.current_a", "imu.long_g"])):
            strict(call())
    rows = strict(api.list_sessions())
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts, reverse=True)                                # 최신순
    assert all(r["driver_label"].startswith("드라이버 ") for r in rows)          # 실명은 오지 않는다


@race
def test_race_channels_carry_units_signs_and_ranges(race_sids):
    for sid in race_sids:
        ch = {c["channel_id"]: c for c in api.list_channels(sid)}
        duration = api.get_session(sid)["summary"]["duration_s"]
        assert ch["imu.long_g"]["sign"] == "가속 +, 제동 −" and ch["imu.lat_g"]["sign"].startswith("좌선회 +") and ch["imu.yaw_dps"]["sign"] == "좌회전 +"
        assert ch["imu.long_g"]["derived"] and not ch["battery.current_a"]["derived"] and ch["battery.current_a"]["sign"].startswith("방전 +")
        for c in ch.values():
            assert 0 <= c["t_range"][0] < 1 and c["t_range"][1] == pytest.approx(duration, abs=0.1)


@race
def test_race_peak_survives_reduction(race_store, race_sids):
    """원래 표본을 4,000 칸으로 줄여도 순간 최대 · 최소가 그대로 남는다."""
    sid = race_sids[0]
    r = api.get_series(sid, ["battery.current_a"])
    raw = pd.read_parquet(race_store / sid / "series" / "battery.parquet")["current_a"]
    assert r["reduced"]["from_points"] == len(raw) and len(r["x"]["values"]) == 4000
    assert max(v for v in r["series"][0]["y_max"] if v is not None) == pytest.approx(float(raw.max()), abs=0.01)
    assert min(v for v in r["series"][0]["y_min"] if v is not None) == pytest.approx(float(raw.min()), abs=0.01)


@race
def test_race_mixed_rates_share_x(race_sids):
    r = api.get_series(race_sids[0], ["gps.speed_kmh", "imu.long_g", "battery.current_a"], max_points=3000)
    assert len(r["x"]["values"]) == 3000 and all(len(s["values"]) == 3000 for s in r["series"])
    assert len(r["laps"]) == len(api.get_laps(race_sids[0])["rows"])


@race
def test_race_zoom_shows_the_dropped_samples(race_sids):
    """품질 경고의 '빈 틈' 구간을 확대하면 실제로 값이 비어 있다 (경고와 그래프가 같은 말을 한다)."""
    for sid in race_sids:
        holes = next((q for q in api.get_quality(sid) if q["code"] == "em_dropped_samples"), {"t_ranges": []})["t_ranges"]
        if not holes:
            continue
        a, b = max(holes, key=lambda h: h[1] - h[0])
        r = api.get_series(sid, ["battery.current_a"], t0=a - 0.3, t1=b + 0.3, max_points=2000)
        x, v = np.array(r["x"]["values"]), r["series"][0]["values"]
        inner = np.flatnonzero((x > a + 0.005) & (x < b - 0.005))
        assert len(inner) and all(v[i] is None for i in inner)


@race
def test_race_smoothing_lowers_camera_noise(race_sids):
    sid = race_sids[0]
    raw = api.get_series(sid, ["imu.long_g"], t0=100, t1=110)
    smooth = api.get_series(sid, ["imu.long_g"], t0=100, t1=110, smooth_s=0.5)
    spread = lambda r: np.nanstd(np.array(r["series"][0]["values"], dtype=float))     # noqa: E731
    assert smooth["reduced"]["smooth_s"] == 0.5 and spread(smooth) < spread(raw)


@race
def test_race_position_channels_follow_the_track(race_sids):
    """시간축 커서를 지도에 찍을 수 있게 세션 전체의 위치가 채널로 온다 — 코스(get_track)와 같은 좌표."""
    sid = race_sids[0]
    tr = api.get_track(sid)
    ta = tr["t_s"][0]
    r = api.get_series(sid, ["gps.x_m", "gps.y_m"], t0=ta - 0.5, t1=ta + 0.5)
    k = int(np.argmin(np.abs(np.array(r["x"]["values"]) - ta)))
    assert r["series"][0]["values"][k] == pytest.approx(tr["x_m"][0], abs=2.0)        # GPS 표본 사이 이동 거리 안
    assert r["series"][1]["values"][k] == pytest.approx(tr["y_m"][0], abs=2.0)
    whole = api.get_series(sid, ["gps.x_m", "gps.y_m"], max_points=2000)
    assert None not in whole["series"][0]["values"] and len(whole["x"]["values"]) == 2000


@race
def test_race_sessions_of_one_event_share_the_gate(race_sids):
    """같은 행사의 세션은 같은 계측선 · 같은 평면 좌표다 (frame_id 가 같다)."""
    summaries = [api.get_session(s)["summary"] for s in race_sids]
    assert len({s["frame_id"] for s in summaries}) == 1
    tracks = [api.get_track(s) for s in race_sids]
    assert all(t["gate"] == tracks[0]["gate"] and t["frame_id"] == summaries[0]["frame_id"] for t in tracks)


@race
def test_race_track_starts_and_ends_at_the_gate(race_sids):
    for sid in race_sids:
        laps = api.get_laps(sid)
        tr = api.get_track(sid)
        best = next(r for r in laps["rows"] if r["best"])
        assert tr["lap"] == best["lap"] == laps["best_lap"]
        assert tr["t_s"][0] == pytest.approx(best["t_start_s"], abs=0.001) and tr["dist_m"][0] == 0.0
        col = api.get_track(sid, color="battery.power_kw")["color"]["values"]
        assert len(col) == len(tr["x_m"])


@race
def test_race_sectors_add_up_and_deltas_close(race_sids):
    """구간 시간의 합 = 랩타임, 미니 구간의 합 = 구간의 합, 시간 차의 끝 = 두 랩의 랩타임 차 (다른 세션 랩끼리)."""
    rows = {}
    for sid in race_sids:
        laps = api.get_laps(sid)
        rows[sid] = {r["lap"]: r for r in laps["rows"]}
        for r in laps["rows"]:
            assert r["s1_s"] + r["s2_s"] + r["s3_s"] == pytest.approx(r["t_end_s"] - r["t_start_s"], abs=0.002)
        for lap in api.get_sectors(sid)["laps"]:
            assert sum(lap["mini_s"]) == pytest.approx(sum(lap["sector_s"]), abs=0.0005 * (len(lap["mini_s"]) + 3))
    a, b = race_sids
    d = api.get_delta(b, 1, a, 1)
    want = (rows[b][1]["t_end_s"] - rows[b][1]["t_start_s"]) - (rows[a][1]["t_end_s"] - rows[a][1]["t_start_s"])
    assert d["total_s"] == pytest.approx(want, abs=0.002) and d["delta_s"][-1] == d["total_s"]
    assert d["on_track"]["values"][-1] == pytest.approx(d["total_s"], abs=0.002) and d["on_track"]["values"][0] == 0.0


@race
def test_race_sector_lines_are_the_same_place_on_every_lap(race_sids):
    """같은 기준 거리 = 코스의 같은 곳 — 구간 경계를 지난 순간의 위치가 랩마다 경계 지점과 진행 방향으로 2 m 안이다
    (기준선에 내리는 방법의 약속. 랩 길이로 늘이고 줄이면 이 몇 배가 어긋난다)."""
    bounds = api.get_sectors(race_sids[0])["sectors"][1:]
    worst = 0.0
    for sid in race_sids:
        for row in api.get_laps(sid)["rows"]:
            tr = api.get_track(sid, row["lap"])
            ref, x, y = (np.array(tr[k], float) for k in ("ref_m", "x_m", "y_m"))
            keep = np.concatenate([[True], np.diff(ref) > 0])
            for c in bounds:
                bx, by = np.interp(c["ref_m0"], ref[keep], x[keep]), np.interp(c["ref_m0"], ref[keep], y[keep])
                h = np.radians(c["heading_deg"])
                worst = max(worst, abs((bx - c["x_m"]) * np.sin(h) + (by - c["y_m"]) * np.cos(h)))
    assert worst < 2.0


@race
def test_race_one_frame_for_the_event(race_store, race_sids):
    frames = list((race_store / store.FRAMES).glob("*.json"))
    assert len(frames) == 1
    fr = json.loads(frames[0].read_text(encoding="utf-8"))
    assert fr["reference"]["sid"] in race_sids and fr["frame_key"] == api.get_session(race_sids[0])["summary"]["frame_id"]
    lengths = [r["distance_m"] for sid in race_sids for r in api.get_laps(sid)["rows"]]
    assert fr["ref_len_m"] == pytest.approx(float(np.median(lengths)), rel=0.01)
    assert {api.get_sectors(s)["ref"]["fixed_at"] for s in race_sids} == {fr["fixed_at"]}


@race
def test_race_estimated_drive_state_agrees_with_the_camera(race_store, race_sids):
    """주행 상태 추정(배터리 전력 + GPS 속도)을 독립된 측정(카메라 가속도계)에 대 본다 — 제동으로 나온 순간은 실제로 감속하고 있고,
    앞뒤 가속도의 가운데값이 제동 < 타력 < 가속 순서다."""
    from lefdata import series as ser
    for sid in race_sids:
        ch = {c["channel_id"]: c for c in api.list_channels(sid)}["drive.state_est"]
        assert ch["derived"] and [lv["value"] for lv in ch["levels"]] == [1, 0, -1]
        st = pd.read_parquet(race_store / sid / "series" / "drive.parquet")
        imu = pd.read_parquet(race_store / sid / "series" / "imu.parquet")
        long_g = ser.sample_at(imu["t_s"].to_numpy(), ser.smooth(imu["t_s"].to_numpy(), imu["long_g"].to_numpy(float), 0.5), st["t_s"].to_numpy())
        state = st["state_est"].to_numpy(float)
        assert set(np.unique(state[np.isfinite(state)])) == {-1.0, 0.0, 1.0}
        med = {k: float(np.nanmedian(long_g[state == k])) for k in (1, 0, -1)}
        assert med[-1] < -0.2 and med[-1] < med[0] < med[1], med
        col = api.get_track(sid, color="drive.state_est")["color"]
        assert {v for v in col["values"] if v is not None} <= {-1.0, 0.0, 1.0} and col["levels"] == ch["levels"]


@race
def test_race_answers_are_fast_enough(race_sids):
    """계약 §9 의 목표 — 3채널 · 4,000점에 0.3 초 안 (캐시가 데워진 뒤)."""
    import time
    ids = ["battery.current_a", "imu.long_g", "gps.speed_kmh"]
    api.get_series(race_sids[0], ids)
    t0 = time.perf_counter()
    api.get_series(race_sids[0], ids)
    assert time.perf_counter() - t0 < 0.3
    api.configure(store_root=api._root())                     # 구간 계산은 캐시가 빈 첫 호출도 목표 안에 (랩마다 기준선에 내리는 계산)
    t0 = time.perf_counter()
    api.get_laps(race_sids[0])
    api.get_delta(race_sids[1], 1, race_sids[0], 1)
    assert time.perf_counter() - t0 < 0.3


# ---- 골든 (값은 api_contract.toml)

@race
@E.need
def test_race_session_list_golden(race_sids):
    assert race_sids == E["sids"]
    rows = {r["sid"]: r for r in strict(api.list_sessions(event_id=E["event_id"]))}
    a, b = rows[E["sids"][0]], rows[E["sids"][1]]
    assert (a["laps"], a["best_lap_s"], a["distance_km"], a["driver_label"]) == (E["s1.laps"], E["s1.best_lap_s"], E["s1.distance_km"], E["s1.driver_label"])
    assert a["duration_s"] == E.approx("s1.duration_s") and a["start"] == E["s1.start"]
    assert a["stats"] == E["s1.stats"]
    assert [[d["block"], d["status"]] for d in a["data_status"]] == E["s1.data_status"]
    assert a["blocks"] == E["s1.blocks"]
    assert b["driver_label"] == E["s2.driver_label"] and b["stats"]["net_wh"] == E["s2.net_wh"]
    assert [r["sid"] for r in api.list_sessions(limit=1)] == [E["sids"][1]] and [r["sid"] for r in api.list_sessions(limit=1, offset=1)] == [E["sids"][0]]


@race
@E.need
def test_race_session_screen_golden():
    sid = E["sids"][0]
    s = strict(api.get_session(sid))
    bat = {b["block"]: b for b in s["blocks"]}["battery"]
    assert any(E["s1.battery_fact"] in f for f in bat["facts"]) and any(E["s1.battery_check"] in c for c in bat["checks"])
    assert any("시간 열(t_s)로 적분" in c for c in bat["cautions"])
    assert bat["default_views"]["map"] == "battery.power_kw"
    raw = bat["raw"][0]
    assert raw["file_id"] and raw["size_bytes"] == E["s1.meter_log_bytes"] and api.open_file(raw["file_id"])["size_bytes"] == E["s1.meter_log_bytes"]
    video = {b["block"]: b for b in s["blocks"]}["video"]
    assert len(video["raw"]) == E["s1.video_files"] and all(r["file_id"] for r in video["raw"])     # 영상도 서버가 내줄 수 있다 (공개 범위는 사이트가 막는다)
    assert [m["block"] for m in s["missing"]] == E["s1.missing_blocks"]


@race
@E.need
def test_race_channels_golden():
    sid = E["sids"][0]
    ch = {c["channel_id"]: c for c in api.list_channels(sid)}
    assert set(ch) == set(E["s1.channels"])
    for cid, hz in E["s1.rate_hz"].items():
        assert ch[cid]["rate_hz"] == pytest.approx(hz, abs=E["s1.rate_abs_hz"]), cid
    r = api.get_series(sid, ["battery.current_a"])
    assert r["reduced"]["from_points"] == E["s1.meter_samples"]
    assert round(max(v for v in r["series"][0]["y_max"] if v is not None)) == E["s1.peak_current_a"]


@race
@E.need
def test_race_laps_track_and_distance_golden():
    sid = E["sids"][0]
    laps = strict(api.get_laps(sid))
    assert laps["best_lap"] == E["s1.best_lap"] and len(laps["rows"]) == E["s1.laps"]
    best = next(r for r in laps["rows"] if r["best"])
    assert best["lap_time_s"] == E.approx("s1.best_lap_time_s") and best["net_wh"] == E.approx("s1.best_lap_net_wh")
    assert {c["key"]: c["block"] for c in laps["columns"]}["net_wh"] == "battery"
    tr = strict(api.get_track(sid))
    assert tr["direction"] == E["s1.direction"] and tr["gate"]["x_m"] == E.approx("s1.gate_x_m")
    assert tr["dist_m"][-1] == E.approx("s1.best_lap_m") and tr["frame_id"] == E["frame_id"]
    col = api.get_track(sid, color="battery.power_kw")["color"]["values"]
    assert max(v for v in col if v is not None) == E.approx("s1.best_lap_peak_power_kw")
    d = api.get_series(sid, ["gps.speed_kmh", "battery.power_kw"], x="distance", lap=E["s1.best_lap"])
    assert d["x"]["values"][0] == pytest.approx(0.0, abs=0.5) and d["x"]["values"][-1] == E.approx("s1.best_lap_m")
    assert max(v for v in d["series"][0]["y_max"] if v is not None) == E.approx("s1.best_lap_vmax_kmh")
    d2 = api.get_series(E["sids"][1], ["gps.speed_kmh"], x="distance", lap=E["s2.best_lap"])
    assert d2["x"]["frame_id"] == E["frame_id"] and d2["x"]["values"][-1] == E.approx("s2.best_lap_m")


@race
@E.need
def test_race_sectors_and_delta_golden(race_store):
    fr = json.loads(next((race_store / store.FRAMES).glob("*.json")).read_text(encoding="utf-8"))
    assert [fr["reference"]["sid"], fr["reference"]["lap"]] == E["frame.reference"] and fr["ref_len_m"] == E.approx("frame.ref_len_m")
    sid = E["sids"][0]
    laps = api.get_laps(sid)
    sec = laps["sectors"]
    assert [sec["best"][f"s{k}_s"]["lap"] for k in (1, 2, 3)] == E["s1.sector_best_laps"]
    assert sec["theoretical_best_s"] == E.approx("s1.theoretical_best_s")
    best = next(r for r in laps["rows"] if r["best"])
    assert [best["s1_s"], best["s2_s"], best["s3_s"]] == pytest.approx(E["s1.best_lap_sectors_s"], abs=E["s1.sector_abs_s"])
    assert api.get_sectors(sid)["mini"]["count"] == E["frame.mini_25_count"]
    (la, na), (lb, nb) = E["delta.lap"], E["delta.ref"]
    d = api.get_delta(la, na, lb, nb)
    assert d["total_s"] == E.approx("delta.total_s")
    x, dd = np.array(d["x"]["values"]), np.array(d["delta_s"])
    assert dd[np.argmin(np.abs(x - x[-1] / 2))] == E.approx("delta.half_s")


@race
@E.need
def test_race_drive_state_golden():
    for key, sid in zip(("s1", "s2"), E["sids"]):
        drive_block = {b["block"]: b for b in api.get_session(sid)["blocks"]}["drive"]
        assert any(E[f"{key}.drive_fact"] in f for f in drive_block["facts"]), drive_block["facts"]


@race
@E.need
def test_race_quality_findings_golden():
    q1 = {q["code"]: q for q in api.get_quality(E["sids"][0])}
    q2 = {q["code"]: q for q in api.get_quality(E["sids"][1])}
    total = lambda q: sum(b - a for a, b in q["t_ranges"])                   # noqa: E731
    assert total(q1["em_dropped_samples"]) == E.approx("s1.dropped_total_s") and "imu_time_uncalibrated" not in q1
    assert total(q2["em_dropped_samples"]) == E.approx("s2.dropped_total_s")
    assert total(q2["imu_time_uncalibrated"]) == E.approx("s2.imu_uncalibrated_s")
    assert q2["imu_time_uncalibrated"]["t_ranges"][-1][1] == E.approx("s2.imu_uncalibrated_end_s")
