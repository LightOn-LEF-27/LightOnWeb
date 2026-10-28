"""시각 구간 세션 (E3 — GPS 없는 세션 · 트랙데이 Run Sheet) — `catalog_notes.toml` 의 `[[timed_session]]` 로 나눈 세션.

앞쪽은 만들어 낸 이벤트로 돈다 (데이터 없이): 에너지미터 · 25Evo STM32 · Orion 레코딩을 장비마다 다른 시계로 써 두고,
기준 시계로 옮겨 자르는지 · 같은 것을 두 장비가 주면 더 촘촘한 쪽을 쓰는지 · 시계 차를 틀리게 적으면 경고가 붙는지 ·
API 가 랩 · 코스 없는 세션을 다루는지 본다.
뒤쪽은 우리 데이터 — LEF-26 테스트위크에서 GPS 세션과 같은 시각으로 시각 구간 세션을 만들면 로거 값이 겹치는지(보편),
25Evo 대회의 GPS 없는 세션(골든, 값은 기대값 파일 `timed_sessions.toml`).
"""

import json

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import api, catalog_build as cb, paths, session_store as store
from lefdata.api import EngineError

E = Expected("timed_sessions")
KST = "Asia/Seoul"
T0 = pd.Timestamp("2027-06-05 10:00:00", tz=KST)     # 기준 시계로 본 첫 전원 주기의 시작
EM_AHEAD_S = 180.0                                   # 에너지미터 시계가 기준 시계보다 이만큼 앞선다 → offset_s = −180
SPIKE_S = 120.0                                      # 전원 주기 시작부터 이 시각에 짧은 봉우리 (시각 확인용 표시)


def current(t):
    """기준 시계로 본 시각(전원 주기 시작부터 s) → 팩 전류 A. 두 번 달리고, 그 사이에 짧은 봉우리 하나."""
    t = np.asarray(t, float)
    a = np.where((t > 60) & (t < 90), 120.0, 0.0) + np.where((t > 150) & (t < 200), 80.0 + 40.0 * np.sin(t), 0.0)
    return a + 250.0 * np.clip(1 - np.abs(t - SPIKE_S) / 0.5, 0, None)       # 꼭짓점이 SPIKE_S 인 삼각형


def voltage(t, amp):
    return np.where(np.asarray(t) >= 5.0, 300.0 - 0.05 * amp, 0.0)          # 5 s 에 고전압이 들어온다


def _meta(d, logger, start, duration_s, **time):
    m = {"car": "LEF-27", "recording_id": d.name, "source": {"logger": logger, "file": f"raw/{d.name}.log"},
         "time": {"t_abs_start": start.isoformat(), "duration_s": duration_s, **time}, "counts": {"records": 1, "checksum_fail": 0}}
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")


def _stamp(start, t):
    return pd.Series(start + pd.to_timedelta(np.asarray(t, float), unit="s"))


def write_event(root, em_offset=-EM_AHEAD_S, extra_notes=""):
    """첫 전원 주기(기준 시계 T0 ~ T0 + 300 s)에 에너지미터 · STM32 · Orion, 둘째(T0 + 1000 s ~ + 200 s)에 STM32 만."""
    ev = root / "LEF-27" / "2027-06-05_test"
    norm = ev / "normalized"
    em_start = T0 + pd.Timedelta(seconds=EM_AHEAD_S)                            # 에너지미터 시계로 본 시작
    t = np.arange(0.0, 300.0, 0.01)
    amp = current(t)
    d = norm / "em1"
    d.mkdir(parents=True)
    pd.DataFrame({"t_rel_ms": (t * 1000).round().astype("int64"), "t_abs": _stamp(em_start, t), "valid": True,
                  "voltage_v": voltage(t, amp), "current_a": amp}).to_parquet(d / "energy.parquet", index=False)
    _meta(d, "FSK 에너지미터 (FSK-EEM)", em_start, 300.0, median_dt_ms=10.0)
    for name, start, span in (("stm32_log001", em_start, 300.0), ("stm32_log002", em_start + pd.Timedelta(seconds=1000), 200.0)):
        ts = np.arange(1.0, span - 1.0, 1 / 3)                                  # 초당 3 행
        a = current(ts) if name.endswith("1") else 50.0 + 30.0 * np.sin(ts / 5)
        v = voltage(ts, a)
        d = norm / name
        d.mkdir()
        rel = (ts * 1000).round().astype("int64")
        pd.DataFrame({"t_rel_ms": rel, "t_abs": _stamp(start, ts), "cell_min_v": 3.9 - ts / 1e4, "temp_max_c": 30 + ts / 60,
                      "temp_min_c": 28.0, "soc_pct": 90 - ts / 60, "pack_v": v, "current_a": a, "power_kw": v * a / 1000}).to_parquet(d / "bms.parquet", index=False)
        pd.DataFrame({"t_rel_ms": rel, "t_abs": _stamp(start, ts), "rpm": -20 * a, "vdc_v": v - 1, "t_igbt_c": 35.0,
                      "t_motor_c": 40.0}).to_parquet(d / "motor.parquet", index=False)
        _meta(d, "자작 STM32 백업 로거 (NUCLEO-L432KC)", start, span, alignment={"clock_of": "에너지미터", "em_recording": "em1"})
    k = np.arange(0, 300)                                                       # Orion: 기준 시계, 1 초 단위 · 그 초 둘레 평균
    fine = np.arange(0.0, 300.0, 0.01)
    mean_a = np.array([current(fine[(fine >= s - 0.5) & (fine < s + 0.5)]).mean() for s in k])
    d = norm / "orion1"
    d.mkdir()
    soc = 90 - k / 60
    soc[0] = 0.0                                                                # 켠 직후 행의 SOC 0
    pd.DataFrame({"t_rel_ms": k * 1000, "t_abs": _stamp(T0, k), "current_a": mean_a, "voltage_v": voltage(k, mean_a), "soc_pct": soc,
                  "cell_min_v": 3.9, "cell_max_v": 3.95, "temp_max_c": 30 + k / 60, "temp_min_c": 28.0, "dcl_a": 300.0,
                  "dcl_limited_temp": False, "dcl_limited_soc": False, "errors_present": False}).to_parquet(d / "bms.parquet", index=False)
    _meta(d, "Orion BMS Connect (Orion BMS 2 Utility 로 CSV 변환)", T0, 300.0)
    (ev / cb.NOTES_NAME).write_text(f'''
[event]
car = "LEF-27"
kind = "테스트"
title = "합성 시험"
place = "시험장"
clock = "orion"

[clocks.energymeter]
offset_s = {em_offset}
basis = "합성 — 만들 때 넣은 값"

[[timed_session]]
start = "2027-06-05 10:00:00"
end = "2027-06-05 10:04:10"
discipline = "연습 주행"
driver = "드라이버 A"

[[timed_session]]
recording = "stm32_log002"
discipline = "연습 주행"
{extra_notes}''', encoding="utf-8")
    return ev


def _blocks(s):
    return {b.key: b for b in s.blocks}


def _ch(s, block, key):
    return next(c for c in (_blocks(s)[block].series or _blocks(s)[block].channels) if c.key == key)


def _codes(s):
    return [f["code"] for b in s.blocks for f in b.findings]


# ---------------------------------------------------------------- 만들어 낸 이벤트 (데이터 없이)

def test_devices_are_moved_to_the_reference_clock_and_cut(tmp_path):
    ev = write_event(tmp_path)
    first, second = cb.build_event(ev)
    assert [s.sid for s in (first, second)] == ["LEF-27_2027-06-05_S1", "LEF-27_2027-06-05_S2"]
    assert first.split_by == "time" and first.start == T0 and first.duration_s == pytest.approx(250.0)
    assert first.track is None and first.laps.empty and first.frame_id == "" and first.stats["laps_n"] is None
    b = _blocks(first)
    assert list(b) == ["battery", "motor", "bms"]                    # 배터리는 에너지미터, BMS 는 Orion (STM32 는 모터만)
    assert "에너지미터" in b["battery"].device and "Orion" in b["bms"].device and "STM32" in b["motor"].device
    for blk in first.blocks:
        for c in blk.series or blk.channels:
            assert len(c.t_s) and np.min(c.t_s) >= 0 and np.max(c.t_s) <= first.duration_s + 1e-9
    amp = _ch(first, "battery", "current_a")
    assert amp.t_s[np.argmax(amp.v)] == pytest.approx(SPIKE_S, abs=0.02)           # 에너지미터 시계 −180 s 로 옮긴 자리
    rpm = _ch(first, "motor", "rpm")
    assert rpm.t_s[np.argmin(rpm.v)] == pytest.approx(SPIKE_S, abs=0.5)            # STM32 도 에너지미터 시계를 따른다
    soc = _ch(first, "bms", "soc_pct")
    assert np.isnan(soc.v[0]) and soc.v[1] == pytest.approx(90 - 1 / 60)           # 켠 직후의 0 은 비운다
    t = np.arange(0.0, 250.0, 0.01)
    a = current(t)
    net = float(np.trapezoid(voltage(t, a) * a, t) / 3600)                       # W · s → Wh
    assert first.stats["net_wh"] == pytest.approx(net, abs=1) and any(f"순 {first.stats['net_wh']:,} Wh" in f for f in b["battery"].facts)
    assert any("에너지미터 전력과 대 봄" in c and "허용 1 s 안" in c for c in b["motor"].checks)
    assert any("에너지미터 전류와 대 봄" in c and "허용 2 s 안" in c for c in b["bms"].checks)
    assert "clock_mismatch" not in _codes(first)
    assert any("Orion BMS Connect 기준 10:00:00 ~ 10:04:10" in n for n in first.notes)
    assert [x.key for x in second.blocks] == ["battery", "bms", "motor"]             # STM32 만 있는 구간 — 셋 다 STM32
    assert second.start == T0 + pd.Timedelta(seconds=1000) and second.duration_s == pytest.approx(200.0)
    p = _ch(second, "battery", "power_kw")
    assert second.stats["net_wh"] == pytest.approx(float(np.trapezoid(p.v, p.t_s) / 3.6), abs=0.5)


def test_a_wrong_clock_offset_is_flagged(tmp_path):
    """에너지미터 시계 차를 3 s 틀리게 적으면 Orion 전류와 어긋나 경고가 붙는다 (STM32 는 같은 시계를 따르므로 그대로 겹친다)."""
    s = cb.build_event(write_event(tmp_path, em_offset=-EM_AHEAD_S + 3))[0]
    bms = _blocks(s)["bms"]
    assert [f["code"] for f in bms.findings] == ["clock_mismatch"] and any("허용 2 s 를 넘는다" in c for c in bms.checks)
    assert not _blocks(s)["motor"].findings
    fixed = cb.build_event(write_event(tmp_path / "b", em_offset=-EM_AHEAD_S + 3,
                                       extra_notes=f"[clocks.energymeter.recordings]\nem1 = {-EM_AHEAD_S}\n"))[0]
    assert "clock_mismatch" not in _codes(fixed)          # 레코딩마다 따로 적은 값이 먼저다 — 그 미터에 맞춰 둔 STM32 도 따른다


@pytest.mark.parametrize("change, message", [
    (lambda t: t.replace('clock = "orion"\n', ""), "clock"),
    (lambda t: t.replace("[clocks.energymeter]\noffset_s = -180.0\n", "[clocks.energymeter]\n"), "옮길 값이 없다"),
    (lambda t: t + '\n[[timed_session]]\nstart = "2027-06-05 10:03:00"\nend = "2027-06-05 10:05:00"\ndiscipline = "x"\n', "겹친다"),
    (lambda t: t + '\n[[timed_session]]\nstart = "2027-06-05 12:00:00"\nend = "2027-06-05 12:05:00"\ndiscipline = "x"\n', "기록이 남은 장비가 없다"),
    (lambda t: t + '\n[[timed_session]]\nstart = "2027-06-05 12:00:00"\ndiscipline = "x"\n', "start · end"),
    (lambda t: t + '\n[[timed_session]]\nstart = "2027-06-05 12:05:00"\nend = "2027-06-05 12:00:00"\ndiscipline = "x"\n', "앞이다"),
    (lambda t: t + '\n[[timed_session]]\nstart = "2027-06-05 12:00:00"\nend = "2027-06-05 12:05:00"\n', "name 이나 discipline"),
])
def test_notes_mistakes_stop_the_build(tmp_path, change, message):
    """잘못 적은 메모는 말없이 넘어가지 않는다 — 시계 · 겹침 · 빈 구간 · 빠진 칸."""
    ev = write_event(tmp_path)
    path = ev / cb.NOTES_NAME
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        cb.build_event(ev)


def test_an_empty_window_needs_allow_empty(tmp_path):
    ev = write_event(tmp_path, extra_notes='\n[[timed_session]]\nstart = "2027-06-05 12:00:00"\nend = "2027-06-05 12:05:00"\n'
                                          'discipline = "연습 주행"\nallow_empty = true\n')
    empty = cb.build_event(ev)[-1]
    assert empty.blocks == [] and any("기록이 남은 장비가 없다" in n for n in empty.notes)


@pytest.fixture()
def timed_store(tmp_path):
    ev = write_event(tmp_path)
    root = tmp_path / "store"
    rows = store.build_event(ev, root, tmp_path)
    api.configure(store_root=root, data_root=tmp_path)
    yield rows
    api.configure()


def test_store_and_api_answer_a_session_without_gps(timed_store):
    """랩 · 코스가 없는 세션 — 목록 · 세션 · 채널 · 시간축 그래프는 그대로, 랩 · 구간은 비고, 코스 · 거리축은 bad_argument."""
    sid = timed_store[0]["sid"]
    row = next(r for r in api.list_sessions() if r["sid"] == sid)
    assert row["split_by"] == "time" and row["laps"] is None and row["distance_km"] is None and row["frame_id"] is None
    assert row["blocks"]["have"] == ["battery", "motor", "bms"]
    s = json.loads(json.dumps(api.get_session(sid), allow_nan=False))
    assert s["best_lap"] is None and [b["block"] for b in s["blocks"]] == ["battery", "motor", "bms"]
    ser = api.get_series(sid, ["battery.current_a", "bms.soc_pct"], max_points=500)
    assert ser["laps"] == [] and len(ser["x"]["values"]) == 500
    laps = api.get_laps(sid)
    assert laps["rows"] == [] and laps["sectors"] is None
    sec = api.get_sectors(sid)
    assert sec["frame_id"] is None and sec["sectors"] == [] and sec["laps"] == []
    for call in (lambda: api.get_track(sid), lambda: api.get_track(sid, lap="all"),
                 lambda: api.get_series(sid, ["battery.current_a"], x="distance", lap=1), lambda: api.get_delta(sid, 1, sid, 1)):
        with pytest.raises(EngineError) as e:
            call()
        assert e.value.code == "bad_argument"
    assert all(f["code"] != "clock_mismatch" for f in api.get_quality(sid))
    assert not list((api._root() / store.FRAMES).glob("*.json"))                   # 랩이 없으니 frame 도 없다


# ---------------------------------------------------------------- 우리 데이터 — LEF-26 테스트위크 (보편)

L26 = paths.LEF26_EVENT
need26 = pytest.mark.skipif(not (L26 / cb.NOTES_NAME).exists() or not (L26 / "derived" / "logger_clock_pairs.csv").exists(),
                            reason="LEF-26 테스트위크 catalog_notes.toml · lef26-clock 결과 없음")


@pytest.fixture(scope="module")
def s26():
    return cb.build_event(L26)


def _logger_mode(s):
    rec = _blocks(s)["motor"].series[0].source.split(", ")[0]
    return json.loads((L26 / "normalized" / rec / "meta.json").read_text(encoding="utf-8"))["time"]["clock"]["mode"]


def _same_window(gps_session):
    """GPS 세션과 같은 시각 구간을 로거 시계(변환기가 바로잡은 t_abs) 기준으로 다시 만든다."""
    notes = {"event": {**cb.load_notes(L26).get("event", {}), "clock": "lef26_logger"},
             "timed_session": [{"start": gps_session.start.isoformat(),
                                "end": (gps_session.start + pd.Timedelta(seconds=gps_session.duration_s)).isoformat(),
                                "discipline": gps_session.discipline}]}
    return cb.timed_sessions(L26, notes)[0]


def _lag(a, b):
    """두 채널이 가장 잘 겹치는 어긋남 → (s, 상관). a 에 더해야 b 와 겹친다."""
    at, av = cb._smoothed(a.t_s, a.v)
    bt, bv = cb._smoothed(b.t_s, b.v)
    lag, _, c = cb.best_lag(at, av, bt, bv)
    return lag, c


@need26
def test_logger_time_window_matches_the_video_session(s26):
    """GPS 가 매초 로거 시계를 맞춘 날 — 로거 시각으로 자른 세션이 영상 GPS 로 맞춘 세션과 겹친다.
    로거 가속도계 · 자이로(차량 축으로 바꾼 것)는 카메라 값과 같은 부호로 같이 움직인다."""
    track = [s for s in s26 if s.discipline == "트랙" and "motor" in _blocks(s) and _logger_mode(s) == "gps_pps"]
    assert track
    g = max(track, key=lambda s: s.duration_s)
    t = _same_window(g)
    assert t.split_by == "time" and {"motor", "bms", "thermistor", "imu"} <= set(_blocks(t))
    lag, c = _lag(_ch(t, "motor", "rpm"), _ch(g, "motor", "rpm"))
    assert abs(lag) <= 0.2 and c > 0.99, (lag, c)
    for key in ("long_g", "lat_g", "yaw_dps"):                                        # 로거 축 ↔ 카메라 축 (서로 다른 센서)
        lag, c = _lag(_ch(t, "imu", key), _ch(g, "imu", key))
        assert abs(lag) <= 0.3 and c > 0.9, (key, lag, c)


@need26
@E.need
def test_a_free_running_logger_clock_is_seconds_off(s26):
    """GPS PPS 없이 RTC 가 혼자 돈 레코딩 — 로거 시각 그대로 자르면 몇 초 어긋난다 (트랙데이에 폰 시각 · 영상이 필요한 이유)."""
    g = next(s for s in s26 if s.sid == E["lef26.free_sid"])
    assert _logger_mode(g) == "free"
    lag, c = _lag(_ch(_same_window(g), "imu", "yaw_dps"), _ch(g, "imu", "yaw_dps"))
    assert c > 0.9 and lag == E.approx("lef26.free_lag_s")


# ---------------------------------------------------------------- 우리 데이터 — 25Evo 대회 (골든)

EVO = paths.LEF25EVO_FSK
need_evo = pytest.mark.skipif(not (EVO / cb.NOTES_NAME).exists(), reason="25Evo 대회 catalog_notes.toml 없음")


@pytest.fixture(scope="module")
def sevo():
    return cb.build_event(EVO)


@need_evo
def test_evo_race_sessions_keep_their_promises(sevo):
    """GPS 없는 세션의 약속 — 모든 표본이 세션 안, 에너지 = 전력의 시간 적분, 장비끼리 시계가 어긋나 보이지 않는다.
    같은 이벤트의 GPS 세션(Monolith 가 있는 전원 주기)도 배터리는 에너지미터, STM32 는 그 미터를 거쳐 GPS 시계로 옮겨져 있다."""
    timed = [s for s in sevo if s.split_by == "time"]
    assert timed and all(s.track is None and s.laps.empty for s in timed)
    for s in sevo:
        for b in s.blocks:
            for c in b.series or b.channels:
                assert np.min(c.t_s) >= 0 and np.max(c.t_s) <= s.duration_s + 1e-6, (s.sid, b.key, c.key)
        p = _ch(s, "battery", "power_kw")
        ok = np.isfinite(p.v)
        assert s.stats["net_wh"] == pytest.approx(np.trapezoid(p.v[ok], p.t_s[ok]) / 3.6, abs=max(2.0, 0.01 * abs(s.stats["net_wh"])))
        assert "clock_mismatch" not in _codes(s), s.sid
    for s in (s for s in sevo if s.split_by == "gps"):
        assert "에너지미터" in _blocks(s)["battery"].device and "에너지미터 전력(같은 물리량)" in _blocks(s)["motor"].checks[0], s.sid


@need_evo
@E.need
def test_evo_race_golden(sevo):
    timed = [s for s in sevo if s.split_by == "time"]
    assert len(timed) == E["evo_race.sessions"] and [s.name for s in timed] == E["evo_race.names"]
    assert all(list(_blocks(s)) == E["evo_race.blocks"] for s in timed)
    s = next(x for x in sevo if x.sid == E["evo_race.endurance_sid"])
    assert store.summary(s)["start"] == E["evo_race.endurance_start"]
    assert s.stats["net_wh"] == E.approx("evo_race.endurance_net_wh")
    facts = _blocks(s)["bms"].facts
    assert E["evo_race.endurance_soc_fact"] in facts and E["evo_race.endurance_hot_fact"] in facts
    assert any(E["evo_race.stm32_lag_text"] in c for c in _blocks(s)["motor"].checks if "에너지미터 전력과 대 봄" in c)
