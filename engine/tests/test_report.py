"""시험 주행 보고서 검증.

앞쪽은 손으로 채운 자료로 도는 테스트다 — **차량이 바뀌어도 `DayReport` 만 채우면 같은 보고서가 나온다**는 약속을 지킨다.
뒤쪽은 실제 데이터 하루치의 골든 값이다 — 값은 기대값 파일 `report.toml` (`lefdata lef26-laps` 까지 끝나 있어야 돈다).
"""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from expected import Expected
from lefdata import lef26_report as lr, paths, report
from lefdata.report import DayReport, Session

TZ = "Asia/Seoul"
SRC = Path(report.__file__).parent


def ts(hm: str) -> pd.Timestamp:
    return pd.Timestamp(f"2027-06-05 {hm}", tz=TZ)


def hand_filled_day() -> DayReport:
    """LEF-27 이라면 이렇게 채울 것이다: 랩 기록이 있는 주행 하나, 없는 주행 하나, 배터리 추이."""
    theta = np.linspace(0, 2 * np.pi, 400)
    trace = pd.DataFrame({"dist_m": theta * 40, "x_m": 40 * np.sin(theta), "y_m": 40 * (1 - np.cos(theta)),
                          "speed_kmh": 50 + 20 * np.sin(3 * theta), "long_g": 0.5 * np.cos(3 * theta), "lat_g": 1.2 + 0 * theta})
    laps = pd.DataFrame({"lap": [1, 2, 3], "lap_time_s": [31.2, 30.1, 30.6], "vmax_kmh": [68.0, 70.0, 69.5],
                         "soc_start_pct": [90.0, 88.0, 86.0], "soc_end_pct": [88.0, 86.0, 84.0]})
    s1 = Session("S1", "gps", ts("10:00"), ts("10:03"), "rec_a", stats={"laps_n": 3, "best_lap_s": 30.1, "distance_km": 0.9,
                 "vmax_kmh": 70.0, "turns": 3.2, "soc_start_pct": 90.0, "soc_end_pct": 84.0}, laps=laps, best_lap=trace, best_lap_no=2)
    s2 = Session("S2", "logger", ts("10:10"), ts("10:14"), "rec_a", stats={"turns": 4.1, "soc_start_pct": 84.0, "soc_end_pct": 76.0})
    t = pd.date_range(ts("09:58"), ts("10:15"), freq="1s")
    day = DayReport(car="LEF-27", day="2027-06-05", place="시험장", sessions=[s1, s2])
    day.recordings = pd.DataFrame({"lane": ["로거"], "name": ["rec_a"], "start": [ts("09:58")], "end": [ts("10:15")]})
    day.series = {"soc": pd.DataFrame({"t": t, "v": np.linspace(90, 76, len(t))}),
                  "cell_min": pd.DataFrame({"t": t, "v": np.linspace(3.9, 3.7, len(t))}),
                  "cell_max": pd.DataFrame({"t": t, "v": np.linspace(4.0, 3.8, len(t))})}
    rng = np.random.default_rng(0)
    day.gg = {"S1": pd.DataFrame({"long_g": rng.normal(0, 0.4, 500), "lat_g": rng.normal(0, 0.8, 500)})}
    day.kpis = [{"label": "최속 랩", "value": "30.10", "unit": "s", "note": "S1 2랩"}]
    day.facts = ["완주 3랩, 최속 30.10 s"]
    day.quality = [{"item": "위치 기록", "status": report.OK, "value": "1 / 2 구간", "note": ""}]
    day.notes = {"meta": {"purpose": "셰이크다운"}, "summary": {"judgments": ["이상 없음"]},
                 "actions": [{"item": "타이어 공기압 기록", "owner": "DAQ", "due": "다음 주행", "basis": "기록 누락"}]}
    return day


def test_report_comes_out_of_a_hand_filled_dayreport(tmp_path):
    """차량별 코드 없이 DayReport 만으로 문서와 그림이 나온다."""
    path = report.write_report(hand_filled_day(), tmp_path)
    doc = path.read_text(encoding="utf-8")
    figs = sorted(p.stem for p in (tmp_path / "figures").glob("*.png"))
    assert figs == ["g_trace", "gg", "lap_times", "soc", "speed_trace", "timeline", "track_map"]
    for expected in ("LEF-27 시험 주행 결과 보고", "1. 요약", "랩 기록", "배터리", "셰이크다운", "타이어 공기압 기록", "30.10"):
        assert expected in doc
    assert doc.count("<figure") == len(figs)
    assert "그림 7." in doc and "그림 8." not in doc              # 그림 번호는 있는 그림에만 차례로 붙는다
    assert "써미스터별 온도" not in doc and "구동계" not in doc      # 자료가 없는 절은 나오지 않는다
    assert "Pretendard" in doc


def test_unknown_things_are_shown_as_unknown(tmp_path):
    """아무것도 못 채운 날도 문서는 나온다. 모르는 칸은 '기록 없음' 이다 (지어내지 않는다)."""
    path = report.write_report(DayReport(car="LEF-27", day="2027-06-05"), tmp_path)
    doc = path.read_text(encoding="utf-8")
    assert doc.count("기록 없음") >= 8
    assert not list((tmp_path / "figures").glob("*.png"))


def test_notes_file_is_optional(tmp_path):
    assert report.load_notes(tmp_path, "2027-06-05") == {}
    (tmp_path / "report_notes").mkdir()
    (tmp_path / "report_notes" / "2027-06-05.toml").write_text('[meta]\npurpose = "내구 연습"\n', encoding="utf-8")
    assert report.load_notes(tmp_path, "2027-06-05")["meta"]["purpose"] == "내구 연습"


@pytest.mark.parametrize("mod", ["report", "report_charts"])
def test_report_layer_does_not_know_the_car(mod):
    """문서와 그림은 차량별 코드를 import 하지 않는다 — LEF-27 에서 그대로 쓰려면 지켜야 한다."""
    tree = ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(([node.module] if node.module else []) + [a.name for a in node.names])
    assert not {n for n in names if n and n.startswith("lef26")}


def test_driving_is_found_from_lateral_acceleration():
    """정지 60 초 → 코너를 도는 주행 120 초 → 정지 60 초. 주행만 잡는다."""
    t = np.arange(0, 240, 0.01)
    lat = np.where((t >= 60) & (t < 180), 0.9 * np.sin(2 * np.pi * t / 7.0), 0.0) + 0.03       # 센서 치우침이 있어도 된다
    seg = lr.driving_segments(t, lat)
    assert len(seg) == 1
    assert seg[0][0] == pytest.approx(60, abs=8) and seg[0][1] == pytest.approx(180, abs=8)
    assert lr.driving_segments(t, np.full(len(t), 0.03)) == []


def test_pack_voltage_ignores_switch_on_and_off():
    """고전압이 꺼지며 0 V 로 내려가는 동안의 값은 '전압 강하' 가 아니다."""
    t = np.arange(0, 100, 0.1)
    v = np.full(len(t), 300.0)
    v[(t >= 40) & (t < 42)] = 260.0                       # 부하에 의한 전압 강하
    off = t >= 80
    v[off] = np.maximum(300.0 - 60.0 * (t[off] - 80.0), 0.0)     # 80 s 에 차단: 5 초에 걸쳐 0 V 로
    steady, falls = lr._hv_steady(pd.DataFrame({"t_s": t, "value": v}))
    assert steady["value"].min() == 260.0
    assert len(falls) == 1 and falls[0] == pytest.approx(82.5, abs=0.2)      # 150 V 아래로 내려간 순간


# ---------------------------------------------------------------- 골든 (실제 데이터 하루치 — report.toml)

E = Expected("report")
DERIVED = paths.LEF26_EVENT / "derived"
needs_data = pytest.mark.skipif(not (DERIVED / "lap_logger.csv").exists(), reason="derived/lap_logger.csv 없음")


@pytest.fixture(scope="module")
def day():
    return lr.build_day(paths.LEF26_EVENT, E["day"])


@needs_data
@E.need
def test_day_has_drives_with_and_without_laps(day):
    assert [[s.key, s.kind] for s in day.sessions] == E["sessions.keys_kinds"]
    assert [len(s.laps) for s in day.gps_sessions()] == E["sessions.gps_laps"]
    assert sum(s.duration_s for s in day.sessions) / 60 == E.approx("sessions.total_min")
    best = day.sessions[E["best.index"]]
    assert best.stats["best_lap_s"] == E.approx("best.lap_s") and best.best_lap_no == E["best.lap_no"]
    assert best.best_lap["dist_m"].iloc[-1] == E.approx("best.lap_m")
    n_gps = len(E["sessions.gps_laps"])
    assert [s.recording for s in day.sessions[n_gps:]] == E["sessions.logger_only_recordings"]


@needs_data
@E.need
def test_logger_only_drives_match_the_gps_ones_where_both_exist(day):
    """가속도계로 찾은 주행이 GPS 가 본 주행과 같은 구간이고, 요레이트로 센 회전수가 랩 수와 맞는다."""
    laps = E["sessions.gps_laps"]
    for s, n in zip(day.sessions[:len(laps)], laps):
        assert s.stats["turns"] == pytest.approx(n + E["turns.offset"], abs=E["turns.abs"])
    assert sum(s.stats["turns"] for s in day.sessions[len(laps):]) == E.approx("turns.logger_only_total")


@needs_data
@E.need
def test_battery_story_of_the_day(day):
    assert [day.sessions[0].stats["soc_start_pct"], day.sessions[1].stats["soc_end_pct"]] == E["battery.morning_soc"]
    assert [day.sessions[2].stats["soc_start_pct"], day.sessions[-1].stats["soc_end_pct"]] == E["battery.afternoon_soc"]
    assert day.series["cell_min"]["v"].min() == E.approx("battery.cell_min_v")
    assert day.series["batt_high"]["v"].max() == E["battery.batt_high_c"]
    assert any(all(w in f for w in E["battery.facts_contain"]) for f in day.facts)
    assert day.sessions[1].stats["vdc_min_v"] == E.approx("battery.s2_vdc_min_v")


@needs_data
@E.need
def test_high_voltage_dropped_while_the_car_was_still_rolling(day):
    assert len(day.events) == E["hv_drop.events"]
    e = day.events[0]
    assert e["session"] == E["hv_drop.session"] and f"{e['time']:%H:%M:%S}" == E["hv_drop.time"]
    assert e["speed_kmh"] == E.approx("hv_drop.speed_kmh") and e["soc_pct"] == E["hv_drop.soc_pct"]


@needs_data
@E.need
def test_thermistors_show_the_same_gradient_morning_and_evening(day):
    """써미스터 묶음마다 낮은 번호가 차갑다."""
    assert list(day.thermistors) == E["thermistors.windows"]
    evening = day.thermistors[E["thermistors.evening"]].set_index("id")["below_hot_c"]
    assert evening.idxmin() == E["thermistors.coldest_id"] and evening.min() == E["thermistors.coldest_below_hot_c"]
    for tens in E["thermistors.groups"]:
        grp = evening[(evening.index >= tens) & (evening.index < tens + 10)]
        assert grp.iloc[:2].mean() < grp.iloc[-2:].mean() - E["thermistors.gradient_c"]


@needs_data
@E.need
def test_quality_section_says_what_is_missing(day):
    status = {q["item"]: q["status"] for q in day.quality}
    assert all(status[k] == report.BAD for k in E["quality.bad"]) and all(status[k] == report.OK for k in E["quality.ok"])
    assert set(E["quality.present"]) <= set(status)
    cov = day.coverage
    assert (cov["가속도 (IMU)"] > E["quality.imu_cover_min"]).all() and cov["모터 전류"].between(*E.range("quality.motor_current_cover")).all()


@needs_data
@E.need
def test_full_report_is_written(day, tmp_path):
    path = report.write_report(day, tmp_path)
    doc = path.read_text(encoding="utf-8")
    assert len(list((tmp_path / "figures").glob("*.png"))) == E["full_report.figures"]
    assert all(s in doc for s in E["full_report.contains"])
