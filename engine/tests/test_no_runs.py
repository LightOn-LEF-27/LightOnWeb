"""주행이 하나도 없는 이벤트 — GPS 는 잡혔지만 차가 서 있기만 한 기록 (정지 상태로 장비를 확인하는 날). 만든 GPS 로 (데이터 없이).

랩 분할 · 종목 분석 · 세션 만들기가 죽지 않고 "주행 없음 · 세션 없음" 으로 끝나야 한다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, events, laps, session_store as store, venue
from test_venue import write_event as circle_event

T0 = pd.Timestamp("2027-06-05 10:00:00", tz="Asia/Seoul")


def still_event(root, name: str = "2027-06-05_still", notes: str = ""):
    """10 분 동안 한 자리에 서 있는 GPS 레코딩 하나 (속도 0.3 km/h — GPS 잡음 수준)."""
    ev = root / "LEF-27" / name
    rec = ev / "normalized" / "2027-06-05_gps"
    rec.mkdir(parents=True)
    t = np.arange(0.0, 600.0, 0.2)
    pd.DataFrame({"t_rel_ms": (t * 1000).astype("int64"), "t_abs": T0 + pd.to_timedelta(t, unit="s"), "fix": True,
                  "lat": np.full(len(t), 37.5), "lon": np.full(len(t), 127.0), "speed_kmh": np.full(len(t), 0.3),
                  "course_deg": np.zeros(len(t))}).to_parquet(rec / "gps.parquet", index=False)
    (rec / "meta.json").write_text(json.dumps({"car": "LEF-27", "label": None, "source": {"logger": "GoPro 온보드 영상 (GPMF)", "files": ["GOPR0001.MP4"]},
                                               "time": {"t_abs_start": T0.isoformat(), "duration_s": 600.0}}, ensure_ascii=False), encoding="utf-8")
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-27"\nsessions = "auto"\n' + notes, encoding="utf-8")
    return ev


def _tables(ev):
    d = ev / "derived"
    return (pd.read_csv(d / "runs.csv", encoding="utf-8-sig"), pd.read_csv(d / "laps.csv", encoding="utf-8-sig"),
            json.loads((d / "gate.json").read_text(encoding="utf-8")))


def test_lap_split_writes_empty_tables_with_headers(tmp_path):
    ev = still_event(tmp_path)
    runs, lp, gate = laps.run_event(ev)
    assert len(runs) == 0 and len(lp) == 0 and gate is None
    again, again_laps, doc = _tables(ev)                                   # 머리줄이 있어야 다음 단계가 다시 읽는다
    assert list(again.columns) == laps.RUN_COLUMNS and len(again) == 0 and len(again_laps) == 0
    assert doc["gate"] is None and doc["origin"] == {"lat0": 37.5, "lon0": 127.0}


def test_the_steps_after_the_lap_split_end_with_no_sessions(tmp_path):
    ev = still_event(tmp_path)
    laps.run_event(ev)
    accel, skid, arcs = events.run_event(ev)
    assert accel.empty and skid.empty and arcs.empty
    report = {}
    assert cb.build_event(ev, report) == [] and report["dropped_runs"] == []
    assert store.build_event(ev, tmp_path / "_sessions", tmp_path) == []


def test_with_a_venue_gate_it_also_ends_with_no_sessions(tmp_path):
    """경기장 계측선을 쓰는 이벤트 — 계측선은 있지만 지나간 적이 없다."""
    first = circle_event(tmp_path, "ev_a")
    laps.run_event(first)
    doc = venue.create_from_event("testtrack", first, tmp_path / "_venues")
    ev = still_event(tmp_path, notes='venue = "testtrack"\n')
    runs, lp, gate = laps.run_event(ev, doc)
    assert len(runs) == 0 and len(lp) == 0 and gate.x == doc["gate"]["x"]
    assert list(_tables(ev)[0].columns) == laps.RUN_COLUMNS
    assert store.build_event(ev, tmp_path / "_sessions", tmp_path) == []


def test_an_old_empty_runs_file_is_read_as_no_runs(tmp_path):
    """머리줄 없이 쓴 빈 runs.csv (전에 만든 결과) 가 남아 있어도 세션 만들기가 죽지 않는다."""
    ev = still_event(tmp_path)
    laps.run_event(ev)
    (ev / "derived" / "runs.csv").write_text("\n", encoding="utf-8-sig")
    assert cb.build_event(ev) == []


def test_an_event_without_any_gps_recording_says_so(tmp_path):
    ev = tmp_path / "LEF-27" / "2027-06-05_none"
    (ev / "normalized").mkdir(parents=True)
    with pytest.raises(ValueError, match="GPS 레코딩이 없다"):
        laps.run_event(ev)
