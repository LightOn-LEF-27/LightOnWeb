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

def circle_session() -> Session:
    """반지름 40 m 원을 반시계 방향으로 세 바퀴 — 10 Hz 속도 하나, 50 Hz 전류 하나(0.5 s 빈 곳 하나)."""
    lap_s, n = 30.0, 3
    t = np.arange(0.0, lap_s * n + 10, 0.1)
    ang = 2 * np.pi * np.clip(t - 5, 0, lap_s * n) / lap_s
    track = pd.DataFrame({"t_s": t, "x_m": 40 * np.sin(ang), "y_m": 40 * (1 - np.cos(ang)), "dist_m": 40 * ang})
    laps = pd.DataFrame({"lap": [1, 2, 3], "t_start_s": [5.0, 35.0, 65.0], "t_end_s": [35.0, 65.0, 95.0], "lap_time_s": [30.4, 29.8, 30.1],
                         "vmax_kmh": [68.0, 70.0, 69.5], "i_max_a": [150.0, 200.0, 180.0]})
    speed = 50 + 20 * np.sin(3 * ang)
    ti = np.concatenate([np.arange(0.0, 50.0, 0.02), np.arange(50.5, 100.0, 0.02)])
    amp = 100 + 50 * np.sin(ti)
    amp[np.searchsorted(ti, 40.0)] = 200.0
    gps = DataBlock("gps", "위치 · 속도 (GPS)", HAVE, "시험용 GPS · 10 Hz", ["표본 1,000개"],
                    exports=[Export("gps.csv", "이 세션의 GPS", track.round(2), "1,000 행")],
                    channels=[Channel("speed_kmh", "속도", "km/h", t, speed, digits=2)], time_keys=["speed_kmh"], trace_keys=["speed_kmh"],
                    map_key="speed_kmh", lap_columns=[LapColumn("lap_time_s", "랩타임", "s", 2, chart=True), LapColumn("vmax_kmh", "최고 속도", "km/h", 1)])
    bat = DataBlock("battery", "배터리 전압 · 전류", HAVE, "시험용 미터 · 50 Hz",
                    channels=[Channel("current_a", "배터리 전류", "A", ti, amp, sign="방전 +", digits=2)], time_keys=["current_a"],
                    lap_columns=[LapColumn("i_max_a", "전류 최대", "A", 0)], cautions=["50.0 ~ 50.5 s 가 비어 있음"],
                    findings=[{"level": "warn", "code": "em_dropped_samples", "message": "빈 틈 1곳", "t_ranges": [[49.98, 50.5]]}])
    return Session("LEF-27_2027-06-05_S1", "LEF-27", "2027-06-05", "셰이크다운 1", pd.Timestamp("2027-06-05 10:00", tz="Asia/Seoul"), 100.0,
                   event_kind="테스트", discipline="연습 주행", driver="드라이버 A", event_id="LEF-27/2027-06-05_test",
                   stats={"distance_km": 0.75, "laps_n": 3, "best_lap_s": 29.8}, laps=laps, track=track,
                   gate={"x_m": 0.0, "y_m": 0.0, "heading_deg": 90.0, "half_width_m": 10.0}, blocks=[gps, bat],
                   missing=[Missing("모터 · 인버터", SEEK, "로거 파일을 못 받음", "SD 카드를 가진 분", "motor"), Missing("조향각", NONE, "센서 없음")])


@pytest.fixture()
def circle(tmp_path):
    root = tmp_path / "store"
    store.write_session(circle_session(), root, tmp_path)
    store.write_index(root)
    api.configure(store_root=root, data_root=tmp_path)
    yield "LEF-27_2027-06-05_S1"
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
                 lambda: api.get_track(sid, color="battery.current_a"), lambda: api.get_quality(sid)):
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
    for fn in (lambda: api.detect("a.log"), lambda: api.ingest(["a.log"]), lambda: api.get_job("j")):
        assert error_code(fn) == "not_implemented"


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
def test_race_answers_are_fast_enough(race_sids):
    """계약 §9 의 목표 — 3채널 · 4,000점에 0.3 초 안 (캐시가 데워진 뒤)."""
    import time
    ids = ["battery.current_a", "imu.long_g", "gps.speed_kmh"]
    api.get_series(race_sids[0], ids)
    t0 = time.perf_counter()
    api.get_series(race_sids[0], ids)
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
    assert tr["dist_m"][-1] == E.approx("s1.best_lap_m") and tr["frame_id"] == E["event_id"]
    col = api.get_track(sid, color="battery.power_kw")["color"]["values"]
    assert max(v for v in col if v is not None) == E.approx("s1.best_lap_peak_power_kw")
    d = api.get_series(sid, ["gps.speed_kmh", "battery.power_kw"], x="distance", lap=E["s1.best_lap"])
    assert d["x"]["values"][0] == pytest.approx(0.0, abs=0.5) and d["x"]["values"][-1] == E.approx("s1.best_lap_m")
    assert max(v for v in d["series"][0]["y_max"] if v is not None) == E.approx("s1.best_lap_vmax_kmh")
    d2 = api.get_series(E["sids"][1], ["gps.speed_kmh"], x="distance", lap=E["s2.best_lap"])
    assert d2["x"]["frame_id"] == E["event_id"] and d2["x"]["values"][-1] == E.approx("s2.best_lap_m")


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
