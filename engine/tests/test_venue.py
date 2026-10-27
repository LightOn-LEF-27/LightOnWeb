"""경기장(venue) — 다른 이벤트가 같은 계측선 · 원점으로 랩을 나눈다. 만들어 낸 원형 코스로 돈다 (데이터 없이)."""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import laps, venue

LAT0, LON0 = 37.5, 127.0
R = 60.0


def write_event(root, name, start_deg=0.0, shift_m=(0.0, 0.0), center_m=(0.0, 0.0), venue_name=None):
    """반지름 R 원을 50 km/h 로 다섯 바퀴 (앞뒤로 30 s 씩 서 있다). start_deg = 출발 각도, shift_m = GPS 가 밀린 만큼."""
    ev = root / name
    rec = ev / "normalized" / f"{name}_rec"
    rec.mkdir(parents=True)
    v = 50 / 3.6
    lap_s = 2 * np.pi * R / v
    t = np.arange(0.0, 60 + 5 * lap_s, 0.1)
    ang = np.radians(start_deg) + np.clip(t - 30, 0, 5 * lap_s) * v / R            # 서 있다가 → 다섯 바퀴 → 다시 선다
    x = center_m[0] + R * np.sin(ang) + shift_m[0]
    y = center_m[1] - R * np.cos(ang) + shift_m[1]
    speed = np.where((t > 30) & (t < 30 + 5 * lap_s), 50.0, 0.0)
    course = (90.0 - np.degrees(ang)) % 360.0                      # 반시계로 도는 원의 진행 방위 (북 = 0, 시계 방향)
    lat = LAT0 + y / 110_950.0
    lon = LON0 + x / (np.cos(np.radians(LAT0)) * 111_320.0)
    pd.DataFrame({"t_rel_ms": (t * 1000).astype("int64"), "fix": True, "lat": lat, "lon": lon, "speed_kmh": speed,
                  "course_deg": course}).to_parquet(rec / "gps.parquet", index=False)
    (rec / "meta.json").write_text(json.dumps({"label": "endurance"}), encoding="utf-8")
    if venue_name:
        (ev / "catalog_notes.toml").write_text(f'[event]\nvenue = "{venue_name}"\n', encoding="utf-8")
    return ev


@pytest.fixture()
def made(tmp_path):
    first = write_event(tmp_path, "ev_a")
    laps.run_event(first)
    base = tmp_path / "_venues"
    return tmp_path, base, venue.create_from_event("testtrack", first, base)


def test_venue_from_an_event_keeps_its_laps(made):
    root, base, doc = made
    assert doc["frame_id"] == "venue/testtrack" and doc["source"]["event"].endswith("ev_a")
    assert doc["source"]["line_m"] == pytest.approx(2 * np.pi * R, rel=0.01)
    own = pd.read_csv(root / "ev_a" / "derived" / "laps.csv", encoding="utf-8-sig")
    runs, lp, gate, gd = laps.split_event(root / "ev_a", doc)
    assert gd["frame_id"] == "venue/testtrack" and gd["course_check"]["near_share"] == 1.0
    assert len(lp) == len(own) and np.allclose(lp["lap_time_s"], own["lap_time_s"], atol=1e-3)


def test_another_event_uses_the_venue_gate_and_origin(made):
    """다른 날 · 다른 차 — 출발 각도가 다르고 GPS 가 3 m 밀려 있어도 경기장 계측선으로 나눈다 (원점도 같다)."""
    root, base, doc = made
    other = write_event(root, "ev_b", start_deg=150.0, shift_m=(2.0, -2.0))
    own_runs, own_laps, own_gate, own_doc = laps.split_event(other)
    runs, lp, gate, gd = laps.split_event(other, doc)
    assert (gate.x, gate.y, gate.heading_deg) == (doc["gate"]["x"], doc["gate"]["y"], doc["gate"]["heading_deg"])
    assert gd["origin"] == doc["origin"] and own_doc["origin"] != doc["origin"]
    assert gd["course_check"]["near_share"] == 1.0 and len(lp) >= 4
    assert lp["lap_time_s"].median() == pytest.approx(2 * np.pi * R / (50 / 3.6), rel=0.01)


def test_a_different_course_is_refused(made):
    root, base, doc = made
    far = write_event(root, "ev_far", center_m=(400.0, 0.0))
    far_doc = {**doc, "gate": {**doc["gate"], "x": doc["gate"]["x"] + 400.0}}       # 계측선은 지나지만 (원째로 400 m 옮김) 기준선과 다른 곳
    with pytest.raises(ValueError, match="다른 코스"):
        laps.split_event(far, far_doc)


def test_event_notes_choose_the_venue(made):
    root, base, doc = made
    ev = write_event(root, "ev_noted", venue_name="testtrack")
    assert venue.for_event(ev, base=base)["frame_id"] == "venue/testtrack"
    assert venue.for_event(root / "ev_a", base=base) is None                        # 적지 않으면 이벤트마다 새로
    with pytest.raises(ValueError):
        venue.path("Bad Name")
    with pytest.raises(FileNotFoundError):
        venue.load("nowhere", base)
