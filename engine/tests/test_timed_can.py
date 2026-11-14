"""시각 구간 세션에 Monolith 가 받은 차량 CAN (모터 컨트롤러 · 가속페달) — 만든 레코딩으로 (데이터 없이).

GPS 가 거의 서 있어 랩 · 코스가 없는 구간 (바퀴를 띄우고 돌린 시험 등) 도 모터 · 페달은 본다. Monolith 시계는 기준 시계보다 11 s 늦다고 둔다.
"""

import json

import numpy as np
import pandas as pd
import pytest

from lefdata import catalog_build as cb, evo_can

DAY = "2027-06-05"
START = pd.Timestamp(f"{DAY} 10:00:00", tz="Asia/Seoul")      # Monolith 시계로 켠 시각


def monolith(ev, rec="2027-06-05_100000", span_s=60.0):
    """Monolith 레코딩 하나 — meta · bamocar (회전수 10 Hz, 20 ~ 40 s 에 돈다 · DC 전압) · apps (20 Hz, 같은 동안 페달 50 %)."""
    d = ev / "normalized" / rec
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"car": "LEF-25evo", "source": {"logger": "Monolith TMA-1"},
                                             "time": {"t_abs_start": START.isoformat(), "duration_s": span_s}}), encoding="utf-8")
    t = np.arange(0.0, span_s, 0.1)
    on = (t >= 20) & (t < 40)
    at = START + pd.to_timedelta(t, unit="s")
    speed = np.where(on, -8000, 0)                                  # 이 차는 전진이 음수
    bam = pd.DataFrame({"t_rel_ms": np.concatenate([t, t]) * 1000, "t_abs": np.concatenate([at, at]),
                        "reg": np.repeat([evo_can.REG_SPEED, evo_can.REG_VDC], len(t)), "raw": np.concatenate([speed, np.full(len(t), 15000)])})
    bam.to_parquet(d / "bamocar.parquet")
    ta = np.arange(0.0, span_s, 0.05)
    pedal = np.where((ta >= 20) & (ta < 40), 50.0, 0.0)
    pd.DataFrame({"t_abs": START + pd.to_timedelta(ta, unit="s"), "pedal_pct": pedal, "apps1_pct": pedal, "apps2_pct": pedal + 0.2,
                  "torque_cmd_raw": -pedal / 100 * evo_can.TORQUE_FULL, "status": 0, "fault_latched": 0,
                  "counter": np.arange(len(ta)) % 256}).to_parquet(d / "apps.parquet")
    return rec


def event(tmp_path, notes):
    ev = tmp_path / "LEF-25evo" / f"{DAY}_test"
    ev.mkdir(parents=True)
    rec = monolith(ev)
    (ev / cb.NOTES_NAME).write_text('[event]\ncar = "LEF-25evo"\nclock = "orion"\n'
                                    f'[clocks.monolith]\nrecordings = {{ "{rec}" = 11.0 }}\nbasis = "시험"\n' + notes, encoding="utf-8")
    return ev, rec


def test_a_time_window_on_a_monolith_recording_gets_its_motor_and_pedal(tmp_path):
    ev, rec = event(tmp_path, '[[timed_session]]\nrecording = "2027-06-05_100000"\nname = "바퀴 띄움"\ndiscipline = "미분류"\n')
    [s] = cb.build_event(ev)
    assert [b.key for b in s.blocks] == ["motor", "pedal"] and s.split_by == "time" and s.track is None
    assert s.start == START + pd.Timedelta(seconds=11) and s.duration_s == pytest.approx(60.0)
    mot, ped = s.blocks
    rpm = next(c for c in mot.series if c.key == "rpm")
    spin = rpm.t_s[rpm.v > 0]
    assert spin.min() == pytest.approx(20.0, abs=0.11) and spin.max() == pytest.approx(39.9, abs=0.11)     # 세션 시간 = Monolith 시각 그대로 (구간이 그 레코딩)
    assert rpm.v.max() == pytest.approx(8000 / evo_can.RPM_FULL_SCALE * evo_can.LOGGER_N100_RPM)          # 전진 + 로 뒤집어 낸다
    assert all("Monolith 시계 +11.0 s" in b.checks[0] for b in (mot, ped))
    assert not any("GPS 속도" in c for c in mot.checks)                                                  # 시각 구간에는 GPS 가 없다
    apps = next(c for c in ped.series if c.key == "apps_pct")
    assert apps.v.max() == pytest.approx(50.0) and apps.t_s[apps.v > 0].min() == pytest.approx(20.0, abs=0.06)


def test_a_window_written_by_time_cuts_the_can_on_the_reference_clock(tmp_path):
    """시각으로 적은 구간 (기준 시계) — Monolith 시각 + 11 s 로 옮겨 자른다. 기준 시계 10:00:41 ~ 10:00:51 = Monolith 30 ~ 40 s."""
    ev, _ = event(tmp_path, f'[[timed_session]]\nstart = "{DAY}T10:00:41"\nend = "{DAY}T10:00:51"\nname = "뒤 절반"\ndiscipline = "미분류"\n')
    [s] = cb.build_event(ev)
    rpm = next(c for c in next(b for b in s.blocks if b.key == "motor").series if c.key == "rpm")
    assert rpm.t_s.min() == pytest.approx(0.0, abs=0.11) and rpm.t_s.max() == pytest.approx(10.0, abs=0.11)
    assert (rpm.v[rpm.t_s < 9.85] > 0).all()                                                             # 그 10 s 동안 내내 돌았다
