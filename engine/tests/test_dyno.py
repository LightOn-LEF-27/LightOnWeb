"""섀시다이나모 기록 변환 · 시각 없는 로거 레코딩 맞추기 · 다이나모 세션 — 만들어 낸 기록으로 (데이터 폴더가 없어도 돈다)."""

import datetime as dt
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lefdata import catalog_build as cb, dyno, session_store
from lefdata.writer import t_abs_array

KST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2027, 1, 2, 10, 0, 0, tzinfo=KST)            # 다이나모 PC 시계로 첫 기록을 켠 때
HEAD = ["Time", "차량속도", "Torque_Sensor", "차량가속도", "F", "동력흡수량", "Udc4", "Idc4", "P4", "실시간효율", "새 열"]
UNITS = ["sec", "km/h", "N-m", "m/s^2", "N", "W", "V", "A", "W", "%", "?"]


def sag_wave(t, seed=0):
    """달리는 차의 직류 버스 전압처럼: 천천히 내려가면서 가속할 때마다 2 ~ 3 초 처진다."""
    rng = np.random.default_rng(seed)
    v = 300 - 0.004 * t
    for c, depth, width in zip(rng.uniform(0, t[-1], int(t[-1] // 8)), rng.uniform(8, 35, 400), rng.uniform(1.5, 3.5, 400)):
        v -= depth * np.exp(-0.5 * ((t - c) / width) ** 2)
    return v


def write_dyno(path, t_from, seconds, volt, reverse_current=False):
    """다이나모 기록 하나 (CP949 탭 텍스트). t_from = 그 파일의 0 초가 다이나모 타임라인의 몇 초인가."""
    t = np.round(np.arange(0, seconds, 0.1), 1)
    v = volt(t_from + t)
    speed = 60 + 40 * np.sin(t / 7)
    force = np.clip(300 - 2 * (v - 280), 50, None)
    roller_w = force * speed / 3.6
    dc_w = roller_w / 0.7                                       # 효율 70 %
    amp = dc_w / v * (-1 if reverse_current else 1)
    lines = ["\t".join(HEAD), "\t".join(UNITS)]
    for k in range(len(t)):
        eff = "Inf" if k == 0 else f"{70.0:.1f}"
        lines.append("\t".join(f"{x:.6f}" for x in (t[k], speed[k], force[k] * 0.256, 0.1, force[k], roller_w[k], v[k], amp[k],
                                                    dc_w[k] * (-1 if reverse_current else 1))) + f"\t{eff}\t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp949"))
    return path


def timeline():
    tl = np.arange(-200, 1500, 0.01)
    w = sag_wave(tl + 200)
    return lambda x: np.interp(x, tl, w)


def stm32_rec(norm, name, t_rel_s, vdc):
    """절대 시각이 없는 25Evo STM32 백업 로거 레코딩 (맞추기에 쓰는 것만)."""
    d = norm / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"source": {"logger": "자작 STM32 (만든 것)"}, "time": {"t_abs_start": None, "duration_s": float(t_rel_s[-1])}},
                                            ensure_ascii=False), encoding="utf-8")
    t_ms = np.round(t_rel_s * 1000).astype(np.int64)
    pq.write_table(pa.table({"t_rel_ms": pa.array(t_ms), "t_abs": t_abs_array(t_ms, mask=np.ones(len(t_ms), bool)), "vdc_v": pa.array(vdc)}),
                   d / "motor.parquet")


def lef26_rec(norm, name, t_rel_s, vdc):
    """절대 시각이 없는 LEF-26 로거 레코딩 (motor 표는 신호마다 줄)."""
    d = norm / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"source": {"logger": "LEF-26 STM32F446RE (만든 것)"}, "time": {"t_abs_start": None}}), encoding="utf-8")
    t_ms = np.round(t_rel_s * 1000).astype(np.int64)
    pq.write_table(pa.table({"t_rel_ms": pa.array(t_ms), "signal": pa.array(["vdc_v"] * len(t_ms)), "value": pa.array(vdc)}), d / "motor.parquet")


@pytest.fixture
def event(tmp_path):
    ev = tmp_path / "CAR" / "2027-01-02_dyno"
    volt = timeline()
    write_dyno(ev / "raw_dyno" / "2027-01-02_100000_dyno.xls", 0.0, 300, volt)
    write_dyno(ev / "raw_dyno" / "test-27-01-02 10 10 00.xls", 600.0, 200, volt)           # 다이나모 프로그램이 지은 이름도 읽는다
    (ev / "raw_dyno" / "2027-01-02_095000_coastdown.xlsx").write_bytes(b"PK")
    rows, skipped = dyno.convert_event(ev)
    return ev, volt, rows, skipped


# ---------------------------------------------------------------- 변환

def test_start_time_comes_from_the_file_name():
    from pathlib import Path
    assert dyno.name_time(Path("2027-01-02_101500_dyno.xls")) == T0.replace(minute=15)
    assert dyno.name_time(Path("test-27-01-02 10 15 00.xls")) == T0.replace(minute=15)
    assert dyno.name_time(Path("dyno.xls")) is None


def test_text_file_becomes_a_table_with_every_column(event):
    ev, _, rows, skipped = event
    assert [r["recording_id"] for r in rows] == ["2027-01-02_100000_dyno", "2027-01-02_101000_dyno"]
    assert [n for n, _ in skipped] == ["2027-01-02_095000_coastdown.xlsx"]                  # 건너뛴 파일은 이유와 같이
    rec = ev / "normalized" / "2027-01-02_100000_dyno"
    d = pq.read_table(rec / "dyno.parquet").to_pandas()
    m = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    assert {"speed_kmh", "force_n", "roller_power_w", "dc_voltage_v", "dc_current_a", "dc_power_w", "efficiency_pct", "col_10"} <= set(d.columns)
    assert m["source"]["unmapped"] == ["새 열"] and m["source"]["columns"]["dc_voltage_v"] == {"name": "Udc4", "unit": "V"}
    assert np.isnan(d["efficiency_pct"].iloc[0])                                              # Inf 는 값 없음
    assert d["t_rel_ms"].iloc[1] - d["t_rel_ms"].iloc[0] == 100
    first = d["t_abs"].iloc[0]
    assert first == pd.Timestamp(T0) and m["time"]["t_abs_start"].startswith("2027-01-02T10:00:00")
    assert m["source"]["logger"].startswith("섀시다이나모") and not m["summary"]["dc_suspect"]


def test_not_a_dyno_file_is_skipped_with_the_reason(tmp_path):
    ev = tmp_path / "CAR" / "E"
    (ev / "raw_dyno").mkdir(parents=True)
    (ev / "raw_dyno" / "2027-01-02_100000_dyno.xls").write_bytes("Time\t속도\r\nsec\tkm/h\r\n0\t1\r\n".encode("cp949"))
    rows, skipped = dyno.convert_event(ev)
    assert rows == [] and "Udc4" not in skipped[0][1] and "dc_voltage_v" in skipped[0][1]


def test_dc_values_that_give_more_than_the_car_took_are_suspect():
    roller = np.full(100, 8000.0)
    accel = np.zeros(100)
    assert dyno.dc_check(roller, accel, roller / 0.7)[2] is False
    share, n, bad = dyno.dc_check(roller, accel, -roller)
    assert bad and share == 1.0 and n == 100
    assert dyno.dc_check(roller[:5], accel[:5], -roller[:5]) == (None, 5, False)            # 표본이 적으면 판정하지 않는다


# ---------------------------------------------------------------- 시각 없는 로거 레코딩 맞추기

def long_logger(volt, start=-100.0, g=1.001, seconds=1200, seed=1):
    """다이나모 타임라인 start 초에 켜져 seconds 초 기록한 로거 (시계 기울기 g, 초당 약 3 행, 전압이 2 V 높게 읽힘)."""
    t_rel = np.sort(np.random.default_rng(seed).uniform(0, seconds, int(seconds * 3)))
    return t_rel, volt(start + g * t_rel) + 2.0


def test_long_recording_gets_one_pair_per_dyno_file(event):
    ev, volt, *_ = event
    t_rel, v = long_logger(volt)
    stm32_rec(ev / "normalized", "stm32_log001", t_rel, v)
    al = dyno.align_event(ev)
    ok = al[al["accepted"].astype(bool)].set_index("dyno_recording")
    assert list(ok.index) == ["2027-01-02_100000_dyno", "2027-01-02_101000_dyno"]
    assert ok.loc["2027-01-02_100000_dyno", "t_rel_s"] == pytest.approx(100 / 1.001, abs=0.3)
    assert ok.loc["2027-01-02_101000_dyno", "t_rel_s"] == pytest.approx(700 / 1.001, abs=0.3)
    assert (ok["method"] == "line").all() and (ok["corr"] > 0.9).all() and ok["g"].iloc[0] == pytest.approx(1.001, abs=5e-4)
    p = dyno.load_alignment(ev)["stm32_log001"]
    assert p.to_dyno([0.0])[0] == pytest.approx(T0.timestamp() - 100, abs=0.5)               # 꺾은선 밖은 기울기로 연장
    assert p.span[1] - p.span[0] == pytest.approx(t_rel[-1] * 1.001, abs=0.5)


def test_short_recording_is_not_paired_by_the_engine(event):
    ev, volt, *_ = event
    t_rel = np.sort(np.random.default_rng(2).uniform(0, 60, 600))
    lef26_rec(ev / "normalized", "2027-01-02_r07", t_rel, volt(650 + t_rel) - 6.0)
    al = dyno.align_event(ev)
    row = al[al["recording_id"] == "2027-01-02_r07"]
    assert len(row) == 1 and not row["accepted"].any() and "[[dyno_pair]]" in row["note"].iloc[0]
    assert dyno.load_alignment(ev) == {}


def test_a_pair_written_by_a_person_is_refined_and_checked(event):
    ev, volt, *_ = event
    t_rel = np.sort(np.random.default_rng(2).uniform(0, 60, 600))
    lef26_rec(ev / "normalized", "2027-01-02_r07", t_rel, volt(650 + t_rel) - 6.0)            # 두 번째 파일(600 s)의 50 s 뒤에 켜짐 → 그 파일 시작 = 경과 −50 s
    pair = '[[dyno_pair]]\nrecording = "2027-01-02_r07"\ndyno = "2027-01-02_101000_dyno"\nt_rel_s = {}\nbasis = "만든 것"\n'
    (ev / cb.NOTES_NAME).write_text(pair.format(-48.5), encoding="utf-8")
    ok = dyno.align_event(ev).query("accepted")
    assert len(ok) == 1 and ok["t_rel_s"].iloc[0] == pytest.approx(-50.0, abs=0.3) and ok["method"].iloc[0] == "notes"
    (ev / cb.NOTES_NAME).write_text(pair.format(-20.0), encoding="utf-8")                    # 엉뚱한 자리 — ±3 s 안에 맞는 곳이 없다
    row = dyno.align_event(ev)
    assert not row["accepted"].any() and row["note"].str.contains("끝|상관").any()


def test_one_dyno_file_cannot_belong_to_two_recordings_of_one_logger(event):
    ev, volt, *_ = event
    for name, seed in (("stm32_log001", 1), ("stm32_log002", 3)):
        t_rel, v = long_logger(volt, seed=seed)
        stm32_rec(ev / "normalized", name, t_rel, v)
    al = dyno.align_event(ev)
    assert not al["accepted"].any() and al["note"].str.contains("다 맞음").all()


# ---------------------------------------------------------------- 세션

def notes(extra=""):
    return ('[event]\ncar = "CAR"\nclock = "dyno"\n'
            '[[missing]]\ndata = "팩 전류"\nblock = "battery"\nstatus = "기록 안 됨"\nreason = "이 이벤트에는 없음"\n'
            '[[timed_session]]\nrecording = "2027-01-02_100000_dyno"\ndiscipline = "다이나모"\n' + extra)


def test_dyno_recording_becomes_a_timed_session_with_roller_and_dc_blocks(event):
    ev, *_ = event
    (ev / cb.NOTES_NAME).write_text(notes(), encoding="utf-8")
    (s,) = cb.build_event(ev)
    keys = [b.key for b in s.blocks]
    assert keys == ["dyno", "battery"] and s.discipline == "다이나모" and s.anchor == "rec:2027-01-02_100000_dyno"
    assert s.duration_s == pytest.approx(299.9, abs=0.2) and s.start == pd.Timestamp(T0)
    assert s.missing == []                                                                   # 배터리 블록이 생겼으니 '없음' 줄을 뺀다
    assert s.stats["vmax_kmh"] == pytest.approx(100.0, abs=0.5) and s.stats["net_wh"] > 0
    bat = s.blocks[1]
    assert bat.status == cb.HAVE and "전력 분석기" in bat.device
    row = session_store.summary(s)
    assert row["blocks"]["have"] == ["dyno", "battery"]


def test_suspect_dc_values_are_flagged_and_kept_out_of_the_summary(tmp_path):
    ev = tmp_path / "CAR" / "2027-01-02_dyno"
    write_dyno(ev / "raw_dyno" / "2027-01-02_100000_dyno.xls", 0.0, 300, timeline(), reverse_current=True)
    dyno.convert_event(ev)
    (ev / cb.NOTES_NAME).write_text(notes(), encoding="utf-8")
    (s,) = cb.build_event(ev)
    bat = next(b for b in s.blocks if b.key == "battery")
    assert bat.status == cb.WARN and "쓰지 말 것" in bat.cautions[0]
    assert "net_wh" not in s.stats and "i_max_a" not in s.stats


def test_logger_placed_on_the_dyno_clock_is_a_recording_of_the_dyno_clock(event):
    ev, volt, *_ = event
    t_rel, v = long_logger(volt)
    stm32_rec(ev / "normalized", "stm32_log001", t_rel, v)
    dyno.align_event(ev)
    x = next(r for r in cb._recordings(ev) if r.rec == "stm32_log001")
    assert x.device == "stm32" and x.clock == "dyno" and x.place is not None
    assert x.t0 == pytest.approx(T0.timestamp() - 100, abs=0.5)


def test_upload_detection_knows_a_dyno_file_but_only_detects_it(event):
    from lefdata import ingest
    ev, *_ = event
    d = ingest.detect(ev / "raw_dyno" / "2027-01-02_100000_dyno.xls")
    assert d["kind"] == "chassis_dyno_txt" and d["ok"] and d["recording_id"] == "2027-01-02_100000_dyno"
    assert d["kind"] not in ingest.RAW_DIR                                                   # 넣기는 lefdata dyno 로 (판별만)
