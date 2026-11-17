"""차량 설정 판 (`lefdata.carconfig`) — 날짜로 판을 고르고, 변환기가 그 판대로 읽는다 (만든 로그로, 데이터 없이).

같은 차의 설정(로거 형식 · BAMOCAR 레지스터 목록 · BMS 프레임 모양)이 날짜에 따라 바뀐다. 새 판을 더해도 옛 데이터는 옛 판으로 읽는다.
데이터 모양은 '판과 데이터가 맞나' 검사가 된다 — 안 맞으면 값은 판대로 읽고 meta 의 config.mismatch 와 세션 경고로 알린다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import carconfig, catalog_build as cb, lef26
from test_lef26_newlog import DAY, cycle, write
from test_lef26_pack import log as pack_log
from test_timed_can import START, event as can_event

LEF26 = """car = "LEF-26"

[[period]]
name = "옛 펌웨어"
from = 2027-01-01
logger.format = "config_on"
bamocar.registers = ["0x30", "0x22", "0x27"]
bms.profile = "checksum"
bms.pack_frame = false

[[period]]
name = "새 펌웨어"
from = 2027-06-01
source = "시험"
logger.format = "log_on"
bamocar.registers = ["0x30", "0x22", "0x27", "0x8A", "0x23", "0x28"]
bms.pack_frame = true
"""


def cars(root, text=LEF26, car="LEF-26"):
    (root / "_cars").mkdir(parents=True, exist_ok=True)
    (root / "_cars" / f"{car}.toml").write_text(text, encoding="utf-8")
    return root / "_cars"


# ---------------------------------------------------------------- 판 고르기

def test_periods_inherit_and_are_chosen_by_date(tmp_path):
    book = carconfig.load("LEF-26", cars(tmp_path))
    old, new = book.periods
    assert new.values["bms.profile"] == "checksum" and new.own.keys() == {"logger.format", "bamocar.registers", "bms.pack_frame"}
    assert book.choose("2027-03-01").period is old and book.choose("2027-06-01").period is new and book.choose("2028-01-01").period is new
    early = book.choose("2026-12-31")                                   # 첫 판보다 앞 — 가장 가까운 판 (사용자 결정 11-16)
    assert early.period is old and early.basis == "nearest"
    assert carconfig.load("LEF-25evo", tmp_path / "_cars") is None      # 설정 파일이 없는 차 → 판 없이 (데이터 모양으로)


def test_a_recording_can_be_assigned_to_a_period(tmp_path):
    root = cars(tmp_path)
    carconfig.assign(root, "LEF-26", f"{DAY}_r00", "옛 펌웨어")
    book = carconfig.load("LEF-26", root)
    got = book.choose(DAY, f"{DAY}_r00")
    assert got.period.name == "옛 펌웨어" and got.basis == "assigned" and book.choose(DAY, f"{DAY}_r01").basis == "date"
    with pytest.raises(ValueError, match="판 '없는 판'"):
        carconfig.assign(root, "LEF-26", f"{DAY}_r00", "없는 판")
    carconfig.assign(root, "LEF-26", f"{DAY}_r00", None)
    assert carconfig.load("LEF-26", root).choose(DAY, f"{DAY}_r00").basis == "date"


def test_editing_a_period_changes_its_hash_but_not_the_others(tmp_path):
    a = carconfig.load("LEF-26", cars(tmp_path))
    b = carconfig.load("LEF-26", cars(tmp_path / "b", LEF26.replace('"0x23", "0x28"]', '"0x23"]')))
    assert a.periods[0].hash == b.periods[0].hash and a.periods[1].hash != b.periods[1].hash


@pytest.mark.parametrize("bad, match", [('name = "새 펌웨어"\nfrom = 2027-06-01', "겹친다"), ("from = 2027-06-01\n", "name 과 from")])
def test_a_broken_book_stops(tmp_path, bad, match):
    text = LEF26 + f"\n[[period]]\n{bad}\n" if "name" in bad else LEF26 + f"\n[[period]]\n{bad}"
    with pytest.raises(ValueError, match=match):
        carconfig.load("LEF-26", cars(tmp_path, text))


# ---------------------------------------------------------------- LEF-26 변환기가 판대로 읽는다

def convert(tmp_path, rows, text=LEF26):
    cars(tmp_path, text)
    ev = write(tmp_path / "LEF-26" / f"{DAY}_test", rows)
    [row] = lef26.convert_event(ev)
    m = json.loads((ev / "normalized" / f"{DAY}_r00" / "meta.json").read_text(encoding="utf-8"))
    return ev, row, m


def test_the_period_of_the_day_decides_and_is_written_down(tmp_path):
    ev, row, m = convert(tmp_path, cycle([(60.0, 10.0)]))
    assert (row["config_period"], row["config_basis"], row["config_mismatch"]) == ("새 펌웨어", "date", 0)
    assert m["config"]["period"] == "새 펌웨어" and m["config"]["hash"] == carconfig.load("LEF-26", tmp_path / "_cars").periods[1].hash
    assert m["config"]["effective"]["bamocar.registers"] == ["0x22", "0x23", "0x27", "0x28", "0x30", "0x8A"]


def test_a_register_left_out_of_the_period_is_not_decoded_and_is_flagged(tmp_path):
    """판의 레지스터 목록에서 0x23 · 0x28 을 빼면 풀지 않는다 — 그런데 데이터에 많이 오면 '판에 없는 레지스터' 로 알린다."""
    text = LEF26.replace('"0x8A", "0x23", "0x28"]', '"0x8A"]')
    ev, row, m = convert(tmp_path, cycle([(60.0, 10.0)]), text)
    motor = pd.read_parquet(ev / "normalized" / f"{DAY}_r00" / "motor.parquet")
    assert set(motor["signal"]) == {"n_act_pct"}
    assert row["config_mismatch"] == 1 and "0x23 1,000개" in m["config"]["mismatch"][0] and "0x28 1,000개" in m["config"]["mismatch"][0]


def test_a_bms_period_that_does_not_fit_the_data_is_read_as_written_and_flagged(tmp_path):
    """판은 checksum 인데 데이터는 바이트 6-7 이 팩 전압 — 판대로 읽고 (BMS 가 무효로 남는다) 맞지 않는다고 적는다."""
    ev, row, m = convert(tmp_path, pack_log())
    assert m["config"]["effective"]["bms.profile"] == "checksum" and row["bms_profile"] == "checksum"
    assert any("bms.profile = checksum" in x and "체크섬이 0% 맞음" in x for x in m["config"]["mismatch"])
    assert any("BMS 줄의" in x for x in m["config"]["mismatch"])


def test_a_format_that_does_not_fit_the_data_is_flagged(tmp_path):
    """그날 판은 옛 형식(CONFIG ON)인데 로그에 LOG ON · STAT 줄이 있다 — 판대로 읽고 (켠 뒤 시각도 없다) 맞지 않는다고 적는다."""
    text = LEF26.replace("from = 2027-06-01", "from = 2027-07-01")
    ev, row, m = convert(tmp_path, cycle([(60.0, 10.0)]), text)
    assert (row["config_period"], row["config_basis"]) == ("옛 펌웨어", "date") and "power_cycle" not in row
    assert any("logger.format = config_on" in x and "있음" in x for x in m["config"]["mismatch"])


def test_a_date_before_the_first_period_reads_with_the_nearest_and_says_so(tmp_path):
    """첫 판보다 앞선 날짜 — 가장 가까운 판으로 읽고 (사용자 결정 11-16) 세션에 안내를 단다."""
    text = 'car = "LEF-26"\n\n' + LEF26[LEF26.index('[[period]]\nname = "새 펌웨어"'):].replace("from = 2027-06-01", "from = 2027-06-10")
    ev, row, m = convert(tmp_path, cycle([(60.0, 10.0)]), text)
    assert (row["config_period"], row["config_basis"], row["config_mismatch"]) == ("새 펌웨어", "nearest", 0)
    on = pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")
    notes = ('[event]\ncar = "LEF-26"\nclock = "phone"\n[clocks.lef26_logger.recordings]\n'
             f'"{DAY}_r00" = "{on.isoformat()}"\n[[timed_session]]\nname = "시험"\ndiscipline = "미분류"\n'
             f'start = "{(on + pd.Timedelta(seconds=60)).isoformat()}"\nend = "{(on + pd.Timedelta(seconds=70)).isoformat()}"\n')
    (ev / cb.NOTES_NAME).write_text(notes, encoding="utf-8")
    [s] = cb.build_event(ev)
    mot = next(b for b in s.blocks if b.key == "motor")
    assert [f["code"] for f in mot.findings if f["code"].startswith("config_")] == ["config_nearest"]
    assert any("해독 설정 판: 새 펌웨어 (가장 가까운 판)" in c for c in mot.checks)


def test_status_finds_recordings_to_convert_again_after_an_edit(tmp_path):
    convert(tmp_path, cycle([(60.0, 10.0)]))
    [r] = carconfig.status(tmp_path, "LEF-26")
    assert r["period"] == "새 펌웨어" and not r["reconvert"]
    cars(tmp_path, LEF26.replace('source = "시험"', 'source = "시험"\nbamocar.n100_rpm = 6500'))
    [r] = carconfig.status(tmp_path, "LEF-26")
    assert r["reconvert"] and r["used_hash"] != r["hash"]


# ---------------------------------------------------------------- 25Evo — AIN1 의 뜻이 판마다

EVO = """car = "LEF-25evo"

[[period]]
name = "미연결"
from = 2027-01-01
monolith.ain1 = "unused"

[[period]]
name = "브레이크 압력"
from = {on}
monolith.ain1 = "brake_pressure_v"
"""


def analog(ev, rec="2027-06-05_100000"):
    t = np.arange(0.0, 60.0, 0.01)
    v = np.where((t > 30) & (t < 32), 2.5, 0.5)                          # 30 ~ 32 s 에 밟음
    pd.DataFrame({"t_abs": START + pd.to_timedelta(t, unit="s"), "valid": True, "ain1_v": v}).to_parquet(ev / "normalized" / rec / "analog.parquet")


@pytest.mark.parametrize("on, has", [("2027-06-01", True), ("2027-06-06", False)])
def test_the_brake_block_appears_only_when_the_period_says_ain1_is_the_brake_sensor(tmp_path, on, has):
    ev, rec = can_event(tmp_path, '[[timed_session]]\nrecording = "2027-06-05_100000"\nname = "시험"\ndiscipline = "미분류"\n')
    analog(ev)
    cars(tmp_path, EVO.format(on=on), car="LEF-25evo")
    [s] = cb.build_event(ev)
    brake = next((b for b in s.blocks if b.key == "brake"), None)
    assert (brake is not None) is has
    if has:
        ch = brake.series[0]
        assert ch.key == "ain1_v" and ch.v.max() == pytest.approx(2.5) and ch.t_s[ch.v > 1].min() == pytest.approx(30.0, abs=0.02)
        assert "브레이크 압력" in brake.checks[-1]
