"""자동 세션의 미분류 · 뺀 주행 · 사람이 정리하기 (`catalog_build._auto_sessions`) — 만든 GPS 로 (데이터 없이).

반지름 60 m 원 코스: 다섯 바퀴(트랙) → 계측선을 다 돌지 않고 300° 만 돈 주행(완주 랩 없음 → 미분류) → 15 km/h 로 100 m 피트 이동(뺌).
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, laps, session_store as store

R, LAT0, LON0 = 60.0, 37.5, 127.0
T0 = pd.Timestamp("2027-06-05 10:00:00", tz="Asia/Seoul")
LAP_S = 2 * np.pi * R / (50 / 3.6)


def _leg(ang0, deg, kmh, dt=0.1):
    """원 위를 ang0 부터 deg 도 만큼 kmh 로 — (각도들, 속도들)."""
    n = max(2, int(np.radians(deg) * R / (kmh / 3.6) / dt))
    return np.linspace(ang0, ang0 + np.radians(deg), n), np.full(n, kmh)


def _stand(ang, s, dt=0.1):
    n = int(s / dt)
    return np.full(n, ang), np.zeros(n)


def write_event(root, legs=("laps", "partial", "pit"), notes=""):
    parts = [_stand(0.0, 30)]
    ang = 0.0
    for leg in legs:
        a, v = {"laps": lambda: _leg(ang, 5 * 360, 50), "partial": lambda: _leg(ang, 300, 50), "pit": lambda: _leg(ang, 100 / R * 180 / np.pi, 15)}[leg]()
        parts += [(a, v), _stand(a[-1], 30)]
        ang = a[-1]
    a = np.concatenate([p[0] for p in parts])
    v = np.concatenate([p[1] for p in parts])
    t = np.arange(len(a)) * 0.1
    x, y = R * np.sin(a), -R * np.cos(a)
    ev = root / "LEF-27" / "2027-06-05_test"
    rec = ev / "normalized" / "2027-06-05_gps"
    rec.mkdir(parents=True)
    pd.DataFrame({"t_rel_ms": (t * 1000).astype("int64"), "t_abs": T0 + pd.to_timedelta(t, unit="s"), "fix": True,
                  "lat": LAT0 + y / 110_950.0, "lon": LON0 + x / (np.cos(np.radians(LAT0)) * 111_320.0), "speed_kmh": v,
                  "course_deg": (90.0 - np.degrees(a)) % 360.0}).to_parquet(rec / "gps.parquet", index=False)
    (rec / "meta.json").write_text(json.dumps({"car": "LEF-27", "label": "practice", "source": {"logger": "GoPro 온보드 영상 (GPMF)", "file": "raw/x.mp4"},
                                               "time": {"t_abs_start": T0.isoformat(), "duration_s": float(t[-1])}}, ensure_ascii=False), encoding="utf-8")
    laps.run_event(ev)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-27"\nsessions = "auto"\n' + notes, encoding="utf-8")
    return ev


def _runs(ev):
    return pd.read_csv(ev / "derived" / "runs.csv", encoding="utf-8-sig")


def test_a_lap_without_the_line_is_unclassified_and_the_pit_move_is_reported(tmp_path):
    ev = write_event(tmp_path)
    runs = _runs(ev)
    assert len(runs) == 3 and list(runs["full_laps"] > 0) == [True, False, False]
    report = {}
    sessions = cb.build_event(ev, report)
    assert [s.discipline for s in sessions] == ["트랙", cb.UNCLASSIFIED] and sessions[1].name == "미분류 1"
    found = [f for b in sessions[1].blocks for f in b.findings if f["code"] == "unclassified_run"]
    assert len(found) == 1 and found[0]["level"] == "warn" and "코스의 8" in found[0]["message"]           # 300° ≈ 코스의 83 %
    assert not any(f["code"] == "unclassified_run" for b in sessions[0].blocks for f in b.findings)
    [d] = report["dropped_runs"]
    assert (d["run"], d["cover"] < cb.UNCLASSIFIED_COVER, d["vmax_kmh"]) == (3, True, 15.0) and "피트 이동" in d["why"]


def test_people_decide_with_session_notes(tmp_path):
    """미분류는 사람이 [[session]] 으로 종목을 정하거나 skip 으로 뺀다. 피트 이동도 이름을 적으면 넣는다."""
    ev = write_event(tmp_path, notes='[[session]]\nrecording = "2027-06-05_gps"\nrun = 2\ndiscipline = "시험 주행"\n'
                                      '[[session]]\nrecording = "2027-06-05_gps"\nrun = 3\ndiscipline = "피트 점검"\n')
    report = {}
    assert [s.discipline for s in cb.build_event(ev, report)] == ["트랙", "시험 주행", "피트 점검"] and report["dropped_runs"] == []
    ev2 = write_event(tmp_path / "b", notes='[[session]]\nrecording = "2027-06-05_gps"\nrun = 2\nskip = true\n')
    report = {}
    assert [s.discipline for s in cb.build_event(ev2, report)] == ["트랙"]
    assert [(d["run"], "skip" in d["why"]) for d in report["dropped_runs"]] == [(2, True), (3, False)]


def test_without_a_course_distance_and_speed_decide(tmp_path):
    """완주 랩이 하나도 없으면 코스를 모른다 — 달린 거리 · 최고 속도로 가른다 (300° = 314 m · 50 km/h 는 뺀다)."""
    ev = write_event(tmp_path, legs=("partial", "pit"))
    report = {}
    assert cb.build_event(ev, report) == []
    assert [(d["run"], d["cover"]) for d in report["dropped_runs"]] == [(1, None), (2, None)]
    assert all("코스를 몰라" in d["why"] for d in report["dropped_runs"])


def test_the_store_lists_unclassified_with_a_warning(tmp_path):
    ev = write_event(tmp_path)
    report = {}
    rows = store.build_event(ev, tmp_path / "store", tmp_path, report=report)
    assert [r["discipline"] for r in rows] == ["트랙", "미분류"] and len(report["dropped_runs"]) == 1
    doc = json.loads((tmp_path / "store" / rows[1]["sid"] / "session.json").read_text(encoding="utf-8"))
    assert [f["code"] for f in doc["findings"]] == ["unclassified_run"]
