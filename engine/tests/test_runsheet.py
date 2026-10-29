"""일정표(Run Sheet, `lefdata.runsheet`) — 만든 GPS 이벤트 위에 (데이터 없이).

원 코스 이벤트 (test_unclassified): 10:00:30 부터 다섯 바퀴(트랙) → 10:03:16 쯤 300° (미분류) → 피트 이동(뺌). GPS 시각 = 참 시각.
일정표는 폰 시각이라 GPS 보다 4 s 늦게 적혔다고 둔다. 에너지미터는 전원 켬 183 s 뒤 시각으로 켜진다 (미터 시계가 앞서 있다).
"""

import json

import pandas as pd
import pytest

from lefdata import catalog_build as cb, runsheet, session_store as store
from test_unclassified import T0, write_event

DAY = "2027-06-05"
CSV = """차,R번호,전원 켬,출발,도착,종목,드라이버,목적,세팅 바뀜,메모
LEF-27,1,09:58:00,10:00:26,10:02:42,연습,A,토크 맵 100 %,앞 댐퍼 +2,
27,2,,10:03:12,10:03:35,시험 주행,driver_2,,,
LEF-27,3,10:08:00,10:10:00,10:15:00,가속,홍길동,,,차가 안 나감
LEF-26,1,,10:00:26,10:02:42,연습,B,,,
"""


def with_meter(ev, start):
    """에너지미터 레코딩 하나 (meta 만 — 전원 켬과 대 보는 데는 시작 시각만 쓴다)."""
    d = ev / "normalized" / f"{start:%Y-%m-%d_%H%M%S}_em"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"source": {"logger": "FSK 에너지미터 (FSK-EEM)"},
                                             "time": {"t_abs_start": start.isoformat(), "duration_s": 900.0}}, ensure_ascii=False), encoding="utf-8")


def event(tmp_path, csv=CSV, notes=""):
    ev = write_event(tmp_path, notes=notes)
    (ev / f"runsheet_{DAY}.csv").write_text(csv, encoding="utf-8")
    with_meter(ev, pd.Timestamp(f"{DAY} 09:58:00", tz="Asia/Seoul") + pd.Timedelta(seconds=183))
    return ev


def _codes(s):
    return [f["code"] for b in s.blocks for f in b.findings]


def test_rows_name_the_sessions_and_the_plan_is_checked(tmp_path):
    report = {}
    s1, s2 = cb.build_event(event(tmp_path), report)
    assert (s1.name, s1.discipline, s1.driver, s1.run_no, s1.purpose) == ("R1 연습", "연습", "드라이버 A", "1", "토크 맵 100 %")
    assert s1.notes[0].startswith("일정표 R1 — 출발 10:00:26 · 도착 10:02:42 (폰 시각, 전원 켬 09:58:00)") and "세팅 바뀜: 앞 댐퍼 +2" in s1.notes
    assert "runsheet_mismatch" not in _codes(s1)                                   # '연습' 은 데이터 종목(트랙)과 대 보지 않는다
    assert (s2.name, s2.discipline, s2.driver) == ("R2 시험 주행", "시험 주행", "드라이버 B") and "unclassified_run" not in _codes(s2)   # 미분류가 풀린다
    c = report["runsheet"]
    assert c["rows"] == 3 and c["offset_s"] == pytest.approx(4.0, abs=0.5) and len(c["matched"]) == 2           # 다른 차의 줄은 세지 않는다
    assert [r["run_no"] for r in c["unmatched_rows"]] == ["3"] and c["unplanned_sessions"] == []
    assert any("실명" in p for p in c["problems"])                                 # 드라이버 칸의 실명은 넣지 않는다
    [p] = c["power_on"]
    assert (p["device"], p["pairs"], p["device_minus_phone_s"]) == ("energymeter", 1, 183.0)
    text = runsheet.check_text(c)
    assert any("세션이 없음: R3" in t for t in text) and any("에너지미터 기록 시작" in t for t in text)
    assert (tmp_path / "LEF-27" / "2027-06-05_test" / "derived" / "runsheet_check.csv").exists()


def test_a_contradicting_discipline_warns_and_a_missing_row_is_flagged(tmp_path):
    csv = "차,R번호,출발,도착,종목\nLEF-27,1,10:00:26,10:02:42,가속\n"
    report = {}
    s1, s2 = cb.build_event(event(tmp_path, csv), report)
    assert s1.discipline == "가속" and "runsheet_mismatch" in _codes(s1)          # 데이터로는 트랙 (완주 랩이 있다)
    assert s2.discipline == cb.UNCLASSIFIED and "runsheet_unplanned" in _codes(s2) and len(report["runsheet"]["unplanned_sessions"]) == 1


def test_print_form_columns_become_notes(tmp_path):
    """인쇄 양식의 칸(전원 끔 · 장비 확인 · 사건 · 직후 측정)도 세션 메모로 — 종이에 적은 것이 시트로 옮기며 사라지지 않게."""
    csv = ("차,R번호,전원 켬,출발,도착,전원 끔,종목,드라이버,목적,세팅 바뀜,장비 확인,사건,직후 측정,메모\n"
           "LEF-27,1,09:58:00,10:00:26,10:02:42,10:05:10,연습,A,,,Monolith ✓ · GoPro ✓,2랩 스핀,디스크 FL 310,\n")
    report = {}
    s1, _ = cb.build_event(event(tmp_path, csv), report)
    assert "전원 끔 10:05:10" in s1.notes[0] and {"장비 확인: Monolith ✓ · GoPro ✓", "사건: 2랩 스핀", "직후 측정: 디스크 FL 310"} <= set(s1.notes)
    assert report["runsheet"]["problems"] == []


def test_hand_written_sessions_come_first(tmp_path):
    notes = '[[session]]\nrecording = "2027-06-05_gps"\nrun = 1\nname = "내구 연습"\ndiscipline = "내구"\ndriver = "드라이버 B"\n'
    s1, _ = cb.build_event(event(tmp_path, notes=notes))
    assert (s1.name, s1.discipline, s1.driver, s1.run_no, s1.purpose) == ("내구 연습", "내구", "드라이버 B", "1", "토크 맵 100 %")


def test_store_summary_carries_run_number_and_purpose(tmp_path):
    rows = store.build_event(event(tmp_path), tmp_path / "store", tmp_path)
    assert [(r["run_no"], r["purpose"]) for r in rows] == [("1", "토크 맵 100 %"), ("2", None)]


def test_reading_rules():
    assert [runsheet.driver_of(x) for x in ("A", "b", "드라이버 C", "driver_1", "2", "", "홍길동")] == [
        "드라이버 A", "드라이버 B", "드라이버 C", "드라이버 A", "드라이버 B", "", None]
    assert [runsheet.car_of(x, "LEF-27") for x in ("", "26", "LEF-25Evo", "모름")] == ["LEF-27", "LEF-26", "LEF-25evo", None]
    assert [runsheet.kind_of(x) for x in ("가속 연습", "스키드패드", "내구 1", "연습")] == ["가속", "스키드패드", "트랙", None]


# ---------------------------------------------------------------- 짝 없는 줄 → 폰 시각 구간 세션 (GPS 없는 날)

def meter_with_drive(ev, meter_start, drive=(60.0, 240.0), amps=50.0, span_s=600.0):
    """진짜 에너지미터 기록 (lefdata energymeter 로 변환) — 미터 경과시간 drive 동안만 amps (280 V). meter_start 는 미터 시계."""
    import numpy as np
    from lefdata import energymeter as em
    from test_ingest import _xor_fill
    raw_dir = ev / "raw_energymeter"
    raw_dir.mkdir(parents=True, exist_ok=True)
    n = int(span_s * 100)
    head = np.zeros(32, "u1")
    head[0], head[1], head[20] = em.MAGIC, em.T_HEADER, 1
    head[24:30] = [meter_start.year - 2000, meter_start.month, meter_start.day, meter_start.hour, meter_start.minute, meter_start.second]
    _xor_fill(head)
    rec = np.zeros(n, em.RECORD)
    t_ms = np.arange(n) * 10
    rec["magic"], rec["type"], rec["t_ms"] = em.MAGIC, em.T_RECORD, t_ms
    on = (t_ms >= drive[0] * 1000) & (t_ms < drive[1] * 1000)
    rec["ch"][:, 0], rec["ch"][:, 1], rec["ch"][:, 2], rec["ch"][:, 3] = 2800, np.where(on, int(amps * 10), 0), 1200, 2500
    raw = rec.view("u1").reshape(n, 16)
    for r in raw:
        _xor_fill(r)
    (raw_dir / f"{meter_start:%Y-%m-%d-%H-%M-%S}-000_test.log").write_bytes(head.tobytes() + raw.tobytes())
    em.convert_event(ev)


def no_gps_event(tmp_path, csv, notes=""):
    ev = tmp_path / "LEF-27" / "2027-06-05_test"
    ev.mkdir(parents=True)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-27"\nsessions = "auto"\n' + notes, encoding="utf-8")
    (ev / f"runsheet_{DAY}.csv").write_text(csv, encoding="utf-8")
    # 전원 켬 10:00:00 (폰) — 미터는 그 순간 켜졌지만 시계가 183 s 앞서 10:03:03 으로 적었다. 주행은 켠 뒤 60 ~ 240 s (폰 10:01 ~ 10:04)
    meter_with_drive(ev, pd.Timestamp(f"{DAY} 10:03:03", tz="Asia/Seoul"))
    return ev


SHEET = "차,R번호,전원 켬,출발,도착,종목,드라이버,목적\nLEF-27,1,10:00:00,10:01:00,10:04:00,연습,A,회생 끔\nLEF-27,2,,10:20:00,10:22:00,연습,B,\n"


def test_a_row_without_gps_becomes_a_time_window_session(tmp_path):
    """GPS 가 없는 날 — 일정표 줄이 폰 시각 구간 세션이 되고, 에너지미터는 전원 켬으로 잰 시계 차(183 s)로 옮겨 잘린다."""
    report = {}
    [s] = cb.build_event(no_gps_event(tmp_path, SHEET), report)
    assert (s.split_by, s.run_no, s.discipline, s.driver, s.purpose, s.name) == ("time", "1", "연습", "드라이버 A", "회생 끔", "R1 연습")
    assert s.start == pd.Timestamp(f"{DAY} 10:01:00", tz="Asia/Seoul") and s.duration_s == pytest.approx(180.0)
    assert s.stats["net_wh"] == pytest.approx(280 * 50 * 180 / 3600, rel=0.02)              # 주행 180 s 가 통째로 들어왔다 (시계가 맞았다)
    bat = next(b for b in s.blocks if b.key == "battery")
    assert any("-183.0 s" in c and "전원 켬" in c for c in bat.checks)
    assert "runsheet_timed" in _codes(s)
    c = report["runsheet"]
    assert [m["run_no"] for m in c["timed_sessions"]] == ["1"] and c["power_on"][0]["recordings"] == {"2027-06-05_100303_em": 183.0}
    [left] = c["unmatched_rows"]
    assert left["run_no"] == "2" and "기록이 남은 장비가 없다" in left["why"]
    assert any("시각 구간 세션으로 만듦" in line for line in runsheet.check_text(c))


def test_clock_written_by_hand_comes_first(tmp_path):
    """catalog_notes.toml 에 폰 기준으로 적은 시계 차가 전원 켬으로 잰 값보다 먼저 — 3 s 다르게 적으면 주행 3 s 가 구간 밖으로 밀린다."""
    ev = no_gps_event(tmp_path, SHEET)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-27"\nsessions = "auto"\nclock = "phone"\n[clocks.energymeter]\noffset_s = -180.0\nbasis = "사람이 잼"\n',
                                    encoding="utf-8")
    [s] = cb.build_event(ev)
    bat = next(b for b in s.blocks if b.key == "battery")
    assert any("-180.0 s" in c and "사람이 잼" in c for c in bat.checks)
    assert s.stats["net_wh"] == pytest.approx(280 * 50 * 177 / 3600, rel=0.02)


def test_a_row_overlapping_a_session_is_not_built_again(tmp_path):
    """이미 세션이 있는 시각의 줄(짝은 못 지었지만 겹친다)은 새로 만들지 않고 이유를 남긴다."""
    csv = "차,R번호,출발,도착,종목\nLEF-27,1,10:00:26,10:02:42,연습\nLEF-27,9,10:02:20,10:03:00,연습\n"      # R9 는 R1 세션의 끝에 걸친다
    report = {}
    sessions = cb.build_event(event(tmp_path, csv), report)
    assert all(s.split_by == "gps" for s in sessions) and [s.run_no for s in sessions] == ["1", ""]
    [nine] = [u for u in report["runsheet"]["unmatched_rows"] if u["run_no"] == "9"]
    assert "겹친다" in nine["why"] and report["runsheet"]["timed_sessions"] == []
