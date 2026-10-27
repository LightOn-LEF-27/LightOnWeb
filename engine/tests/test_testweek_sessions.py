"""테스트위크 세션 (E3 — 트랙데이 대비) — 차량 로거 · 백업 로거를 GPS 세션에 붙인 결과와 경기장 계측선.

트랙데이와 장비 조합이 같은 8월 테스트위크(같은 곳)로 확인한다. 데이터 · catalog_notes.toml 이 없으면 건너뛴다.
약속(모양 · 불변식)은 코드에, 우리 데이터의 값은 기대값 파일 `testweek_sessions.toml` 에.
"""

import numpy as np
import pytest

from expected import Expected
from lefdata import catalog_build as cb, laps, paths, session_store as store, venue

E = Expected("testweek_sessions")
L26, EVO = paths.LEF26_EVENT, paths.LEF25EVO_EVENT
need26 = pytest.mark.skipif(not (L26 / cb.NOTES_NAME).exists() or not (L26 / "derived" / "logger_clock_pairs.csv").exists(),
                            reason="LEF-26 테스트위크 catalog_notes.toml · lef26-clock 결과 없음")
need_evo = pytest.mark.skipif(not (EVO / cb.NOTES_NAME).exists() or not (EVO / "derived" / "stm32_alignment.csv").exists(),
                              reason="25Evo 테스트위크 catalog_notes.toml · evo-stm32 결과 없음")
DISCIPLINES = {"트랙", "가속", "스키드패드"}


@pytest.fixture(scope="module")
def s26():
    return cb.build_event(L26)


@pytest.fixture(scope="module")
def sevo():
    return cb.build_event(EVO)


def _blocks(s):
    return {b.key: b for b in s.blocks}


def _channel(s, block, key):
    b = _blocks(s).get(block)
    return None if b is None else next((c for c in b.series if c.key == key), None)


def _inside(s):
    """모든 채널의 표본이 세션 시간 안에 있다 (로거 시각을 세션 시간으로 옮긴 결과)."""
    return all(len(c.t_s) == 0 or (np.min(c.t_s) >= -1e-6 and np.max(c.t_s) <= s.duration_s + 1e-6) for b in s.blocks for c in b.series)


def _rpm_lag(s):
    rpm, speed = _channel(s, "motor", "rpm"), _channel(s, "gps", "speed_kmh")
    if rpm is None or len(rpm.t_s) < 100:
        return None
    return cb.best_lag(rpm.t_s, np.abs(rpm.v), speed.t_s, speed.v)


# ---- 약속 (모양 · 불변식)

@need26
def test_lef26_logger_blocks_are_on_the_session_clock(s26):
    """로거 블록이 붙고, 로거 시각이 세션(영상 GPS) 시간으로 옮겨졌다 — 모터 회전수가 같은 순간의 GPS 속도와 0.5 s 안에서 겹친다."""
    assert {s.discipline for s in s26} <= DISCIPLINES and all(s.sid.startswith("LEF-26_2026-08-1") for s in s26)
    checked = 0
    for s in s26:
        assert _inside(s), s.sid
        b = _blocks(s)
        if "thermistor" in b and len(s.laps):
            assert s.laps["cell_t_max_c"].notna().any()
        if "motor" in b and len(s.laps) and _channel(s, "motor", "inverter_c") is not None:
            assert s.laps["inverter_t_max_c"].notna().any()
        lag = _rpm_lag(s)
        if lag is not None and np.isfinite(lag[2]):
            assert abs(lag[0]) <= 0.5 and lag[2] > 0.85, (s.sid, lag)
            checked += 1
    assert checked >= 3
    rpm = next(_channel(s, "motor", "rpm") for s in s26 if _channel(s, "motor", "rpm") is not None)
    assert rpm.sign.startswith("전진할 때 −")                                   # 전진에서 음수로 기록된다 (GPS 속도와 반대 부호로 상관)


@need26
def test_lef26_sessions_cover_the_three_disciplines(s26):
    accel = [s for s in s26 if s.discipline == "가속"]
    assert accel and all(any(n.startswith("가속 75 m") for n in s.notes) for s in accel)
    skid = [s for s in s26 if s.discipline == "스키드패드"]
    assert skid and all(any("회전 — 원" in n for n in s.notes) for s in skid)
    track = [s for s in s26 if s.discipline == "트랙"]
    assert track and all(len(s.laps) > 0 for s in track) and all(len(s.laps) == 0 for s in accel)


@need_evo
def test_evo_backup_logger_blocks_and_clock_check(sevo):
    """25Evo — STM32 백업 로거가 battery · bms · motor 로 붙고, 엔진이 주행 상태를 추정한다.
    세션 안에서 다시 대 본 시계는 0.5 s 안이거나, 아니면 '어긋났을 수 있다' 고 경고가 붙어 있다."""
    assert {s.discipline for s in sevo} <= DISCIPLINES
    for s in sevo:
        assert _inside(s), s.sid
        b = _blocks(s)
        if "battery" not in b:
            continue
        assert store.drive_block(s) is not None
        lag = _rpm_lag(s)
        if lag is not None and np.isfinite(lag[2]) and abs(lag[0]) > 0.55:
            assert any(f["code"] == "logger_clock_uncertain" for f in b["battery"].findings), (s.sid, lag)
    track = [s for s in sevo if s.discipline == "트랙" and "battery" in _blocks(s)]
    for s in track:                                                           # 랩 에너지 = 평균 전력 × 랩 시간 (같은 값을 두 길로)
        ok = s.laps["net_wh"].notna() & s.laps["p_mean_kw"].notna()
        assert np.allclose(s.laps.loc[ok, "net_wh"], s.laps.loc[ok, "p_mean_kw"] * s.laps.loc[ok, "lap_time_s"] / 3.6, rtol=0.05)


@need26
def test_testweek_runs_on_the_venue_course():
    """트랙데이 대비 — 같은 곳의 다른 이벤트 GPS 가 경기장(대회 계측선) 코스와 같은 코스로 판정된다 (파일은 쓰지 않는다)."""
    try:
        ven = venue.load("yeonggwang")
    except FileNotFoundError:
        pytest.skip("경기장 yeonggwang 이 없음")
    race = (paths.LEF26_FSK / "derived" / "gate.json").read_text(encoding="utf-8")
    assert '"frame_id": "venue/yeonggwang"' in race
    for ev in (L26, EVO):
        _, lp, gate, doc = laps.split_event(ev, ven)
        assert doc["frame_id"] == "venue/yeonggwang" and doc["course_check"]["near_share"] >= laps.COURSE_SHARE and len(lp) > 0


# ---- 골든 (값은 testweek_sessions.toml)

@need26
@E.need
def test_lef26_testweek_golden(s26):
    from collections import Counter
    assert len(s26) == E["lef26.sessions"] and dict(Counter(s.discipline for s in s26)) == E["lef26.by_discipline"]
    s = next(x for x in s26 if x.sid == E["lef26.track_sid"])
    assert float(s.laps["cell_t_max_c"].max()) == E.approx("lef26.track_cell_t_max_c")
    assert float(s.laps["inverter_t_max_c"].max()) == E.approx("lef26.track_inverter_t_max_c")
    assert int(s.laps["lap"].max()) == E["lef26.track_laps"]
    best = min((x for x in s26 if x.discipline == "가속"), key=lambda x: float(x.notes[0].split()[3]))     # "가속 75 m <시간> s …"
    assert best.notes[0].startswith(E["lef26.best_accel_note"])


@need_evo
@E.need
def test_evo_testweek_golden(sevo):
    from collections import Counter
    assert len(sevo) == E["evo.sessions"] and dict(Counter(s.discipline for s in sevo)) == E["evo.by_discipline"]
    moved = [s.sid for s in sevo if "battery" in _blocks(s) and "옮겼다" in _blocks(s)["battery"].checks[0]]
    assert moved == E["evo.clock_moved"]
    s = next(x for x in sevo if x.sid == E["evo.track_sid"])
    assert store.drive_block(s).facts[0].endswith(E["evo.track_drive_fact"])
    assert float(s.laps["net_wh"].sum()) == E.approx("evo.track_net_wh")
